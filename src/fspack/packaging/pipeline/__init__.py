"""构建流水线编排入口：``build`` 主入口 + re-export.

``__init__.py`` 为 facade，按职责拆分到子模块（公开 API / 测试 patch 路径
保持不变）：

- :mod:`fspack.packaging.pipeline.executor`：``build`` / ``_execute_build`` /
  ``resolve_project_info`` 主编排（顶部轻量化：console/progress/profile 均
  为函数内延迟导入）；拦截编排内部调用的 monkeypatch 请 patch
  ``fspack.packaging.pipeline.executor.<fn>``
- :mod:`fspack.packaging.pipeline.stages`（re-export 门面）：阶段函数实现，
  继续拆分为 :mod:`context` / :mod:`runtime_stage` / :mod:`deps_stage` /
  :mod:`compile_stage` 四个聚焦模块
- :mod:`fspack.packaging.pipeline.dist_helpers`：dist 半成品检测、失败诊断
  标记、clean_dist 清理（180+ 行）
- :mod:`fspack.packaging.pipeline.plan_printer`：dry-run 打包计划打印（120+ 行，
  惰性加载避免顶层引入 rich）

本模块 re-export 各子模块名字保持 ``fspack.packaging.pipeline.<fn>`` 引用与
``stages.<fn>`` patch 路径兼容；``_print_build_plan`` 因延迟加载约定不在此
re-export（直接从 :mod:`fspack.packaging.pipeline.plan_printer` 导入）。
"""

from __future__ import annotations

from fspack.config import DependencyReport  # __all__ re-export（F401 豁免：__all__ 成员）
from fspack.packaging.loader import compile_loader  # noqa: F401 — facade re-export（历史引用面兼容）
from fspack.packaging.log_file import LogFormat, setup_log_file, teardown_log_file  # noqa: F401 — facade re-export
from fspack.packaging.pipeline.dist_helpers import (
    _BUILD_FAILED,
    _BUILD_OK,
    _KEEP_NSI,
    _NUITKA_STAMP,
    _PYC_STAMP,
    _clean_dist_dir,
    _handle_dist_incomplete,  # noqa: F401 — facade re-export（test_builder 从本包导入）
    _has_build_stamps,
    _load_build_failure,
    _remove_build_failure,  # noqa: F401 — facade re-export（test_builder 从本包导入）
    _remove_build_ok,  # noqa: F401 — facade re-export（test_builder 从本包导入）
    _save_build_failure,  # noqa: F401 — facade re-export（test_builder 从本包导入）
    _save_build_ok,  # noqa: F401 — facade re-export（test_builder 从本包导入）
    clean_dist,
)
from fspack.packaging.pipeline.executor import (  # noqa: F401 — build 编排入口 re-export
    _execute_build,
    build,
    resolve_project_info,
)
from fspack.packaging.pipeline.stages import (
    _DEFAULT_ICON,  # noqa: F401
    BuildContext,
    _analyze_binary_dependencies,  # noqa: F401
    _analyze_dependencies,  # noqa: F401
    _build_entry_loaders,  # noqa: F401
    _compile_user_sources,  # noqa: F401
    _dep_cache_load,  # noqa: F401
    _dep_cache_path,  # noqa: F401
    _dep_cache_save,  # noqa: F401
    _download_dependencies,  # noqa: F401
    _normalize_pkg_name,  # noqa: F401
    _prepare_runtime,  # noqa: F401
    _prepare_standalone_runtime,  # noqa: F401
    _prepare_windows_runtime,  # noqa: F401
    _resolve_project_icon,  # noqa: F401
    _site_packages_has_deps,  # noqa: F401
    _slim_runtime,  # noqa: F401
    _strip_version_specifier,  # noqa: F401
    default_icon_path,
    fspack_wheel_cache_dir,
    unpack_wheels,
)
from fspack.packaging.runtime import (  # noqa: F401 — facade re-export（历史引用面兼容）
    download_embed,
    download_standalone,
    extract_embed,
    extract_standalone,
    write_pth,
)
from fspack.packaging.sync import copy_source  # noqa: F401 — facade re-export
from fspack.packaging.wheels import download_wheels  # noqa: F401 — facade re-export
from fspack.platform import Platform, detect_platform  # noqa: F401 — facade re-export

__all__ = [
    "_BUILD_FAILED",
    "_BUILD_OK",
    "_KEEP_NSI",
    "_NUITKA_STAMP",
    "_PYC_STAMP",
    "BuildContext",
    "DependencyReport",
    "_clean_dist_dir",
    "_has_build_stamps",
    "_load_build_failure",
    "build",
    "clean_dist",
    "default_icon_path",
    "fspack_wheel_cache_dir",
    "resolve_project_info",
    "unpack_wheels",
]
