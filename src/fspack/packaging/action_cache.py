"""通用增量 action 缓存：输入指纹 → ``dist/build`` 下的 JSON 标记.

背景：图标转换、embed stdlib zip 精简、Win7 dist 扫描与 loader 校验原先各自
实现了一套「版本号 + 输入指纹 + marker 文件」缓存，原子写、损坏容忍、版本
失效这些细节重复四遍且容易漂移（例如标记文件曾写到 runtime 目录，混进产物）。
本模块把它们收敛为一套实现：

- **键**：调用方用 :func:`fingerprint` 从输入元数据（源文件 sha1、size/mtime、
  清单指纹、工具版本…）派生；键不符即未命中
- **值**：JSON 记录 ``{"version": n, "key": ..., "payload": {...}}``，
  ``payload`` 供 per-entry 结果类缓存（Win7 扫描）存放明细
- **位置**：``<dist>/build/action-<name>.json``——``build`` 目录不进入安装包、
  也不参与 dist 扫描，标记只会随构建产物一起被清理
- **失效**：版本号不符、键不符、JSON 损坏一律视为未命中（损坏文件顺手删除）；
  写入失败只告警，下次构建退化为重算
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any

__all__ = [
    "ACTION_CACHE_VERSION",
    "fingerprint",
    "load",
    "marker_path",
    "save",
    "source_digest",
]

_logger = logging.getLogger(__name__)

# 缓存记录格式版本：字段语义变化时 +1，让旧标记整体失效
ACTION_CACHE_VERSION = 1


def fingerprint(*parts: object) -> str:
    """把任意可 JSON 化的输入元数据压成稳定摘要（sha1 hex）.

    ``json.dumps(sort_keys=True)`` 保证 dict 字段顺序不影响结果；
    不可 JSON 化的对象（如 ``Path``）按 ``str`` 处理。
    """
    data = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def marker_path(cache_dir: Path, name: str) -> Path:
    """标记文件路径：``<cache_dir>/action-<name>.json``."""
    return cache_dir / f"action-{name}.json"


def source_digest(module: ModuleType) -> str:
    """模块源码内容摘要，用作缓存键里"规则可能已变"的自动失效杠杆.

    判定规则（API 黑名单、导入表改写表）与解析逻辑都写在模块源码里，靠人工
    递增版本号容易漏升；直接对源码字节取 sha1 可让任何规则/逻辑改动自动生效，
    代价是每次构建多读一次模块文件（数十 KB，微秒级）。模块无文件路径
    （frozen/zipimport 非常规安装）时退回空串，只损失自动失效能力，不误命中。
    """
    source = getattr(module, "__file__", None)
    if not source:
        return ""
    try:
        return hashlib.sha1(Path(source).read_bytes()).hexdigest()
    except OSError:
        return ""


def load(cache_dir: Path, name: str, *, version: int = ACTION_CACHE_VERSION) -> dict[str, Any] | None:
    """读取标记记录；缺失、损坏或版本不符返回 ``None``.

    损坏文件会被删除（best-effort），避免每次构建重复解析同一份坏数据。
    """
    path = marker_path(cache_dir, name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _discard(path)
        return None
    if not isinstance(raw, dict) or raw.get("version") != version or "key" not in raw:
        _discard(path)
        return None
    return raw


def save(cache_dir: Path, name: str, key: str, *, payload: dict[str, Any] | None = None) -> bool:
    """写入标记记录（先写临时文件再原子替换）；失败仅告警返回 ``False``."""
    path = marker_path(cache_dir, name)
    record: dict[str, Any] = {"version": ACTION_CACHE_VERSION, "key": key}
    if payload is not None:
        record["payload"] = payload
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        _logger.warning("写入 action 缓存 %s 失败（不影响构建）: %s", name, e)
        return False
    return True


def _discard(path: Path) -> None:
    """删除损坏/过期标记（失败忽略：下次仍会按未命中处理）."""
    with contextlib.suppress(OSError):
        path.unlink()
