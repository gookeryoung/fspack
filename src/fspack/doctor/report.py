"""``fsp doctor`` 诊断报告渲染.

将 :class:`fspack.doctor.models.DoctorReport` 渲染到控制台：环境信息表 +
工具检查表 + 汇总结论。颜色映射 OK=绿、WARN=黄、ERROR=红，表格用
:class:`rich.table.Table`，通过 :data:`fspack.console.console` 输出，
复用现有日志配置（颜色/编码）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fspack.console import console
from fspack.doctor.models import CheckResult, CheckStatus, DoctorReport

if TYPE_CHECKING:
    from rich.table import Table

__all__ = [
    "_build_table",
    "_format_status",
    "_print_summary",
    "print_doctor_report",
]


def print_doctor_report(report: DoctorReport) -> None:
    """渲染诊断报告到控制台：环境信息表 + 工具检查表 + 汇总.

    颜色映射：OK=绿、WARN=黄、ERROR=红。表格用 :class:`rich.table.Table`，
    通过 :data:`fspack.console.console` 输出，复用现有日志配置（颜色/编码）。
    """
    console.step("环境信息")
    console.rich.print(_build_table("环境信息", report.env_info))

    console.rich.print()
    console.step("工具检查")
    console.rich.print(_build_table("工具检查", report.tool_checks))

    console.rich.print()
    _print_summary(report)


def _build_table(title: str, results: tuple[CheckResult, ...]) -> Table:
    """构建 rich Table：名称 / 状态 / 详情 / 修复建议."""
    from rich.table import Table

    table = Table(title=title, show_lines=False)
    table.add_column("名称", style="cyan", no_wrap=True)
    table.add_column("状态", justify="center")
    table.add_column("详情")
    table.add_column("修复建议", style="yellow")

    for result in results:
        status_str, style = _format_status(result.status)
        table.add_row(
            result.name,
            f"[{style}]{status_str}[/]",
            result.detail,
            result.suggestion,
        )
    return table


def _format_status(status: CheckStatus) -> tuple[str, str]:
    """状态枚举转中文 + rich 样式字符串."""
    if status is CheckStatus.OK:
        return ("√ OK", "green")
    if status is CheckStatus.WARN:
        return ("! WARN", "yellow")
    return ("× ERROR", "red")


def _print_summary(report: DoctorReport) -> None:
    """打印汇总：错误数 / 警告数 / 总体结论."""
    errors = sum(1 for c in report.tool_checks if c.status is CheckStatus.ERROR)
    warns = sum(1 for c in report.tool_checks if c.status is CheckStatus.WARN)
    oks = sum(1 for c in report.tool_checks if c.status is CheckStatus.OK)

    if errors:
        console.error(f"诊断完成：{oks} OK / {warns} 警告 / {errors} 错误")
        console.error(f"存在 {errors} 项错误，可能导致打包失败，请按'修复建议'处理")
    elif warns:
        console.warn(f"诊断完成：{oks} OK / {warns} 警告 / {errors} 错误")
        console.warn("存在警告项，不阻塞打包但建议处理")
    else:
        console.success(f"诊断完成：{oks} OK / {warns} 警告 / {errors} 错误")
        console.success("环境就绪，可以开始打包")
