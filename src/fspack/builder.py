"""构建流水线 facade：从 sync/pyc/pipeline 三个模块 re-export 公开 API.

本模块是 :mod:`fspack.builder` 的入口与 API 索引，无业务逻辑。原 builder.py
按职责拆分到三个模块：

- :mod:`fspack.packaging.sync`：源码同步（``copy_source``/``_sync_tree``/
  ``_dir_size``/``_site_packages_fingerprint``）
- :mod:`fspack.packaging.pyc`：字节码预编译（``_precompile_pyc``/``_trim_stdlib``/
  ``_inject_win7_compat_dll``/``_needs_win7_compat_dll``/``_strip_py_sources``）
- :mod:`fspack.packaging.pipeline`：阶段编排（``build``/``_dep_cache_*``/
  ``_slim_runtime``/``_site_packages_has_deps``/``clean_dist`` 等）

私有符号 re-export 仅为两类既有引用保留：测试 ``from fspack.builder import ...``
（test_builder/test_icon）与 ``nuitka.standalone`` 的 ``_inject_win7_compat_dll``；
monkeypatch 标准库调用请直接 patch ``subprocess.run`` 等标准库属性。
"""

from __future__ import annotations

from fspack.config import DEFAULT_PY_VERSION
from fspack.packaging.pipeline import (
    _DEFAULT_ICON,  # noqa: F401
    _dep_cache_load,  # noqa: F401
    _dep_cache_path,  # noqa: F401
    _dep_cache_save,  # noqa: F401
    _resolve_project_icon,  # noqa: F401
    _site_packages_has_deps,  # noqa: F401
    _slim_runtime,  # noqa: F401
    build,
    clean_dist,
    default_icon_path,
    fspack_wheel_cache_dir,
    resolve_project_info,
    unpack_wheels,
)
from fspack.packaging.pyc import (
    _inject_win7_compat_dll,  # noqa: F401
    _needs_win7_compat_dll,  # noqa: F401
    _precompile_pyc,  # noqa: F401
    _pyc_stamp_key,  # noqa: F401
    _pyc_stamp_path,  # noqa: F401
    _strip_elf_symbols,  # noqa: F401
    _strip_py_sources,  # noqa: F401
    _strip_tcl_tk_counted,  # noqa: F401
    _trim_standalone_runtime,  # noqa: F401
    _trim_stdlib,  # noqa: F401
)
from fspack.packaging.sync import (
    _dir_size,  # noqa: F401
    _site_packages_fingerprint,  # noqa: F401
    _sync_tree,  # noqa: F401
    copy_source,
)
from fspack.packaging.wheels import download_wheels

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
