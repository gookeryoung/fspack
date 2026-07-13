"""Wheel 缓存复用：从 uv/pip/fspack 缓存搜索并复用已下载的 wheel。."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "WheelInfo",
    "fspack_wheel_cache_dir",
    "get_pip_cache_dir",
    "get_uv_cache_dir",
    "harvest_external_caches",
    "normalize_name",
    "parse_wheel_filename",
    "save_to_cache",
    "search_cache_dir",
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


def get_uv_cache_dir() -> Path | None:
    """返回 uv 缓存目录，未安装 uv 或失败返回 None。."""
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        result = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True, timeout=10, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    path = Path(result.stdout.strip())
    return path if path.is_dir() else None


def get_pip_cache_dir(python: str) -> Path | None:
    """返回 pip 缓存目录，失败返回 None。."""
    try:
        result = subprocess.run(
            [python, "-m", "pip", "cache", "dir"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    path = Path(result.stdout.strip())
    return path if path.is_dir() else None


def search_cache_dir(
    cache_dir: Path,
    packages: set[str],
    py_tag: str,
    platform_tags: Sequence[str],
) -> list[Path]:
    """在缓存目录递归搜索匹配的 wheel 文件，返回路径列表。."""
    result: list[Path] = []
    for whl in cache_dir.rglob("*.whl"):
        info = parse_wheel_filename(whl.name)
        if info is not None and wheel_matches(info, packages, py_tag, platform_tags):
            result.append(whl)
    return result


def harvest_external_caches(
    packages: set[str],
    py_version: str,
    platform_tags: Sequence[str],
    dest: Path,
) -> int:
    """从 uv/pip 缓存搜索匹配 wheel 并复制到 dest，返回复制数量。

    按序搜索 uv cache → pip cache（用 ``_find_pip_python`` 找到的解释器）。
    每个缓存目录 best-effort：不可用或无匹配则跳过。
    """
    major, minor = py_version.split(".")[:2]
    py_tag = f"cp{major}{minor}"
    dest.mkdir(parents=True, exist_ok=True)
    existing = {f.name for f in dest.glob("*.whl")}
    count = 0

    for cache_dir in _iter_external_cache_dirs():
        if cache_dir is None:
            continue
        _logger.debug("搜索外部缓存: %s", cache_dir)
        for whl in search_cache_dir(cache_dir, packages, py_tag, platform_tags):
            if whl.name in existing:
                continue
            shutil.copy2(whl, dest / whl.name)
            existing.add(whl.name)
            count += 1
            _logger.info("收割 wheel: %s", whl.name)
    return count


def _iter_external_cache_dirs() -> list[Path | None]:
    """返回 uv 与 pip 缓存目录列表（可能含 None）。."""
    dirs: list[Path | None] = [get_uv_cache_dir()]
    # 延迟导入避免循环依赖
    from fspack.builder import _find_pip_python
    from fspack.exceptions import DependencyError

    try:
        py = _find_pip_python()
    except (DependencyError, OSError):
        py = None
    if py is not None:
        dirs.append(get_pip_cache_dir(py))
    return dirs


def save_to_cache(wheels: Iterable[Path], cache: Path) -> int:
    """将 wheel 复制到缓存目录（已存在则跳过），返回新增数量。."""
    cache.mkdir(parents=True, exist_ok=True)
    existing = {f.name for f in cache.glob("*.whl")}
    count = 0
    for whl in wheels:
        if whl.name in existing:
            continue
        shutil.copy2(whl, cache / whl.name)
        existing.add(whl.name)
        count += 1
    return count
