"""loader 编译缓存键与缓存目录解析."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fspack.config import AppType
from fspack.core.fsutil import sha256_file
from fspack.core.platform import Platform
from fspack.packaging.loader.resource import LoaderVersionInfo

__all__ = ["loader_cache_dir"]


def loader_cache_dir() -> Path:
    """返回 loader 缓存目录."""
    from fspack.config.cache import loader_cache_dir as _cache_dir

    return _cache_dir()


def _structured_hash(kind: str, fields: dict[str, object]) -> str:
    payload = json.dumps([kind, fields], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _effective_app_type(app_type: AppType, platform: Platform) -> AppType:
    if platform is Platform.WINDOWS and app_type in (AppType.GUI, AppType.WEB):
        return AppType.GUI
    return AppType.CLI


def _object_cache_key(source: str, platform: Platform, toolchain_key: str, flags: tuple[str, ...]) -> str:
    return _structured_hash(
        "loader-object-v1",
        {"source": source, "platform": platform.value, "toolchain": toolchain_key, "flags": flags},
    )


def _resource_cache_key(icon_hash: str, version_info: LoaderVersionInfo | None, toolchain_key: str) -> str:
    from fspack.packaging.loader import toolchain

    return _structured_hash(
        "loader-resource-v1",
        {
            "rc": toolchain.generate_resource_rc(version_info, has_icon=bool(icon_hash)),
            "manifest": toolchain.generate_app_manifest(
                version_info.name if version_info is not None else "app",
                version_info.version if version_info is not None else "1.0.0",
            ),
            "icon": icon_hash,
            "toolchain": toolchain_key,
        },
    )


def _loader_cache_key(  # noqa: PLR0913
    source: str,
    app_type: AppType,
    platform: Platform,
    icon_hash: str = "",
    version_info_hash: str = "",
    *,
    toolchain_key: str = "",
    compile_flags: tuple[str, ...] = (),
    link_flags: tuple[str, ...] = (),
    resource_key: str = "",
) -> str:
    return _structured_hash(
        "loader-executable-v2",
        {
            "source": source,
            "app_type": _effective_app_type(app_type, platform).value,
            "platform": platform.value,
            "icon": icon_hash,
            "version_info": version_info_hash,
            "toolchain": toolchain_key,
            "compile_flags": compile_flags,
            "link_flags": link_flags,
            "resource": resource_key,
        },
    )


def _version_info_hash(info: LoaderVersionInfo) -> str:
    return _structured_hash(
        "loader-version-info-v1",
        {
            "name": info.name,
            "version": info.version,
            "description": info.description,
            "author": info.author,
            "exe_filename": info.exe_filename,
        },
    )


def _icon_hash(icon: Path) -> str:
    """返回 icon 内容的 sha256 前 16 字符."""
    return sha256_file(icon)[:16]
