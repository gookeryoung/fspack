"""Wheel 依赖解析缓存：避免重复调用 pip 解析依赖图。

缓存文件 ``.deps-<key>.json`` 记录上次 pip 解析出的 wheel 文件名列表。
命中后逐个校验 wheel 文件仍存在于 cache_dir，任一缺失则视为未命中
（避免 wheel 被手动删除后仍跳过 pip）。

缓存键纳入依赖列表、Python 版本、平台标签与私有包源，确保跨项目/跨版本/
跨平台/跨私有源不会误命中。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Sequence

__all__ = [
    "_deps_cache_key",
    "_load_deps_cache",
    "_save_deps_cache",
]

_logger = logging.getLogger(__name__)


def _deps_cache_key(
    packages: tuple[str, ...] | list[str],
    py_version: str,
    platform_tags: Sequence[str],
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
) -> str:
    """根据依赖列表、Python 版本、平台标签与私有包源计算缓存键。

    不同组合产生不同键，确保跨项目/跨版本/跨平台/跨私有源不会误命中。
    私有包源纳入键：切换 ``--extra-index-url``/``--find-links`` 后强制重新解析，
    避免旧缓存返回来自其他源的 wheel。
    返回 16 位 hex 摘要，用于 ``.deps-<key>.json`` 文件名。
    """
    data = f"{sorted(packages)}|{py_version}|{list(platform_tags)}|{list(extra_index_urls)}|{list(find_links)}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _load_deps_cache(cache_dir: Path, key: str) -> list[Path] | None:
    """读取依赖解析缓存，返回 wheel 路径列表；未命中或文件丢失返回 None。

    缓存文件 ``.deps-<key>.json`` 记录上次 pip 解析出的 wheel 文件名列表。
    命中后逐个校验 wheel 文件仍存在于 cache_dir，任一缺失则视为未命中
    （避免 wheel 被手动删除后仍跳过 pip）。

    缓存文件内容损坏（JSON 解析失败、结构不合法、编码错误）时删除文件，
    避免下次构建重复触发损坏告警；best-effort 删除失败仅告警不抛异常。
    ``OSError``（读写权限/磁盘 I/O）不删除：可能是瞬时文件系统问题，
    删除反而误伤可恢复的缓存。iter-128 引入。
    """
    cache_file = cache_dir / f".deps-{key}.json"
    if not cache_file.is_file():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"缓存根对象不是 dict: {type(data).__name__}")
        names = data.get("wheels", [])
        if not isinstance(names, list):
            raise ValueError(f"wheels 字段不是 list: {type(names).__name__}")
        wheels = [cache_dir / name for name in names]
        if wheels and all(w.is_file() for w in wheels):
            return wheels
    except (json.JSONDecodeError, ValueError) as e:
        # 内容损坏（非法 JSON、结构不合法、值类型错误、UTF-8 解码失败）：
        # 删除文件避免下次构建重复告警。UnicodeDecodeError 是 ValueError 子类。
        _logger.warning("依赖解析缓存损坏，删除并重新解析: %s: %s", cache_file, e)
        try:
            cache_file.unlink()
        except OSError as unlink_err:  # pragma: no cover - 删除失败极罕见
            _logger.warning("删除损坏缓存文件失败: %s: %s", cache_file, unlink_err)
    except OSError as e:
        # 文件系统层错误（权限/磁盘 I/O）：不删除，可能是瞬时问题，下次构建重试。
        _logger.warning("读取依赖解析缓存失败，将重新解析: %s: %s", cache_file, e)
    return None


def _save_deps_cache(cache_dir: Path, key: str, wheels: Sequence[Path]) -> None:
    """写入依赖解析缓存，记录 wheel 文件名列表。

    best-effort：写入失败仅 warning 不影响构建（缓存只是优化，缺失会回退到 pip）。
    """
    cache_file = cache_dir / f".deps-{key}.json"
    try:
        cache_file.write_text(
            json.dumps({"wheels": [w.name for w in wheels]}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        _logger.warning("写入依赖解析缓存失败: %s", e)
