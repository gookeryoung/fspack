"""dist 产物 Win7 兼容门禁：loader exe 硬门禁 + dist 全量扫描报告 + shim 自动注入.

P1 产物门禁补全的三道防线（配合 win7_dll 的 python3XX.dll 门禁形成完整链路）：

- :func:`enforce_win7_loaders`：loader exe 导入表校验，违规抛
  :class:`Win7ScanError` 阻断构建。loader 由 fspack 内置 C 源码 + mingw
  编译，若引入 Win8+ API 属 fspack 自身回归，必须在构建期立即暴露，
  不允许带病出包。
- :func:`scan_dist_win7`：dist 下全部 ``.dll``/``.pyd``/``.exe`` 导入表
  扫描。第三方依赖（site-packages 的 pyd/dll）与 Nuitka 用户产物违规
  无法自动修复（只能更换依赖版本），故不阻断构建，聚合渲染为文本报告
  （``dist/release/win7-compat-report.txt``）供人工决策。python3XX.dll
  已由 :func:`fspack.packaging.win7.dll.ensure_win7_dll` 单独硬门禁。
- :func:`inject_win7_shims`：扫描后自动将内置 shim DLL 注入 dist 根目录。
  当前支持两类 shim：``api-ms-win-core-path-l1-1-0.dll``（Win8+ PathCch*）
  和 ``bcryptprimitives.dll``（Win10+ ProcessPrng，Rust 1.78+ wheel 硬链接）。
  注入是**根目录级别**的（不侵入 site-packages），PE loader 优先从同目录
  加载，遮蔽系统缺失的 Win10+ DLL。

报告包含：违规文件与 API 明细、需 shim 文件数、已注入 shim 列表、
api-ms-win-crt-* 依赖提示（Win7 SP1 需 KB2999226 UCRT）。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from fspack.exceptions import FspackError
from fspack.packaging.win7.check import (
    PeParseError,
    Win7CheckResult,
    check_win7_imports,
)
from fspack.packaging.win7.dll import WIN7_SHIM_DLL_PATH, WIN7_SYSTEM_SHIMS

__all__ = [
    "Win7ScanError",
    "Win7ScanReport",
    "enforce_win7_loaders",
    "inject_win7_shims",
    "iter_pe_files",
    "render_win7_report",
    "scan_dist_win7",
    "write_win7_report",
]

_logger = logging.getLogger(__name__)

# 扫描的 PE 扩展名（小写）
_SCAN_SUFFIXES = frozenset({".dll", ".pyd", ".exe"})

# 排除的 dist 一级子目录：build 为编译中间产物，release 为报告输出目录
_EXCLUDE_PARTS = frozenset({"build", "release"})

# 报告输出文件名（write_win7_report 写到 dist/release/ 下）
REPORT_FILENAME = "win7-compat-report.txt"


class Win7ScanError(FspackError):
    """Win7 产物门禁失败（loader exe 导入表含 Win8+ 静态导入）。"""


@dataclass(frozen=True)
class Win7ScanReport:
    """dist 全量 Win7 扫描汇总（不阻断构建，供报告渲染）.

    scanned 为成功解析的 PE 数；skipped 为扩展名匹配但解析失败（非 PE/
    截断）的文件名；violations 为存在违规的文件结果（ok=False）；
    injected_shims 为本次注入的 shim DLL 文件名（dist 根目录下已存在的
    shim 也包含在内）。
    """

    scanned: int = 0
    skipped: tuple[str, ...] = ()
    violations: tuple[Win7CheckResult, ...] = ()
    shim_files: int = 0
    ucrt_files: int = 0
    injected_shims: tuple[str, ...] = ()
    dist_dir: Path | None = None

    @property
    def ok(self) -> bool:
        """是否全部通过（无违规文件）。"""
        return not self.violations


def iter_pe_files(dist_dir: Path) -> list[Path]:
    """收集 dist 下待扫描的 PE 文件（排除 build/release，排序保证确定性）.

    公开给 NSIS 生成器复用（UCRT 依赖检测遍历同一文件集合）。
    """
    files = [
        p
        for p in dist_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _SCAN_SUFFIXES and p.relative_to(dist_dir).parts[0] not in _EXCLUDE_PARTS
    ]
    return sorted(files)


def scan_dist_win7(dist_dir: Path, *, shim: Path | None = WIN7_SHIM_DLL_PATH) -> Win7ScanReport:
    """扫描 dist 下全部 .dll/.pyd/.exe 的导入表 Win7 兼容性.

    单文件解析失败（非 PE/截断）跳过并记入 ``skipped``，不中断扫描；
    违规聚合到 ``violations``，由调用方决定输出报告（不阻断构建）。

    Args:
        dist_dir: dist 根目录（内含 runtime/、site-packages/、loader exe 等）。
        shim: api-ms-win-core-path shim 路径，用于校验 PathCch* 导出覆盖；
            传 None 跳过 shim 覆盖校验。
    """
    scanned = 0
    skipped: list[str] = []
    violations: list[Win7CheckResult] = []
    shim_files = 0
    ucrt_files = 0
    for path in iter_pe_files(dist_dir):
        try:
            result = check_win7_imports(path, shim=shim)
        except PeParseError as exc:
            skipped.append(path.name)
            _logger.warning("Win7 扫描跳过非 PE 文件 %s: %s", path, exc)
            continue
        scanned += 1
        if result.shim_dlls:
            shim_files += 1
        if any("api-ms-win-crt" in note for note in result.notes):
            ucrt_files += 1
        if not result.ok:
            violations.append(result)
    return Win7ScanReport(
        scanned=scanned,
        skipped=tuple(skipped),
        violations=tuple(violations),
        shim_files=shim_files,
        ucrt_files=ucrt_files,
        dist_dir=dist_dir,
    )


def enforce_win7_loaders(exes: list[Path] | tuple[Path, ...], *, shim: Path | None = WIN7_SHIM_DLL_PATH) -> None:
    """校验 loader exe 导入表 Win7 兼容性，违规抛 :class:`Win7ScanError` 阻断构建.

    loader 由 fspack 内置 C 源码 + mingw 交叉编译，正常只依赖 Win7 可用
    API；违规意味着 C 源码或工具链引入了 Win8+ 导入（fspack 回归），须
    立即修复而非出包。缓存命中的 exe 同样校验（PE 解析毫秒级，开销可忽略）。

    exe 缺失或非合法 PE 镜像时仅告警跳过：真实构建产物必为 mingw 生成的
    合法 PE，缺失仅出现在测试 stub 等场景，宽容处理避免误伤；核心门禁
    （Win8+ API 拦截）不受影响。

    Raises:
        Win7ScanError: 任一 exe 含 Win8+ 静态导入（消息含全部违规明细）。
    """
    failures: list[str] = []
    for exe in exes:
        try:
            result = check_win7_imports(exe, shim=shim)
        except (PeParseError, OSError) as exc:
            _logger.warning("Win7 门禁跳过非 PE loader %s: %s", exe, exc)
            continue
        if not result.ok:
            detail = "；".join(f"{v.target}（{v.reason}）" for v in result.violations)
            failures.append(f"{exe.name}: {detail}")
    if failures:
        raise Win7ScanError(
            "loader exe 导入表含 Win8+ 依赖，无法在 Win7 上加载（fspack 回归，请修复 loader 源码或工具链）:\n  "
            + "\n  ".join(failures)
        )


def inject_win7_shims(dist_dir: Path) -> tuple[str, ...]:
    """扫描 dist PE 导入表，自动注入内置 Win7 兼容 shim DLL 到 dist 根目录.

    覆盖两类已知 Win7 不兼容场景：

    1. **api-ms-win-core-path-l1-1-0.dll**（Win8+ PathCch* 系列）：Python 3.9+
       的 embed runtime 与部分依赖静态导入，Win7 上缺失；
    2. **bcryptprimitives.dll**（Win10+ ProcessPrng）：Rust 1.78（2024-05）
       将 Windows 默认 target 最低 OS 提升到 Win10，之后编译的 Rust 扩展
       wheel（pydantic-core/bcrypt/cryptography/watchfiles 等）硬链接
       ProcessPrng，导致 Win7 加载失败（WinError 127）。

    注入策略：遍历 dist 全部 PE 导入表，收集缺失 shim 的 DLL 名集合，
    从 :data:`fspack.packaging.win7.dll.WIN7_SYSTEM_SHIMS` 查源路径，
    复制到 dist 根目录（PE loader 优先从同目录加载，遮蔽系统缺失 DLL）。

    Returns:
        本次注入的 shim DLL 文件名元组（不含已存在跳过的）。返回空元组
        表示 dist 中无 PE 需 shim。

    Raises:
        OSError: 源 shim DLL 缺失或目标目录写入失败（硬错误，阻断构建）。
    """
    # 扫描 dist PE 导入表，收集需要 shim 的 DLL 名
    needed: set[str] = set()
    for path in iter_pe_files(dist_dir):
        try:
            result = check_win7_imports(path)
        except PeParseError:
            continue
        for shim_dll in result.shim_dlls:
            low = shim_dll.lower()
            if low in WIN7_SYSTEM_SHIMS:
                needed.add(low)

    if not needed:
        _logger.info("dist 下无 PE 需 Win7 shim，跳过注入")
        return ()

    injected: list[str] = []
    for dll_name in sorted(needed):
        src = WIN7_SYSTEM_SHIMS[dll_name]
        if not src.is_file():
            raise OSError(f"Win7 shim 源文件缺失: {src}（dll: {dll_name}）")
        dest = dist_dir / dll_name
        if dest.is_file():
            _logger.info("Win7 shim 已存在，跳过: %s", dest)
            continue
        shutil.copy2(src, dest)
        _logger.info("注入 Win7 shim: %s（%d bytes）", dest, src.stat().st_size)
        injected.append(dll_name)

    return tuple(injected)


def render_win7_report(report: Win7ScanReport) -> str:
    """把扫描汇总渲染为多行中文文本报告."""
    lines = [
        "Win7 兼容性扫描报告",
        "=" * 40,
        "扫描范围: dist 下 .dll/.pyd/.exe（排除 build/release）",
        f"扫描文件: {report.scanned}（通过 {report.scanned - len(report.violations)}，"
        f"违规 {len(report.violations)}，跳过 {len(report.skipped)}）",
    ]
    for result in report.violations:
        rel = result.path.relative_to(report.dist_dir) if report.dist_dir else result.path.name
        lines.append("")
        lines.append(f"[违规] {rel}")
        lines.extend(f"  {v.target} — {v.reason}" for v in result.violations)
    lines.append("")
    if report.injected_shims:
        lines.append(f"[已注入] Win7 shim DLL: {', '.join(report.injected_shims)}")
    if report.shim_files:
        lines.append(f"[提示] {report.shim_files} 个文件依赖 Win8+ 系统库（shim 已自动注入，见上方）")
    if report.ucrt_files:
        lines.append(f"[提示] {report.ucrt_files} 个文件依赖 api-ms-win-crt-*（Win7 SP1 需 KB2999226 UCRT）")
    if report.skipped:
        lines.append(f"[提示] 跳过非 PE 文件: {', '.join(report.skipped)}")
    if report.ok:
        lines.append("结论: 全部通过，产物可在 Win7 SP1 加载（前提：系统已装 UCRT）")
    else:
        lines.append(f"结论: {len(report.violations)} 个文件在 Win7 上可能无法加载，详见上方明细")
        lines.append("      （第三方依赖无法自动修复，可考虑更换依赖版本；本扫描不阻断构建）")
    return "\n".join(lines) + "\n"


def write_win7_report(report: Win7ScanReport, dist_dir: Path) -> Path:
    """渲染扫描报告并写入 ``dist/release/win7-compat-report.txt``，返回路径."""
    out_dir = dist_dir / "release"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / REPORT_FILENAME
    out_path.write_text(render_win7_report(report), encoding="utf-8")
    _logger.info("Win7 兼容报告已写入: %s", out_path)
    return out_path
