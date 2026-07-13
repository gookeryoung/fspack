"""Wheel 缓存：fspack 自有缓存目录的 wheel 文件名解析工具。."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "WheelInfo",
    "fspack_wheel_cache_dir",
    "normalize_name",
    "parse_wheel_filename",
]

# PEP 427 wheel 文件名正则：name-version(-build)?-py-abi-plat.whl
_WHEEL_RE = re.compile(
    r"^(?P<name>.+?)-(?P<ver>.+?)(-(?P<build>\d[^-]*?))?-"
    r"(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WheelInfo:
    """解析后的 wheel 元信息。."""

    name: str
    version: str
    python_tags: tuple[str, ...]
    abi_tag: str
    platform_tags: tuple[str, ...]


def normalize_name(name: str) -> str:
    """PEP 503 名称归一化：小写，连续的 ``-_.`` 合并为 ``-``。."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_wheel_filename(filename: str) -> WheelInfo | None:
    """解析 wheel 文件名为 WheelInfo，无法解析返回 None。."""
    m = _WHEEL_RE.match(filename)
    if m is None:
        return None
    return WheelInfo(
        name=m.group("name"),
        version=m.group("ver"),
        python_tags=tuple(m.group("py").split(".")),
        abi_tag=m.group("abi"),
        platform_tags=tuple(m.group("plat").split(".")),
    )


def fspack_wheel_cache_dir() -> Path:
    """返回 fspack wheel 缓存目录 ``~/.fspack/cache/wheels/``。."""
    return Path.home() / ".fspack" / "cache" / "wheels"
