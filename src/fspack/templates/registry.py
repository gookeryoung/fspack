"""模板数据结构与注册表.

定义 :class:`Template`/``TemplateFile`` 数据结构与模板注册表。模板用
``frozen=True`` 的 dataclass 描述，便于作为不可变值传递。

模板分类（``category``）：

- ``cli`` — 命令行工具（iter-82 填充 6 项）
- ``gui`` — 桌面 GUI 应用（iter-83 填充 6 项）
- ``game`` — 游戏开发（iter-84 填充 2 项）
- ``sci`` — 科学计算（iter-84 填充 3 项）
- ``web`` — Web 服务（iter-84 填充 2 项）
- ``config`` — 配置示例（iter-84 填充 1 项，iter-85 填充 2 项）

模板注册表 :data:`_TEMPLATES` 是模块级私有 tuple，通过 :func:`list_templates`
与 :func:`get_template` 公开查询。

注意：模板内容用 :class:`string.Template` 渲染，``$variable``/``${variable}``
是占位符。代码中字面量 ``$`` 需用 ``$$`` 转义。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Template", "TemplateFile", "get_template", "list_templates"]


@dataclass(frozen=True)
class TemplateFile:
    """模板文件：相对路径 + 内容模板（``string.Template`` 语法）.

    ``rel_path`` 支持占位符（如 ``$project_name/main.py``），渲染时与
    ``content`` 一并替换。``content`` 中的 ``$variable`` 与 ``${variable}``
    占位符按 :class:`string.Template` 规则替换。

    :param rel_path: 模板内相对路径（POSIX 风格，渲染后转 ``Path``）
    :param content: 文件内容模板
    """

    rel_path: str
    content: str


@dataclass(frozen=True)
class Template:
    """项目模板：id + 名称 + 描述 + 分类 + 文件列表 + 依赖.

    :param id: 模板唯一标识（如 ``helloworld``/``pyside2``）
    :param name: 显示名称（如 ``Hello World``）
    :param description: 简短描述（一行）
    :param category: 分类（cli/gui/game/sci/web/config）
    :param files: 模板文件元组
    :param dependencies: 项目依赖元组（写入 ``pyproject.toml`` 的 ``dependencies``）
    :param app_type: 应用类型（cli/gui/web），影响 loader 编译与控制台窗口
    :param py_version: 推荐 Python 版本（如 ``3.11.9``），``None`` 用默认
    :param extra_config: 额外 ``[tool.fspack]`` 配置行（如 ``icon = "assets/app.ico"``）
    """

    id: str
    name: str
    description: str
    category: str
    files: tuple[TemplateFile, ...]
    dependencies: tuple[str, ...] = ()
    app_type: str = "cli"
    py_version: str | None = None
    extra_config: str = ""


# 通用 pyproject.toml 模板：dependencies 为空时省略 dependencies 字段
_PYPROJECT_NO_DEPS = """[project]
name = "$project_name"
version = "0.1.0"
description = "$description"
requires-python = "$requires_python"
"""

_PYPROJECT_WITH_DEPS = """[project]
name = "$project_name"
version = "0.1.0"
description = "$description"
requires-python = "$requires_python"
dependencies = [
$dependencies_block
]
"""


def _format_dependencies_block(dependencies: tuple[str, ...]) -> str:
    """格式化依赖列表为 TOML 数组元素（每行一个，缩进 4 空格）."""
    return "\n".join(f'    "{dep}",' for dep in dependencies)


def _pyproject(dependencies: tuple[str, ...] = (), requires_python: str = ">=3.8") -> str:
    """根据依赖列表与 Python 版本约束返回 pyproject.toml 内容模板.

    Args:
        dependencies: 依赖包名列表（含版本约束），空元组省略 dependencies 字段。
        requires_python: ``requires-python`` 约束字符串，默认 ``">=3.8"``。
            PySide2 不支持 Python 3.11+，其模板应传 ``">=3.8,<3.11"``。
    """
    if not dependencies:
        return _PYPROJECT_NO_DEPS.replace("$requires_python", requires_python)
    return _PYPROJECT_WITH_DEPS.replace("$requires_python", requires_python).replace(
        "$dependencies_block", _format_dependencies_block(dependencies)
    )


# ---- CLI 模板（iter-82）----

_HELLOWORLD_ENTRY = '''"""$project_name 入口."""


def main() -> None:
    """打印问候语."""
    print("hello, world")


if __name__ == "__main__":
    main()
'''

_ARGS_ENTRY = '''"""$project_name 入口：argparse 命令行参数示例."""

import argparse


def main() -> None:
    """解析命令行参数并打印问候."""
    parser = argparse.ArgumentParser(prog="$project_name", description="$description")
    parser.add_argument("name", help="要问候的名字")
    parser.add_argument("-c", "--count", type=int, default=1, help="重复次数")
    args = parser.parse_args()

    for _ in range(args.count):
        print(f"hello, {args.name}!")


if __name__ == "__main__":
    main()
'''

_RICH_ENTRY = '''"""$project_name 入口：rich 终端美化示例."""

from rich.console import Console
from rich.table import Table


def main() -> None:
    """用 rich 输出彩色表格."""
    console = Console()
    console.print("[bold green]$project_name[/bold green]")

    table = Table(title="项目信息")
    table.add_column("字段", style="cyan", no_wrap=True)
    table.add_column("值", style="magenta")
    table.add_row("name", "$project_name")
    table.add_row("version", "0.1.0")
    table.add_row("type", "rich CLI")
    console.print(table)


if __name__ == "__main__":
    main()
'''

_REQUESTS_ENTRY = '''"""$project_name 入口：requests HTTP 请求示例."""

import requests


def main() -> None:
    """发送 HTTP GET 请求并打印响应."""
    print("$project_name: 发送 HTTP 请求...")
    response = requests.get("https://httpbin.org/get", params={"name": "$project_name"}, timeout=10)
    response.raise_for_status()

    data = response.json()
    print(f"请求 URL: {data['url']}")
    print(f"响应参数: {data['args']}")


if __name__ == "__main__":
    main()
'''

_CLICK_ENTRY = '''"""$project_name 入口：click CLI 框架示例."""

import click


@click.group()
def cli() -> None:
    """$project_name 命令行工具."""


@cli.command()
@click.argument("name")
@click.option("-c", "--count", default=1, help="重复次数")
def hello(name: str, count: int) -> None:
    """问候指定名字."""
    for _ in range(count):
        click.echo(f"hello, {name}!")


@cli.command()
def version() -> None:
    """显示版本号."""
    click.echo("$project_name v0.1.0")


if __name__ == "__main__":
    cli()
'''

_TYPER_ENTRY = '''"""$project_name 入口：typer CLI 框架示例."""

import typer

app = typer.Typer(help="$project_name 命令行工具")


@app.command()
def hello(
    name: str = typer.Argument("world", help="要问候的名字"),
    count: int = typer.Option(1, "-c", "--count", help="重复次数"),
) -> None:
    """问候指定名字."""
    for _ in range(count):
        typer.echo(f"hello, {name}!")


@app.command()
def version() -> None:
    """显示版本号."""
    typer.echo("$project_name v0.1.0")


if __name__ == "__main__":
    app()
'''


# ---- GUI 模板（iter-83）----
#
# GUI 类型由 fspack.config.infer_app_type 根据 import 自动推断：
# PySide2/PySide6/PyQt5/tkinter 在 _GUI_HINTS 中，入口脚本 import 任一即识别为 GUI，
# 打包时自动关闭控制台窗口。模板无需在 [tool.fspack] 显式声明 app_type。
#
# PySide2/PySide6/PyQt5 的 widgets 入口共享 MainWindow 骨架，PySide2/PySide6 的
# QML 入口共享 QQmlApplicationEngine 骨架，仅在框架名与 ``exec``/``exec_`` 方法
# 上存在差异。用 ``_qt_widgets_entry``/``_qt_qml_entry`` 工厂函数组合生成，
# 避免 5 段近乎一致的模板源码重复维护。


def _qt_widgets_entry(framework: str, exec_method: str = "exec_") -> str:
    """生成 PySide2/PySide6/PyQt5 widgets 入口模板（MainWindow 点击计数）.

    三个框架的 widgets 入口共享 MainWindow 类骨架与按钮计数逻辑，仅在框架名
    与事件循环方法上存在差异。``exec_`` 是 PySide2/PyQt5 的旧 API（``exec`` 在
    Python 2 是保留字故加下划线），PySide6 改用 ``exec``。

    :param framework: 框架名（``PySide2``/``PySide6``/``PyQt5``）
    :param exec_method: 事件循环方法名（``exec_`` 或 ``exec``）
    :return: 入口脚本模板（含 ``$project_name`` 占位符，未渲染）
    """
    return f'''"""$project_name 入口：{framework} 主窗口示例."""

import sys

from {framework}.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget


class MainWindow(QMainWindow):
    """主窗口：点击按钮计数."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("$project_name")
        self.resize(400, 200)

        self._count = 0
        central = QWidget(self)
        layout = QVBoxLayout(central)

        self.label = QLabel("点击按钮 0 次", central)
        self.button = QPushButton("点我", central)
        self.button.clicked.connect(self._on_click)

        layout.addWidget(self.label)
        layout.addWidget(self.button)
        self.setCentralWidget(central)

    def _on_click(self) -> None:
        self._count += 1
        self.label.setText(f"点击按钮 {{self._count}} 次")


def main() -> None:
    """启动 {framework} 应用."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.{exec_method}())


if __name__ == "__main__":
    main()
'''


def _qt_qml_entry(framework: str, exec_method: str = "exec_") -> str:
    """生成 PySide2/PySide6 + QML 入口模板（QQmlApplicationEngine 加载 main.qml）.

    :param framework: 框架名（``PySide2``/``PySide6``）
    :param exec_method: 事件循环方法名（``exec_`` 或 ``exec``）
    :return: 入口脚本模板（含 ``$project_name`` 占位符，未渲染）
    """
    return f'''"""$project_name 入口：{framework} + QML 示例."""

import sys
from pathlib import Path

from {framework}.QtGui import QGuiApplication
from {framework}.QtQml import QQmlApplicationEngine


def main() -> None:
    """加载 main.qml 并启动 {framework} 应用."""
    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    qml_path = Path(__file__).parent / "main.qml"
    engine.load(str(qml_path))
    if not engine.rootObjects():
        sys.exit(1)
    sys.exit(app.{exec_method}())


if __name__ == "__main__":
    main()
'''


_PYSIDE2_ENTRY = _qt_widgets_entry("PySide2")
_PYSIDE6_ENTRY = _qt_widgets_entry("PySide6", exec_method="exec")
_PYSIDE2_QML_ENTRY = _qt_qml_entry("PySide2")
_PYSIDE6_QML_ENTRY = _qt_qml_entry("PySide6", exec_method="exec")

_MAIN_QML = """// $project_name QML 主窗口
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ApplicationWindow {
    title: "$project_name"
    width: 400
    height: 200
    visible: true

    ColumnLayout {
        anchors.centerIn: parent

        Label {
            id: counter
            text: "点击按钮 0 次"
            Layout.alignment: Qt.AlignHCenter
            font.pixelSize: 16
        }

        Button {
            text: "点我"
            Layout.alignment: Qt.AlignHCenter
            onClicked: {
                var n = parseInt(counter.text.match(/\\d+/)[0]) + 1
                counter.text = "点击按钮 " + n + " 次"
            }
        }
    }
}
"""

_PYQT5_ENTRY = _qt_widgets_entry("PyQt5")

_TKINTER_ENTRY = '''"""$project_name 入口：tkinter 标准库 GUI 示例."""

import tkinter as tk
from tkinter import ttk


class MainWindow(tk.Tk):
    """主窗口：点击按钮计数."""

    def __init__(self) -> None:
        super().__init__()
        self.title("$project_name")
        self.geometry("400x200")
        self._count = 0

        self.label = ttk.Label(self, text="点击按钮 0 次", font=("Segoe UI", 12))
        self.label.pack(pady=30)

        self.button = ttk.Button(self, text="点我", command=self._on_click)
        self.button.pack()

    def _on_click(self) -> None:
        self._count += 1
        self.label.config(text=f"点击按钮 {self._count} 次")


def main() -> None:
    """启动 tkinter 应用."""
    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
'''


# ---- 游戏/科学/Web 模板（iter-84）----
#
# pygame/matplotlib 在 _GUI_HINTS 中，入口脚本 import 任一即识别为 GUI（关闭控制台）。
# numpy/scipy/flask/fastapi 不在 _GUI_HINTS 中，识别为 CLI（保留控制台，便于看输出）。

_PYGAME_ENTRY = '''"""$project_name 入口：Pygame 游戏骨架示例."""

import sys

import pygame


def main() -> None:
    """启动 Pygame 游戏窗口（ESC 或关闭窗口退出）."""
    pygame.init()
    screen = pygame.display.set_mode((640, 480))
    pygame.display.set_caption("$project_name")
    clock = pygame.time.Clock()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        screen.fill((30, 30, 30))
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
'''

_SNAKE_ENTRY = '''"""$project_name 入口：贪吃蛇完整游戏."""

import random
import sys

import pygame

CELL = 20
COLS, ROWS = 32, 24
WIDTH, HEIGHT = COLS * CELL, ROWS * CELL


def _spawn_food(snake: list[tuple[int, int]]) -> tuple[int, int]:
    """随机生成食物位置（避开蛇身）."""
    while True:
        food = (random.randint(0, COLS - 1), random.randint(0, ROWS - 1))
        if food not in snake:
            return food


def main() -> None:
    """启动贪吃蛇游戏（方向键控制，ESC 退出）."""
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("$project_name - 贪吃蛇")
    clock = pygame.time.Clock()

    snake = [(COLS // 2, ROWS // 2)]
    direction = (1, 0)
    food = _spawn_food(snake)
    score = 0

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_UP and direction != (0, 1):
                    direction = (0, -1)
                elif event.key == pygame.K_DOWN and direction != (0, -1):
                    direction = (0, 1)
                elif event.key == pygame.K_LEFT and direction != (1, 0):
                    direction = (-1, 0)
                elif event.key == pygame.K_RIGHT and direction != (-1, 0):
                    direction = (1, 0)

        head = (snake[0][0] + direction[0], snake[0][1] + direction[1])
        if head[0] < 0 or head[0] >= COLS or head[1] < 0 or head[1] >= ROWS or head in snake:
            running = False
            continue

        snake.insert(0, head)
        if head == food:
            score += 10
            food = _spawn_food(snake)
        else:
            snake.pop()

        screen.fill((15, 15, 15))
        for seg in snake:
            pygame.draw.rect(screen, (50, 200, 50), (seg[0] * CELL, seg[1] * CELL, CELL, CELL))
        pygame.draw.rect(screen, (200, 50, 50), (food[0] * CELL, food[1] * CELL, CELL, CELL))
        pygame.display.flip()
        clock.tick(10)

    pygame.quit()
    print(f"游戏结束，得分: {score}")
    sys.exit(0)


if __name__ == "__main__":
    main()
'''

_MATPLOTLIB_ENTRY = '''"""$project_name 入口：Matplotlib 图表示例."""

import numpy as np
import matplotlib.pyplot as plt


def main() -> None:
    """绘制正弦波图表并显示."""
    x = np.linspace(0, 2 * np.pi, 200)
    y = np.sin(x)

    plt.figure(figsize=(8, 5))
    plt.plot(x, y, label="sin(x)", color="steelblue")
    plt.title("$project_name")
    plt.xlabel("x")
    plt.ylabel("sin(x)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
'''

_NUMPY_ENTRY = '''"""$project_name 入口：NumPy 数值计算示例."""

import numpy as np


def main() -> None:
    """演示 NumPy 数组运算与统计."""
    a = np.array([[1, 2, 3], [4, 5, 6]])
    b = np.array([[1, 0], [0, 1], [2, 3]])

    print("$project_name: NumPy 数值计算")
    print(f"数组 a:\\n{a}")
    print(f"数组 b:\\n{b}")
    print(f"矩阵乘法 a @ b:\\n{a @ b}")
    print(f"a 的均值: {a.mean():.2f}")
    print(f"a 的标准差: {a.std():.2f}")
    print(f"a 的转置:\\n{a.T}")


if __name__ == "__main__":
    main()
'''

_SCIPY_ENTRY = '''"""$project_name 入口：SciPy 科学计算示例."""

import numpy as np
from scipy import linalg


def main() -> None:
    """求解线性方程组 Ax = b 并验证."""
    A = np.array([[3, 2, 1], [2, 3, 2], [1, 2, 4]], dtype=float)
    b = np.array([6, 7, 8], dtype=float)

    print("$project_name: SciPy 科学计算")
    print(f"系数矩阵 A:\\n{A}")
    print(f"常数向量 b: {b}")

    x = linalg.solve(A, b)
    print(f"解向量 x: {x}")
    print(f"验证 A @ x: {A @ x}")

    det = linalg.det(A)
    print(f"行列式 |A|: {det:.4f}")


if __name__ == "__main__":
    main()
'''

_FLASK_ENTRY = '''"""$project_name 入口：Flask Web 服务示例."""

from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def index() -> object:
    """根路由返回项目信息."""
    return jsonify({"name": "$project_name", "version": "0.1.0"})


@app.route("/hello/<name>")
def hello(name: str) -> object:
    """问候路由."""
    return jsonify({"message": f"hello, {name}!"})


def main() -> None:
    """启动 Flask 开发服务器."""
    print("$project_name: 启动 Flask 服务 http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True)


if __name__ == "__main__":
    main()
'''

_FASTAPI_ENTRY = '''"""$project_name 入口：FastAPI Web 服务示例."""

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="$project_name", version="0.1.0")


@app.get("/")
def index() -> dict[str, str]:
    """根路由返回项目信息."""
    return {"name": "$project_name", "version": "0.1.0"}


@app.get("/hello/{name}")
def hello(name: str) -> dict[str, str]:
    """问候路由."""
    return {"message": f"hello, {name}!"}


def main() -> None:
    """启动 FastAPI 服务（uvicorn）."""
    print("$project_name: 启动 FastAPI 服务 http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
'''

_PYINSTALLER_ENTRY = '''"""$project_name 入口：PyInstaller 兼容配置示例.

此模板演示 [tool.fspack] 完整配置，适合从 PyInstaller 迁移的用户参考。
"""


def main() -> None:
    """打印问候语与项目说明."""
    print("hello, world")
    print("$project_name: PyInstaller 兼容配置示例")


if __name__ == "__main__":
    main()
'''

# pyinstaller 模板的 pyproject.toml 包含完整 [tool.fspack] 配置示例，
# 不复用 _pyproject()，独立定义以展示所有可用配置项。
_PYINSTALLER_PYPROJECT = """[project]
name = "$project_name"
version = "0.1.0"
description = "$description"
requires-python = ">=3.8"

[tool.fspack]
# 构建默认值（CLI 标志可覆盖）
nuitka = false
pyc_strip = true
pyc_optimize = 2
no_site = false
no_pyc = false
no_stdlib_trim = true
ccache = false

# 图标（相对项目目录的路径，未设置时用默认 app.ico）
# icon = "assets/app.ico"

# 额外排除目录/文件模式（合并到内置排除规则）
exclude = ["tests", "docs", ".github"]

# 私有 PyPI 服务器（可选）
# extra-index-urls = ["https://pypi.example.com/simple"]
# find-links = ["./local-wheels"]

# Nuitka 编译的第三方包（可选，需配合 nuitka = true）
# nuitka_packages = ["numpy"]

# 多入口声明（可选，键为 exe 名，值为入口脚本路径）
# [tool.fspack.entries]
# cli = "src/cli.py"
# gui = "src/gui.py"
"""


# ---- 多入口/完整配置模板（iter-85）----

_MULTI_ENTRY_CLI = '''"""$project_name CLI 入口：argparse 参数解析."""

import argparse


def main() -> None:
    """CLI 入口：解析参数并打印问候."""
    parser = argparse.ArgumentParser(prog="$project_name", description="$description")
    parser.add_argument("name", help="要问候的名字")
    args = parser.parse_args()
    print(f"hello, {args.name}!")


if __name__ == "__main__":
    main()
'''

_MULTI_ENTRY_GUI = '''"""$project_name GUI 入口：tkinter 主窗口."""

import tkinter as tk
from tkinter import ttk


def main() -> None:
    """GUI 入口：tkinter 主窗口显示问候."""
    root = tk.Tk()
    root.title("$project_name")
    root.geometry("300x150")

    label = ttk.Label(root, text="hello, $project_name!", font=("Segoe UI", 14))
    label.pack(pady=50)

    root.mainloop()


if __name__ == "__main__":
    main()
'''

# multi-entry 模板 pyproject.toml：声明 [tool.fspack.entries] 多入口
_MULTI_ENTRY_PYPROJECT = """[project]
name = "$project_name"
version = "0.1.0"
description = "$description"
requires-python = ">=3.8"

[tool.fspack.entries]
cli = "src/cli.py"
gui = "src/gui.py"
"""

_FULL_CONFIG_ENTRY = '''"""$project_name 入口：完整配置最佳实践示例."""


def main() -> None:
    """打印问候语与版本信息."""
    print("hello, world")
    print("$project_name v0.1.0")


if __name__ == "__main__":
    main()
'''

_FULL_CONFIG_README = """# $project_name

$description

## 安装

```bash
pip install -e .
```

## 运行

```bash
python $entry_module.py
```

## 打包

```bash
fspack b                      # 构建可执行文件
fspack p                      # 构建并生成安装包
```

## 测试

```bash
pytest
```
"""

_FULL_CONFIG_GITIGNORE = """__pycache__/
*.pyc
*.pyo
*.egg-info/
dist/
build/
.venv/
.fspack/
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
"""

_FULL_CONFIG_TEST = '''"""$project_name 测试骨架."""

from $entry_module import main


def test_main(capsys: object) -> None:
    """测试 main 函数输出含 hello, world."""
    main()
    # capsys 是 pytest fixture，捕获 stdout/stderr
    # 此处仅作骨架演示，实际项目按需补充断言
'''

# full-config 模板 pyproject.toml：含 [tool.fspack] 实际配置（非注释）
_FULL_CONFIG_PYPROJECT = """[project]
name = "$project_name"
version = "0.1.0"
description = "$description"
requires-python = ">=3.8"

[tool.fspack]
pyc_strip = true
pyc_optimize = 2
no_stdlib_trim = true
exclude = ["tests", "docs", ".github"]
"""


# 模板注册表：iter-82 填充 6 个 CLI 模板，iter-83 填充 6 个 GUI 模板，
# iter-84 填充 8 个游戏/科学/Web 模板，iter-85 填充 2 个多入口/完整配置。
_TEMPLATES: tuple[Template, ...] = (
    Template(
        id="helloworld",
        name="Hello World",
        description="最小 Hello World 示例，验证基础流水线",
        category="cli",
        app_type="cli",
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject()),
            TemplateFile(rel_path="$entry_module.py", content=_HELLOWORLD_ENTRY),
        ),
    ),
    Template(
        id="args",
        name="argparse 命令行参数",
        description="argparse 参数解析示例，无第三方依赖",
        category="cli",
        app_type="cli",
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject()),
            TemplateFile(rel_path="$entry_module.py", content=_ARGS_ENTRY),
        ),
    ),
    Template(
        id="rich",
        name="rich 终端美化",
        description="rich 彩色表格与 markup 示例",
        category="cli",
        app_type="cli",
        dependencies=("rich",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("rich",))),
            TemplateFile(rel_path="$entry_module.py", content=_RICH_ENTRY),
        ),
    ),
    Template(
        id="requests",
        name="requests HTTP 客户端",
        description="requests HTTP GET 请求示例",
        category="cli",
        app_type="cli",
        dependencies=("requests",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("requests",))),
            TemplateFile(rel_path="$entry_module.py", content=_REQUESTS_ENTRY),
        ),
    ),
    Template(
        id="click",
        name="click CLI 框架",
        description="click 命令组与子命令示例",
        category="cli",
        app_type="cli",
        dependencies=("click",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("click",))),
            TemplateFile(rel_path="$entry_module.py", content=_CLICK_ENTRY),
        ),
    ),
    Template(
        id="typer",
        name="typer CLI 框架",
        description="typer 类型驱动 CLI 示例",
        category="cli",
        app_type="cli",
        dependencies=("typer",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("typer",))),
            TemplateFile(rel_path="$entry_module.py", content=_TYPER_ENTRY),
        ),
    ),
    # ---- GUI 模板（iter-83）----
    Template(
        id="pyside2",
        name="PySide2 桌面 GUI",
        description="PySide2 QMainWindow 主窗口示例",
        category="gui",
        app_type="gui",
        dependencies=("PySide2",),
        files=(
            TemplateFile(
                rel_path="pyproject.toml",
                # PySide2 不支持 Python 3.11+，约束到 3.8-3.10
                content=_pyproject(("PySide2",), requires_python=">=3.8,<3.11"),
            ),
            TemplateFile(rel_path="$entry_module.py", content=_PYSIDE2_ENTRY),
        ),
    ),
    Template(
        id="pyside6",
        name="PySide6 桌面 GUI",
        description="PySide6 QMainWindow 主窗口示例",
        category="gui",
        app_type="gui",
        dependencies=("PySide6",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("PySide6",))),
            TemplateFile(rel_path="$entry_module.py", content=_PYSIDE6_ENTRY),
        ),
    ),
    Template(
        id="pyside2-qml",
        name="PySide2 + QML",
        description="PySide2 QQmlApplicationEngine + QML 声明式界面示例",
        category="gui",
        app_type="gui",
        dependencies=("PySide2",),
        files=(
            TemplateFile(
                rel_path="pyproject.toml",
                # PySide2 不支持 Python 3.11+，约束到 3.8-3.10
                content=_pyproject(("PySide2",), requires_python=">=3.8,<3.11"),
            ),
            TemplateFile(rel_path="$entry_module.py", content=_PYSIDE2_QML_ENTRY),
            TemplateFile(rel_path="main.qml", content=_MAIN_QML),
        ),
    ),
    Template(
        id="pyside6-qml",
        name="PySide6 + QML",
        description="PySide6 QQmlApplicationEngine + QML 声明式界面示例",
        category="gui",
        app_type="gui",
        dependencies=("PySide6",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("PySide6",))),
            TemplateFile(rel_path="$entry_module.py", content=_PYSIDE6_QML_ENTRY),
            TemplateFile(rel_path="main.qml", content=_MAIN_QML),
        ),
    ),
    Template(
        id="pyqt5",
        name="PyQt5 桌面 GUI",
        description="PyQt5 QMainWindow 主窗口示例",
        category="gui",
        app_type="gui",
        dependencies=("PyQt5",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("PyQt5",))),
            TemplateFile(rel_path="$entry_module.py", content=_PYQT5_ENTRY),
        ),
    ),
    Template(
        id="tkinter",
        name="tkinter 标准库 GUI",
        description="tkinter 标准库 GUI 示例，无第三方依赖",
        category="gui",
        app_type="gui",
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject()),
            TemplateFile(rel_path="$entry_module.py", content=_TKINTER_ENTRY),
        ),
    ),
    # ---- 游戏模板（iter-84）----
    Template(
        id="pygame",
        name="Pygame 游戏骨架",
        description="Pygame 窗口与事件循环骨架示例",
        category="game",
        app_type="gui",
        dependencies=("pygame",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("pygame",))),
            TemplateFile(rel_path="$entry_module.py", content=_PYGAME_ENTRY),
        ),
    ),
    Template(
        id="snake",
        name="贪吃蛇游戏",
        description="Pygame 贪吃蛇完整游戏（方向键控制）",
        category="game",
        app_type="gui",
        dependencies=("pygame",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("pygame",))),
            TemplateFile(rel_path="$entry_module.py", content=_SNAKE_ENTRY),
        ),
    ),
    # ---- 科学计算模板（iter-84）----
    Template(
        id="matplotlib",
        name="Matplotlib 图表",
        description="Matplotlib 正弦波图表示例",
        category="sci",
        app_type="gui",
        dependencies=("matplotlib",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("matplotlib",))),
            TemplateFile(rel_path="$entry_module.py", content=_MATPLOTLIB_ENTRY),
        ),
    ),
    Template(
        id="numpy",
        name="NumPy 数值计算",
        description="NumPy 数组运算与统计示例",
        category="sci",
        app_type="cli",
        dependencies=("numpy",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("numpy",))),
            TemplateFile(rel_path="$entry_module.py", content=_NUMPY_ENTRY),
        ),
    ),
    Template(
        id="scipy",
        name="SciPy 科学计算",
        description="SciPy 线性方程组求解示例",
        category="sci",
        app_type="cli",
        dependencies=("numpy", "scipy"),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("numpy", "scipy"))),
            TemplateFile(rel_path="$entry_module.py", content=_SCIPY_ENTRY),
        ),
    ),
    # ---- Web 服务模板（iter-84）----
    Template(
        id="flask",
        name="Flask Web 服务",
        description="Flask 路由与 JSON 响应示例",
        category="web",
        app_type="cli",
        dependencies=("flask",),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("flask",))),
            TemplateFile(rel_path="$entry_module.py", content=_FLASK_ENTRY),
        ),
    ),
    Template(
        id="fastapi",
        name="FastAPI Web 服务",
        description="FastAPI 路由与 uvicorn 启动示例",
        category="web",
        app_type="cli",
        dependencies=("fastapi", "uvicorn"),
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_pyproject(("fastapi", "uvicorn"))),
            TemplateFile(rel_path="$entry_module.py", content=_FASTAPI_ENTRY),
        ),
    ),
    # ---- 配置示例模板（iter-84）----
    Template(
        id="pyinstaller",
        name="PyInstaller 兼容配置",
        description="完整 [tool.fspack] 配置示例，适合从 PyInstaller 迁移",
        category="config",
        app_type="cli",
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_PYINSTALLER_PYPROJECT),
            TemplateFile(rel_path="$entry_module.py", content=_PYINSTALLER_ENTRY),
        ),
    ),
    # ---- 多入口/完整配置模板（iter-85）----
    Template(
        id="multi-entry",
        name="多入口项目（CLI + GUI）",
        description="[tool.fspack.entries] 多入口声明，CLI + tkinter GUI 双入口",
        category="config",
        app_type="cli",
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_MULTI_ENTRY_PYPROJECT),
            TemplateFile(rel_path="src/cli.py", content=_MULTI_ENTRY_CLI),
            TemplateFile(rel_path="src/gui.py", content=_MULTI_ENTRY_GUI),
        ),
    ),
    Template(
        id="full-config",
        name="完整配置最佳实践",
        description="完整项目结构：[tool.fspack] 配置 + README + tests + .gitignore",
        category="config",
        app_type="cli",
        files=(
            TemplateFile(rel_path="pyproject.toml", content=_FULL_CONFIG_PYPROJECT),
            TemplateFile(rel_path="$entry_module.py", content=_FULL_CONFIG_ENTRY),
            TemplateFile(rel_path="README.md", content=_FULL_CONFIG_README),
            TemplateFile(rel_path=".gitignore", content=_FULL_CONFIG_GITIGNORE),
            TemplateFile(rel_path="tests/test_main.py", content=_FULL_CONFIG_TEST),
        ),
    ),
)


def list_templates() -> tuple[Template, ...]:
    """返回所有已注册模板，按 (category, id) 字母序排序.

    排序保证 ``--list`` 输出稳定，便于测试与用户查找。
    """
    return tuple(sorted(_TEMPLATES, key=lambda t: (t.category, t.id)))


def get_template(template_id: str) -> Template | None:
    """按 id 查询模板，未找到返回 ``None``.

    :param template_id: 模板 id（如 ``helloworld``）
    :return: 模板对象或 ``None``
    """
    for tpl in _TEMPLATES:
        if tpl.id == template_id:
            return tpl
    return None
