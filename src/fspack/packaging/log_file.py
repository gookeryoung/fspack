"""构建日志持久化.

在 :func:`fspack.packaging.pipeline.build` 执行期间将日志写入文件，便于
CI 上传与问题排查。支持 text（默认，人类可读）与 json（结构化，便于解析）
两种格式。

公共 API：

- :func:`setup_log_file` — 创建文件 handler 附加到 root logger，返回 handler
- :func:`teardown_log_file` — 从 root logger 移除并关闭 handler
- :class:`LogFormat` — 日志格式枚举（TEXT/JSON）

典型用法::

    from fspack.packaging.log_file import LogFormat, setup_log_file, teardown_log_file

    handler = setup_log_file(Path("build.log"), LogFormat.TEXT)
    try:
        build(...)
    finally:
        teardown_log_file(handler)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from fspack._compat import override

__all__ = [
    "JsonFormatter",
    "LogFormat",
    "TextFormatter",
    "setup_log_file",
    "teardown_log_file",
]

_logger = logging.getLogger(__name__)

# 文本格式：时间戳 [级别] logger 名: 消息（含异常栈）
_TEXT_FMT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
# JSON 标准属性集合（用于过滤 extra 字段）
_STANDARD_ATTRS = frozenset(
    set(logging.LogRecord(name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None).__dict__.keys())
)


class LogFormat(Enum):
    """日志文件格式.

    - ``TEXT``：人类可读纯文本，含时间戳/级别/logger 名/消息/异常栈
    - ``JSON``：结构化 JSON，每行一条记录，便于 ELK/Loki 采集与检索
    """

    TEXT = "text"
    JSON = "json"

    @classmethod
    def parse(cls, value: str | None) -> LogFormat:
        """从字符串解析格式，``None`` 或空字符串返回默认 ``TEXT``.

        :param value: 字符串值（``"text"``/``"json"``，大小写不敏感）
        :raises ValueError: 未知格式名
        """
        if not value:
            return cls.TEXT
        try:
            return cls(value.lower())
        except ValueError as exc:
            raise ValueError(f"未知日志格式: {value!r}，可选: text/json") from exc


class TextFormatter(logging.Formatter):
    """纯文本日志 Formatter.

    格式：``2026-07-28 12:34:56,789 [INFO    ] fspack.packaging.pipeline: 消息``
    异常栈以多行形式附加在消息后。
    """

    def __init__(self) -> None:
        """初始化文本 Formatter，使用固定格式与 ISO 时间戳."""
        super().__init__(fmt=_TEXT_FMT, datefmt="%Y-%m-%d %H:%M:%S")


class JsonFormatter(logging.Formatter):
    """JSON 结构化日志 Formatter.

    每行一条 JSON 记录，字段：``timestamp``/``level``/``logger``/``message``/
    ``module``/``function``/``line``，异常时附加 ``exception`` 字段，
    ``extra=`` 传入的自定义字段原样保留。

    不可 JSON 序列化的对象用 :meth:`_safe_serialize` 转为 ``repr`` 字符串。
    """

    @override
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: ARG002
        """覆盖时间格式化：ISO 8601 + 毫秒（``%f`` 在 Windows 不可用，手动拼接）.

        ``datefmt`` 参数为父类 :meth:`logging.Formatter.formatTime` 签名兼容保留，
        本实现忽略用户传入的 ``datefmt``，固定输出 ``YYYY-MM-DDTHH:MM:SS.mmm``。
        """
        ct = self.converter(record.created)
        base = time.strftime("%Y-%m-%dT%H:%M:%S", ct)
        return f"{base}.{int(record.msecs):03d}"

    @override
    def format(self, record: logging.LogRecord) -> str:
        """将 LogRecord 序列化为 JSON 字符串."""
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        # extra 字段（用户通过 extra= 传入的自定义字段）
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in log_entry:
                log_entry[key] = self._safe_serialize(value)
        return json.dumps(log_entry, ensure_ascii=False)

    @staticmethod
    def _safe_serialize(value: Any) -> Any:
        """安全序列化：不可 JSON 序列化的对象转 repr."""
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return repr(value)


@dataclass(frozen=True)
class LogFileHandler:
    """日志文件 handler 包装，记录 handler 与文件路径便于清理."""

    handler: logging.FileHandler
    path: Path
    previous_root_level: int


def setup_log_file(path: Path, fmt: LogFormat = LogFormat.TEXT) -> LogFileHandler:
    """创建日志文件 handler 并附加到 root logger.

    自动创建父目录。文件以 UTF-8 编码写入（追加模式，不轮转；构建日志通常
    单次构建单文件，无需轮转）。

    :param path: 日志文件路径
    :param fmt: 日志格式，默认 :attr:`LogFormat.TEXT`
    :return: :class:`LogFileHandler` 包装，传给 :func:`teardown_log_file` 清理
    """
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # FileHandler 默认 mode="a" 追加，encoding=None 平台默认（Windows GBK）
    # 显式指定 utf-8 避免中文日志乱码
    handler = logging.FileHandler(filename=path, mode="a", encoding="utf-8")
    handler.setLevel(logging.DEBUG)  # 文件记录全部级别，由 root logger 级别过滤
    handler.setFormatter(JsonFormatter() if fmt is LogFormat.JSON else TextFormatter())
    root = logging.getLogger()
    # root 级别高于 INFO（默认 WARNING）时 INFO 级构建日志在 logger 层就被丢弃，
    # 文件恒空——作为 API 调用 build(log_file=...)（未经 CLI setup_logging）时
    # 即如此。降为 INFO 保证日志文件可用，teardown 恢复原级别（CLI 路径已是
    # INFO，此操作无变化）。
    previous_level = root.level
    if previous_level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    _logger.info("日志文件已启用: %s（格式: %s）", path, fmt.value)
    return LogFileHandler(handler=handler, path=path, previous_root_level=previous_level)


def teardown_log_file(wrapper: LogFileHandler | None) -> None:
    """从 root logger 移除并关闭日志文件 handler.

    ``wrapper`` 为 ``None`` 时无操作，便于 ``try/finally`` 中无需 None 检查。
    恢复 :func:`setup_log_file` 降级前的 root logger 级别。

    :param wrapper: :func:`setup_log_file` 返回的包装，``None`` 时无操作
    """
    if wrapper is None:
        return
    root = logging.getLogger()
    root.removeHandler(wrapper.handler)
    wrapper.handler.close()
    root.setLevel(wrapper.previous_root_level)
