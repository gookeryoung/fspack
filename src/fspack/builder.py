"""构建流水线 facade：从 sync/pyc/pipeline 三个模块 re-export 公开 API.

本模块是 :mod:`fspack.builder` 的入口与 API 索引，无业务逻辑。原 builder.py
按职责拆分到三个模块：

- :mod:`fspack.packaging.sync`：源码同步（``copy_source``/``_sync_tree``/
  ``_dir_size``/``_site_packages_fingerprint``/``_EXCLUDE``）
- :mod:`fspack.packaging.pyc`：字节码预编译（``_precompile_pyc``/``_trim_stdlib``/
  ``_inject_win7_compat_dll``/``_needs_win7_compat_dll``/``_strip_py_sources``）
- :mod:`fspack.packaging.pipeline`：阶段编排（``build``/``_prepare_runtime``/
  ``_analyze_dependencies``/``_download_dependencies``/``_compile_user_sources``/
  ``_build_entry_loaders``/``BuildContext``/``_dep_cache_*``/``clean_dist`` 等）

显式 ``import`` 标准库模块与运行时依赖（``subprocess``/``shutil``/``re``/``json``/
``TkinterBundler``/``download_embed`` 等）兼容测试 ``monkeypatch.setattr("fspack.builder.<attr>", ...)``
路径解析：pytest monkeypatch 解析 dotted path 时需要 facade 模块有这些属性，
patch 设置的是模块对象的属性，全局生效，对三个底层模块同样有效。
"""

from __future__ import annotations

# 标准库模块：兼容测试 monkeypatch.setattr("fspack.builder.subprocess.run", ...)
import json  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401
from pathlib import Path  # noqa: F401

# 公开 API 与私有辅助：re-export 保持 import 路径兼容
from fspack.config import DEFAULT_PY_VERSION

# 第三方依赖与运行时调用：兼容测试 monkeypatch.setattr("fspack.builder.<func>", ...)
from fspack.packaging.builtin import TkinterBundler  # noqa: F401
from fspack.packaging.loader import compile_loader  # noqa: F401
from fspack.packaging.pipeline import (
    _DEFAULT_ICON,  # noqa: F401
    _KEEP_NSI,  # noqa: F401
    BuildContext,  # noqa: F401
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
    _strip_version_specifier,  # noqa: F401
    build,
    clean_dist,
    default_icon_path,
    fspack_wheel_cache_dir,
    resolve_project_info,
    unpack_wheels,
)
from fspack.packaging.pyc import (
    _WIN7_COMPAT_DLL_NAME,  # noqa: F401
    _inject_win7_compat_dll,  # noqa: F401
    _needs_win7_compat_dll,  # noqa: F401
    _precompile_pyc,  # noqa: F401
    _pyc_stamp_key,  # noqa: F401
    _pyc_stamp_path,  # noqa: F401
    _strip_py_sources,  # noqa: F401
    _trim_stdlib,  # noqa: F401
)
from fspack.packaging.runtime import (  # noqa: F401
    download_embed,
    download_standalone,
    extract_embed,
    extract_standalone,
)
from fspack.packaging.sync import (  # noqa: F401
    _EXCLUDE,
    _dir_size,
    _merge_excludes,
    _site_packages_fingerprint,
    _sync_tree,
    copy_source,
)
from fspack.packaging.wheels import download_wheels
from fspack.platform import detect_platform  # noqa: F401

__all__ = [
    "DEFAULT_PY_VERSION",
    "build",
    "clean_dist",
    "copy_source",
    "default_icon_path",
    "download_wheels",
    "fspack_wheel_cache_dir",
    "resolve_project_info",
    "unpack_wheels",
]
