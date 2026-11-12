"""Wheel 缓存：依赖解析缓存（``.deps-<key>.json``）与缓存目录维护.

缓存文件 ``.deps-<key>.json`` 记录上次 pip 解析出的 wheel 文件名列表。
命中后逐个校验 wheel 文件仍存在于 cache_dir，任一缺失则视为未命中
（避免 wheel 被手动删除后仍跳过 pip）。

缓存键纳入依赖列表、Python 版本、平台标签与私有包源，确保跨项目/跨版本/
跨平台/跨私有源不会误命中。

另含缓存目录维护：下载失败时清理本次部分下载的 wheel
（:func:`_cleanup_partial_wheels`），以及下载阶段的缓存命中统计回写
（:func:`_record_wheel_stage`）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.fsutil import atomic_write_text
from fspack.jsoncache import load_json_dict

if TYPE_CHECKING:
    from fspack.metrics import StageRecorder

__all__ = [
    "_cleanup_partial_wheels",
    "_deps_cache_key",
    "_load_deps_cache",
    "_record_wheel_stage",
    "_save_deps_cache",
]

_logger = logging.getLogger(__name__)


def _deps_cache_key(  # noqa: PLR0913 - 缓存键须覆盖全部影响解析结果的包源与校验模式
    packages: tuple[str, ...] | list[str],
    py_version: str,
    platform_tags: Sequence[str],
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
    *,
    pypi_index: str = "",
    require_hashes: bool = False,
) -> str:
    """根据依赖列表、Python 版本、平台标签与全部包源计算缓存键。

    不同组合产生不同键，确保跨项目/跨版本/跨平台/跨私有源不会误命中。
    私有包源纳入键：切换 ``--extra-index-url``/``--find-links`` 后强制重新解析，
    避免旧缓存返回来自其他源的 wheel。

    ``pypi_index``（主索引）同样纳入键：镜像切换（华为云 → 清华源）后各索引
    各存自己的解析结果，切回旧索引仍命中自己的缓存。此前主索引缺失会让换镜像
    直接复用旧解析列表，把上一个索引的版本选择带进新索引。

    ``require_hashes``（强制哈希校验模式）纳入键，并与调用方的"该模式不读写
    解析缓存"策略配套：解析缓存只记录 wheel 文件名，无法证明 wheel 通过哈希校验。

    返回 16 位 hex 摘要，用于 ``.deps-<key>.json`` 文件名。
    """
    data = (
        f"{sorted(packages)}|{py_version}|{list(platform_tags)}|{list(extra_index_urls)}"
        f"|{list(find_links)}|{pypi_index}|{require_hashes}"
    )
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

    读取 → 解析 → 根 dict 校验 → 损坏删除的公共骨架委托
    :func:`fspack.jsoncache.load_json_dict`；``wheels`` 字段类型校验与
    wheel 文件存在性校验为本函数专属外壳，故保留在此。
    """
    cache_file = cache_dir / f".deps-{key}.json"
    data = load_json_dict(cache_file, delete_on_corrupt=True, logger=_logger)
    if data is None:
        return None
    names = data.get("wheels", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        # wheels 字段类型错误（非 list，或元素含非 str 如 int）：视为损坏，删除文件
        # 避免下次重复解析时 ``cache_dir / name`` 触发未捕获 TypeError
        _logger.warning("依赖解析缓存 wheels 字段非法（非 list 或元素非 str），删除并重新解析: %s", cache_file)
        try:
            cache_file.unlink()
        except OSError as unlink_err:  # pragma: no cover - 删除失败极罕见
            _logger.warning("删除损坏缓存文件失败: %s: %s", cache_file, unlink_err)
        return None
    wheels = [cache_dir / name for name in names]
    if wheels and all(w.is_file() for w in wheels):
        return wheels
    return None


def _save_deps_cache(cache_dir: Path, key: str, wheels: Sequence[Path]) -> None:
    """写入依赖解析缓存，记录 wheel 文件名列表。

    best-effort：写入失败仅 warning 不影响构建（缓存只是优化，缺失会回退到 pip）。
    """
    cache_file = cache_dir / f".deps-{key}.json"
    try:
        atomic_write_text(
            cache_file,
            json.dumps({"wheels": [w.name for w in wheels]}, ensure_ascii=False),
        )
    except OSError as e:
        _logger.warning("写入依赖解析缓存失败: %s", e)


def _cleanup_partial_wheels(cache_dir: Path, before: set[str]) -> None:
    """清理本次下载产生的部分 wheel 文件.

    pip download 失败时可能已下载部分 wheel 才中断，残留的半成品 wheel 会被
    下次构建的 ``--no-index`` 离线解析错误命中（认为依赖已就绪但实际不完整），
    导致依赖缺失。删除本次新增的 wheel（``cache_dir`` 中存在但不在 ``before``
    集合中的），保留下载前已存在的 wheel（其他项目的依赖缓存）。

    Args:
        cache_dir: wheel 缓存目录.
        before: 下载前 ``cache_dir`` 中已有的 wheel 文件名集合.
    """
    partial = [f for f in cache_dir.glob("*.whl") if f.name not in before]
    if not partial:
        return
    for whl in partial:
        try:
            whl.unlink()
        except OSError as e:
            _logger.warning("清理部分下载的 wheel 失败 %s: %s", whl, e)
    _logger.warning("pip download 失败，已清理 %d 个部分下载的 wheel", len(partial))


def _record_wheel_stage(stage: StageRecorder, wheels: list[Path], before: set[str]) -> None:
    """回写 wheel 下载阶段统计到 stage：新增字节数、缓存命中数、总数."""
    new_wheels = [w for w in wheels if w.name not in before]
    existing_wheels = [w for w in wheels if w.name in before]
    if new_wheels:
        stage.add_bytes(sum(w.stat().st_size for w in new_wheels))
    if existing_wheels:
        stage.hit_cache(len(existing_wheels))
    stage.processed(len(wheels))
    cache_status = "缓存命中" if not new_wheels else f"新增 {len(new_wheels)}"
    stage.set_detail(f"{len(wheels)} wheels, {cache_status}")
