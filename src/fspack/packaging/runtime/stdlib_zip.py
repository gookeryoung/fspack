"""Standalone 标准库 zip 化：Linux/macOS 启动提速.

把 ``runtime/python/lib/pythonX.Y[t]/`` 下的纯 ``.py`` 编译为 ``.pyc`` 并打包为
``lib/pythonX.Y[t].zip``（legacy 布局条目，如 ``os.pyc``、``json/__init__.pyc``），
随后删除源 ``.py`` 与 ``__pycache__``。运行时 CPython ``getpath`` 检测到
``lib/pythonX.Y[t].zip`` 自动加入 ``sys.path``（与 Windows embed 的
``python3XX.zip`` 同机制，free-threaded build 的 ABI 标志 ``t`` 计入 zip 名），
import 命中 zip 条目：省去每次启动对数百个 stdlib 目录的 ``stat`` 遍历与
源码重编译（配合 loader 的 ``PYTHONDONTWRITEBYTECODE=1``，散装 ``.py`` 每次
启动都要重新编译），冷启动收益 30-80ms（磁盘/杀软敏感环境更多）。

排除目录（保留目录形态、不打包不删源）：

- ``site-packages``：第三方依赖平铺在 ``dist/site-packages``，runtime 内即使
  存在也不属于 stdlib
- ``lib-dynload``：``.so`` 扩展必须目录形态加载
- ``config-*``：链接期配置（Makefile 等）

幂等：重复构建时 stdlib 无 ``.py`` 可收集则跳过（zip 已生成）。
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.platform import Platform

if TYPE_CHECKING:
    from fspack.progress import StageRecorder

__all__ = ["zip_stdlib"]

_logger = logging.getLogger(__name__)

# stdlib compileall 超时（秒）：stdlib 约 400 模块，-j 0 并行实测 <20s，
# 300s 覆盖慢速 CI/杀软扫描延迟，与 loader 编译超时一致
_STDLIB_COMPILEALL_TIMEOUT = 300.0

# 不参与 zip 化的 stdlib 子目录：第三方依赖 / .so 扩展 / 链接配置
_STDLIB_ZIP_EXCLUDE_DIRS = frozenset({"site-packages", "lib-dynload"})

# __pycache__ 条目名 → legacy 布局名：os.cpython-311-x86_64-linux-gnu.pyc → os.pyc
_PYC_TAG_RE = re.compile(r"\.cpython-\d+[a-z]*(?:-[^.]*)?\.pyc$")


def _stdlib_layout(runtime_dir: Path, py_version: str) -> tuple[Path, Path, Path] | None:
    """返回 (stdlib 目录, zip 路径, 构建 python 可执行文件)，非 Linux/macOS 返回 None."""
    is_t = py_version.endswith("t")
    base = py_version[:-1] if is_t else py_version
    major, minor = base.split(".")[:2]
    suffix = "t" if is_t else ""
    lib_dir = runtime_dir / "python" / "lib"
    stdlib = lib_dir / f"python{major}.{minor}{suffix}"
    # zip 与 stdlib 目录平级：CPython getpath 在 lib/ 下按
    # pythonX.Y[ABI].zip 名检测并自动加入 sys.path
    zip_path = lib_dir / f"python{major}.{minor}{suffix}.zip"
    py_exe = runtime_dir / "python" / "bin" / f"python{major}.{minor}{suffix}"
    if not stdlib.is_dir():
        return None
    return stdlib, zip_path, py_exe


def _collect_pyc_entries(stdlib: Path) -> list[tuple[Path, str]]:
    """收集 stdlib 下 __pycache__/*.pyc，返回 (pyc 路径, zip 内 legacy 相对名) 列表.

    排除 site-packages/lib-dynload/config-* 子树（第三方依赖与 .so 扩展
    保留目录形态）。条目名剥离 PEP 488 平台标签：``os.cpython-311-x86_64-
    linux-gnu.pyc`` → ``os.pyc``，``json/__init__.pyc`` 保持包内相对路径。
    """
    entries: list[tuple[Path, str]] = []
    for cache_dir in stdlib.rglob("__pycache__"):
        rel_parent = cache_dir.parent.relative_to(stdlib)
        parts = rel_parent.parts
        # 排除子树：site-packages/lib-dynload 与 config-* 链接配置
        if any(p in _STDLIB_ZIP_EXCLUDE_DIRS for p in parts):
            continue
        if any(p.startswith("config-") for p in parts):
            continue
        for pyc in sorted(cache_dir.glob("*.pyc")):
            legacy_name = _PYC_TAG_RE.sub(".pyc", pyc.name)
            if legacy_name == pyc.name:
                # 无平台标签的 pyc（异常命名），保守跳过
                continue
            arcname = "/".join((*parts, legacy_name)) if parts else legacy_name
            entries.append((pyc, arcname))
    return entries


def _remove_py_sources(stdlib: Path) -> tuple[int, int]:
    """删除 stdlib 下（排除 site-packages/lib-dynload/config-*）的 .py 与 __pycache__.

    返回 (删除的 .py 文件数, 节省字节数)。残留 .py 无害——sys.path 的
    stdlib 目录条目仍在，import 退回源码模式；本函数仅在 zip 完整写入后调用。
    """
    removed = 0
    saved = 0
    for path in sorted(stdlib.rglob("*")):
        rel_parts = path.relative_to(stdlib).parts
        # 排除子树：路径任一层命中排除目录即跳过
        if any(p in _STDLIB_ZIP_EXCLUDE_DIRS for p in rel_parts[:-1] if p != "__pycache__"):
            continue
        if any(p.startswith("config-") for p in rel_parts[:-1] if p != "__pycache__"):
            continue
        try:
            if path.is_dir() and path.name == "__pycache__":
                saved += sum(f.stat().st_size for f in path.iterdir() if f.is_file())
                shutil.rmtree(path)
            elif path.is_file() and path.suffix == ".py":
                saved += path.stat().st_size
                path.unlink()
                removed += 1
        except OSError as e:  # pragma: no cover - 杀软占用等文件系统异常容错
            _logger.warning("删除 stdlib 源文件失败 %s: %s", path, e)
    return removed, saved


def _prepare_zip_stdlib(
    runtime_dir: Path, py_version: str, target: Platform
) -> tuple[str | None, tuple[Path, Path, Path] | None]:
    """前置检查：返回 (跳过原因, (stdlib, zip 路径, python 可执行文件))。

    原因为 None 时 layout 有效、继续 zip 化；否则调用方 set_detail 原因后跳过。
    覆盖：非 Linux/macOS 目标、stdlib 目录缺失、runtime python 缺失、
    已 zip 化（幂等重入）四种情况。
    """
    if target not in (Platform.LINUX, Platform.MACOS):
        return "仅 Linux/macOS，跳过", None
    layout = _stdlib_layout(runtime_dir, py_version)
    if layout is None:
        return "标准库目录不存在，跳过", None
    stdlib, _, py_exe = layout
    if not py_exe.is_file():
        return "runtime python 不存在，跳过", None
    # 幂等：stdlib 无 .py 可编译（已 zip 化）则跳过。
    # 只查 stdlib 顶层与一层子目录的 .py（rglob 全树每次构建开销大）
    has_py = any(p.suffix == ".py" for p in stdlib.glob("*.py")) or any(
        p.suffix == ".py" for p in stdlib.glob("*/*.py") if p.parts[-2] not in _STDLIB_ZIP_EXCLUDE_DIRS
    )
    if not has_py:
        return "已 zip 化，跳过", None
    return None, layout


def zip_stdlib(runtime_dir: Path, py_version: str, target: Platform, stage: StageRecorder) -> None:
    """把 standalone 标准库 .py 编译为 .pyc 打包为 zip（仅 Linux/macOS）.

    流程：compileall（runtime 自身 python，ABI 一致）→ 收集 ``__pycache__``
    条目转 legacy 名写 zip → 删除源 ``.py`` 与 ``__pycache__``。compileall
    失败时降级保留目录形态（warning 不阻断构建）；zip 写入完整成功后才删源，
    中途崩溃残留的 ``.py`` 下次构建幂等重打包。

    必须在 ``_precompile_pyc`` 之后、``_trim_standalone_runtime`` 删除 python
    二进制之前调用（compileall 需要 python/bin/pythonX.Y[t]）。
    """
    reason, layout = _prepare_zip_stdlib(runtime_dir, py_version, target)
    if layout is None:
        stage.set_detail(reason or "跳过")
        return
    stdlib, zip_path, py_exe = layout

    # 编译 stdlib：生成 __pycache__/*.pyc（构建机可写）
    cmd = [str(py_exe), "-m", "compileall", str(stdlib), "-q", "-j", "0"]
    _logger.info("编译标准库: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_STDLIB_COMPILEALL_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        _logger.warning("标准库编译异常，保留目录形态: %s", e)
        stage.set_detail("compileall 异常，跳过 zip 化")
        return
    if result.returncode != 0:
        _logger.warning("标准库编译失败（退出码 %d），保留目录形态", result.returncode)
        stage.set_detail("compileall 失败，跳过 zip 化")
        return

    entries = _collect_pyc_entries(stdlib)
    if not entries:
        stage.set_detail("无 pyc 可打包，跳过")
        return

    # 写 zip：临时文件 + 同目录替换（原子性），完整写入成功后才删源
    zip_tmp = zip_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(zip_tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for pyc, arcname in entries:
                zf.write(pyc, arcname)
        zip_tmp.replace(zip_path)
    except OSError as e:
        _logger.warning("标准库 zip 写入失败，保留目录形态: %s", e)
        # 清理写了一半的临时文件；清理本身失败无害（下次构建覆盖）
        with contextlib.suppress(OSError):
            zip_tmp.unlink(missing_ok=True)
        stage.set_detail("zip 写入失败，跳过")
        return

    removed, saved_py = _remove_py_sources(stdlib)
    zip_size = zip_path.stat().st_size
    saved = max(0, saved_py - zip_size)
    stage.processed(1)
    stage.add_saved_bytes(saved)
    stage.set_detail(f"打包 {len(entries)} 模块，删 {removed} 个 .py")
    _logger.info("标准库 zip 化: %s（%d 条目，净省 %d 字节）", zip_path.name, len(entries), saved)
