"""wheel 缓存健康扫描与清理（``fsp cache status`` / ``fsp cache clean``）.

从 :mod:`fspack.doctor` facade（原 ``cli_doctor``）拆分而来，封装 ``fsp cache``
子命令逻辑：扫描 wheel 缓存目录健康状态、清理 stale deps 与孤儿 wheel。
底层扫描/清理复用 :func:`fspack.doctor.envs._scan_cache_health` 与
:func:`fspack.doctor.envs._clean_cache_issues`，本模块仅负责命令编排与渲染。

与 :func:`fspack.doctor.run_doctor_cache_check`（``fsp doctor --check-cache``）
的差异：``fsp cache status`` 输出更详细（分组列出损坏/stale/orphan 具体文件名），
不渲染为单行 :class:`CheckResult` 表格。
"""

from __future__ import annotations

from fspack.doctor.envs import _clean_cache_issues, _format_size, _scan_cache_health
from fspack.doctor.models import CacheHealthReport

__all__ = [
    "run_cache_clean",
    "run_cache_status",
]


def run_cache_status() -> CacheHealthReport:
    """扫描 wheel 缓存目录健康状态，渲染详细报告到控制台.

    iter-139 引入：``fsp cache status`` 调用。

    与 :func:`fspack.doctor.run_doctor_cache_check` 的差异：

    - 输出更详细：分别列出损坏/stale/orphan 的具体文件名（前若干个）
    - 不渲染为单行 ``CheckResult`` 表格，而是分组输出便于阅读
    - 返回 :class:`CacheHealthReport`，调用方可基于字段做后续处理
    """
    from fspack.config.cache import wheel_cache_dir
    from fspack.console import console

    report = _scan_cache_health(wheel_cache_dir())
    console.step(f"缓存健康扫描：{report.cache_dir}")

    if not report.cache_dir.is_dir():
        console.warn(f"缓存目录不存在: {report.cache_dir}")
        return report

    if report.total_deps_files == 0 and report.total_wheels == 0:
        console.success("缓存目录为空（无 deps 缓存文件与 wheel 文件）")
        return report

    console.rich.print("  " + _format_cache_summary(report))
    _print_cache_detail_lists(report)

    if not report.has_issues:
        console.success("缓存健康，无需清理")
    else:
        console.warn(
            f"运行 `fsp cache clean` 清理 stale deps + 孤儿 wheel（可释放 {_format_size(report.orphan_size_bytes)}）"
        )
    return report


def run_cache_clean(*, dry_run: bool = False) -> CacheHealthReport:
    """清理 wheel 缓存中的 stale deps 与 orphan wheels，渲染清理结果.

    iter-139 引入：``fsp cache clean`` 调用，``--dry-run`` 仅预览不删除。

    清理前先 :func:`_scan_cache_health` 获取最新状态，删除 stale deps 文件
    （引用缺失 wheel）与 orphan wheel 文件（未被任何 deps 引用）。
    损坏的 ``.deps-*.json`` 在扫描阶段已自动删除，本函数不再处理。
    """
    from fspack.config.cache import wheel_cache_dir
    from fspack.console import console

    label = "预览" if dry_run else "清理"
    console.step(f"{label} wheel 缓存：{wheel_cache_dir()}")

    report = _clean_cache_issues(wheel_cache_dir(), dry_run=dry_run)

    if not report.cache_dir.is_dir():
        console.warn(f"缓存目录不存在: {report.cache_dir}")
        return report

    if not report.has_issues:
        console.success("缓存健康，无需清理")
        return report

    action = "将删除" if dry_run else "已删除"
    _print_cache_clean_lists(report, action)

    if dry_run:
        console.warn(f"预览完成：运行 `fsp cache clean` 实际删除（可释放 {_format_size(report.orphan_size_bytes)}）")
    else:
        freed = _format_size(report.orphan_size_bytes)
        cleaned_count = len(report.stale_deps_files) + len(report.orphan_wheels) + len(report.corrupt_deps_files)
        console.success(f"清理完成：{cleaned_count} 个文件，释放 {freed}")
    return report


def _format_cache_summary(report: CacheHealthReport) -> str:
    """格式化缓存扫描概要行：deps 计数 + wheel 计数（含孤儿体积）."""
    parts: list[str] = []
    if report.total_deps_files > 0:
        valid = report.total_deps_files - len(report.corrupt_deps_files) - len(report.stale_deps_files)
        detail = f"deps {report.total_deps_files} 个（有效 {valid}"
        if report.corrupt_deps_files:
            detail += f"，损坏已删除 {len(report.corrupt_deps_files)}"
        if report.stale_deps_files:
            detail += f"，stale {len(report.stale_deps_files)}"
        parts.append(detail + "）")
    if report.total_wheels > 0:
        wheel_detail = f"wheel {report.total_wheels} 个"
        if report.orphan_wheels:
            wheel_detail += f"（孤儿 {len(report.orphan_wheels)}，{_format_size(report.orphan_size_bytes)}）"
        parts.append(wheel_detail)
    return "，".join(parts)


def _print_cache_detail_lists(report: CacheHealthReport) -> None:
    """渲染缓存扫描的详细文件名列表（损坏/stale/orphan 各一行）."""
    from fspack.console import console

    if report.corrupt_deps_files:
        console.error(f"损坏 deps（已删除）: {_preview_names(report.corrupt_deps_files)}")
    if report.stale_deps_files:
        console.warn(f"stale deps（引用缺失 wheel）: {_preview_names(report.stale_deps_files)}")
        if report.missing_wheels:
            console.warn(f"  缺失 wheel: {_preview_names(report.missing_wheels)}")
    if report.orphan_wheels:
        console.warn(f"孤儿 wheel（未被任何 deps 引用）: {_preview_names(report.orphan_wheels)}")


def _print_cache_clean_lists(report: CacheHealthReport, action: str) -> None:
    """渲染清理/预览时的文件名列表（action 为"已删除"或"将删除"）."""
    from fspack.console import console

    if report.corrupt_deps_files:
        console.rich.print(f"  损坏 deps（扫描阶段{action}）: {_preview_names(report.corrupt_deps_files)}")
    if report.stale_deps_files:
        console.rich.print(f"  stale deps {action}: {_preview_names(report.stale_deps_files)}")
    if report.orphan_wheels:
        console.rich.print(
            f"  孤儿 wheel {action}: {_preview_names(report.orphan_wheels)}（{_format_size(report.orphan_size_bytes)}）"
        )


def _preview_names(names: tuple[str, ...], limit: int = 5) -> str:
    """格式化文件名列表为预览字符串（前 limit 个 + 总数提示）."""
    if not names:
        return ""
    preview = ", ".join(names[:limit])
    if len(names) > limit:
        preview += f" 等 {len(names)} 个"
    return preview
