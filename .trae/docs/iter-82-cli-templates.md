# iter-82：CLI 模板 6 项

## 需求清单

- [x] req-46：缓存环境变量配置 + 离线环境支持 + init 新建项目命令（iter-82 部分：CLI 模板）

## 迭代目标

在 `src/fspack/templates/registry.py` 注册 6 个 CLI 模板，覆盖最小示例、参数解析、
终端美化、HTTP 客户端与主流 CLI 框架，为 `fsp init --template <id>` 提供可用模板。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/fspack/templates/registry.py` | 新增 6 个 CLI 模板定义（helloworld/args/rich/requests/click/typer） |
| `tests/test_init_list.py` | 修复 iter-81 测试排序假设错误（helloworld 非第 1 个，按字母序 args 在前） |

## 关键决策与依据

### 通用 pyproject.toml 生成函数

**问题**：每个模板都需要 pyproject.toml，依赖列表不同，重复内容多。

**方案**：抽取 `_pyproject(dependencies)` 函数，根据依赖列表返回对应模板字符串。
无依赖时用 `_PYPROJECT_NO_DEPS`（省略 dependencies 字段），有依赖时用
`_PYPROJECT_WITH_DEPS` 并替换 `$dependencies_block`。

### CLI 模板内容设计

| id | 特点 | 依赖 |
|----|------|------|
| `helloworld` | 最小示例，print("hello, world") | 无 |
| `args` | argparse 参数解析，无第三方依赖 | 无 |
| `rich` | rich 彩色表格与 markup | rich |
| `requests` | HTTP GET 请求示例 | requests |
| `click` | click 命令组与子命令 | click |
| `typer` | typer 类型驱动 CLI | typer |

### iter-81 测试排序修复

**问题**：iter-81 测试假设 helloworld 是列表第 1 个，但 `list_templates` 按
`(category, id)` 字母序排序后 args 排在 helloworld 之前（a < h）。

**修复**：测试动态查询 helloworld 在列表中的位置，避免硬编码编号。

## 代码实现情况

- 6 个 CLI 模板入口脚本内容定义（`_HELLOWORLD_ENTRY` 等）
- `_pyproject()` 辅助函数动态生成 pyproject.toml
- 模板注册表 `_TEMPLATES` 添加 6 个 Template 元组项
- 模板按 (category, id) 字母序排序，`--list` 输出稳定

## 测试验证结果

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：All checks passed
- `uv run pytest tests/test_init_engine.py tests/test_init_list.py --no-cov`：36 passed
- 修复后 iter-81 的 2 个失败测试（排序假设错误）全部通过

## 遗留事项

无。

## 下一轮计划：iter-83 GUI 模板 6 项

添加 pyside2/pyside6/pyside2-qml/pyside6-qml/pyqt5/tkinter 共 6 个 GUI 模板：
- 每个模板含 pyproject.toml 与入口脚本
- QML 模板额外含 main.qml 文件
- 入口脚本 import PySide2/PySide6/PyQt5/tkinter 触发 `infer_app_type` 自动识别为 GUI
- 新增 `tests/test_init_templates.py` 验证模板元数据、渲染、语法正确性与 app_type 推断
