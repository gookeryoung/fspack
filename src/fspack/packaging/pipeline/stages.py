"""构建阶段函数 re-export 门面，保持公开导入路径兼容.

原 stages.py（744 行）按职责拆为多个聚焦模块：

- :mod:`fspack.packaging.pipeline.context`：``BuildContext`` 数据类 + 路径常量
- :mod:`fspack.packaging.pipeline.runtime_stage`：runtime 下载/解压/精简
- :mod:`fspack.packaging.pipeline.deps_stage`：依赖分析/下载/缓存/wheel 解压
- :mod:`fspack.packaging.pipeline.compile_stage`：源码编译/loader 生成/图标/二进制依赖分析
- :mod:`fspack.packaging.pipeline.frontend_stage`：web 结构识别与前端自动构建

本模块从四个子模块 re-export 所有名字，保持 ``fspack.packaging.pipeline.stages.*``
与 ``fspack.packaging.pipeline.*`` 原 patch 路径不变，测试 monkeypatch 和外部
调用 100% 兼容。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 显式导入运行时依赖：兼容测试 monkeypatch.setattr("fspack.packaging.pipeline.stages.<fn>", ...)
# 阶段函数内部通过本模块属性名字解析调用（见各子模块 _S/_RS/_CS/_DS dispatch 函数），
# 因此 patch 本模块同名属性即可在运行时被感知。
# ---------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor

from fspack.packaging.builtin import TkinterBundler
from fspack.packaging.loader import compile_loader
from fspack.packaging.pipeline.compile_stage import (
    _analyze_binary_dependencies,
    _build_entry_loaders,
    _compile_user_sources,
    _resolve_project_icon,
)
from fspack.packaging.pipeline.context import (
    _DEFAULT_ICON,
    _MAX_LOADER_WORKERS,
    BuildContext,
    default_icon_path,
    fspack_wheel_cache_dir,
)
from fspack.packaging.pipeline.deps_stage import (
    _analyze_dependencies,
    _dep_cache_load,
    _dep_cache_path,
    _dep_cache_save,
    _download_dependencies,
    _site_packages_has_deps,
    _strip_version_specifier,
    unpack_wheels,
)
from fspack.packaging.pipeline.frontend_stage import (
    FrontendProject,
    _build_frontend,
    _detect_frontends,
    _frontend_prune_map,
)
from fspack.packaging.pipeline.runtime_stage import (
    _prepare_runtime,
    _prepare_standalone_runtime,
    _prepare_windows_runtime,
    _slim_runtime,
)
from fspack.packaging.runtime import (
    download_embed,
    download_standalone,
    extract_embed,
    extract_standalone,
    write_pth,
)
from fspack.packaging.site_packages import normalize_pkg_name as _normalize_pkg_name
from fspack.packaging.wheels import download_wheels
from fspack.platform import detect_platform

__all__ = [
    "_DEFAULT_ICON",
    "_MAX_LOADER_WORKERS",
    "BuildContext",
    "FrontendProject",
    "ThreadPoolExecutor",
    "TkinterBundler",
    "_analyze_binary_dependencies",
    "_analyze_dependencies",
    "_build_entry_loaders",
    "_build_frontend",
    "_compile_user_sources",
    "_dep_cache_load",
    "_dep_cache_path",
    "_dep_cache_save",
    "_detect_frontends",
    "_download_dependencies",
    "_frontend_prune_map",
    "_normalize_pkg_name",
    "_prepare_runtime",
    "_prepare_standalone_runtime",
    "_prepare_windows_runtime",
    "_resolve_project_icon",
    "_site_packages_has_deps",
    "_slim_runtime",
    "_strip_version_specifier",
    "compile_loader",
    "default_icon_path",
    "detect_platform",
    "download_embed",
    "download_standalone",
    "download_wheels",
    "extract_embed",
    "extract_standalone",
    "fspack_wheel_cache_dir",
    "unpack_wheels",
    "write_pth",
]
