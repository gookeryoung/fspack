# iter-84：游戏/科学/Web 模板 8 项

## 需求清单

- [x] req-46：缓存环境变量配置 + 离线环境支持 + init 新建项目命令（iter-84 部分：游戏/科学/Web/配置模板）

## 迭代目标

在 `src/fspack/templates/registry.py` 注册 8 个模板，覆盖游戏开发、科学计算、Web 服务
与配置示例领域，使模板总数达 20，满足"不少于 20 项"要求。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/fspack/templates/registry.py` | 新增 8 个模板（pygame/snake/matplotlib/numpy/scipy/flask/fastapi/pyinstaller） |
| `tests/test_init_templates.py` | 新增 iter-84 模板测试 40 项（注册/依赖/语法/app_type 推断/init_project） |

## 关键决策与依据

### app_type 自动推断：pygame/matplotlib → GUI，numpy/scipy/flask/fastapi → CLI

`_GUI_HINTS = {tkinter, PySide2, PySide6, PyQt5, PyQt6, matplotlib, wx, win32gui, pygame}`：
- pygame/snake/matplotlib 的入口脚本 import pygame/matplotlib，自动推断为 GUI（关闭控制台窗口）
- numpy/scipy/flask/fastapi 不在 `_GUI_HINTS` 中，推断为 CLI（保留控制台，便于看输出）

模板通过 `app_type` 字段声明预期类型，同时入口脚本 import 触发 `infer_app_type` 验证一致。

### pyinstaller 模板：完整 [tool.fspack] 配置示例

此模板独立定义 `_PYINSTALLER_PYPROJECT`（不复用 `_pyproject()`），展示所有可用配置项：
- 构建默认值（nuitka/pyc_strip/pyc_optimize/no_site/no_pyc/no_stdlib_trim/ccache）
- 图标（icon，注释形式）
- 排除目录（exclude = ["tests", "docs", ".github"]）
- 私有 PyPI（extra-index-urls/find-links，注释形式）
- Nuitka 编译包（nuitka_packages，注释形式）
- 多入口声明（[tool.fspack.entries]，注释形式）

入口脚本为简单 hello world，重点在 pyproject.toml 配置示例，适合从 PyInstaller 迁移的用户参考。

### snake 模板：完整可玩游戏

贪吃蛇模板包含完整游戏逻辑：蛇头移动、方向键控制、食物生成、碰撞检测、得分。
`_spawn_food` 函数放在 `main` 之前（Python 运行时查找，但代码顺序更清晰）。

### matplotlib 模板依赖声明

matplotlib 安装时自动安装 numpy，模板仅声明 matplotlib 依赖。入口脚本 `import numpy as np`
用于生成数据，`import matplotlib.pyplot as plt` 触发 GUI 推断。

## 代码实现情况

### 新增入口脚本

- `_PYGAME_ENTRY`：Pygame 窗口与事件循环骨架（ESC 退出）
- `_SNAKE_ENTRY`：贪吃蛇完整游戏（方向键控制，碰撞结束）
- `_MATPLOTLIB_ENTRY`：正弦波图表（numpy 生成数据，plt.show 显示）
- `_NUMPY_ENTRY`：数组运算与统计（矩阵乘法、均值、标准差、转置）
- `_SCIPY_ENTRY`：线性方程组求解（linalg.solve + 行列式）
- `_FLASK_ENTRY`：Flask 路由与 JSON 响应（/ 与 /hello/<name>）
- `_FASTAPI_ENTRY`：FastAPI 路由与 uvicorn 启动
- `_PYINSTALLER_ENTRY`：简单 hello world（配置示例入口）
- `_PYINSTALLER_PYPROJECT`：完整 [tool.fspack] 配置示例

### 模板注册

`_TEMPLATES` 添加 8 个 Template 项，分类：game(2)/sci(3)/web(2)/config(1)。

## 测试验证结果

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：80 files already formatted
- `uv run pyrefly check`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1249 passed, 1 skipped, 30 deselected，覆盖率 98.46%
- 新增 iter-84 测试 40 项全部通过：
  - 8 个模板注册完整性
  - 依赖声明参数化验证
  - 入口脚本 AST 语法正确性
  - app_type 推断（pygame/snake/matplotlib→gui，numpy/scipy/flask/fastapi/pyinstaller→cli）
  - pyinstaller 模板含 [tool.fspack] 配置
  - init_project 创建 pygame/snake/matplotlib/numpy/flask/fastapi/pyinstaller 项目结构正确
  - 模板总数 >= 20（满足需求）

## 遗留事项

无。

## 下一轮计划：iter-85 多入口/完整配置模板 2 项 + 端到端测试 + 文档收尾

- `multi-entry` 模板：多入口项目（CLI + GUI），声明 [tool.fspack.entries]
- `full-config` 模板：完整 [tool.fspack] 配置示例（含 icon/exclude/entries 等）
- `tests/test_init_e2e.py`：init → build 端到端测试（helloworld 模板生成后立即构建）
- README.md 新增"快速开始：fsp init"章节
- 文档收尾，模板总数达 22

完成后 10 轮迭代计划全部交付。
