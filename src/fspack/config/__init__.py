"""fspack 配置 facade：从 models/parsing/versions 三个模块 re-export 公开 API.

本模块是 :mod:`fspack.config` 的入口与 API 索引，无业务逻辑。原 ``config.py``
（887 行）按职责拆分到三个模块：

- :mod:`fspack.config.models`：数据结构（``AppType``/``MirrorConfig``/
  ``EntryPoint``/``ProjectInfo``/``DependencyReport``/``BuildConfig``/
  ``BuildOptions``/``SlimRules``/``BuildDefaults``）+ 镜像源 + 工具函数
  （``_parse_string_list_cfg``/``_match_any_glob``）
- :mod:`fspack.config.parsing`：pyproject.toml 解析（``parse_project``）+
  入口识别（``detect_entry``/``infer_app_type``）+
  ``[tool.fspack]`` 配置项解析（``_parse_build_defaults``/``_resolve_icon`` 等）
- :mod:`fspack.config.versions`：Python embed/standalone 版本映射 +
  Nuitka 版本锁定 + PEP 440 ``requires-python`` 匹配

公共 API：

- 数据结构：``AppType``/``MirrorConfig``/``EntryPoint``/``ProjectInfo``/
  ``DependencyReport``/``BuildConfig``
- 镜像源：``MIRRORS``/``DEFAULT_MIRROR``/``get_mirror``
- 项目解析：``parse_project``/``detect_entry``/``infer_app_type``/
  ``clear_project_cache``/``resolve_py_version``/``DEFAULT_PY_VERSION``/
  ``DEFAULT_LINUX_PY_VERSION``/``KNOWN_EMBED_VERSIONS``/``KNOWN_STANDALONE_VERSIONS``/
  ``known_versions``
"""

from __future__ import annotations

# 公开 API 与私有辅助：re-export 保持 ``from fspack.config import X`` 路径兼容
from fspack.config.cache import (
    cache_root,
    ccache_cache_dir,
    embed_cache_dir,
    is_offline,
    loader_cache_dir,
    nuitka_cache_dir,
    standalone_cache_dir,
    tkinter_cache_dir,
    wheel_cache_dir,
)
from fspack.config.models import (
    DEFAULT_MIRROR,
    DEFAULT_SLIM_RULES,  # noqa: F401
    MIRRORS,
    AppType,
    BuildConfig,
    BuildDefaults,
    BuildOptions,
    DependencyReport,
    EntryPoint,
    MirrorConfig,
    ProjectInfo,
    SlimRules,  # noqa: F401
    _match_any_glob,  # noqa: F401
    _parse_string_list_cfg,  # noqa: F401
    build_options_from_defaults,
    get_mirror,
)
from fspack.config.parsing import (
    _BUILD_DEFAULT_KEYS,  # noqa: F401
    _GUI_HINTS,  # noqa: F401
    _has_entry,  # noqa: F401
    _is_main_check,  # noqa: F401
    _merge_entries,  # noqa: F401
    _parse_build_defaults,  # noqa: F401
    _parse_entries,  # noqa: F401
    _parse_exclude_dirs,  # noqa: F401
    _parse_project_scripts,  # noqa: F401
    _resolve_icon,  # noqa: F401
    _resolve_module_script,  # noqa: F401
    clear_project_cache,
    detect_entry,
    infer_app_type,
    parse_project,
)
from fspack.config.versions import (
    _SPEC_RE,  # noqa: F401
    DEFAULT_LINUX_PY_VERSION,
    DEFAULT_NUITKA_VERSION,
    DEFAULT_PY_VERSION,
    KNOWN_EMBED_VERSIONS,
    KNOWN_STANDALONE_VERSIONS,
    NUITKA_VERSIONS,
    _normalize_py_version,  # noqa: F401
    _read_python_version,  # noqa: F401
    _satisfies,  # noqa: F401
    _satisfies_wildcard,  # noqa: F401
    _ver_key,  # noqa: F401
    known_versions,
    nuitka_version_for,
    resolve_py_version,
)

__all__ = [
    "DEFAULT_LINUX_PY_VERSION",
    "DEFAULT_MIRROR",
    "DEFAULT_NUITKA_VERSION",
    "DEFAULT_PY_VERSION",
    "KNOWN_EMBED_VERSIONS",
    "KNOWN_STANDALONE_VERSIONS",
    "MIRRORS",
    "NUITKA_VERSIONS",
    "AppType",
    "BuildConfig",
    "BuildDefaults",
    "BuildOptions",
    "DependencyReport",
    "EntryPoint",
    "MirrorConfig",
    "ProjectInfo",
    "build_options_from_defaults",
    "cache_root",
    "ccache_cache_dir",
    "clear_project_cache",
    "detect_entry",
    "embed_cache_dir",
    "get_mirror",
    "infer_app_type",
    "is_offline",
    "known_versions",
    "loader_cache_dir",
    "nuitka_cache_dir",
    "nuitka_version_for",
    "parse_project",
    "resolve_py_version",
    "standalone_cache_dir",
    "tkinter_cache_dir",
    "wheel_cache_dir",
]
