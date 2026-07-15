---
name: "python-logging"
description: "Python 日志技能：标准库 logging 全链路指南，涵盖模块级 Logger 约定、dictConfig 配置、文件轮转、自定义 Formatter、结构化 JSON 日志、分层策略、GUI/CLI 集成、性能与脱敏。当需要记录运行日志、配置日志输出、轮转日志文件、结构化采集、为 GUI/CLI 接入日志面板或调整日志级别时调用。"
---

# Python 日志

自包含的日志指南：标准库 `logging` 全链路实践，涵盖模块级 Logger 约定、`dictConfig` 配置、文件轮转、自定义 Formatter、结构化 JSON 输出、分层策略、GUI/CLI 集成、性能与脱敏。禁止 `print` 残留（rule-11 强制），日志用 `%` 延迟格式化（不用 f-string），凭证/密码不进日志。所有示例遵循 `rule-11-python-standards.md`（`from __future__ import annotations`、中文 docstring、类型注解）。

## 何时调用

- 需要为模块/库添加运行日志（替代 `print` 调试残留）
- 需要配置日志输出（控制台 + 文件 + 多 handler）
- 需要日志文件轮转（按大小/按时间）
- 需要结构化日志（JSON 输出，便于 ELK/Loki 采集）
- 需要为第三方库降级日志、动态调整级别
- 需要为 PySide GUI 接入日志面板或 CLI 接入 `--verbose`/`--quiet`
- 需要日志脱敏（密码、令牌、个人信息）

## 模块级 Logger 约定

每模块顶部 `logger = logging.getLogger(__name__)`；禁止 `print` 残留；禁用 `logging.basicConfig` 在库代码中（仅入口脚本可用）。

```python
"""fspack.core 模块."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def process_item(item_id: str) -> None:
    """处理单个条目，记录关键步骤."""
    logger.debug("开始处理条目: %s", item_id)
    try:
        _do_work(item_id)
    except ValueError as exc:
        logger.error("处理条目失败: %s", item_id, exc_info=True)  # 含异常栈
        raise
    logger.info("条目处理完成: %s", item_id)
```

级别策略：DEBUG（详细诊断，仅开发时）、INFO（关键业务节点，默认生产级别）、WARNING（可恢复异常/降级/即将超限）、ERROR（操作失败但程序继续）、CRITICAL（系统不可用，需立即介入）。

要点：
- 模块顶部 `logger = logging.getLogger(__name__)`，`__name__` 自动形成层级（`fspack.core.sub`）。
- 库代码**禁止** `basicConfig`（污染调用方配置）；禁用 `print` 残留，用 `logger.debug` 替代。
- 异常日志用 `exc_info=True` 或 `logger.exception(...)`（等价 `ERROR + exc_info`）。
- 消息文本用中文短语，参数用 `%s` 占位（延迟格式化，见性能章节）。

## dictConfig 配置

`logging.config.dictConfig` 优于 `basicConfig`/手动 `addHandler`：声明式、可序列化、易切换开发/生产配置。

```python
"""fspack.logging_config 模块."""

from __future__ import annotations

import logging.config
from pathlib import Path
from typing import Any

_SIMPLE_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_THIRD_PARTY = {"urllib3": {"level": "WARNING"}, "asyncio": {"level": "WARNING"}}

# 开发配置：控制台 + DEBUG + 简单格式
DEV_CONFIG: dict[str, Any] = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": _SIMPLE_FMT},
        "verbose": {"format": "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d %(funcName)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "level": "DEBUG",
                     "formatter": "simple", "stream": "ext://sys.stderr"},
    },
    "loggers": _THIRD_PARTY,
    "root": {"level": "DEBUG", "handlers": ["console"]},
}

# 生产配置：控制台 + 文件 + INFO + JSON（文件用 RotatingFileHandler 轮转）
PROD_CONFIG: dict[str, Any] = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "fspack.logging_config.JsonFormatter"},
        "simple": {"format": _SIMPLE_FMT},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "level": "INFO",
                     "formatter": "simple", "stream": "ext://sys.stderr"},
        "file": {"class": "logging.handlers.RotatingFileHandler", "level": "INFO",
                  "formatter": "json", "filename": "logs/fspack.log",
                  "maxBytes": 10485760, "backupCount": 5, "encoding": "utf-8"},  # 10 MB
    },
    "loggers": _THIRD_PARTY,
    "root": {"level": "INFO", "handlers": ["console", "file"]},
}


def setup_logging(env: str = "dev", log_dir: Path | None = None) -> None:
    """初始化日志系统，按环境加载配置（env: dev/prod; log_dir: prod 模式创建）."""
    config = PROD_CONFIG if env == "prod" else DEV_CONFIG
    if env == "prod" and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        config["handlers"]["file"]["filename"] = str(log_dir / "fspack.log")
    logging.config.dictConfig(config)
```

要点：
- `disable_existing_loggers: False`：保留第三方库已注册的 logger，避免静默丢失日志。
- `ext://sys.stderr`：用 `ext://` 前缀引用 `sys` 模块属性；自定义 Formatter 用 `"()": "包.模块:类名"`。
- 开发配置：控制台 + DEBUG + 简单格式；生产配置：控制台 + 文件 + INFO + JSON。
- 入口 `main()` 调用 `setup_logging(env)` 一次，禁止在库代码中重复调用。

## 文件轮转

长运行进程须轮转日志，避免单文件无限增长。

```python
from __future__ import annotations

import logging.handlers
from pathlib import Path


def make_size_handler(path: Path, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5) -> logging.handlers.RotatingFileHandler:
    """按大小轮转：达到 max_bytes 后切分，保留 backup_count 个备份.

    命名：fspack.log / .1 / .2 / ... / .5（最旧，超出删除）
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return logging.handlers.RotatingFileHandler(
        filename=path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )


def make_time_handler(path: Path, when: str = "midnight", interval: int = 1, backup_count: int = 14) -> logging.handlers.TimedRotatingFileHandler:
    """按时间轮转：when（'S'秒/'M'分/'H'时/'D'天/'midnight'午夜/'W0'周一）+ interval 切分."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return logging.handlers.TimedRotatingFileHandler(
        filename=path, when=when, interval=interval, backupCount=backup_count,
        encoding="utf-8", utc=False,  # 本地时区；跨时区服务设 True
    )
```

要点：
- `RotatingFileHandler` 按磁盘配额管理；`TimedRotatingFileHandler` 按日志保留期管理（如保留 14 天）。
- `encoding="utf-8"` 必填（Windows 默认 GBK 导致中文乱码）；跨时区服务设 `utc=True`。
- `backupCount` 按业务保留期设定：开发期 3-5，生产期 14-30。
- 多进程写同一日志文件**不安全**（rotating handler 非进程安全），多进程用 `ConcurrentLogHandler` 或中央采集。

## 自定义 Formatter

带时间戳、模块名、行号、函数名的格式；中文日志消息直接写入 format 字符串。

```python
from __future__ import annotations

import logging
import time

VERBOSE_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s:%(lineno)d %(funcName)s(): %(message)s"
SIMPLE_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"


class LocalTimeFormatter(logging.Formatter):
    """自定义 Formatter：覆盖 formatTime 用本地时区与毫秒."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """覆盖时间格式化（默认 time.localtime；跨时区用 time.gmtime 替换 converter）."""
        ct = self.converter(record.created)
        if datefmt:
            return time.strftime(datefmt, ct)
        return f"{time.strftime('%Y-%m-%d %H:%M:%S', ct)},{int(record.msecs):03d}"
# 使用：LocalTimeFormatter(fmt=VERBOSE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
```

要点：
- `%(levelname)-8s` 左对齐占 8 字符，保证列对齐。
- `%(funcName)s`/`%(lineno)d` 仅在 DEBUG 配置中启用，生产环境省略以减少噪声。
- 默认 `asctime` 用 `time.localtime`（本地时区）；跨时区服务用 `time.gmtime` 替换 `converter`。
- 中文消息直接写入 `logger.info("用户 %s 登录成功", username)`，Formatter 不需要特殊处理。

## 结构化日志

JSON 输出便于 ELK/Loki/Datadog 采集与检索；`extra` 字段附加业务上下文。

```python
from __future__ import annotations

import json
import logging
from typing import Any

# LogRecord 标准属性集合（用于过滤出 extra 字段）
_STANDARD_ATTRS = set(logging.LogRecord(
    name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None,
).__dict__.keys())


class JsonFormatter(logging.Formatter):
    """JSON 格式 Formatter，输出结构化日志便于采集系统检索."""

    def format(self, record: logging.LogRecord) -> str:
        """将 LogRecord 序列化为 JSON 字符串."""
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f"),
            "level": record.levelname, "logger": record.name,
            "message": record.getMessage(), "module": record.module,
            "function": record.funcName, "line": record.lineno,
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


# 使用 extra 附加业务上下文（输出 JSON 含 request_id/event_type/amount 字段）
# logger.info("转账完成: %s -> %s %.2f", from_id, to_id, amount,
#             extra={"request_id": "req-abc-123", "event_type": "transfer", "amount": amount})
```

要点：
- JSON 日志每行一条（`json.dumps` 不缩进），便于 `tail -f` 与采集器按行解析；`ensure_ascii=False` 保留中文。
- `extra=` 附加字段会进入 `LogRecord.__dict__`，Formatter 中过滤标准属性后取出。
- 不可序列化对象（如 `Path`/`datetime`）用 `_safe_serialize` 兜底转 `repr`。
- 多进程场景：每进程写独立文件 + 采集器（filebeat/promtail）汇总，避免并发写冲突。

## 日志分层策略

利用 logger 层级（`parent.child`）按包/模块独立配置级别。

```python
"""fspack.logging_setup 模块."""

from __future__ import annotations

import logging


def configure_loggers() -> None:
    """配置分层 logger 级别策略.

    层级：root=ERROR（兜底）→ fspack=INFO（自研默认）→ 子模块按需放低；
    第三方库（urllib3/asyncio/httpx）一律 WARNING。
    """
    logging.getLogger().setLevel(logging.ERROR)  # root 兜底
    logging.getLogger("fspack").setLevel(logging.INFO)
    # 调试某模块时动态放低（不改 root）：logging.getLogger("fspack.db").setLevel(logging.DEBUG)
    for noisy in ("urllib3", "asyncio", "httpx", "httpcore", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def set_debug(module: str, enabled: bool = True) -> None:
    """运行时动态调整某模块日志级别（True=DEBUG, False=INFO）."""
    logging.getLogger(module).setLevel(logging.DEBUG if enabled else logging.INFO)
```

要点：
- 自研包根 logger 设 INFO；第三方库根设 WARNING；root 设 ERROR 兜底。
- 调试某模块时**只放低该模块**，不动 root，避免全量噪声。
- `logger.propagate`：默认 True（向父传播）；设 False 时该 logger 日志不向上传播。
- `getLogger("a.b.c")` 自动建立层级；第三方库噪声：`urllib3`/`asyncio`/`httpx`/`httpcore`/`charset_normalizer`。

## GUI 集成

PySide GUI 中将日志输出到 `QTextEdit` 面板；跨线程日志须通过信号槽转发到主线程（参考 `gui-pyside` SKILL 的 QThread/Signal 模式）。

```python
"""fspack.gui.log_panel 模块."""

from __future__ import annotations

import logging
from datetime import datetime

try:
    from PySide2.QtCore import QObject, Signal
    from PySide2.QtGui import QTextCursor
    from PySide2.QtWidgets import QTextEdit, QWidget
except ImportError:
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import QTextEdit, QWidget

LEVEL_COLORS: dict[str, str] = {  # 令牌驱动，见 gui-pyside theme
    "DEBUG": "#6c757d", "INFO": "#212529", "WARNING": "#fd7e14",
    "ERROR": "#dc3545", "CRITICAL": "#b71c1c",
}


class LogBridge(QObject):
    """日志信号桥：跨线程转发到主线程槽（必须用信号槽，禁直接操作 GUI）."""

    log_signal = Signal(str, str)  # (levelname, formatted_message)


class QtLogHandler(logging.Handler):
    """将日志通过 Qt 信号转发到主线程（线程安全；直接操作 GUI 非主线程会崩溃）."""

    def __init__(self, bridge: LogBridge) -> None:
        """初始化 handler，绑定信号桥接器."""
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        """重写 emit：格式化后通过信号发出."""
        try:
            self._bridge.log_signal.emit(record.levelname, self.format(record))
        except Exception:
            self.handleError(record)


class LogPanel(QTextEdit):
    """日志面板：只读 QTextEdit，按级别着色，自动滚动到底部."""

    MAX_LINES: int = 5000  # 超过则截断头部，防止内存膨胀

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化日志面板."""
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.NoWrap)
        self._line_count = 0

    def append_log(self, level: str, message: str) -> None:
        """追加一条日志（主线程槽函数，HTML 着色 + 自动滚动）."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = LEVEL_COLORS.get(level, "#212529")
        self.append(
            f'<span style="color:#6c757d">{timestamp}</span> '
            f'<span style="color:{color};font-weight:bold">[{level}]</span> '
            f'<span style="color:{color}">{message}</span>'
        )
        self._line_count += 1
        if self._line_count > self.MAX_LINES:  # 截断头部 500 行
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor, 500)
            cursor.removeSelectedText()
            self._line_count -= 500
        self.moveCursor(QTextCursor.End)

# 接线：bridge=LogBridge(); handler=QtLogHandler(bridge); bridge.log_signal.connect(panel.append_log)
# handler.setFormatter(logging.Formatter("%(name)s: %(message)s")); logger.addHandler(handler)
```

要点：
- `QtLogHandler.emit` 中**禁止**直接操作 GUI（`QTextEdit.append`），必须通过 `Signal.emit` 转发。
- Qt 信号槽跨线程自动用 `QueuedConnection`，槽函数在主线程执行，安全刷新 GUI。
- `LogBridge` 实例须保留引用（赋值给主窗口属性），否则 GC 后信号断开。
- `MAX_LINES` 截断头部防止内存膨胀；级别颜色用令牌（见 `gui-pyside` SKILL 的 `theme.py`）。

## CLI 集成

`click.echo`/`typer.echo` 用于用户可见输出；`logging` 用于诊断日志。`--verbose`/`--quiet` 调整级别。

```python
"""fspack.cli 模块（CLI 日志集成片段）."""

from __future__ import annotations

import logging

import click


def setup_cli_logging(verbose: int, quiet: bool) -> None:
    """根据 CLI 参数配置日志级别（verbose: 0=INFO, 1=DEBUG, 2=DEBUG+第三方; quiet: 仅 ERROR）."""
    if quiet:
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
        for noisy in ("urllib3", "asyncio", "httpx"):
            logging.getLogger(noisy).setLevel(logging.DEBUG)
    elif verbose >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("-v", "--verbose", count=True, help="增加详细度（-v=DEBUG, -vv=含第三方）")
@click.option("-q", "--quiet", is_flag=True, help="仅输出 ERROR")
def cli(verbose: int, quiet: bool) -> None:
    """fspack 命令行工具."""
    setup_cli_logging(verbose, quiet)

# 日志默认到 stderr，不污染 stdout 管道；错误转 ClickException + logger.error(exc_info=True)
```

要点：
- `click.echo` 输出**用户结果**（命令产物）；`logging` 输出**诊断信息**（进度、错误细节）。
- `-v`/`--verbose` 用 `count=True`：`-v`=1, `-vv`=2，对应不同详细度；`--quiet` 优先级高于 `-v`（仅 ERROR）。
- 诊断日志默认输出到 stderr（`basicConfig` 默认 stream 是 stderr），不污染 stdout 管道。
- 日志错误转 `ClickException` 输出友好消息，同时 logger 记录完整栈（`exc_info=True`）。

## 性能与安全

### 延迟格式化：用 `%` 不用 f-string

`logger.debug(f"data={x}")` 在级别低于 DEBUG 时**仍会**计算 f-string；用 `%` 占位延迟格式化可避免。循环内大量 DEBUG 用 `isEnabledFor` 守卫。

```python
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def bad_example(data: dict) -> None:
    """反面示例：f-string 立即格式化，DEBUG 关闭时仍求值."""
    logger.debug(f"数据处理: {data}")  # 禁止
    logger.debug("数据处理: %s", data)  # 推荐：% 延迟格式化


def good_loop(items: list[str]) -> None:
    """正面示例：循环内 DEBUG 用 isEnabledFor 守卫，避免参数求值开销."""
    if logger.isEnabledFor(logging.DEBUG):
        details = [f"{i}:{item}" for i, item in enumerate(items)]
        logger.debug("批量处理 %d 项: %s", len(items), details)
    logger.info("批量处理完成，共 %d 项", len(items))
```

### 日志脱敏

凭证、密码、令牌、个人信息不进日志；脱敏后记录。

```python
from __future__ import annotations

import logging
import re  # 手机号脱敏用 re.fullmatch

logger = logging.getLogger(__name__)


def mask_token(token: str, keep: int = 4) -> str:
    """脱敏令牌：仅保留首尾 keep 位，中间用 * 替换."""
    if len(token) <= keep * 2:
        return "*" * len(token)
    return f"{token[:keep]}{'*' * (len(token) - keep * 2)}{token[-keep:]}"


def mask_password(password: str) -> str:
    """密码一律不记录原文，仅返回长度提示."""
    return f"<password len={len(password)}>"
# 手机号：re.fullmatch(r'\d{11}', p) 时返回 f'{p[:3]}****{p[-4:]}'


def login(username: str, password: str, token: str) -> None:
    """登录示例：敏感字段脱敏后记录."""
    logger.info("用户登录: username=%s, password=%s, token=%s",
                username, mask_password(password), mask_token(token))
    # 输出：用户登录: username=alice, password=<password len=12>, token=abcd****wxyz
```

要点：
- **延迟格式化**：`logger.debug("x=%s", x)` 而非 `logger.debug(f"x={x}")`；rule-11 强制 `%` 风格。
- **循环内慎用 DEBUG**：大列表/高频循环用 `isEnabledFor(logging.DEBUG)` 守卫，避免参数求值开销。
- **密码/令牌不进日志**：一律脱敏（`mask_token`/`mask_password`），原文仅在内存中流转。
- **个人信息**：手机号、身份证、邮箱、银行卡号脱敏后记录（保留前 3 后 4）。
- **日志文件权限**：含敏感信息的日志文件设 `0600`（`Path.chmod(0o600)`）；HTTP 请求过滤 `Authorization`/`Cookie` 头。

## 常见陷阱

1. **`print` 残留**：开发期 `print` 调试未删除，污染 stdout。rule-11 禁止；一律用 `logger.debug`。
2. **f-string 格式化日志**：`logger.debug(f"x={x}")` 立即求值，DEBUG 关闭时仍计算。用 `%s` 延迟格式化。
3. **库代码调用 `basicConfig`**：污染调用方日志配置。库只用 `getLogger`，配置由入口负责。
4. **每模块新建 StreamHandler**：日志重复输出 N 次。Handler 只在 `setup_logging` 配置一次。
5. **`disable_existing_loggers=True`**：dictConfig 默认禁用已存在 logger，第三方库日志丢失。设 `False`。
6. **异常日志漏 `exc_info`**：不含栈跟踪无法定位根因。用 `logger.exception(...)` 或 `exc_info=True`。
7. **多进程写同一日志文件**：`RotatingFileHandler` 非进程安全。用 `ConcurrentLogHandler` 或独立文件 + 采集器。
8. **密码明文进日志**：`logger.info("password=%s", pwd)` 凭证泄露。脱敏后记录，原文不进任何级别。
9. **GUI 直接在 handler 操作部件**：非主线程调用 `QTextEdit.append` 崩溃。必须通过 `Signal.emit` 转发。
10. **第三方库噪声**：`urllib3`/`httpx` 输出大量请求细节。在 `setup_logging` 统一降到 WARNING。
