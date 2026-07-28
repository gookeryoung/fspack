# iter-89: `--log-file` 构建日志持久化

## 需求清单

- [x] 新增 `packaging/log_file.py` 模块实现日志文件创建与关闭
- [x] 支持 text（默认，人类可读）与 json（结构化）两种格式
- [x] CLI `fsp b` 添加 `--log-file`/`--log-format` 标志
- [x] `pipeline.build()` 集成日志 handler 生命周期管理（`try/finally`）
- [x] 异常时正确清理 handler，避免文件句柄泄露
- [x] 日志含时间戳/级别/logger 名/消息/异常栈，json 格式支持 `extra` 字段
- [x] 编写测试覆盖 log_file 模块、CLI 透传与 build 集成
- [x] 全套门禁通过 + 文档更新 + git 提交

## 迭代目标

为 `fsp b` 添加 `--log-file <path>` 选项，将构建过程日志写入文件，便于 CI 上传
与问题排查。日志格式支持 text（默认，人类可读）与 json（结构化，便于 ELK/Loki
采集）。构建开始时创建 handler、结束时自动关闭，异常路径用 `try/finally` 确保
清理。

## 改动文件清单

- `src/fspack/packaging/log_file.py`：新增模块，定义 `LogFormat` 枚举、
  `TextFormatter`/`JsonFormatter` 格式化器、`LogFileHandler` 数据类、
  `setup_log_file`/`teardown_log_file` 生命周期函数
- `src/fspack/packaging/pipeline.py`：`build()` 添加 `log_file`/`log_format`
  参数；拆分构建逻辑到 `_execute_build`，外层 `try/finally` 管理 handler 生命周期
- `src/fspack/cli.py`：`_add_build_subparser` 注册 `--log-file`/`--log-format`
  选项；`_run_build` 解析并透传 `log_file`/`log_format` 参数
- `tests/test_log_file.py`：新增 35 个测试覆盖 log_file 模块、CLI 透传与 build 集成
- `tests/test_cli.py`：`fake_build` 签名同步添加 `log_file`/`log_format` 参数
- `tests/test_build_dry_run.py`：3 处 `fake_build` 签名同步新参数
- `tests/test_size_report.py`：2 处 `fake_build` 签名同步新参数
- `tests/test_cli_recursive.py`：3 处 `fake_build` 签名同步新参数
- `README.md`：`fsp build` 命令速查表与章节添加 `--log-file`/`--log-format` 选项
- `.trae/req/req-47-feature-perf-polish.md`：iter-89 标记完成

## 关键决策与依据

### 1. 独立模块而非内联到 pipeline.py

按用户偏好「按责任拆分模块」，日志持久化涉及文件 handler 创建、格式化器选择、
生命周期管理等多个关注点，独立到 `packaging/log_file.py` 便于测试与复用。
pipeline.py 仅在 `build()` 入口做一次 `setup_log_file`/`teardown_log_file` 调用。

### 2. 文件 handler 不轮转

构建日志通常单次构建单文件，无需 `RotatingFileHandler`/`TimedRotatingFileHandler`
轮转。用 `logging.FileHandler` 追加模式（`mode="a"`）即可，CI 多次构建会累加
日志便于对比。需轮转的场景由 CI 自身日志轮转策略处理。

### 3. JsonFormatter 覆盖 formatTime 解决 Windows 毫秒问题

Windows `time.strftime` 不支持 `%f` 毫秒格式，直接用父类 `formatTime` 会抛
`ValueError: Invalid format string`。覆盖 `formatTime` 手动拼接毫秒：
`time.strftime("%Y-%m-%dT%H:%M:%S", ct)` + `f".{int(record.msecs):03d}"`，
输出 ISO 8601 兼容格式 `2026-07-28T12:34:56.789`。

### 4. extra 字段透传

`logger.info(..., extra={"key": value})` 传入的自定义字段会进入
`LogRecord.__dict__`，`JsonFormatter` 过滤标准属性后原样保留到 JSON 输出，
便于业务上下文（如 `build_id`/`project_name`）随日志采集。不可序列化对象
用 `_safe_serialize` 兜底转 `repr`。

### 5. handler 生命周期用 try/finally

`build()` 拆分出 `_execute_build` 承载原构建逻辑，外层 `try/finally` 确保
`teardown_log_file` 在构建完成或异常时均被调用，避免文件句柄泄露导致后续
构建无法写入同一日志文件。`teardown_log_file(None)` 无操作，简化调用方代码。

### 6. FileHandler 显式 UTF-8 编码

`logging.FileHandler` 默认 `encoding=None` 使用平台默认编码（Windows 为 GBK），
中文日志会乱码。显式指定 `encoding="utf-8"` 确保跨平台一致。

## 代码实现情况

### log_file.py 核心类与函数

```python
class LogFormat(Enum):
    """日志文件格式：TEXT（默认）/JSON."""
    TEXT = "text"
    JSON = "json"

    @classmethod
    def parse(cls, value: str | None) -> LogFormat:
        """从字符串解析格式，None/空字符串返回默认 TEXT."""
        ...

class JsonFormatter(logging.Formatter):
    """JSON 结构化日志 Formatter，每行一条 JSON 记录."""

    @override
    def formatTime(self, record, datefmt=None):  # noqa: ARG002
        """覆盖时间格式化：ISO 8601 + 毫秒（%f 在 Windows 不可用）."""
        ct = self.converter(record.created)
        base = time.strftime("%Y-%m-%dT%H:%M:%S", ct)
        return f"{base}.{int(record.msecs):03d}"

    @override
    def format(self, record):
        """将 LogRecord 序列化为 JSON 字符串，含 extra 字段."""
        ...

@dataclass(frozen=True)
class LogFileHandler:
    """日志文件 handler 包装."""
    handler: logging.FileHandler
    path: Path

def setup_log_file(path: Path, fmt: LogFormat = LogFormat.TEXT) -> LogFileHandler:
    """创建文件 handler 附加到 root logger，返回包装."""

def teardown_log_file(wrapper: LogFileHandler | None) -> None:
    """从 root logger 移除并关闭 handler，None 时无操作."""
```

### pipeline.py 集成

```python
def build(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    ...
    log_file: Path | None = None,
    log_format: LogFormat = LogFormat.TEXT,
) -> ProjectInfo:
    """执行完整构建流水线，返回项目信息."""
    ...
    log_wrapper = setup_log_file(Path(log_file), log_format) if log_file is not None else None
    try:
        info = _execute_build(
            tracker, project_dir, py_version, target, cfg, opts,
            extra_index_urls, find_links, dry_run,
        )
    finally:
        teardown_log_file(log_wrapper)
    return info
```

### CLI 透传

```python
p.add_argument(
    "--log-file",
    default=None,
    metavar="PATH",
    help="将构建日志写入文件（含时间戳/级别/logger 名/消息/异常栈）...",
)
p.add_argument(
    "--log-format",
    default="text",
    choices=["text", "json"],
    help="日志文件格式：text=人类可读纯文本（默认），json=结构化 JSON...",
)

# _run_build
log_file = Path(ns.log_file).resolve() if ns.log_file else None
log_format = LogFormat.parse(ns.log_format)
build(..., log_file=log_file, log_format=log_format)
```

## 测试验证结果

`tests/test_log_file.py` 35 个测试：

**LogFormat 枚举（6 个）**：
- 枚举值正确/parse('text')/parse('json')/大小写不敏感/None 默认 TEXT/空字符串默认 TEXT/未知格式抛 ValueError

**TextFormatter（4 个）**：
- 基本格式（含时间戳/级别/logger 名/消息）/级别左对齐/异常栈附加/`%` 延迟格式化

**JsonFormatter（8 个）**：
- 输出合法 JSON/字段完整（timestamp/level/logger/message/module/function/line）
- 异常时附加 `exception` 字段/`extra` 字段透传/不可序列化对象转 repr
- 时间戳格式 `YYYY-MM-DDTHH:MM:SS.mmm`/毫秒部分 3 位/多行异常栈

**setup_log_file/teardown_log_file（6 个）**：
- 创建文件/父目录自动创建/UTF-8 编码/追加模式/teardown 移除 handler/teardown(None) 无操作

**CLI 透传（4 个）**：
- `--log-file <path>` 透传/`--log-format json` 透传/未指定时 None+TEXT/`--log-format xml` 被拒绝

**build 集成（5 个）**：
- `build(log_file=...)` 写入文件/异常时清理 handler/`log_format=JSON` 写入 JSON
- 未指定 `log_file` 时不创建文件/日志内容含项目信息

**fake_build 签名同步（2 个隐含）**：
- `test_cli.py`/`test_build_dry_run.py`/`test_size_report.py`/`test_cli_recursive.py`
  共 9 处 `fake_build` 签名同步添加 `log_file`/`log_format` 参数

**门禁结果**：
- ruff check：All checks passed
- ruff format：88 files already formatted
- pyrefly check：0 errors（4 suppressed, 7 warnings not shown）
- pytest：1398 passed, 1 skipped, 覆盖率 97.86%（>= 95%）

## 整合优化情况

- `LogFormat.parse` 复用枚举 `__call__` 而非 if-elif 链，符合 Pythonic 风格
- `_STANDARD_ATTRS` 用 `frozenset` 而非 `set`，不可变且查找 O(1)
- `JsonFormatter._safe_serialize` 用 `try/except (TypeError, ValueError)` 兜底，
  覆盖所有不可 JSON 序列化的对象类型
- `teardown_log_file(None)` 无操作设计，调用方无需 None 检查，简化 `try/finally` 代码
- `setup_log_file` 显式 `encoding="utf-8"`，避免 Windows GBK 中文乱码

## 遗留事项

无。日志持久化已完整覆盖格式化、生命周期管理、CLI 透传、异常清理。

## 下一轮计划

iter-90：`--profile` 耗时分析报告，复用 `BuildTracker.stage()` 已收集的耗时数据，
扩展内存峰值采集（psutil，新增依赖），输出各阶段 wall time / CPU time / 内存峰值。
