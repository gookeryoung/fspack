---
name: "python-config"
description: "Python 配置管理技能：配置层次、TOML 读取、环境变量、.env 文件、Pydantic Settings、dataclass 配置、配置校验、热重载、多环境配置。当需要加载配置文件、解析环境变量、管理多环境配置、实现配置热重载、校验配置项、定义配置类时调用。"
---

# Python 配置管理

自包含的配置管理指南：配置层次、TOML/env 读取、Pydantic Settings、dataclass 配置、校验、热重载、多环境。所有配置类优先用 `@dataclass(frozen=True)` 或 Pydantic BaseSettings；凭证放 `.env`/环境变量，`.gitignore` 须含 `.env`（rule-11 安全要求）；路径用 `pathlib.Path`（ruff `PTH` 强制）；类型注解必须完整（rule-11 要求）。

## 何时调用

- 需要读取 pyproject.toml 或自定义 .toml 配置文件
- 需要从环境变量加载配置（含类型转换、前缀过滤）
- 需要加载 .env 文件（python-dotenv）
- 需要定义配置类（Pydantic BaseSettings 或 dataclass）
- 需要校验配置项（必填、范围、类型安全）
- 需要实现配置热重载（文件监听 + 回调）
- 需要管理多环境配置（dev/staging/prod 分离）
- 需要合并多层配置（默认值 < 文件 < 环境变量 < 命令行）

## 配置层次

优先级递增覆盖：默认值 < 配置文件 < 环境变量 < 命令行参数。

```python
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

# 1. 默认值（代码内常量）
DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1", "port": 8000, "debug": False, "log_level": "INFO",
}


def build_config() -> dict[str, Any]:
    """按优先级合并四层配置（低→高覆盖）。

    load_toml / load_env / parse_args 的实现见后续各节。
    """
    config: dict[str, Any] = DEFAULTS.copy()                      # 1. 默认值
    config_path = Path("config.toml")
    if config_path.exists():
        config.update(read_toml(config_path))                     # 2. 配置文件（见 TOML 节）
    config.update({                                                # 3. 环境变量（APP_ 前缀）
        k[4:].lower(): v for k, v in os.environ.items() if k.startswith("APP_")
    })
    parser = argparse.ArgumentParser()                             # 4. 命令行参数
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    config.update({k: v for k, v in vars(args).items() if v is not None})
    return config
```

要点：
- 低优先级先加载，高优先级后覆盖；`dict.update()` 实现覆盖语义。
- 默认值放代码内常量，便于审查与 IDE 提示；命令行参数优先级最高，便于临时调试。
- `None` 表示"未提供"，不覆盖下层；显式空值用类型零值。

## TOML 读取

`tomllib` 是 Python 3.11+ 标准库；低版本用 `tomli`（API 兼容）。

```python
from __future__ import annotations

from pathlib import Path
from typing import Any


def read_toml(path: Path) -> dict[str, Any]:
    """读取 TOML 文件（3.11+ 用 tomllib，低版本回退 tomli）。

    用法：
        data = read_toml(Path("pyproject.toml"))
        # 读取 [tool.fspack] section：
        cfg = data.get("tool", {}).get("fspack", {})
    """
    try:
        import tomllib  # Python 3.11+ 标准库
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as f:  # tomllib 只接受二进制模式
        return tomllib.load(f)
```

要点：
- `tomllib.load()` 只接受二进制流（`open("rb")`），不接受文本模式。
- `tomllib.loads()` 接受字符串；`load()` 接受文件对象。
- 写 TOML 用 `tomli_w`（非标准库）：`tomli_w.dumps(data)`。
- `pyproject.toml` 的 `[tool.fspack]` section 是自定义配置的标准位置。

## 环境变量

`os.environ.get` + 类型转换；前缀过滤避免污染命名空间。

```python
from __future__ import annotations

import os
from typing import Any


def get_typed(key: str, target_type: type, default: Any) -> Any:
    """读取并类型转换环境变量（支持 int/float/bool/str）。

    用法：
        port = get_typed("APP_PORT", int, 8000)
        debug = get_typed("APP_DEBUG", bool, False)
        ratio = get_typed("APP_RATIO", float, 0.5)
    """
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    if target_type is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return target_type(raw)
    except ValueError:
        return default


def load_prefixed(prefix: str = "APP_") -> dict[str, str]:
    """加载带前缀的环境变量，去除前缀后小写化。

    示例：APP_HOST=0.0.0.0 → {"host": "0.0.0.0"}
    """
    return {
        k[len(prefix):].lower(): v
        for k, v in os.environ.items()
        if k.startswith(prefix)
    }
```

要点：
- 环境变量永远是字符串，必须显式类型转换；`bool` 不能用 `bool("false")`（恒为 True），用枚举集合判断。
- 前缀（如 `APP_`）避免与系统变量冲突，便于批量加载。
- 凭证（密码、Token、API Key）只放环境变量/`.env`，不进配置文件。

## .env 文件加载

`python-dotenv` 加载 `.env` 到 `os.environ`；`.env.example` 提交到仓库作模板，`.env` 须在 `.gitignore`。

```python
from __future__ import annotations

from pathlib import Path


def load_env_file(path: Path | None = None, override: bool = False) -> None:
    """加载 .env 文件到 os.environ。

    path 为 None 时自动查找；override=True 时覆盖已存在环境变量。
    """
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=path, override=override)
```

要点：
- `.env` 含凭证，**禁止**提交；`.env.example` 作模板提交；`.gitignore` 须含 `.env`（rule-11 安全要求）。
- `override=False`（默认）：不覆盖已存在的系统环境变量，便于 CI/容器注入。
- `.env` 用于本地开发；生产用真实环境变量（K8s Secret、AWS Parameter Store 等）。

## Pydantic Settings 模式

`pydantic-settings`（Pydantic v2）的 `BaseSettings` 自动从环境变量/`.env` 加载配置，自带类型校验。

```python
from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseConfig(BaseSettings):
    """数据库配置（嵌套配置示例）。

    从 DATABASE_URL、DATABASE_POOL_SIZE 等环境变量加载。
    """

    model_config = SettingsConfigDict(env_prefix="DATABASE_", env_file=".env")

    url: str = Field(default="postgresql://localhost/app", description="数据库连接 URL")
    pool_size: int = Field(default=5, ge=1, le=100, description="连接池大小")


class AppConfig(BaseSettings):
    """应用配置，自动从环境变量和 .env 加载。

    环境变量名：APP_HOST、APP_PORT、APP_DEBUG 等（env_prefix="APP_"）。
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # 忽略未声明的字段（默认 forbid 会报错）
    )

    host: str = Field(default="127.0.0.1", description="监听地址")
    port: int = Field(default=8000, ge=1, le=65535, description="监听端口")
    debug: bool = Field(default=False, description="调试模式")
    log_level: str = Field(default="INFO", description="日志级别")
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """校验日志级别合法。"""
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level 必须是 {allowed} 之一，得到 {v!r}")
        return upper


# 加载：AppConfig() 自动从环境变量 + .env 构造
# 从字典构造（from_attributes 模式）：AppConfig.model_validate(data)
# 序列化：cfg.model_dump()
```

要点：
- `env_prefix="APP_"`：`APP_HOST` 自动映射到 `host` 字段；嵌套用双下划线（如 `APP_DATABASE__URL`）。
- `env_file=".env"`：自动加载 `.env` 文件（优先级低于系统环境变量）。
- `Field(ge=..., le=...)`：数值范围校验；`field_validator` 自定义校验逻辑。
- `extra="ignore"` 忽略多余字段；`model_validate(data)` 从字典构造；`model_dump()` 序列化。

## dataclass 配置

轻量场景用 `@dataclass(frozen=True)` 不可变配置，避免引入 Pydantic 依赖。

```python
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any


@dataclass(frozen=True)
class ServerConfig:
    """服务器配置（不可变，线程安全）。"""

    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    workers: int = 4


@dataclass(frozen=True)
class AppConfig:
    """应用配置（不可变根配置）。"""

    server: ServerConfig = field(default_factory=ServerConfig)
    log_level: str = "INFO"
    secret_key: str = ""  # 凭证从环境变量注入

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        """从字典构造配置，支持嵌套（server 字段递归构造）。"""
        field_map = {f.name: f for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in field_map:
                continue  # 忽略未知字段
            f = field_map[key]
            if isinstance(f.type, type) and hasattr(f.type, "from_dict") and isinstance(value, dict):
                kwargs[key] = f.type.from_dict(value)  # type: ignore[attr-defined]
            else:
                kwargs[key] = value
        return cls(**kwargs)

    def merge(self, overrides: dict[str, Any]) -> "AppConfig":
        """合并覆盖项，返回新实例（不可变，不修改原对象）。"""
        return type(self).from_dict({**self.__dict__, **overrides})


# 等价：dataclasses.replace(cfg, log_level="DEBUG")
```

要点：
- `frozen=True`：实例不可变，天然线程安全，可作为模块级常量。
- `from_dict`：工厂方法，处理嵌套 dataclass 递归构造；`fields()` 获取字段元信息。
- `merge`/`replace`：返回新实例，符合不可变语义。
- 凭证字段（如 `secret_key`）默认空，启动时从环境变量注入。

## 配置校验与默认值

必填校验、范围校验、类型安全、缺失字段友好报错。

```python
from __future__ import annotations

from typing import Any


class ConfigError(Exception):
    """配置错误（聚合所有缺失/非法字段）。"""


def validate_or_raise(
    data: dict[str, Any], schema: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """按 schema 校验配置，聚合所有错误一次性抛出。

    schema: {"port": {"type": int, "required": True, "min": 1, "max": 65535}, ...}
    """
    errors: list[str] = []
    result: dict[str, Any] = {}
    for key, rule in schema.items():
        raw = data.get(key, rule.get("default"))
        if raw in (None, ""):
            if rule.get("required"):
                errors.append(f"必填字段 {key!r} 缺失")
            continue
        target = rule["type"]
        try:
            if target is bool:
                value: Any = str(raw).strip().lower() in {"1", "true", "yes", "on"}
            else:
                value = target(raw)  # int/float/str 等直接构造
        except (ValueError, TypeError) as e:
            errors.append(f"字段 {key!r} 无法转为 {target.__name__}: {e}")
            continue
        if target in (int, float):
            lo, hi = rule.get("min"), rule.get("max")
            if lo is not None and value < lo:
                errors.append(f"{key}={value} 不能小于 {lo}")
            elif hi is not None and value > hi:
                errors.append(f"{key}={value} 不能大于 {hi}")
        result[key] = value
    if errors:
        raise ConfigError("配置校验失败:\n  - " + "\n  - ".join(errors))
    return result
```

要点：
- 聚合所有错误一次性抛出，便于一次性修复（不要逐字段抛）。
- `bool` 转换用枚举集合，不用 `bool(str)`（非空字符串恒 True）；范围校验用闭区间 `lo <= v <= hi`。
- 缺失字段给友好提示（字段名 + 期望类型），而非 `KeyError` 栈追踪。
- Pydantic Settings 已内置校验，无需重复实现；dataclass 场景用上述手写校验。

## 热重载

`watchfiles` 监听配置文件变更，触发回调重新加载；线程安全读取用 `threading.Lock` 或快照替换。

```python
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

try:
    from watchfiles import watch
except ImportError:
    watch = None  # type: ignore[assignment]


class HotReloadConfig:
    """热重载配置管理器。

    监听配置文件变更，自动重新加载；读取线程安全。

    用法：
        mgr = HotReloadConfig(Path("config.toml"), loader=read_toml)  # read_toml 见 TOML 节
        mgr.start()                 # 启动监听线程
        cfg = mgr.get()             # 线程安全读取当前配置
        mgr.on_change(on_reload)    # 注册变更回调
    """

    def __init__(
        self, path: Path, loader: Callable[[Path], dict[str, Any]]
    ) -> None:
        self._path = path
        self._loader = loader
        self._lock = threading.Lock()
        self._config: dict[str, Any] = self._loader(path)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []

    def get(self) -> dict[str, Any]:
        """线程安全读取当前配置快照。"""
        with self._lock:
            return self._config.copy()  # 返回副本，避免外部修改

    def on_change(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """注册配置变更回调。"""
        self._callbacks.append(callback)

    def start(self) -> None:
        """启动文件监听线程（后台守护线程）。"""
        if self._thread is not None or watch is None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止监听。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _watch_loop(self) -> None:
        """监听循环（在后台线程运行）。"""
        for _changes in watch(self._path.parent, stop_event=self._stop_event):
            try:
                new_config = self._loader(self._path)
            except Exception:
                continue  # 解析失败保留旧配置
            with self._lock:
                self._config = new_config
            for cb in self._callbacks:
                try:
                    cb(new_config.copy())
                except Exception:
                    pass  # 回调异常不影响监听
```

要点：
- `threading.Lock` 保护 `_config` 的读写；`get()` 返回副本避免外部修改。
- `watchfiles.watch(stop_event=...)` 支持优雅停止；监听父目录过滤目标文件。
- 解析失败时保留旧配置，回调异常隔离，均不中断监听循环。
- `daemon=True`：主进程退出时自动结束监听线程。

## 多环境配置

dev/staging/prod 配置文件分离；`APP_ENV` 环境变量选择当前环境。

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# 配置目录约定：config/base.toml + config/{env}.toml
CONFIG_DIR = Path("config")
ENVIRONMENTS = {"dev", "staging", "prod"}


def current_env() -> str:
    """获取当前环境（默认 dev）。"""
    env = os.environ.get("APP_ENV", "dev").lower()
    if env not in ENVIRONMENTS:
        raise ValueError(f"APP_ENV={env!r} 非法，应为 {ENVIRONMENTS} 之一")
    return env


def load_env_config(env: str | None = None) -> dict[str, Any]:
    """加载多环境配置：base.toml 被指定环境覆盖（env=None 时从 APP_ENV 读取）。"""
    env = env or current_env()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    config: dict[str, Any] = {}
    for path in [CONFIG_DIR / "base.toml", CONFIG_DIR / f"{env}.toml"]:
        if path.exists():
            with path.open("rb") as f:
                _deep_merge(config, tomllib.load(f))
    return config


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并 override 到 base（原地修改 base 并返回）。"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
```

要点：
- 目录约定：`config/base.toml`（共享）+ `config/{env}.toml`（环境覆盖）。
- `APP_ENV` 选择环境；默认 `dev`；非法值立即报错（fail-fast）。
- 深合并（deep merge）：嵌套 dict 递归覆盖，避免 base 的嵌套字段被整体替换。
- 敏感字段不写进 `{env}.toml`，用环境变量/`.env` 注入（环境变量优先级最高）。

## 常见陷阱

1. **环境变量当字符串用不转换类型**：`os.environ["PORT"]` 是 `"8000"` 字符串，传给需要 int 的 API 报错。必须 `int()` 转换。
2. **`bool("false")` 返回 True**：非空字符串恒为 True。用 `value.lower() in {"1","true","yes"}` 判断。
3. **`.env` 提交到仓库**：泄露凭证。`.gitignore` 必须含 `.env`（rule-11 安全要求）；提交 `.env.example` 作模板。
4. **低版本 Python 用 `import tomllib` 直接报错**：3.11 才进标准库。用 `try: import tomllib except ImportError: import tomli as tomllib` 回退。
5. **`tomllib.load()` 传文本模式**：`open("r")` 抛 `TypeError`。必须 `open("rb")` 二进制模式。
6. **配置类可变导致线程不安全**：多线程修改共享配置实例产生竞态。用 `@dataclass(frozen=True)` 或加锁。
7. **嵌套配置整体覆盖**：浅合并 `base.update(env)` 会让 `env` 的整个 `[database]` 替换 base 的，丢失 base 的字段。用深合并。
8. **缺失字段 `KeyError` 直接抛**：用户看到栈追踪不知所云。聚合所有错误友好提示（字段名 + 期望类型）。
9. **热重载解析失败中断监听**：配置文件写到一半触发重载，解析异常未捕获，监听线程退出。`try/except` 保留旧配置继续监听。
10. **环境变量前缀不加**：`HOST`、`PORT` 等通用名易与系统变量冲突。用 `APP_` 前缀过滤批量加载。
