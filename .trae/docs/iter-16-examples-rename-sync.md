# 迭代 16：examples 重命名同步

## 迭代目标

用户手动调整了 examples 目录：删除 tk_basic、重命名 pygame_demo→pygame_cli、
pyqt5_app→pyqt5_cli（含入口文件与 pyproject.toml name 字段）。本迭代同步完善项目
中引用旧名的位置，保持文档与代码一致。

## 用户调整内容

- **删除 tk_basic**：iter-15 文档化 tkinter 限制后，用户决定直接删除该示例
- **pygame_demo → pygame_cli**：入口 pygamedemo.py → pygame_cli.py，name 字段同步
- **pyqt5_app → pyqt5_cli**：入口 pyqt5app.py → pyqt5_cli.py，name 字段同步，新增 uv.lock

命名统一为 `*_cli` 风格（pygame_cli、pyqt5_cli）。pyside2_app 保留（真正的 GUI 应用），
pygame_snake 保留（贪吃蛇游戏示例）。

## 改动文件清单

### 文档同步

- `README.md`：
  - 示例表格删除 tk_basic 行，pyqt5_app→pyqt5_cli、pygame_demo→pygame_cli
  - 已知限制章节的 `examples/pyqt5_app` 引用更新为 `examples/pyqt5_cli`

### 测试同步

- `tests/test_e2e_slow.py`：
  - `test_build_and_run_pygamedemo` → `test_build_and_run_pygame_cli`
    （目录名、docstring、断言路径同步）
  - `test_build_and_run_pyqt5app` → `test_build_and_run_pyqt5_cli`
    （目录名、exe 名、docstring、copytree 源路径同步）

### 记忆更新

- `project_memory.md`：示例目录整合章节与多版本慢测试矩阵章节更新引用

## 关键决策

### 1. 不改写历史迭代文档

iter-13/14/15 文档中仍引用旧名（tk_basic、pygame_demo、pyqt5_app），这些是历史记录，
不改写。当前状态以 README.md 与 examples/ 目录为准。

### 2. 保留 pyside2_app 命名

pyside2_app 是真正的 GUI 应用（PySide2 在 _GUI_HINTS 中，被识别为 GUI 类型），
不改为 pyside2_cli。用户的 `*_cli` 重命名仅针对 pygame（CLI 类型）和 pyqt5
（虽是 GUI 类型但用户统一命名）。

### 3. tk_basic 删除而非保留限制说明

iter-15 文档化了 tkinter 限制，用户选择直接删除 tk_basic 示例。README 已知限制章节
仍保留 tkinter 限制说明（限制本身存在，只是不再有示例）。

## 验证结果

### 门禁

- `ruff check`：All checks passed
- `ruff format --check`：45 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack`：298 passed，覆盖率 98.42%

## 遗留事项

无。examples 目录现状：cli_complex、cli_helloworld、cli_office、cli_tool、gui_calc、
pygame_cli、pygame_snake、pyqt5_cli、pyside2_app、web_app（共 10 个）。
