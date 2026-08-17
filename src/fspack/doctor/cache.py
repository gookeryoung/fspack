"""缓存健康扫描与清理（``fsp cache status`` / ``fsp cache clean``）.

从 :mod:`fspack.doctor` facade（原 ``cli_doctor``）拆分而来，封装 ``fsp cache``
子命令逻辑：扫描各 cache 子目录健康状态、清理 stale 与孤儿文件。
底层扫描/清理委托 :mod:`fspack.doctor.envs` 中的扫描器与分发器，本模块仅负责
命令编排与渲染。

iter-139：仅扫描 wheels 目录，``fsp cache status``/``fsp cache clean [--dry-run]``。
iter-148 扩展为多 cache 类型：

- 默认扫描全部 cache 子目录（``wheels``/``embed``/``standalone``/``nuitka``/
  ``loaders``/``ccache``/``tkinter``），逐个渲染报告
- ``--target <name>`` 指定单个 cache 类型
- ``--stale`` 启用过期文件清理（旧版本 zip/tar/子目录），默认仅清理损坏文件
  （wheels 例外：``stale_deps``/``orphan_wheels`` 仍按 iter-139 既有行为清理）

与 :func:`fspack.doctor.run_doctor_cache_check`（``fsp doctor --check-cache``）
的差异：``fsp cache status`` 输出更详细（分组列出损坏/stale/orphan 具体文件名），
不渲染为单行 :class:`CheckResult` 表格。
"""

from __future__ import annotations

from typing import Iterable

from fspack.doctor.envs import (
    _clean_all_caches,
    _clean_cache_by_type,
    _format_size,
    _scan_all_caches,
    _scan_cache_by_type,
)
from fspack.doctor.models import CacheHealthReport

__all__ = [
    "CACHE_TYPES",
    "run_cache_clean",
    "run_cache_status",
]

# 支持的 cache 类型清单（与 envs._CACHE_TARGETS 一致），用于 CLI 校验与默认迭代
CACHE_TYPES: tuple[str, ...] = (
    "wheels",
    "embed",
    "standalone",
    "nuitka",
    "loaders",
    "ccache",
    "tkinter",
)


def run_cache_status(target: str | None = None, *, full_verify: bool = False) -> tuple[CacheHealthReport, ...]:
    """扫描 cache 目录健康状态，渲染详细报告到控制台.

    iter-139 引入：``fsp cache status`` 调用，仅扫描 wheels。
    iter-148 扩展：``target=None`` 扫描全部 cache 类型，逐个渲染；
    ``target=<name>`` 仅扫描指定类型。

    与 :func:`fspack.doctor.run_doctor_cache_check` 的差异：

    - 输出更详细：分别列出损坏/stale/orphan 的具体文件名（前若干个）
    - 不渲染为单行 ``CheckResult`` 表格，而是分组输出便于阅读
    - 返回 :class:`CacheHealthReport` 元组，调用方可基于字段做后续处理

    :param target: 指定单 cache 类型扫描；``None`` 扫描全部类型
    :param full_verify: True 时对 zip 归档类缓存（embed/tkinter）启用全量
        CRC 校验（逐文件 ``testzip``，慢但可发现数据区损坏），默认 False
        快检中心目录（``fsp cache status --verify`` 启用全量）
    :return: 报告元组（单 target 时为 1 元组）
    """
    from fspack.console import console

    if target is not None:
        _validate_cache_type(target)
        reports = (_scan_cache_by_type(target, full_verify=full_verify),)
    else:
        reports = _scan_all_caches(full_verify=full_verify)

    console.step(f"缓存健康扫描：{len(reports)} 个目录")
    for report in reports:
        _render_status_report(report)
    return reports


def run_cache_clean(
    *,
    dry_run: bool = False,
    include_stale: bool = False,
    target: str | None = None,
) -> tuple[CacheHealthReport, ...]:
    """清理 cache 目录中的损坏与过期文件，渲染清理结果.

    iter-139 引入：``fsp cache clean`` 调用，``--dry-run`` 仅预览不删除。
    iter-148 扩展：``target`` 指定单类型，``include_stale`` 启用过期文件清理。

    清理规则：

    - 损坏文件（zip/tar 结构损坏、PE 头缺失、空文件）：``dry_run=False`` 时
      扫描期删除（扫描器带 ``delete_corrupt=True``）；``dry_run=True`` 只报告
    - wheels 类型：``stale_deps``（引用缺失 wheel）+ ``orphan_wheels``（孤儿 wheel）
      始终清理（iter-139 既有行为，与 ``include_stale`` 无关）
    - 非 wheels 类型：``stale_files``（旧版本 zip/tar/子目录）仅在
      ``include_stale=True`` 时清理，默认保留（用户可能需要多版本切换）
    - 缺失文件（如 ccache 二进制未下载）无文件可删，不计入 ``total_cleaned``
    - ``dry_run=True``：仅扫描不删除，预览结果与实际清理一致（共享扫描入口）

    :param dry_run: ``True`` 时仅扫描不删除
    :param include_stale: ``True`` 时额外清理过期文件（非 wheels 类型）
    :param target: 指定单 cache 类型清理；``None`` 清理全部类型
    :return: 清理后报告元组
    """
    from fspack.console import console

    if target is not None:
        _validate_cache_type(target)
        reports = (_clean_cache_by_type(target, dry_run=dry_run, include_stale=include_stale),)
    else:
        reports = _clean_all_caches(dry_run=dry_run, include_stale=include_stale)

    label = "预览" if dry_run else "清理"
    stale_label = "（含过期文件）" if include_stale else ""
    console.step(f"{label} cache：{len(reports)} 个目录{stale_label}")
    total_freed = 0
    total_cleaned = 0
    for report in reports:
        _render_clean_report(report, dry_run=dry_run)
        total_freed += _report_freed_size(report)
        total_cleaned += report.issues_count

    if total_cleaned > 0:
        if dry_run:
            console.warn(
                f"预览完成：运行 `fsp cache clean{' --stale' if include_stale else ''}`"
                f"实际删除（可释放 {_format_size(total_freed)}）"
            )
        else:
            console.success(f"清理完成：{total_cleaned} 个文件，释放 {_format_size(total_freed)}")
    return reports


def _validate_cache_type(target: str) -> None:
    """校验 cache 类型名合法，非法时打印错误并以退出码 2 退出."""
    if target not in CACHE_TYPES:
        from fspack.console import console

        console.error(f"未知 cache 类型: {target}，可选: {', '.join(CACHE_TYPES)}")
        raise SystemExit(2) from None


def _render_status_report(report: CacheHealthReport) -> None:
    """渲染单个 cache 类型的健康扫描报告到控制台."""
    from fspack.console import console

    console.rich.print(f"\n[bold cyan]{report.cache_type}[/]: {report.cache_dir}")

    if not report.cache_dir.is_dir():
        console.warn("  目录不存在")
        return

    if _is_empty_report(report) and not report.missing_files:
        console.success("  目录为空（无缓存文件）")
        return

    console.rich.print("  " + _format_cache_summary(report))
    _print_cache_detail_lists(report)

    if not report.has_issues:
        console.success("  健康，无需清理")
    else:
        freed_hint = _format_size(_report_freed_size(report))
        clean_hint = _build_clean_hint(report)
        console.warn(f"  运行 `fsp cache clean{clean_hint}` 清理（可释放 {freed_hint}）")


def _render_clean_report(report: CacheHealthReport, *, dry_run: bool) -> None:
    """渲染单个 cache 类型的清理/预览报告."""
    from fspack.console import console

    console.rich.print(f"\n[bold cyan]{report.cache_type}[/]: {report.cache_dir}")

    if not report.cache_dir.is_dir():
        console.warn("  目录不存在")
        return

    if not report.has_issues and not report.missing_files:
        console.success("  健康，无需清理")
        return

    action = "将删除" if dry_run else "已删除"
    _print_cache_clean_lists(report, action)


def _is_empty_report(report: CacheHealthReport) -> bool:
    """报告是否表示空目录（无任何文件可统计）."""
    if report.cache_type == "wheels":
        return report.total_deps_files == 0 and report.total_wheels == 0
    return report.total_files == 0


def _format_cache_summary(report: CacheHealthReport) -> str:
    """格式化缓存扫描概要行.

    wheels 用专用字段（deps + wheel 计数），其他类型用通用字段（文件总数 + 问题计数）。
    """
    if report.cache_type == "wheels":
        return _format_wheels_summary(report)
    return _format_generic_summary(report)


def _format_wheels_summary(report: CacheHealthReport) -> str:
    """wheels 类型概要：deps 计数 + wheel 计数（含孤儿体积）."""
    parts: list[str] = []
    if report.total_deps_files > 0:
        valid = report.total_deps_files - len(report.corrupt_deps_files) - len(report.stale_deps_files)
        detail = f"deps {report.total_deps_files} 个（有效 {valid}"
        if report.corrupt_deps_files:
            detail += f"，损坏 {len(report.corrupt_deps_files)}"
        if report.stale_deps_files:
            detail += f"，stale {len(report.stale_deps_files)}"
        parts.append(detail + "）")
    if report.total_wheels > 0:
        wheel_detail = f"wheel {report.total_wheels} 个"
        if report.orphan_wheels:
            wheel_detail += f"（孤儿 {len(report.orphan_wheels)}，{_format_size(report.orphan_size_bytes)}）"
        parts.append(wheel_detail)
    return "，".join(parts)


def _format_generic_summary(report: CacheHealthReport) -> str:
    """非 wheels 类型概要：文件总数 + 损坏/过期计数 + 可释放体积."""
    parts: list[str] = [f"文件 {report.total_files} 个"]
    if report.corrupt_files:
        parts.append(f"损坏 {len(report.corrupt_files)}")
    if report.stale_files:
        parts.append(f"过期 {len(report.stale_files)}")
    if report.orphan_files:
        parts.append(f"孤儿 {len(report.orphan_files)}")
    if report.issues_size_bytes > 0:
        parts.append(f"可释放 {_format_size(report.issues_size_bytes)}")
    return "，".join(parts)


def _print_cache_detail_lists(report: CacheHealthReport) -> None:
    """渲染缓存扫描的详细文件名列表（损坏/缺失/stale/orphan 各一行）."""
    from fspack.console import console

    if report.cache_type == "wheels":
        _print_wheels_detail_lists(report)
        return

    if report.corrupt_files:
        console.error(f"  损坏文件: {_preview_names(report.corrupt_files)}")
    if report.missing_files:
        console.warn(f"  缺失文件: {_preview_names(report.missing_files)}")
    if report.stale_files:
        console.warn(f"  过期文件（旧版本，需 --stale 清理）: {_preview_names(report.stale_files)}")
    if report.orphan_files:
        console.warn(f"  孤儿文件: {_preview_names(report.orphan_files)}")


def _print_wheels_detail_lists(report: CacheHealthReport) -> None:
    """wheels 类型专用：渲染损坏 deps / stale deps / 孤儿 wheel 列表."""
    from fspack.console import console

    if report.corrupt_deps_files:
        console.error(f"  损坏 deps: {_preview_names(report.corrupt_deps_files)}")
    if report.stale_deps_files:
        console.warn(f"  stale deps（引用缺失 wheel）: {_preview_names(report.stale_deps_files)}")
        if report.missing_wheels:
            console.warn(f"  缺失 wheel: {_preview_names(report.missing_wheels)}")
    if report.orphan_wheels:
        console.warn(f"  孤儿 wheel（未被任何 deps 引用）: {_preview_names(report.orphan_wheels)}")


def _print_cache_clean_lists(report: CacheHealthReport, action: str) -> None:
    """渲染清理/预览时的文件名列表（action 为"已删除"或"将删除"）."""
    from fspack.console import console

    if report.cache_type == "wheels":
        _print_wheels_clean_lists(report, action)
        return

    if report.corrupt_files:
        console.rich.print(f"  损坏文件（扫描阶段{action}）: {_preview_names(report.corrupt_files)}")
    if report.missing_files:
        # 缺失文件无文件可删，不参与 action（删除）统计，单独提示需重新下载
        console.warn(f"  缺失文件（下次使用时自动重新下载）: {_preview_names(report.missing_files)}")
    if report.stale_files:
        console.rich.print(f"  过期文件 {action}: {_preview_names(report.stale_files)}")
    if report.orphan_files:
        console.rich.print(f"  孤儿文件 {action}: {_preview_names(report.orphan_files)}")


def _print_wheels_clean_lists(report: CacheHealthReport, action: str) -> None:
    """wheels 类型专用：渲染清理/预览列表."""
    from fspack.console import console

    if report.corrupt_deps_files:
        console.rich.print(f"  损坏 deps（扫描阶段{action}）: {_preview_names(report.corrupt_deps_files)}")
    if report.stale_deps_files:
        console.rich.print(f"  stale deps {action}: {_preview_names(report.stale_deps_files)}")
    if report.orphan_wheels:
        console.rich.print(
            f"  孤儿 wheel {action}: {_preview_names(report.orphan_wheels)}（{_format_size(report.orphan_size_bytes)}）"
        )


def _report_freed_size(report: CacheHealthReport) -> int:
    """计算单 report 可释放字节数（wheels 用 orphan_size_bytes，其他用 issues_size_bytes）."""
    if report.cache_type == "wheels":
        return report.orphan_size_bytes
    return report.issues_size_bytes


def _build_clean_hint(report: CacheHealthReport) -> str:
    """根据报告内容生成 ``fsp cache clean`` 提示参数（含 --target / --stale）."""
    parts: list[str] = []
    if report.cache_type != "wheels":
        parts.append(f"--target {report.cache_type}")
    if report.stale_files and report.cache_type != "wheels":
        parts.append("--stale")
    return " " + " ".join(parts) if parts else ""


def _preview_names(names: Iterable[str], limit: int = 5) -> str:
    """格式化文件名列表为预览字符串（前 limit 个 + 总数提示）."""
    names_tuple = tuple(names)
    if not names_tuple:
        return ""
    preview = ", ".join(names_tuple[:limit])
    if len(names_tuple) > limit:
        preview += f" 等 {len(names_tuple)} 个"
    return preview
