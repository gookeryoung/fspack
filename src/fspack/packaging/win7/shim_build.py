"""Win7 C shim 就地编译模块.

背景：Win7 SP1 缺失的 Win8+/Win10+ API Set DLL，fspack 内置了 3 个 shim
（path / synch / bcryptprimitives）。其中 path 与 synch 的二进制已随仓库
分发，bcryptprimitives 的二进制也已入库——这里提供 **源码 → DLL** 的就地
编译能力，让开发者能：

1. ``make shims`` 重建全部 shim DLL（修改了 .c 后出二进制入库）；
2. 打包 pipeline 检测 shim DLL 是否缺失，缺失时自动尝试本地编译
   （CI 环境或精简 clone 场景的兜底）。

编译工具链复用 :mod:`fspack.packaging.loader.toolchain` 的 mingw gcc
发现逻辑（``x86_64-w64-mingw32-gcc`` 优先，回退无前缀 ``gcc``），
无需额外依赖。

CLI::

    python -m fspack.packaging.win7.shim_build [--force] [--only synch,bcrypt]

退出码：0 全部就绪（已存在或成功编译）；1 任一 shim 编译失败或工具链缺失。
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ALL_SHIMS",
    "SHIM_SRC_DIR",
    "ShimBuildError",
    "ShimSpec",
    "build_all_shims",
    "build_shim",
    "ensure_all_shims",
    "is_shim_missing",
    "main",
]

_logger = logging.getLogger(__name__)

# shim C 源码与编译产物所在目录 —— 与 loader C 源码内联不同，shim 源码以 .c
# 文件形式随仓库分发，编译输出也落在同一目录，方便 make shims 一键重建。
_SHIM_DIR = Path(__file__).parent.parent.parent / "assets" / "runtime"


@dataclass(frozen=True)
class ShimSpec:
    """单个 Win7 shim 的源码与构建参数.

    src_name / dll_name / def_name 不带路径，均相对于 :data:`SHIM_SRC_DIR`。
    libs 为 gcc -l 链接参数列表；cflags 为额外编译/链接参数。
    linkscript / def 文件用于解决 "同名符号在 kernel32.a 中已实现" 的链接冲突
    （如 synch shim 自己导出 Sleep/SleepEx，但 kernel32.a 也有这两个符号），
    用 .def 文件显式列出导出，让 ld 优先采用我们的定义。
    """

    src_name: str
    dll_name: str
    libs: tuple[str, ...] = ()
    cflags: tuple[str, ...] = ()
    def_name: str | None = None

    @property
    def src_path(self) -> Path:
        return SHIM_SRC_DIR / self.src_name

    @property
    def dll_path(self) -> Path:
        return SHIM_SRC_DIR / self.dll_name

    @property
    def def_path(self) -> Path | None:
        return SHIM_SRC_DIR / self.def_name if self.def_name else None


# shim 注册表：3 个 Win7 兼容 DLL 的源码 → 产物映射。
# path shim 仅以二进制入库（LGPL-2.1 ReactOS 实现，22 个函数，源码仓库
# 内不展开），这里不列 src；synch / bcryptprimitives 源码已入库。
SHIM_SRC_DIR: Path = _SHIM_DIR

# shim 注册表（公开给外部模块遍历，见 win7/__init__.py re-export）。
#
# 编译参数说明：
#   -nostdlib  禁止 MinGW 默认链接 msvcrt.dll / libmingwex —— shim 只转发
#              Win32 API，引入 CRT 会导致 Win7 上因 msvcrt 版本不匹配
#              而无法加载（见 issue: fspack shim 在 Win7 无法运行）。
#   -e DllMain  指定 DllMain 为入口符号（-nostdlib 移除了 CRT startup，
#              原 DllMainCRTStartup 不再存在）。
#   synch shim 的 .c 源码里已内联 memcmp（static _memcmp_impl），无需
#   libmingwex 提供任何 C 运行时函数。
_NO_CRT_CFLAGS = ("-nostdlib", "-e", "DllMain")

ALL_SHIMS: tuple[ShimSpec, ...] = (
    ShimSpec(
        src_name="api-ms-win-core-synch-l1-2-0.c",
        dll_name="api-ms-win-core-synch-l1-2-0.dll",
        cflags=_NO_CRT_CFLAGS,
    ),
    ShimSpec(
        src_name="bcryptprimitives.c",
        dll_name="bcryptprimitives.dll",
        libs=("bcrypt",),
        cflags=_NO_CRT_CFLAGS,
    ),
    ShimSpec(
        src_name="api-ms-win-core-kernel32-shim.c",
        dll_name="api-ms-win-core-kernel32-shim.dll",
        cflags=_NO_CRT_CFLAGS,
    ),
)

# 私有别名（内部引用走私有名，公开 API 用 ALL_SHIMS）
_ALL_SHIMS = ALL_SHIMS


class ShimBuildError(RuntimeError):
    """shim 编译失败（工具链缺失 / gcc 返回非零 / 源码缺失）。"""


def _find_mingw_gcc() -> str | None:
    """与 loader.toolchain._find_mingw_gcc 同逻辑的本地副本——避免循环导入。"""
    gcc = shutil.which("x86_64-w64-mingw32-gcc")
    if gcc:
        return gcc
    if sys.platform == "win32" and shutil.which("gcc"):
        return "gcc"
    return None


def _compile_one(spec: ShimSpec, gcc: str, *, work_dir: Path) -> Path:
    """gcc 编译单个 shim .c → .dll，返回产物路径。

    命令形如::

        x86_64-w64-mingw32-gcc -shared -O2 -o <dll> <src> [<def>] -lkernel32 [-lbcrypt ...]

    -shared 产出 DLL；-O2 体积/速度平衡。若 spec 指定了 def 文件，显式追加
    到命令行让 ld 按 .def 中的 EXPORTS 列表控制导出（解决 synch shim 自身
    导出 Sleep/SleepEx 与 kernel32.a 中同名实现冲突的问题）。
    """
    cmd: list[str] = [gcc, "-shared", "-O2", "-o"]
    cmd.append(str(spec.dll_path))
    cmd.append(str(spec.src_path))
    if spec.def_path is not None:
        if not spec.def_path.is_file():
            raise ShimBuildError(f".def 文件缺失: {spec.def_path}")
        cmd.append(str(spec.def_path))
    cmd.append("-lkernel32")  # 所有 shim 至少依赖 kernel32（Win32 API）
    cmd.extend(f"-l{lib}" for lib in spec.libs)
    cmd.extend(spec.cflags)
    _logger.info("编译 shim %s: %s", spec.dll_name, " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(work_dir),
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise ShimBuildError(
            f"{spec.src_name} 编译失败（exit {result.returncode}）:\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  stderr: {result.stderr.strip()}\n"
            f"  stdout: {result.stdout.strip()}"
        )
    if not spec.dll_path.is_file():
        raise ShimBuildError(f"{spec.src_name} 编译完成但产物缺失: {spec.dll_path}")
    return spec.dll_path


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------


def is_shim_missing(dll_name: str) -> bool:
    """SHIM_SRC_DIR 下指定 DLL 是否缺失（或大小为 0）。"""
    p = SHIM_SRC_DIR / dll_name
    return (not p.is_file()) or p.stat().st_size == 0


def build_shim(spec: ShimSpec, *, force: bool = False) -> Path | None:
    """编译单个 shim，成功返回 DLL 路径；已存在且非 force 时返回 None.

    工具链缺失静默返回 None（非强制且已存在则无需编译）；强制模式下工具链
    缺失抛 ShimBuildError。
    """
    if not force and spec.dll_path.is_file() and spec.dll_path.stat().st_size > 0:
        _logger.debug("%s 已存在，跳过编译（--force 可重建）", spec.dll_name)
        return None
    if not spec.src_path.is_file():
        raise ShimBuildError(f"{spec.dll_name} 二进制缺失且源码缺失: {spec.src_path}")
    gcc = _find_mingw_gcc()
    if gcc is None:
        if force:
            raise ShimBuildError(
                f"编译 {spec.dll_name} 失败：未找到 mingw gcc （需 x86_64-w64-mingw32-gcc 或系统 gcc）"
            )
        _logger.warning("未找到 mingw gcc，跳过 %s 编译（依赖仓库内已分发的二进制）", spec.dll_name)
        return None
    return _compile_one(spec, gcc, work_dir=SHIM_SRC_DIR)


def build_all_shims(
    specs: Sequence[ShimSpec] | None = None,
    *,
    force: bool = False,
) -> dict[str, Path | None]:
    """编译全部或指定 shim，返回 {dll_name: 产物路径（跳过或失败为 None）}."""
    specs = specs or _ALL_SHIMS
    result: dict[str, Path | None] = {}
    for spec in specs:
        try:
            result[spec.dll_name] = build_shim(spec, force=force)
        except ShimBuildError:
            if force:
                raise
            _logger.exception("shim %s 编译失败（非强制，继续下一个）", spec.dll_name)
            result[spec.dll_name] = None
    return result


def ensure_all_shims() -> dict[str, Path | None]:
    """构建期兜底：确保 SHIM_SRC_DIR 下有全部 shim DLL.

    逐个检查，缺失时尝试本地编译；编译不可用则仅 warning（正常 clone 仓库
    应已随包分发，兜底场景才会走到这里）。返回结果供调用方记录到 log.
    """
    result: dict[str, Path | None] = {}
    for spec in _ALL_SHIMS:
        if not is_shim_missing(spec.dll_name):
            result[spec.dll_name] = None  # 已就绪
            continue
        _logger.warning("%s 缺失，尝试本地编译...", spec.dll_name)
        try:
            result[spec.dll_name] = build_shim(spec, force=True)
        except ShimBuildError as exc:
            _logger.warning(
                "%s 编译失败，继续依赖打包期注入流程（dist 级别的 shim 检查会再报一次）: %s", spec.dll_name, exc
            )
            result[spec.dll_name] = None
    return result


def _named_specs(names: Sequence[str]) -> list[ShimSpec]:
    """按 dll_name 或 src_name 过滤 ALL_SHIMS（不区分大小写、支持逗号分隔、子串匹配）。"""
    targets = {n.strip().lower() for raw in names for n in raw.split(",") if n.strip()}
    if not targets:
        return list(_ALL_SHIMS)
    return [s for s in _ALL_SHIMS if any(t in s.dll_name.lower() or t in s.src_name.lower() for t in targets)]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：重建指定 / 全部 shim DLL."""
    parser = argparse.ArgumentParser(
        prog="python -m fspack.packaging.win7.shim_build",
        description="编译 Win7 shim DLL（synch / bcryptprimitives / kernel32-shim）到 assets/runtime/",
    )
    parser.add_argument("--force", "-f", action="store_true", help="强制重建所有 shim（即使已存在）")
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="只编译指定 shim，逗号分隔子串匹配（如 synch,bcrypt,kernel32），默认全部",
    )
    args = parser.parse_args(argv)
    specs = _named_specs(args.only.split(",")) if args.only else list(_ALL_SHIMS)
    if not specs:
        all_names = ", ".join(s.dll_name for s in _ALL_SHIMS)
        print(f"没有匹配的 shim，可选子串: {all_names}")
        return 1
    try:
        result = build_all_shims(specs, force=args.force)
    except ShimBuildError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    for dll, path in result.items():
        if path is not None:
            print(f"[ok] {dll} → {path} ({path.stat().st_size} bytes)")
        elif args.force:
            # force 模式下 None 意味着已存在（build_shim 成功返回 None = 跳过）
            print(f"[skip] {dll} 已存在")
        else:
            print(f"[skip] {dll} 已存在（--force 可重建）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
