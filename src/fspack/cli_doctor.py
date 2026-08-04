"""``fsp doctor`` 环境诊断子命令.

facade 模块：编排 :mod:`fspack.doctor_envs`（环境信息检查）、
:mod:`fspack.doctor_tools`（工具版本检查）、:mod:`fspack.doctor_report`
（报告渲染）、:mod:`fspack.doctor_templates`（模板构建测试与基准）、
:mod:`fspack.doctor_bench`（基准历史持久化与对比）完成环境诊断。

检查 fspack 打包所需工具的可用性与版本，显示 Python 版本、平台、镜像源
配置、缓存目录大小，输出绿/黄/红三色诊断结果与修复建议，帮助用户前置
发现环境问题，避免打包中途失败。

诊断项分两类：

- **工具检查**：mingw-w64/gcc/NSIS/wine/pip/uv/Pillow 等外部依赖
  （按平台过滤，Windows 不查 gcc/wine，Linux 不查 mingw/NSIS）
- **环境信息**：Python 版本、平台、镜像源、缓存目录大小

设计要点：

- 工具检查用 ``subprocess.run([tool, "--version"], ...)``
  + ``shell=False``，超时 5s，失败返回 :class:`CheckResult` 标记缺失
- 缓存目录大小扫描用 ``os.scandir`` 递归累加文件大小，避免引入额外依赖
- 报告渲染用 :class:`rich.table.Table`，复用 :data:`fspack.console.console`
- 所有 check 函数返回 :class:`CheckResult`，主入口 :func:`run_doctor`
  聚合为 :class:`DoctorReport`，便于测试断言

facade 通过 ``from submodule import _xxx`` 把测试需要 monkeypatch 的私有
函数（``_check_*``/``_dir_size``/``_format_size`` 等）绑定到本模块命名空间，
``run_doctor`` 调用时访问 facade globals，patch ``fspack.cli_doctor._xxx``
即可生效。``shutil``/``subprocess`` 模块属性同样保留，patch
``fspack.cli_doctor.shutil.which`` 修改标准库模块属性全局生效。
"""

from __future__ import annotations

import shutil  # noqa: F401 - 测试 patch `fspack.cli_doctor.shutil.which` 需要本模块属性
import subprocess  # noqa: F401 - 测试 patch `fspack.cli_doctor.subprocess.run/Popen` 需要本模块属性

from fspack.doctor_bench import (
    _bench_history_group_dir,
    _collect_machine_info,
    _deserialize_bench_results,
    _format_bench_delta,
    _load_previous_bench_history,
    _machine_id,
    _print_bench_comparison,
    _save_and_compare_bench,
    _save_bench_history,
    _serialize_bench_results,
)
from fspack.doctor_envs import (
    _check_cache_dir,
    _check_cache_integrity,
    _check_fspack_version,
    _check_mirror_config,
    _check_platform_info,
    _check_python,
    _clean_cache_issues,
    _dir_size,
    _format_size,
    _scan_cache_health,
)
from fspack.doctor_models import (
    CacheHealthReport,
    CheckResult,
    CheckStatus,
    DoctorReport,
    TemplateBuildResult,
    TemplateRunResult,
)
from fspack.doctor_report import (
    _build_table,
    _format_status,
    _print_summary,
    print_doctor_report,
)
from fspack.doctor_templates import (
    _build_debug_cmd,
    _build_run_cmd,
    _build_single_template,
    _find_debug_python,
    _find_dist_exe,
    _find_wrapper,
    _format_run_status,
    _print_performance_analysis,
    _print_run_summary,
    _print_template_build_summary,
    _run_template,
    run_doctor_bench,
    run_doctor_test,
)
from fspack.doctor_tools import (
    _check_clang,
    _check_gcc,
    _check_makensis_on_linux,
    _check_mingw,
    _check_nsis,
    _check_pillow,
    _check_pip,
    _check_tool_version,
    _check_uv,
    _check_wine,
)

__all__ = [
    "CacheHealthReport",
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "TemplateBuildResult",
    "TemplateRunResult",
    # 测试 monkeypatch 需要的私有名（绑定到 facade 命名空间，
    # patch `fspack.cli_doctor._xxx` 即可影响 `run_doctor` 调用）
    "_bench_history_group_dir",
    "_build_debug_cmd",
    "_build_run_cmd",
    "_build_single_template",
    "_build_table",
    "_check_cache_dir",
    "_check_cache_integrity",
    "_check_clang",
    "_check_fspack_version",
    "_check_gcc",
    "_check_makensis_on_linux",
    "_check_mingw",
    "_check_mirror_config",
    "_check_nsis",
    "_check_pillow",
    "_check_pip",
    "_check_platform_info",
    "_check_python",
    "_check_tool_version",
    "_check_uv",
    "_check_wine",
    "_clean_cache_issues",
    "_collect_machine_info",
    "_deserialize_bench_results",
    "_dir_size",
    "_find_debug_python",
    "_find_dist_exe",
    "_find_wrapper",
    "_format_bench_delta",
    "_format_run_status",
    "_format_size",
    "_format_status",
    "_load_previous_bench_history",
    "_machine_id",
    "_print_bench_comparison",
    "_print_performance_analysis",
    "_print_run_summary",
    "_print_summary",
    "_print_template_build_summary",
    "_run_template",
    "_save_and_compare_bench",
    "_save_bench_history",
    "_scan_cache_health",
    "_serialize_bench_results",
    "print_doctor_report",
    "run_cache_clean",
    "run_cache_status",
    "run_doctor",
    "run_doctor_bench",
    "run_doctor_cache_check",
    "run_doctor_test",
]


def run_doctor() -> DoctorReport:
    """执行环境诊断，返回完整报告.

    按当前平台过滤工具检查项（Windows 查 mingw/NSIS，Linux 查 gcc/wine），
    聚合环境信息与工具检查结果。不抛异常：所有失败转为 :class:`CheckResult`
    标记 ERROR/WARN，便于报告统一渲染。
    """
    from fspack.config import DEFAULT_MIRROR, MIRRORS
    from fspack.config.cache import cache_root
    from fspack.platform import Platform, detect_platform

    platform = detect_platform()
    env_info: list[CheckResult] = [
        _check_python(),
        _check_platform_info(platform),
        _check_fspack_version(),
        _check_mirror_config(DEFAULT_MIRROR, MIRRORS),
        _check_cache_dir(cache_root()),
    ]

    tool_checks: list[CheckResult] = []
    # 通用工具：pip/uv/Pillow（两平台都需要）
    tool_checks.append(_check_pip())
    tool_checks.append(_check_uv())
    tool_checks.append(_check_pillow())

    if platform is Platform.WINDOWS:
        tool_checks.append(_check_mingw())
        tool_checks.append(_check_nsis())
    elif platform is Platform.MACOS:
        tool_checks.append(_check_clang())
    else:
        tool_checks.append(_check_gcc())
        tool_checks.append(_check_wine())
        tool_checks.append(_check_makensis_on_linux())

    return DoctorReport(
        env_info=tuple(env_info),
        tool_checks=tuple(tool_checks),
    )


def run_doctor_cache_check() -> CheckResult:
    """执行缓存完整性检查，渲染结果到控制台并返回 :class:`CheckResult`.

    iter-128 引入：``fsp doctor --check-cache`` 调用。
    iter-139 扩展：详情中追加 stale deps（引用缺失 wheel）与 orphan wheels
    （未被任何 deps 引用）计数，并提示用户用 ``fsp cache clean`` 清理。
    """
    from fspack.config.cache import wheel_cache_dir
    from fspack.console import console

    result = _check_cache_integrity(wheel_cache_dir())
    console.step("缓存完整性检查")
    console.rich.print(_build_table("缓存完整性", (result,)))
    return result


def run_cache_status() -> CacheHealthReport:
    """扫描 wheel 缓存目录健康状态，渲染详细报告到控制台.

    iter-139 引入：``fsp cache status`` 调用。

    与 :func:`run_doctor_cache_check` 的差异：

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

    # 概要行
    summary_parts: list[str] = []
    if report.total_deps_files > 0:
        valid = report.total_deps_files - len(report.corrupt_deps_files) - len(report.stale_deps_files)
        summary_parts.append(f"deps {report.total_deps_files} 个（有效 {valid}")
        if report.corrupt_deps_files:
            summary_parts[-1] += f"，损坏已删除 {len(report.corrupt_deps_files)}"
        if report.stale_deps_files:
            summary_parts[-1] += f"，stale {len(report.stale_deps_files)}"
        summary_parts[-1] += "）"
    if report.total_wheels > 0:
        summary_parts.append(f"wheel {report.total_wheels} 个")
        if report.orphan_wheels:
            summary_parts[-1] += f"（孤儿 {len(report.orphan_wheels)}，{_format_size(report.orphan_size_bytes)}）"
    console.rich.print("  " + "，".join(summary_parts))

    # 详细列表（每组最多列 5 个，避免刷屏）
    if report.corrupt_deps_files:
        console.error(f"损坏 deps（已删除）: {_preview_names(report.corrupt_deps_files)}")
    if report.stale_deps_files:
        console.warn(f"stale deps（引用缺失 wheel）: {_preview_names(report.stale_deps_files)}")
        if report.missing_wheels:
            console.warn(f"  缺失 wheel: {_preview_names(report.missing_wheels)}")
    if report.orphan_wheels:
        console.warn(f"孤儿 wheel（未被任何 deps 引用）: {_preview_names(report.orphan_wheels)}")

    if not report.has_issues:
        console.success("缓存健康，无需清理")
    else:
        console.warn(f"运行 `fsp cache clean` 清理 stale deps + 孤儿 wheel（可释放 {_format_size(report.orphan_size_bytes)}）")
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
    if report.corrupt_deps_files:
        console.rich.print(f"  损坏 deps（扫描阶段{action}）: {_preview_names(report.corrupt_deps_files)}")
    if report.stale_deps_files:
        console.rich.print(f"  stale deps {action}: {_preview_names(report.stale_deps_files)}")
    if report.orphan_wheels:
        console.rich.print(
            f"  孤儿 wheel {action}: {_preview_names(report.orphan_wheels)}"
            f"（{_format_size(report.orphan_size_bytes)}）"
        )

    if dry_run:
        console.warn(f"预览完成：运行 `fsp cache clean` 实际删除（可释放 {_format_size(report.orphan_size_bytes)}）")
    else:
        freed = _format_size(report.orphan_size_bytes)
        cleaned_count = len(report.stale_deps_files) + len(report.orphan_wheels) + len(report.corrupt_deps_files)
        console.success(f"清理完成：{cleaned_count} 个文件，释放 {freed}")
    return report


def _preview_names(names: tuple[str, ...], limit: int = 5) -> str:
    """格式化文件名列表为预览字符串（前 limit 个 + 总数提示）."""
    if not names:
        return ""
    preview = ", ".join(names[:limit])
    if len(names) > limit:
        preview += f" 等 {len(names)} 个"
    return preview
