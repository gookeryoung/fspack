# iter-83：GUI 模板 6 项

## 需求清单

- [x] req-46：缓存环境变量配置 + 离线环境支持 + init 新建项目命令（iter-83 部分：GUI 模板）

## 迭代目标

在 `src/fspack/templates/registry.py` 注册 6 个 GUI 模板，覆盖 PySide2/PySide6/PyQt5
三大 Qt 框架、QML 声明式界面与 tkinter 标准库，使 `fsp init --template pyside2` 等
命令能生成可直接打包的 GUI 项目骨架。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/fspack/templates/registry.py` | 新增 6 个 GUI 模板定义（pyside2/pyside6/pyside2-qml/pyside6-qml/pyqt5/tkinter） |
| `tests/test_init_templates.py` | 新增：GUI 模板元数据、渲染、语法正确性、app_type 推断、init_project 共 40 个测试 |

## 关键决策与依据

### GUI 类型自动推断（无需显式声明 app_type）

**问题**：GUI 应用打包时需关闭控制台窗口，是否要在模板的 `[tool.fspack]` 显式声明 `app_type`？

**决策**：不需要。`fspack.config.infer_app_type` 会扫描入口脚本的 import，若 import 了
`_GUI_HINTS`（PySide2/PySide6/PyQt5/PyQt6/tkinter/matplotlib/wx/win32gui/pygame）中的任一
顶层模块，自动推断为 GUI 类型。模板入口脚本 import 对应库即可触发推断，保持模板最简。

### PySide2 vs PySide6 的 exec 差异

PySide2 用 `app.exec_()`（带下划线，兼容 PyQt4 命名），PySide6 用 `app.exec()`
（无下划线，对齐 Qt6 原生命名）。两个模板的入口脚本各自保持框架原生风格，
不强行统一，避免用户复制代码后困惑。

### QML 模板设计

QML 模板包含三个文件：
- `pyproject.toml`（声明 PySide2/PySide6 依赖）
- 入口脚本：`QGuiApplication` + `QQmlApplicationEngine.load(main.qml)`
- `main.qml`：`ApplicationWindow` + `Button` 点击计数示例

QML 文件用 `// $project_name QML 主窗口` 注释注入项目名，JavaScript 逻辑用
正则 `/\d+/` 提取当前计数并递增，演示 QML 与 Python 的分工。

### tkinter 模板无第三方依赖

tkinter 是 Python 标准库，但 Windows embed 发行版不含 tkinter，fspack 的
`TkinterBundler` 会单独打包 tkinter 相关文件。模板的 pyproject.toml 用
`_pyproject()`（无依赖版本），入口脚本 `import tkinter` 触发 GUI 推断。

## 代码实现情况

### 新增 GUI 模板入口脚本

- `_PYSIDE2_ENTRY` / `_PYSIDE6_ENTRY`：QMainWindow + QPushButton 点击计数
- `_PYSIDE2_QML_ENTRY` / `_PYSIDE6_QML_ENTRY`：QQmlApplicationEngine 加载 main.qml
- `_PYQT5_ENTRY`：QMainWindow + QPushButton（与 PySide2 结构一致，import PyQt5）
- `_TKINTER_ENTRY`：tk.Tk + ttk.Button 点击计数
- `_MAIN_QML`：ApplicationWindow + ColumnLayout + Label + Button

### 模板注册

`_TEMPLATES` 元组添加 6 个 GUI Template 项，每个声明：
- `category="gui"`、`app_type="gui"`
- 依赖：PySide2/PySide6/PyQt5（tkinter 无依赖）
- 文件列表：pyproject.toml + 入口脚本（QML 模板额外含 main.qml）

## 测试验证结果

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：All checks passed
- `uv run pyrefly check src/fspack/templates tests/test_init_templates.py`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1209 passed, 1 skipped, 30 deselected，覆盖率 98.46%
- 新增 `tests/test_init_templates.py` 40 个测试全部通过：
  - GUI 模板注册完整性（6 个 ID 都可查询）
  - 模板元数据（category/app_type/dependencies）参数化验证
  - QML 模板含 main.qml 文件
  - 模板渲染后文件树正确（pyproject.toml + 入口脚本 + QML）
  - 入口脚本能被 AST 解析（语法正确）
  - `infer_app_type` 正确识别为 GUI 类型
  - `init_project` 创建 pyside2/tkinter/pyside2-qml/pyside6 项目结构正确
  - 模板总数 >= 12（6 CLI + 6 GUI）

## 整合优化情况

- 修复 iter-81 测试排序假设错误（动态查询模板位置，避免硬编码编号）
- `templates/registry.py` 覆盖率 100%

## 遗留事项

无。

## 下一轮计划：iter-84 游戏/科学/Web 模板 8 项

添加 8 个模板覆盖游戏、科学计算与 Web 服务领域：
- `pygame`：Pygame 游戏骨架
- `snake`：贪吃蛇完整游戏
- `matplotlib`：Matplotlib 图表
- `numpy`：NumPy 数值计算
- `scipy`：SciPy 科学计算
- `flask`：Flask Web 服务
- `fastapi`：FastAPI Web 服务
- `pyinstaller`：PyInstaller 兼容配置示例

完成后模板总数达 20，满足"不少于 20 项"要求。后续 iter-85 补充多入口/完整配置
模板 2 项 + init→build 端到端测试 + 文档收尾。
