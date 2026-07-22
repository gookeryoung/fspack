---
name: "python-testing"
description: "Python 测试技能：pytest fixture、参数化、mock、覆盖率、pytest-qt GUI 测试等可复用模板与最佳实践。当需要编写或审查测试、配置 pytest/coverage、设计 fixture、mock 外部依赖、测试异常路径或 PySide GUI 时调用。"
---

# Python 测试

自包含的 pytest 测试指南：fixture 设计、参数化、mock 策略、异常断言、覆盖率、pytest-qt GUI 测试。所有示例遵循 `rule-11-python-standards.md`（类型注解、中文 docstring、`from __future__ import annotations`）。

## 何时调用

- 需要为新功能或公共 API 编写单元测试
- 需要设计 fixture、conftest 层级或测试工厂
- 需要参数化测试、覆盖多种输入组合
- 需要 mock 外部依赖（环境变量、属性、第三方调用）
- 需要测试异常路径与错误消息
- 需要配置 coverage（branch、排除规则、`# pragma: no cover`）
- 需要测试 PySide/Qt GUI（`@pytest.mark.gui`、qtbot、waitSignal）
- 需要组织测试目录、注册自定义标记

## 测试组织

### 目录结构

```
tests/
├── __init__.py                 # 可选：让 tests 成为包，便于跨模块共享 fixture
├── conftest.py                 # 根 conftest：全局 fixture、autouse 钩子
├── unit/                       # 单元测试（快、隔离）
│   ├── __init__.py
│   ├── test_services.py
│   └── test_parser.py
├── integration/               # 集成测试（多组件协作，标 @pytest.mark.slow）
│   ├── __init__.py
│   └── test_pipeline.py
├── gui/                        # GUI 测试（标 @pytest.mark.gui，仅 GUI 项目）
│   ├── __init__.py
│   └── test_main_window.py
└── fixtures/                   # 测试数据（JSON/CSV/二进制）
    └── sample.json
```

要点：
- 按类型分目录：`unit/` 快速隔离、`integration/` 多组件协作、`gui/` Qt 界面测试。
- 每个目录可放 `conftest.py` 提供该层 fixture；根 `conftest.py` 只放跨目录共享的全局 fixture。
- `tests/**` 忽略 `ARG001`/`ARG002`（见 `ruff.toml` `[lint.per-file-ignores]`）。

### 命名规范

- 文件 `test_<模块>.py`（对应被测模块）；函数 `test_<对象>_<场景>`。
- 场景名描述行为而非实现：`test_parse_empty_input_raises` 而非 `test_parse_1`。
- 原生 `assert`，禁用 `self.assertEqual` 等 unittest 风格断言。

```python
from __future__ import annotations


def test_validate_non_empty_rejects_blank_string() -> None:
    """空白字符串应抛 ValueError（行为描述优先于实现细节）。"""
    ...
```

### 标记注册

`pytest.ini` 的 `markers` 字段注册标记，配合 `--strict-markers` 在拼写错误时直接失败。完整 `pytest.ini` 示例见下文「测试配置文件」章节。

要点：
- `slow`：慢测试（I/O、网络、集成），默认 `-m "not slow"` 跳过。
- `gui`：GUI 项目用，需 Qt 环境；CI 与无头环境用 `-m "not gui"` 隔离。
- 新增标记必须在此注册，否则 `--strict-markers` 直接报错。
- `--strict-config` 让解析阶段的配置错误也视为失败。

## 测试配置文件

coopie 模板将 pytest 配置放 `pytest.ini`，coverage 配置放 `.coveragerc`，便于 `copier update` 时独立更新工具链而不影响 `pyproject.toml` 中的项目元数据。

`pytest.ini`：

```ini
[pytest]
addopts = -ra --strict-markers --strict-config
asyncio_default_fixture_loop_scope = function
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    gui: marks tests requiring Qt/GUI (deselect with '-m "not gui"')
testpaths = tests
```

`.coveragerc`：

```ini
[run]
branch = true
concurrency = thread
omit = tests/*
source = fspack

[report]
fail_under = 95
exclude_lines =
    if TYPE_CHECKING:
    if __name__ == .__main__.:
    pragma: no cover
    raise NotImplementedError
show_missing = true
```

要点：
- `testpaths=["tests"]`：限定收集范围，避免误跑 src 下的同名文件。
- `asyncio_default_fixture_loop_scope="function"`：每个测试独立事件循环。
- `branch=true`：分支覆盖（if/else 两支都需走到），非仅行覆盖。
- `concurrency=["thread"]`：线程并发场景下覆盖率正确合并，避免数据丢失。

## Fixture 模式

### factory fixture：动态构造测试对象

需要多个变体时，fixture 返回工厂函数而非固定对象，避免共享状态污染。

```python
from __future__ import annotations

from typing import Callable

import pytest

from fspack import RequestBatch


@pytest.fixture
def make_batch() -> Callable[..., RequestBatch]:
    """批量请求工厂：每轮测试构造独立实例，参数有默认值便于按需覆盖。"""

    def _factory(endpoint: str = "https://api.example.com", items: list[str] | None = None) -> RequestBatch:
        """构造 RequestBatch，可选注入初始请求项。"""
        batch = RequestBatch(endpoint=endpoint)
        for item in items or []:
            batch.add(item)
        return batch

    return _factory


def test_batch_add_appends_item(make_batch: Callable[..., RequestBatch]) -> None:
    """add 应将请求追加到 items 列表末尾。"""
    batch = make_batch(items=["a", "b"])
    batch.add("c")
    assert batch.items == ["a", "b", "c"]
```

### conftest 与 fixture scope

`conftest.py` 中的 fixture 自动对同层及子层测试可见，无需导入。

```python
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """临时工作目录（基于 tmp_path，测试结束自动清理）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture(scope="session")
def shared_config() -> dict:
    """会话级只读配置（昂贵且不变的资源用 session scope）。"""
    return {"timeout": 30, "retries": 3}
```

scope 决策：
- `function`（默认）：每轮测试新建，最安全；绝大多数 fixture 用此。
- `module`：模块内共享、构造昂贵但只读 → 谨慎用。
- `session`：全进程共享、只读、构造极贵（如启动服务）→ 极少用。
- 跨 scope 依赖只能向上引用（function 可依赖 session，反之不可）。

### session scope：昂贵共享资源

会话级 fixture 用 `tmp_path_factory`（session 版 `tmp_path`）创建跨测试共享的临时目录。

```python
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def shared_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """会话级共享数据目录（全进程只创建一次，结束后统一清理）。"""
    return tmp_path_factory.mktemp("data")


def test_uses_shared_dir(shared_data_dir: Path) -> None:
    """function scope 测试可安全依赖 session scope fixture。"""
    assert shared_data_dir.is_dir()
```

### fixture composition：组合复用

fixture 可组合：一个 fixture 注入其他 fixture，形成层级，降低重复。

```python
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def config_file(tmp_workspace: Path) -> Path:
    """写入测试配置文件（依赖 tmp_workspace fixture 自动触发清理）。"""
    path = tmp_workspace / "config.toml"
    path.write_text('timeout = 30\n', encoding="utf-8")
    return path


def test_config_file_exists(config_file: Path) -> None:
    """组合 fixture 链：config_file 自动触发 tmp_workspace 创建。"""
    assert config_file.exists()
    assert config_file.read_text(encoding="utf-8").startswith("timeout")
```

要点：
- 组合降低重复：`config_file` 复用 `tmp_workspace` 的清理逻辑。
- autouse 仅全局必需时用（如重置全局状态）；普通 fixture 显式声明依赖，避免隐式副作用。

## 参数化测试

`@pytest.mark.parametrize` 覆盖多输入组合，避免重复测试函数。

```python
from __future__ import annotations

import pytest

from fspack import validate_non_empty


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("hello", "hello"),
        ("  trimmed  ", "  trimmed  "),
        ("中文内容", "中文内容"),
    ],
    ids=["ascii", "whitespace_preserved", "unicode"],
)
def test_validate_non_empty_accepts_valid(value: str, expected: str) -> None:
    """合法非空字符串应原样返回（ids 让失败用例可读）。"""
    assert validate_non_empty(value, "field") == expected


@pytest.mark.parametrize(
    "value",
    ["", "   ", "\t\n"],
    ids=["empty", "spaces", "tabs_newlines"],
)
def test_validate_non_empty_rejects_blank(value: str) -> None:
    """空白字符串应抛 ValueError，消息含字段名。"""
    with pytest.raises(ValueError, match="field"):
        validate_non_empty(value, "field")
```

要点：
- `ids` 让失败输出可读（`ascii` 优于 `value0`），描述场景而非值。
- 多参数用元组 `("value", "expected")` 并列；`ids` 可为字符串列表或 callable。
- 参数化覆盖：正常值、边界值、空值、Unicode；每组一个断言，避免组合爆炸。
- 参数列表过长时抽到模块级常量，保持测试函数聚焦。

### callable ids：自动生成用例名

参数含复杂对象时，`ids` 用 callable 从参数值生成可读标识。

```python
from __future__ import annotations

import pytest

from fspack import format_size


@pytest.mark.parametrize(
    "bytes_count",
    [0, 1024, 1536, 1048576],
    ids=lambda v: f"{v}B",
)
def test_format_size_units(bytes_count: int) -> None:
    """format_size 应按量级选择单位（callable ids 标注原始字节数）。"""
    result = format_size(bytes_count)
    if bytes_count >= 1048576:
        assert "MB" in result
    elif bytes_count >= 1024:
        assert "KB" in result
    else:
        assert "B" in result
```

## Mock 策略

优先级：`monkeypatch` > 内联 stub > `unittest.mock`（context manager 形式的 `patch()`）> `pytest-mock`。

**禁用**：`@patch` 装饰器、`mock.patch.object` 上下文、`pytest-mock` 的 `mocker` fixture。`monkeypatch` 在测试结束自动还原，无需手动 cleanup。

### monkeypatch.setattr：替换属性/方法

```python
from __future__ import annotations

from pathlib import Path

import pytest

from fspack import services


def test_load_config_reads_file_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """load_config 应只读取配置文件一次。"""
    call_count = 0

    def fake_read_text(self: Path, encoding: str = "utf-8") -> str:
        """记录调用次数的替身。"""
        nonlocal call_count
        call_count += 1
        return 'key = "value"\n'

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    # 测试结束自动还原，不影响其他测试

    services.load_config(tmp_path / "config.toml")
    assert call_count == 1
```

### monkeypatch.setenv / delenv：环境变量

```python
from __future__ import annotations

import pytest

from fspack import config


def test_load_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config 应读取环境变量覆盖默认值。"""
    monkeypatch.setenv("FSPACK_TIMEOUT", "60")
    cfg = config.load_from_env()
    assert cfg.timeout == 60


def test_load_config_falls_back_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量缺失时应回退到默认值。"""
    monkeypatch.delenv("FSPACK_TIMEOUT", raising=False)
    cfg = config.load_from_env()
    assert cfg.timeout == 30
```

### monkeypatch.chdir：切换工作目录

测试依赖当前工作目录的逻辑时用 `monkeypatch.chdir`，结束自动还原。

```python
from __future__ import annotations

from pathlib import Path

import pytest

from fspack import discover_configs


def test_discover_configs_from_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """discover_configs 应从当前目录扫描配置文件。"""
    (tmp_path / "config.toml").write_text('key = "v"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # 测试结束工作目录自动还原

    configs = discover_configs()
    assert any(p.name == "config.toml" for p in configs)
```

### monkeypatch 上下文管理：临时隔离

需要在测试中途切换 mock 或限定作用域时用 `with monkeypatch.context()`。

```python
from __future__ import annotations

from pathlib import Path

import pytest

from fspack import services


def test_partial_mock_with_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """context() 内的 mock 仅作用于 with 块，退出后还原。"""
    monkeypatch.setattr(services, "DEBUG", True)

    with monkeypatch.context() as m:
        m.setattr(Path, "exists", lambda self: False)
        # 此处 exists 恒返回 False
        assert not services.find_config(tmp_path)

    # 退出 with 后 Path.exists 已还原
    assert tmp_path.exists()
```

### 内联 stub：简单场景

被测对象通过依赖注入接收依赖时，直接传入假对象，无需全局 mock。

```python
from __future__ import annotations


class FakeStorage:
    """内存假存储，实现 Storage 接口。"""

    def __init__(self) -> None:
        """初始化内存字典。"""
        self._data: dict[str, bytes] = {}

    def read(self, key: str) -> bytes:
        """读取键对应字节。"""
        return self._data[key]

    def write(self, key: str, data: bytes) -> None:
        """写入键值。"""
        self._data[key] = data


def test_storage_roundtrip() -> None:
    """假存储写入后应能读回（依赖注入更易测试）。"""
    storage = FakeStorage()
    storage.write("k", b"v")
    assert storage.read("k") == b"v"
```

### unittest.mock（context manager 形式）

`monkeypatch` 无法覆盖时（如需断言调用次数、参数），用 `with patch()` 形式；**禁止** `@patch` 装饰器与 `mock.patch.object` 上下文。

```python
from __future__ import annotations

from unittest.mock import patch

from fspack import services


def test_fetch_calls_request_once() -> None:
    """fetch 应恰好调用 request 一次，参数为传入 URL。"""
    with patch.object(services, "request", return_value='{"ok": true}') as mock_req:
        result = services.fetch("https://example.com")
        assert result == {"ok": True}
        mock_req.assert_called_once_with("https://example.com")
```

## 异常与错误测试

`pytest.raises` 测试异常路径，**必填** `match=` 断言消息（仅断言类型会漏检消息变化）。

```python
from __future__ import annotations

import pytest

from fspack import parse_number


def test_parse_number_rejects_non_digit() -> None:
    """非数字字符串应抛 ValueError，消息含原始输入。"""
    with pytest.raises(ValueError, match="非数字: abc"):
        parse_number("abc")


def test_parse_number_rejects_negative() -> None:
    """负数应抛 ValueError，match 用正则匹配部分消息。"""
    with pytest.raises(ValueError, match=r"超出范围.*-1"):
        parse_number("-1")
```

要点：
- `match=` 用 `re.search`，支持正则；匹配消息子串即可，无需全文匹配。
- 多异常分支分别测：`test_x_rejects_a`、`test_x_rejects_b`，不要一个测试覆盖多分支。
- 自定义异常用 `from exc` 保留因果链时，测试应验证 `__cause__`。

```python
from __future__ import annotations

import pytest

from fspack import StorageError, load


def test_load_wraps_keyerror_as_storageerror() -> None:
    """底层 KeyError 应被包装为 StorageError 并保留因果链。"""
    with pytest.raises(StorageError, match="加载失败") as exc_info:
        load("missing")
    assert isinstance(exc_info.value.__cause__, KeyError)
```

## 覆盖率

### 配置（.coveragerc）

branch 覆盖必开，`fail_under` 阈值不得低于上一次值。

```ini
[run]
branch = true
concurrency = thread
omit = tests/*
source = fspack

[report]
fail_under = 95
exclude_lines =
    if TYPE_CHECKING:
    if __name__ == .__main__.:
    pragma: no cover
    raise NotImplementedError
show_missing = true
```

### 排除规则与 `# pragma: no cover`

`# pragma: no cover` 标记不可达或平台特定代码，仅用于真正无法测试的分支。

```python
from __future__ import annotations

import sys


def get_platform_name() -> str:
    """返回当前平台名。"""
    if sys.platform == "win32":  # pragma: no cover  # Linux/macOS CI 跑不到此分支
        return "Windows"
    return "Unix"


if __name__ == "__main__":  # pragma: no cover
    # 入口守卫，测试不直接执行
    get_platform_name()
```

要点：
- `branch=true`：if/else 两支都需走到，单纯行覆盖会漏掉未走的分支。
- 排除项克制使用：`# pragma: no cover` 仅用于不可达代码（`if __name__`、平台守卫、抽象方法中的 `raise NotImplementedError`）。
- `show_missing=true`：报告标注未覆盖行号，便于定位。
- 覆盖率下降即失败：`fail_under` 是硬门禁，不得调低绕过。

### 运行与报告

```bash
# 默认跑非慢测试 + 覆盖率
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95

# 全量（含慢测试）
uv run pytest --cov=fspack --cov-branch --cov-report=term-missing

# HTML 报告（本地排查未覆盖行）
uv run pytest --cov=fspack --cov-report=html
```

## pytest-qt GUI 测试

GUI 测试用 `@pytest.mark.gui` 标记，无头环境设 `QT_QPA_PLATFORM=offscreen`。

### conftest 配置

```python
from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """CI 或无头环境强制 offscreen，避免真实窗口阻塞测试。"""
    if os.environ.get("CI") or not os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
```

### 基础 GUI 测试

```python
from __future__ import annotations

import pytest

from fspack.windows.main_window import MainWindow


@pytest.mark.gui
def test_main_window_title(qtbot) -> None:
    """主窗口标题应正确设置。"""
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.windowTitle() == "fspack"


@pytest.mark.gui
def test_click_button_emits_signal(qtbot) -> None:
    """点击按钮应触发 submitted 信号。"""
    window = MainWindow()
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.submitted, timeout=1000):
        window.submit_button.click()
```

### waitSignal 与异步断言

```python
from __future__ import annotations

import pytest

from fspack.windows.main_window import MainWindow


@pytest.mark.gui
def test_async_task_updates_label(qtbot) -> None:
    """后台任务完成后应更新结果标签。"""
    window = MainWindow()
    qtbot.addWidget(window)
    window.start_task()
    qtbot.waitSignal(window.task_finished, timeout=5000)
    assert "完成" in window.result_label.text()
```

要点：
- `qtbot.addWidget(window)`：注册窗口，测试结束自动清理。
- `qtbot.waitSignal(signal, timeout=ms)`：等待信号发射，超时失败；避免 `sleep` 硬等待。
- `QT_QPA_PLATFORM=offscreen`：CI/无头环境强制 offscreen，本地保留真实渲染。
- GUI 测试默认不跑：`pytest -m "not gui"`，CI 中按需启用。
- 业务逻辑放 `core/` 纯 Python 模块，GUI 测试只验证信号槽连接与状态展示。

### 键盘交互模拟

`qtbot.keyClick` / `qtbot.keyClicks` 模拟按键，验证输入校验与快捷键。

```python
from __future__ import annotations

from pytestqt.qtbot import QtBot

from fspack.widgets.search_box import SearchBox


@pytest.mark.gui
def test_enter_key_submits_query(qtbot: QtBot) -> None:
    """在搜索框按回车应触发 submitted 信号并携带当前文本。"""
    box = SearchBox()
    qtbot.addWidget(box)
    qtbot.keyClicks(box.input_field, "关键词")
    with qtbot.waitSignal(box.submitted, timeout=1000) as blocker:
        qtbot.keyClick(box.input_field, 16777220)  # Qt.Key_Return
    assert blocker.args[0] == "关键词"
```

## 常见陷阱

1. **共享 fixture 状态污染**：`scope="module"` 的 fixture 返回可变对象，多个测试修改后互相污染。需可变时用 `function` scope 或返回副本。
2. **参数化 ids 缺失**：`parametrize` 不写 `ids` 时失败输出为 `value0/value1`，难定位。始终用语义化 `ids`。
3. **mock 不还原**：模块级 `patch()` 不带 `with` 会泄漏到其他测试。统一用 `monkeypatch`（自动还原）或 `with patch()` 形式。
4. **`pytest.raises` 不带 `match=`**：只断言异常类型会漏检消息变化。必须 `match=` 验证关键消息。
5. **断言实现而非行为**：`assert obj._internal_list == [...]` 耦合私有结构。改用公共接口 `assert obj.count() == 3`。
6. **覆盖率门禁调低绕过**：测试失败时降低 `fail_under` 而非补测试。覆盖率不得低于上一次值。
7. **滥用 `# pragma: no cover`**：标记可达代码以规避覆盖。仅用于不可达分支（`if __name__`、平台守卫）。
8. **GUI 测试硬等待**：`time.sleep(2)` 等信号导致偶发失败或拖慢。改用 `qtbot.waitSignal` 基于事件等待，超时即失败。
9. **autouse fixture 副作用**：全局 autouse 改环境变量影响非预期测试。仅全局必需时用，否则显式声明依赖。
10. **mock 优先级混乱**：用 `pytest-mock` 的 `mocker` 或 `@patch` 装饰器。统一 `monkeypatch` 优先，其次 `with patch()` 形式。
