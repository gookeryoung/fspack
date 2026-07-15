---
name: "python-class-design"
description: "Python 类与面向对象设计技能：dataclass、ABC、Enum、继承组合、特殊方法、设计模式等可复用代码模板。当需要设计类结构、定义数据模型、实现接口/抽象基类、选择继承或组合、实现工厂/策略/观察者模式时调用。"
---

# Python 类与面向对象设计

自包含的 Python OOP 设计指南：数据建模、接口抽象、行为复用、设计模式。所有示例遵循 `rule-11-python-standards.md`（类型注解、中文 docstring、`from __future__ import annotations`）。

## 何时调用

- 需要设计类结构、定义数据模型或值对象
- 需要抽象基类（ABC）、接口约定或多态分发
- 需要枚举、状态机或类型安全的标志值
- 需要选择继承 vs 组合、实现 Mixin 或提取公共行为
- 需要实现工厂、策略、观察者等设计模式
- 需要正确实现 `__repr__`/`__eq__`/`__hash__`/上下文管理器协议

## dataclass：数据建模首选

配置/描述/传输类用 `@dataclass(frozen=True)`；可变类用普通 `@dataclass`。

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class ServerConfig:
    """不可变服务器配置（frozen=True 可哈希、可作字典键）。"""

    host: str
    port: int = 8080
    timeout: float = 30.0
    tags: tuple[str, ...] = ()  # frozen 类需用不可变类型（tuple 而非 list）


@dataclass
class RequestBatch:
    """可变批量请求（可变默认参数用 field(default_factory)）。"""

    endpoint: str
    items: List[str] = field(default_factory=list)
    retry_count: int = 0

    def add(self, item: str) -> None:
        """追加单个请求项。"""
        self.items.append(item)
```

要点：
- `frozen=True`：不可变、可哈希、线程安全；值对象/配置类首选。
- 可变默认参数**禁止**直接用 `[]`/`{}`，用 `field(default_factory=list)`。
- `slots=True`（3.10+）省内存、加速属性访问；低版本用 `__slots__ = (...)` 手动声明。
- `field(repr=False, compare=False)` 排除敏感或大字段。

## ABC：接口与抽象基类

接口用 `abc.ABC` + `@abstractmethod`，约定子类必须实现的方法。

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Storage(ABC):
    """存储后端抽象基类，约定读/写/删除接口。"""

    @abstractmethod
    def read(self, key: str) -> bytes:
        """读取指定键的原始字节。"""

    @abstractmethod
    def write(self, key: str, data: bytes) -> None:
        """写入指定键的原始字节。"""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """删除指定键，返回是否实际删除。"""

    # 非抽象方法提供默认实现，子类可覆盖
    def exists(self, key: str) -> bool:
        """判断键是否存在（默认尝试读取，子类可优化）。"""
        try:
            self.read(key)
            return True
        except KeyError:
            return False


class FileStorage(Storage):
    """基于文件系统的存储实现。"""

    def __init__(self, root: Path) -> None:
        """初始化并创建根目录。"""
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def read(self, key: str) -> bytes:
        """读取文件内容，键不存在时抛 KeyError。"""
        path = self._root / key
        if not path.exists():
            raise KeyError(key)
        return path.read_bytes()

    def write(self, key: str, data: bytes) -> None:
        """写入文件内容。"""
        (self._root / key).write_bytes(data)

    def delete(self, key: str) -> bool:
        """删除文件，返回是否实际删除。"""
        path = self._root / key
        if path.exists():
            path.unlink()
            return True
        return False
```

要点：
- ABC 定义「能做什么」而非「是什么」，面向接口编程。
- 子类未实现全部 `@abstractmethod` 时无法实例化，编译期捕获遗漏。
- 抽象方法体只写 `"""docstring"""` 或 `...`，不放实现。
- 配合 `@classmethod` 实现多态构造（见工厂模式）。

## Enum：状态与标志值

状态/类型/标志用 `enum.Enum`，禁止裸字符串或魔术数字。

```python
from __future__ import annotations

from enum import Enum, IntEnum, auto


class TaskStatus(Enum):
    """任务生命周期状态。"""

    PENDING = auto()    # auto() 自动分配递增值
    RUNNING = auto()
    DONE = auto()
    FAILED = auto()

    def is_terminal(self) -> bool:
        """判断是否终态（不可再流转）。"""
        return self in (self.DONE, self.FAILED)


class LogLevel(IntEnum):
    """日志级别（IntEnum 支持与 int 比较、排序）。"""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40


# 使用：类型安全，拼写错误在编译期暴露
status = TaskStatus.RUNNING
if status.is_terminal():
    print("任务已结束")
```

要点：
- `Enum`：语义清晰、防拼写错误、可遍历、可作字典键。
- `IntEnum`：需要与整数互操作时用（如日志级别比较）。
- `auto()` 避免手动赋值；值无语义意义时首选。
- `Flag`/`IntFlag`：位组合标志（如权限 `READ | WRITE`）。
- 命名 `UPPER_SNAKE`，枚举类名单数。

## 继承 vs 组合

优先组合，谨慎继承。继承表达「is-a」，组合表达「has-a」。

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


# --- 组合（推荐）：灵活、低耦合 ---
@dataclass(frozen=True)
class Address:
    """地址值对象。"""
    city: str
    street: str
    zip_code: str


@dataclass
class User:
    """用户聚合，通过组合持有地址（而非继承 Address）。"""
    name: str
    address: Address  # has-a 关系
    aliases: List[str] = field(default_factory=list)


# --- 继承：仅在真正的 is-a 分类时用 ---
class Animal:
    """动物基类。"""

    def __init__(self, name: str) -> None:
        """初始化动物名称。"""
        self.name = name

    def speak(self) -> str:
        """发声（子类必须覆盖）。"""
        raise NotImplementedError


class Dog(Animal):
    """狗是动物（is-a），覆盖 speak 行为。"""

    def speak(self) -> str:
        """狗叫。"""
        return f"{self.name}: 汪汪"
```

决策准则：
- **组合**：行为复用、运行时可替换、多角色叠加 → 首选。
- **继承**：真正的分类关系、接口约定（ABC）、模板方法模式 → 谨慎用。
- 继承层级 ≤ 2 层；超过说明设计可能有问题，考虑组合。
- 避免菱形继承；多行为复用用 Mixin 或组合。

## 特殊方法

可变类实现 `__repr__`；值对象实现 `__eq__`/`__hash__`；资源类实现上下文管理器。

```python
from __future__ import annotations

from typing import IO


class ConnectionPool:
    """数据库连接池（可变类，实现 repr + 上下文管理）。"""

    def __init__(self, dsn: str, size: int = 5) -> None:
        """初始化连接池。"""
        self._dsn = dsn
        self._size = size
        self._pool: list[IO] = []  # 实际为连接对象

    def __repr__(self) -> str:
        """调试友好的表示（含关键字段，不含敏感信息）。"""
        return f"ConnectionPool(dsn={self._dsn!r}, size={self._size}, active={len(self._pool)})"

    def __enter__(self) -> "ConnectionPool":
        """进入上下文，初始化连接池。"""
        self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """退出上下文，关闭所有连接（不吞异常）。"""
        self._close_all()
        # 返回 None/False 表示不抑制异常

    def _connect(self) -> None:
        """创建连接。"""

    def _close_all(self) -> None:
        """关闭所有连接。"""


# with 用法：资源自动释放
with ConnectionPool("postgres://localhost/db") as pool:
    print(pool)  # 触发 __repr__
```

要点：
- `__repr__`：可变类必备，含关键字段，便于调试；脱敏敏感字段。
- `__eq__`/`__hash__`：值相等语义时成对实现（`__hash__` 为 None 则不可哈希）。
- `__enter__`/`__exit__`：资源类实现上下文管理器，`__exit__` 返回 falsy 不吞异常。
- 多资源用 `contextlib.ExitStack`。
- `__eq__` 改变后 `__hash__` 必须一致：可变对象设 `__hash__ = None`。

## property 与 cached_property

只读属性用 `@property`；计算昂贵的缓存属性用 `@cached_property`。

```python
from __future__ import annotations

import hashlib
from functools import cached_property
from pathlib import Path


class Document:
    """文档封装：懒加载内容、缓存校验和。"""

    def __init__(self, path: Path) -> None:
        """初始化文档路径。"""
        self._path = path

    @property
    def path(self) -> Path:
        """只读路径（外部不可修改）。"""
        return self._path

    @cached_property
    def content(self) -> str:
        """文件内容（首次访问时读取，后续走缓存）。"""
        return self._path.read_text(encoding="utf-8")

    @cached_property
    def checksum(self) -> str:
        """内容 SHA256（缓存，文件不变时无需重算）。"""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def invalidate_cache(self) -> None:
        """文件变更后手动清空缓存（cached_property 无法自动失效）。"""
        for name in ("content", "checksum"):
            self.__dict__.pop(name, None)
```

要点：
- `@property`：只读封装、计算属性、访问控制；避免滥用（简单属性直接公开）。
- `@cached_property`：首次访问计算并缓存到实例 `__dict__`；不可变类（frozen）无法用。
- 缓存源变更后须手动清空（删 `__dict__` 中的键）。
- 写入校验用 `@x.setter`；但优先保持不可变，用新对象替代修改。

## Mixin 与模块级函数

行为复用：模块级纯函数优先于 Mixin；三处以上重复才考虑提取。

```python
from __future__ import annotations

from typing import Any, Mapping


# --- 模块级纯函数（首选）：无状态、易测试、无继承耦合 ---
def validate_non_empty(value: str, field_name: str) -> str:
    """校验非空字符串，返回原值或抛 ValueError。"""
    if not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value


def merge_config(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    """合并配置字典，override 覆盖 base。"""
    return {**base, **override}


# --- Mixin（谨慎用）：为不相关类添加相同行为 ---
class JsonSerializableMixin:
    """JSON 序列化 Mixin，要求子类实现 to_dict。"""

    def to_dict(self) -> dict:
        """转换为字典（子类实现具体逻辑）。"""
        raise NotImplementedError

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False)


class User(JsonSerializableMixin):
    """用户类，通过 Mixin 获得 to_json 能力。"""

    def __init__(self, name: str, age: int) -> None:
        """初始化用户。"""
        self.name = validate_non_empty(name, "name")
        self.age = age

    def to_dict(self) -> dict:
        """转换为字典。"""
        return {"name": self.name, "age": self.age}
```

决策准则：
- **模块级函数**：无状态行为、纯计算 → 首选，避免继承耦合。
- **Mixin**：跨不相关类共享行为、且需要访问 `self` → 谨慎用。
- **基类**：真正的 is-a 分类 + 接口约定 → 用 ABC。
- 三处以下重复不提取；三处以上才考虑 Mixin 或函数。

## 设计模式

### 工厂模式：多态构造

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class Parser(ABC):
    """解析器抽象基类。"""

    @abstractmethod
    def parse(self, raw: str) -> dict:
        """解析原始字符串为字典。"""

    @classmethod
    def for_format(cls, fmt: str) -> "Parser":
        """工厂方法：按格式名创建对应解析器。"""
        factories = {"json": JsonParser, "yaml": YamlParser, "toml": TomlParser}
        factory = factories.get(fmt)
        if factory is None:
            raise ValueError(f"不支持的格式: {fmt}")
        return factory()


class JsonParser(Parser):
    """JSON 解析器。"""
    def parse(self, raw: str) -> dict:
        """解析 JSON。"""
        import json
        return json.loads(raw)


class YamlParser(Parser):
    """YAML 解析器。"""
    def parse(self, raw: str) -> dict:
        """解析 YAML。"""
        # 实际用 yaml.safe_load
        return {"_placeholder": raw}


class TomlParser(Parser):
    """TOML 解析器。"""
    def parse(self, raw: str) -> dict:
        """解析 TOML。"""
        # 实际用 tomllib.loads
        return {"_placeholder": raw}


# 使用：调用方无需知道具体类
parser = Parser.for_format("json")
result = parser.parse('{"key": "value"}')
```

### 策略模式：可替换算法

```python
from __future__ import annotations

from typing import Callable, List
from dataclasses import dataclass


@dataclass(frozen=True)
class SortConfig:
    """排序策略配置。"""
    key: Callable[[object], object]
    reverse: bool = False


def sort_items(items: List[object], strategy: SortConfig) -> List[object]:
    """按策略排序列表（策略作为参数注入，运行时可替换）。"""
    return sorted(items, key=strategy.key, reverse=strategy.reverse)


# 不同策略
by_name = SortConfig(key=lambda x: getattr(x, "name", ""))
by_age_desc = SortConfig(key=lambda x: getattr(x, "age", 0), reverse=True)
```

### 观察者模式：事件通知

```python
from __future__ import annotations

from typing import Callable, List
from dataclasses import dataclass, field


@dataclass
class EventBus:
    """简单事件总线：发布-订阅模式。"""

    _subscribers: dict = field(default_factory=dict)

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """订阅事件。"""
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        """取消订阅。"""
        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def publish(self, event_type: str, data: object = None) -> None:
        """发布事件，通知所有订阅者。"""
        for handler in self._subscribers.get(event_type, []):
            try:
                handler(data)
            except (TypeError, ValueError) as exc:
                # 第三方回调异常仅记录，不影响主流程
                import logging
                logging.getLogger(__name__).warning("事件处理器异常: %s", exc)


# 使用
bus = EventBus()
bus.subscribe("user_created", lambda data: print(f"发送欢迎邮件: {data}"))
bus.publish("user_created", {"name": "张三"})
```

## 常见陷阱

1. **可变默认参数**：`def f(items=[]):` 多次调用共享同一列表。用 `None` 哨兵或 `field(default_factory=list)`。
2. **frozen 类持有可变字段**：`@dataclass(frozen=True)` 的 list 字段仍可被修改（frozen 只防重新赋值）。用 `tuple` 或 `MappingProxyType`。
3. **`__eq__` 不配 `__hash__`**：定义 `__eq__` 后 `__hash__` 自动设为 None，对象不可哈希。值对象需成对实现。
4. **滥用继承**：为了复用一个方法而继承，引入不必要的耦合。改用组合或模块级函数。
5. **ABC 当基类用**：ABC 是定义接口的，不是提供实现的基类。混用导致层级混乱。
6. **裸字符串代替 Enum**：`status = "running"` 拼写错误不报错。用 `Enum` 在编译期捕获。
7. **cached_property 不失效**：数据源变更后缓存仍是旧值。手动清 `__dict__` 或改用 `property`。
8. **`__repr__` 泄露敏感信息**：密码/令牌出现在 repr 里会进日志。脱敏或 `field(repr=False)`。
9. **深层继承链**：3 层以上继承难以维护。拆成组合 + ABC。
10. **Mixin 状态污染**：Mixin 持有状态与子类字段冲突。Mixin 尽量无状态，只提供方法。
