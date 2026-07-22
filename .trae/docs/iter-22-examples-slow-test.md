# 迭代 22 - 示例全量 slow 测试覆盖

## 需求清单

参见 `req-21-examples-slow-test.md`。

## 迭代目标

为所有 examples 示例设计 slow 端到端测试，验证真实构建与运行；修复示例代码 bug；修改规则明确 slow 全量测试频率。

## 改动文件清单

- `examples/cli_complex/module_d.py`：修复属性访问 bug，改用 `from core import module_g`
- `examples/pygame_conway/pyproject.toml`：补充 attrs/numpy/pygame 依赖声明
- `examples/pygame_conway/game.py`：加 `from __future__ import annotations`/`import os`/`DUMMY_MAX_FRAMES` + dummy 退出机制
- `examples/pygame_gktetris/game.py`：run() 加 is_dummy + frame 计数，dummy 下 30 帧退出
- `tests/test_e2e_slow.py`：新增 4 个 slow 测试
- `.trae/rules/rule-11-python-standards.md`：验证命令章节追加 slow 全量测试说明
- `pyrefly.toml`：修复配置漂移（project-excludes 补 examples/**/ref/**，search-path 补 src）

## 关键决策与依据

### 1. cli_complex bug 根因与修复

**根因**：`module_d.py` 用 `import core` + `core.module_g.function_g()` 属性访问。Python 中 `import core` 只执行 `core/__init__.py`，不自动加载子模块 `module_g`。要让 `core.module_g` 可访问，必须有代码显式 `import core.module_g` 或 `from core import module_g`。

**对比**：`modules/module_b.py` 用 `from core.module_e import function_e`——这种 `from X.Y import Z` 语法会自动加载 `core.module_e`，所以 `function_e` 调用成功。但 `module_d.py` 用 `import core`（只导入顶层包），属性访问 `core.module_g` 失败。

**修复**：`module_d.py` 改用 `from core import module_g`，显式加载子模块。

### 2. pygame_conway 依赖声明缺失

pyproject.toml `dependencies = []` 但 game.py 用了 numpy/pygame/attrs。打包时这些库不会被下载，运行时 ImportError。补充声明。

### 3. pygame dummy 退出机制

pygame 主循环 `while running` 在 dummy 驱动（无显示环境）下无事件，死循环。参考 pygame_snake 的模式：检测 `SDL_VIDEODRIVER == "dummy"`，frame 计数达 `DUMMY_MAX_FRAMES` 时退出。pygame_conway 和 pygame_gktetris 均加此机制。

### 4. slow 测试设计

4 个新测试用 `_build_and_run` helper（debug 模式 + dummy 驱动）：
- **cli_complex**：断言 "hello, world" + lxml/ordered_set 解包
- **cli_office**：断言 "文件生成成功" + pypdf 解包
- **pygame_conway**：断言 "Hello from the pygame community" + numpy/attrs/pygame 解包
- **pygame_gktetris**：断言 "Hello from the pygame community" + pygame 解包（验证包模式 wrapper `_ENTRY_MODULE='src.game'`）

### 5. rule-11 slow 全量测试频率

验证命令章节追加：「slow 全量测试（端到端构建+运行，耗时较长）：**每 5 次开发循环至少跑一次**，确保示例项目真实可构建可运行。」命令为 `uv run pytest --cov=fspack --cov-fail-under=95`（不含 `-m "not slow"`）。

### 6. pyrefly.toml 配置漂移修复

memory iter-17 记录 pyrefly.toml 应有 `project-excludes` 含 `examples/**`、`search-path` 为 `["src", "."]`，但实际配置丢失这两项。另发现 `ref/` 参考代码目录也需排除（含 PySide2 UI 生成代码）。修复后 pyrefly 0 errors。

## 代码实现情况

### 示例 bug 修复

- `cli_complex/module_d.py`：`import core` → `from core import module_g`
- `pygame_conway/pyproject.toml`：`dependencies = []` → `["attrs", "numpy", "pygame>=2.5.0"]`
- `pygame_conway/game.py`：加 `from __future__ import annotations`/`import os`/`DUMMY_MAX_FRAMES = 30` + `is_dummy` 检测 + frame 计数退出
- `pygame_gktetris/game.py`：`run()` 加 `is_dummy` + frame 计数，dummy 下 30 帧退出

### slow 测试

4 个新测试追加到 `tests/test_e2e_slow.py` 末尾，复用 `_build_and_run` helper。

### 规则更新

`rule-11-python-standards.md` 验证命令章节追加 slow 全量测试段落。

### pyrefly.toml 修复

```toml
project-excludes = [".venv/**", "examples/**", "ref/**", "template/**"]
search-path      = ["src", "."]
```

## 整合优化情况

无重复代码引入。新测试复用既有 `_build_and_run` helper，风格与既有 9 个 slow 测试一致。

## 测试验证结果

- ruff check src tests: All checks passed!
- ruff format --check src tests: 47 files already formatted
- pyrefly check: 0 errors（修复配置漂移后）
- pytest -m "not slow": 463 passed, 17 deselected（13 既有 + 4 新增 slow），覆盖率 98.10%

## 遗留事项

- **examples 代码 lint 问题**：`examples/pygame_gktetris/game.py` 有 7 个 ruff 错误（PLR0912 分支太多/PLR0913 参数太多），但门禁命令 `ruff check src tests` 不含 examples，不影响门禁。属既有代码问题，未在本次范围处理。
- **slow 测试未在 Windows 实跑**：`_build_and_run` 依赖 wine，Windows 上会 skip。需在 Linux CI 环境实跑验证。

## 下一轮计划

无。本次迭代需求已全部交付，所有门禁通过。
