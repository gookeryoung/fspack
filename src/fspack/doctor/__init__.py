"""``fsp doctor`` 环境诊断子包.

facade 子包：编排 :mod:`fspack.doctor.envs`（环境信息检查）、
:mod:`fspack.doctor.tools`（工具版本检查）、:mod:`fspack.doctor.report`
（报告渲染）、:mod:`fspack.doctor.templates`（模板构建测试与基准）、
:mod:`fspack.doctor.bench`（基准历史持久化与对比）、:mod:`fspack.doctor.cache`
（wheel 缓存健康扫描与清理）、:mod:`fspack.doctor.runner`（诊断编排入口）
完成环境诊断。

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

facade 通过 ``from submodule import _xxx`` 把各私有函数（``_check_*``/
``_dir_size``/``_format_size`` 等）绑定到本模块命名空间供直接引用与单元
测试导入。``run_doctor`` 的内部调用经 :mod:`fspack.doctor.runner` 全局名
解析——拦截其内部调用的 monkeypatch 请 patch ``fspack.doctor.runner._check_*``
（定义所在模块）。``shutil``/``subprocess`` 模块属性同样保留，patch
``fspack.doctor.shutil.which`` 修改标准库模块属性全局生效。
"""

from __future__ import annotations

import shutil  # noqa: F401 - 测试 patch `fspack.doctor.shutil.which` 需要本模块属性
import subprocess  # noqa: F401 - 测试 patch `fspack.doctor.subprocess.run/Popen` 需要本模块属性

from fspack.doctor.bench import (
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
from fspack.doctor.cache import (
    CACHE_TYPES,
    _preview_names,
    run_cache_clean,
    run_cache_status,
)
from fspack.doctor.cache_health import (
    _clean_all_caches,
    _clean_cache_by_type,
    _clean_cache_issues,
    _scan_all_caches,
    _scan_cache_by_type,
    _scan_cache_health,
    _scan_ccache_health,
    _scan_embed_health,
    _scan_loader_health,
    _scan_nuitka_health,
    _scan_standalone_health,
    _scan_tkinter_health,
)
from fspack.doctor.envs import (
    _check_cache_dir,
    _check_cache_integrity,
    _check_fspack_version,
    _check_mirror_config,
    _check_platform_info,
    _check_python,
    _dir_size,
    _format_size,
)
from fspack.doctor.integrity import (
    _file_size,
    _is_pe_file,
    _is_tar_intact,
    _is_zip_intact,
    _try_unlink,
)
from fspack.doctor.models import (
    CacheHealthReport,
    CheckResult,
    CheckStatus,
    DoctorReport,
    TemplateBuildResult,
    TemplateRunResult,
)
from fspack.doctor.report import (
    _build_table,
    _format_status,
    _print_summary,
    print_doctor_report,
)
from fspack.doctor.runner import (
    run_doctor,
    run_doctor_cache_check,
)
from fspack.doctor.templates import (
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
from fspack.doctor.tools import (
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
from fspack.doctor.win7 import _check_win7_compat

__all__ = [
    "CACHE_TYPES",
    "CacheHealthReport",
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "TemplateBuildResult",
    "TemplateRunResult",
    # 各私有名（绑定到 facade 命名空间供直接引用与单元测试导入，
    # 拦截 run_doctor 内部调用请 patch `fspack.doctor.runner._xxx`）
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
    "_check_win7_compat",
    "_check_wine",
    "_clean_all_caches",
    "_clean_cache_by_type",
    "_clean_cache_issues",
    "_collect_machine_info",
    "_deserialize_bench_results",
    "_dir_size",
    "_file_size",
    "_find_debug_python",
    "_find_dist_exe",
    "_find_wrapper",
    "_format_bench_delta",
    "_format_run_status",
    "_format_size",
    "_format_status",
    "_is_pe_file",
    "_is_tar_intact",
    "_is_zip_intact",
    "_load_previous_bench_history",
    "_machine_id",
    "_preview_names",
    "_print_bench_comparison",
    "_print_performance_analysis",
    "_print_run_summary",
    "_print_summary",
    "_print_template_build_summary",
    "_run_template",
    "_save_and_compare_bench",
    "_save_bench_history",
    "_scan_all_caches",
    "_scan_cache_by_type",
    "_scan_cache_health",
    "_scan_ccache_health",
    "_scan_embed_health",
    "_scan_loader_health",
    "_scan_nuitka_health",
    "_scan_standalone_health",
    "_scan_tkinter_health",
    "_serialize_bench_results",
    "_try_unlink",
    "print_doctor_report",
    "run_cache_clean",
    "run_cache_status",
    "run_doctor",
    "run_doctor_bench",
    "run_doctor_cache_check",
    "run_doctor_test",
]
