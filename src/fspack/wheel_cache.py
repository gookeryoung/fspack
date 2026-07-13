"""Wheel 缓存：fspack 自有缓存目录的 wheel 文件名解析与匹配工具。."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

__all__ = [
    "WheelInfo",
    "fspack_wheel_cache_dir",
    "normalize_name",
    "parse_wheel_filename",
    "wheel_matches",
]

_logger = logging.getLogger(__name__)

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


def wheel_matches(
    wheel: WheelInfo,
    packages: set[str],
    py_tag: str,
    platform_tags: Sequence[str],
) -> bool:
    """检查 wheel 是否匹配目标包名、Python 标签与平台标签。

    ``py_tag`` 形如 ``cp39``；兼容的通用标签 ``py3``/``py3{minor}`` 也算命中。
    平台标签取 wheel 与目标的交集，或 wheel 含 ``any``。
    """
    if normalize_name(wheel.name) not in packages:
        return False
    compatible = {py_tag, f"py{py_tag[2:]}", "py3"}
    if not (set(wheel.python_tags) & compatible):
        return False
    target = set(platform_tags)
    wheel_plats = set(wheel.platform_tags)
    return bool(wheel_plats & target) or "any" in wheel_plats


def fspack_wheel_cache_dir() -> Path:
    """返回 fspack wheel 缓存目录 ``~/.fspack/cache/wheels/``。."""
    return Path.home() / ".fspack" / "cache" / "wheels"
