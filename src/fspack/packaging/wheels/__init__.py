"""fspack wheel 下载 facade：从 downloader/cache/markers 三个模块 re-export 公开 API.

本模块是 :mod:`fspack.packaging.wheels` 的入口与 API 索引，无业务逻辑。原
``wheels.py``（709 行）按职责拆分到三个模块：

- :mod:`fspack.packaging.wheels.downloader`：pip/uv 调用 + sdist 回退 + 流式输出 +
  ``download_wheels`` 入口 + wheel 文件名解析
- :mod:`fspack.packaging.wheels.cache`：依赖解析缓存（``.deps-<key>.json``）
- :mod:`fspack.packaging.wheels.markers`：``python_version`` 环境标记预过滤

显式 ``import`` 标准库模块（``os``/``re``/``shutil``/``subprocess``/``sys``）
是为了兼容测试中的 ``monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", ...)``
等 patch 路径——patch 设置的是模块对象的属性，因标准库模块为单例，全局生效，
对 :mod:`fspack.packaging.wheels.downloader` 内的调用同样有效。
"""

from __future__ import annotations

# 显式导入标准库模块：兼容测试中 ``fspack.packaging.wheels.<module>.<attr>`` 的 patch 路径。
# 这些模块为单例，patch 设置属性后对 downloader/cache/markers 同样生效。
import os  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
from pathlib import Path  # noqa: F401

# re-export 公开 API 与私有辅助：保持 ``from fspack.packaging.wheels import X`` 路径兼容
from fspack.packaging.wheels.cache import (
    _deps_cache_key,  # noqa: F401
    _load_deps_cache,  # noqa: F401
    _save_deps_cache,  # noqa: F401
)
from fspack.packaging.wheels.downloader import (
    _MISSING_PKG_RE,  # noqa: F401
    _PIP_PYTHON_NAMES,  # noqa: F401
    _PIP_WHEEL_LINE_RE,  # noqa: F401
    _UV_RESOLVED_LINE_RE,  # noqa: F401
    _build_pip_download_args,  # noqa: F401
    _build_sdist_wheels,  # noqa: F401
    _download_one_resolved,  # noqa: F401
    _download_online,  # noqa: F401
    _download_resolved_parallel,  # noqa: F401
    _find_pip_python,  # noqa: F401
    _find_uv,  # noqa: F401
    _handle_sdist_fallback,  # noqa: F401
    _merge_parallel_results,  # noqa: F401
    _parse_missing_packages,  # noqa: F401
    _parse_pip_download_wheels,  # noqa: F401
    _parse_wheel_names,  # noqa: F401
    _prefilter_by_python_version,  # noqa: F401
    _record_wheel_stage,  # noqa: F401
    _resolve_with_uv,  # noqa: F401
    _run_pip,  # noqa: F401
    _run_pip_download,  # noqa: F401
    _stream_subprocess,  # noqa: F401
    download_wheels,
)
from fspack.packaging.wheels.markers import (
    _MARKER_PY_VER_RE,  # noqa: F401
    _eval_python_version_marker,  # noqa: F401
    _eval_single_marker,  # noqa: F401
    _filter_by_python_version,  # noqa: F401
)

__all__ = ["download_wheels"]
