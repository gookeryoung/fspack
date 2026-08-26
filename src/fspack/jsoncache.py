"""JSON 缓存读取骨架：读取 → 解析 → 结构校验 → 损坏处理.

统一此前散落多处的"读取 JSON → 解析 → 根 dict 校验 → 损坏时删除/忽略"模式。
当前消费方 :func:`fspack.packaging.wheels.cache._load_deps_cache` 复用骨架后，
仅保留自身的 ``wheels`` 字段校验与 wheel 文件存在性检查外壳。

只抽取共同骨架 :func:`load_json_dict`，各调用点保留自身的类型转换/回写外壳，
避免过度抽象。日志文案被测试锁死的调用点（如 ``nuitka.compile._load_hash_index``
断言 "hash 索引损坏"、``doctor_envs._scan_cache_health`` 需区分损坏计数语义）
不复用本骨架，保持各自实现。

``delete_on_corrupt`` 区分两类语义：

- ``True``（删除型）：内容损坏时删除文件，避免下次重复触发损坏告警
  （wheels 依赖缓存）。
- ``False``（忽略型）：内容损坏时仅返回 ``None``，不删除
  （文件可能仍有诊断价值）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

__all__ = ["load_json_dict"]

_logger = logging.getLogger(__name__)


def load_json_dict(
    path: Path,
    *,
    delete_on_corrupt: bool = True,
    logger: logging.Logger | None = None,
) -> dict[str, object] | None:
    """读取 JSON 文件并校验根对象为 dict，返回解析后的字典或 ``None``.

    行为分层：

    - 文件不存在（``FileNotFoundError``）：返回 ``None``（无缓存，不告警）。
    - 其他 ``OSError``（权限/磁盘 I/O）：告警并返回 ``None``，**不删除**
      （可能是瞬时问题，删除反而误伤可恢复的缓存）。
    - 内容损坏（非法 JSON / 根对象非 dict / UTF-8 解码失败）：告警并返回
      ``None``；``delete_on_corrupt`` 为 ``True`` 时 best-effort 删除文件
      （删除失败仅告警不抛）。

    :param path: JSON 文件路径
    :param delete_on_corrupt: 内容损坏时是否删除文件，默认 ``True``
    :param logger: 记录日志的日志器，``None`` 时用本模块日志器
    :return: 解析后的字典（根对象为 dict）；文件不存在/读取失败/内容损坏时返回 ``None``
    """
    log = logger or _logger
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("读取 JSON 缓存失败，视为空: %s: %s", path, e)
        return None
    except ValueError as e:
        # UnicodeDecodeError（ValueError 子类）：文件含非法 UTF-8 字节，视为内容损坏。
        # read_text 的解码失败不属于 OSError，需单独捕获与"内容损坏"同等处理。
        log.warning("JSON 缓存损坏: %s: %s", path, e)
        _delete_if_corrupt(path, delete_on_corrupt=delete_on_corrupt, logger=log)
        return None

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"根对象不是 dict: {type(data).__name__}")
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("JSON 缓存损坏: %s: %s", path, e)
        _delete_if_corrupt(path, delete_on_corrupt=delete_on_corrupt, logger=log)
        return None

    return data


def _delete_if_corrupt(path: Path, *, delete_on_corrupt: bool, logger: logging.Logger) -> None:
    """内容损坏时按 ``delete_on_corrupt`` best-effort 删除文件（删除失败仅告警）."""
    if not delete_on_corrupt:
        return
    try:
        path.unlink()
    except OSError as unlink_err:
        logger.warning("删除损坏缓存文件失败: %s: %s", path, unlink_err)
