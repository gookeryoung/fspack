"""loader 编译缓存键计算.

从 :mod:`fspack.packaging.loader.compile` 拆分而来，聚集「缓存」职责：

- :func:`loader_cache_dir`：缓存目录解析（转发 :mod:`fspack.config.cache`）
- :func:`_loader_cache_key`：源码 + 应用类型 + 平台 + icon + 版本信息组合哈希
- :func:`_version_info_hash` / :func:`_icon_hash`：资源元数据哈希
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fspack.config import AppType
from fspack.packaging.loader.resource import LoaderVersionInfo
from fspack.platform import Platform

__all__ = ["loader_cache_dir"]


def loader_cache_dir() -> Path:
    """返回 fspack loader 缓存目录（``FSPACK_CACHE_DIR`` 环境变量 > 默认 ``~/.fspack/cache/loaders``）."""
    from fspack.config.cache import loader_cache_dir as _cache_dir

    return _cache_dir()


def _loader_cache_key(
    source: str,
    app_type: AppType,
    platform: Platform,
    icon_hash: str = "",
    version_info_hash: str = "",
) -> str:
    """计算 loader 缓存键：sha256(source + app_type + platform + icon_hash + version_info_hash) 前 16 字符 hex。

    源码仅依赖 ``py_xy`` 与平台（入口路径运行时从 ``<exe_basename>.entry``
    或回退 ``.entry`` 读取），应用类型影响 ``-mwindows`` 编译选项，icon_hash
    区分不同 icon（空串表示无 icon），version_info_hash 区分不同版本信息元数据
    （CompanyName/ProductVersion 等变化时资源段变化需重编）。五者组合哈希作为
    缓存文件名，保证同配置命中、改配置失效。
    """
    h = hashlib.sha256()
    h.update(source.encode("utf-8"))
    h.update(app_type.value.encode("utf-8"))
    h.update(platform.value.encode("utf-8"))
    h.update(icon_hash.encode("utf-8"))
    h.update(version_info_hash.encode("utf-8"))
    return h.hexdigest()[:16]


def _version_info_hash(info: LoaderVersionInfo) -> str:
    """计算版本信息元数据的哈希（sha256 前 16 字符），用于 loader 缓存键.

    五字段（name/version/description/author/exe_filename）任一变化即视为不同配置，
    触发资源段重编。``exe_filename`` 纳入使多入口项目不同入口的 exe 资源段独立缓存
    （OriginalFilename 字段不同）。
    """
    h = hashlib.sha256()
    h.update(info.name.encode("utf-8"))
    h.update(info.version.encode("utf-8"))
    h.update(info.description.encode("utf-8"))
    h.update(info.author.encode("utf-8"))
    h.update(info.exe_filename.encode("utf-8"))
    return h.hexdigest()[:16]


def _icon_hash(icon: Path) -> str:
    """计算 icon 文件内容的 sha256 前 16 字符 hex，用于缓存键。"""
    h = hashlib.sha256()
    h.update(icon.read_bytes())
    return h.hexdigest()[:16]
