"""项目模板内容测试.

验证 iter-82~iter-85 注册的各分类模板（cli/gui/game/sci/web/config）的：

- 模板元数据正确（id/name/category/app_type/dependencies）
- 文件列表完整（入口脚本、pyproject.toml、资源文件如 QML）
- 模板渲染后生成正确文件树，入口脚本能被 AST 解析（语法正确）
- 入口脚本 import 触发正确的 app_type 推断（GUI 模板识别为 GUI）
- init_project 创建项目后目录结构与文件内容正确

iter-83 覆盖 6 个 GUI 模板：pyside2/pyside6/pyside2-qml/pyside6-qml/pyqt5/tkinter。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fspack.cli_init import init_project
from fspack.templates import default_variables, get_template, list_templates, render_template

# ---- GUI 模板注册与元数据（iter-83）----

GUI_TEMPLATE_IDS = ("pyside2", "pyside6", "pyside2-qml", "pyside6-qml", "pyqt5", "tkinter")

# 各 GUI 模板的依赖声明（与 registry.py 一致）
GUI_TEMPLATE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "pyside2": ("PySide2",),
    "pyside6": ("PySide6",),
    "pyside2-qml": ("PySide2",),
    "pyside6-qml": ("PySide6",),
    "pyqt5": ("PyQt5",),
    "tkinter": (),
}

# QML 模板需要额外的 main.qml 文件
QML_TEMPLATE_IDS = ("pyside2-qml", "pyside6-qml")


def test_gui_templates_registered() -> None:
    """6 个 GUI 模板都已注册到 registry."""
    for tpl_id in GUI_TEMPLATE_IDS:
        assert get_template(tpl_id) is not None, f"GUI 模板 {tpl_id!r} 未注册"


@pytest.mark.parametrize("tpl_id", GUI_TEMPLATE_IDS)
def test_gui_template_metadata(tpl_id: str) -> None:
    """GUI 模板元数据：category=gui, app_type=gui."""
    tpl = get_template(tpl_id)
    assert tpl is not None
    assert tpl.category == "gui"
    assert tpl.app_type == "gui"


@pytest.mark.parametrize("tpl_id", GUI_TEMPLATE_IDS)
def test_gui_template_dependencies(tpl_id: str) -> None:
    """GUI 模板依赖声明与设计一致."""
    tpl = get_template(tpl_id)
    assert tpl is not None
    assert tpl.dependencies == GUI_TEMPLATE_DEPENDENCIES[tpl_id]


@pytest.mark.parametrize("tpl_id", QML_TEMPLATE_IDS)
def test_qml_templates_contain_main_qml(tpl_id: str) -> None:
    """QML 模板文件列表包含 main.qml."""
    tpl = get_template(tpl_id)
    assert tpl is not None
    rel_paths = [f.rel_path for f in tpl.files]
    assert "main.qml" in rel_paths


# ---- 模板渲染与语法正确性 ----


def _render_template_files(tpl_id: str) -> dict[Path, str]:
    """渲染模板到 {Path: content} 字典（用固定 project_name=test-app）."""
    tpl = get_template(tpl_id)
    assert tpl is not None, f"模板 {tpl_id!r} 不存在"
    variables = default_variables("test-app", description="test")
    return render_template(tpl, variables)


@pytest.mark.parametrize("tpl_id", GUI_TEMPLATE_IDS)
def test_gui_template_rendering_contains_pyproject_and_entry(tpl_id: str) -> None:
    """GUI 模板渲染后包含 pyproject.toml 与入口脚本（.py）."""
    rendered = _render_template_files(tpl_id)
    paths = list(rendered.keys())
    # pyproject.toml
    assert Path("pyproject.toml") in paths
    # 入口脚本（连字符转下划线：test-app → test_app.py）
    assert Path("test_app.py") in paths
    # pyproject.toml 含项目名
    pyproject = rendered[Path("pyproject.toml")]
    assert 'name = "test-app"' in pyproject


@pytest.mark.parametrize("tpl_id", QML_TEMPLATE_IDS)
def test_qml_template_rendering_contains_main_qml(tpl_id: str) -> None:
    """QML 模板渲染后包含 main.qml，且 qml 内容含项目名."""
    rendered = _render_template_files(tpl_id)
    assert Path("main.qml") in rendered
    qml = rendered[Path("main.qml")]
    assert "test-app" in qml
    assert "ApplicationWindow" in qml


@pytest.mark.parametrize("tpl_id", GUI_TEMPLATE_IDS)
def test_gui_template_entry_script_syntax_valid(tpl_id: str) -> None:
    """GUI 模板渲染后的入口脚本能被 AST 解析（语法正确）."""
    rendered = _render_template_files(tpl_id)
    entry = rendered[Path("test_app.py")]
    # 若语法错误，ast.parse 抛 SyntaxError
    ast.parse(entry)


@pytest.mark.parametrize("tpl_id", GUI_TEMPLATE_IDS)
def test_gui_template_app_type_inferred_as_gui(tpl_id: str, tmp_path: Path) -> None:
    """GUI 模板入口脚本被 infer_app_type 识别为 GUI 类型.

    将渲染后的入口脚本写到临时文件，调用 infer_app_type 验证。
    infer_app_type 根据 import 中的 PySide2/PySide6/PyQt5/tkinter 自动推断。
    """
    from fspack.config import infer_app_type

    rendered = _render_template_files(tpl_id)
    entry_content = rendered[Path("test_app.py")]
    entry_file = tmp_path / "test_app.py"
    entry_file.write_text(entry_content, encoding="utf-8")

    declared = GUI_TEMPLATE_DEPENDENCIES[tpl_id]
    app_type = infer_app_type(entry_file, declared)
    assert app_type.value == "gui", f"模板 {tpl_id} 的入口脚本未被识别为 GUI"


# ---- init_project 创建 GUI 模板项目 ----


def test_init_project_pyside2(tmp_path: Path) -> None:
    """init_project 用 pyside2 模板创建项目，目录结构与文件内容正确."""
    target = init_project("my-gui", template_id="pyside2", directory=tmp_path, description="PySide2 demo")
    assert target.is_dir()
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "my-gui"' in pyproject
    assert '"PySide2"' in pyproject
    # PySide2 不支持 Python 3.11+，模板约束 requires-python
    assert 'requires-python = ">=3.8,<3.11"' in pyproject
    entry = (target / "my_gui.py").read_text(encoding="utf-8")
    assert "from PySide2.QtWidgets" in entry
    assert "QMainWindow" in entry


def test_init_project_tkinter(tmp_path: Path) -> None:
    """init_project 用 tkinter 模板创建项目（无第三方依赖）."""
    target = init_project("tk-app", template_id="tkinter", directory=tmp_path)
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    # tkinter 无依赖，pyproject.toml 不含 dependencies 字段
    assert "dependencies" not in pyproject
    entry = (target / "tk_app.py").read_text(encoding="utf-8")
    assert "import tkinter" in entry


def test_init_project_pyside2_qml(tmp_path: Path) -> None:
    """init_project 用 pyside2-qml 模板创建项目，含 main.qml 文件."""
    target = init_project("qml-app", template_id="pyside2-qml", directory=tmp_path)
    # PySide2 不支持 Python 3.11+，模板约束 requires-python
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.8,<3.11"' in pyproject
    # 入口脚本
    entry = (target / "qml_app.py").read_text(encoding="utf-8")
    assert "QQmlApplicationEngine" in entry
    # QML 文件
    qml_file = target / "main.qml"
    assert qml_file.is_file()
    qml = qml_file.read_text(encoding="utf-8")
    assert "ApplicationWindow" in qml
    assert "qml-app" in qml


def test_init_project_pyside6(tmp_path: Path) -> None:
    """init_project 用 pyside6 模板创建项目，使用 PySide6（exec 而非 exec_）."""
    target = init_project("p6-app", template_id="pyside6", directory=tmp_path)
    entry = (target / "p6_app.py").read_text(encoding="utf-8")
    assert "from PySide6.QtWidgets" in entry
    # PySide6 用 exec() 而非 exec_()
    assert "app.exec()" in entry


# ---- iter-84 游戏/科学/Web/配置模板 ----

GAME_TEMPLATE_IDS = ("pygame", "snake")
SCI_TEMPLATE_IDS = ("matplotlib", "numpy", "scipy")
WEB_TEMPLATE_IDS = ("flask", "fastapi")
CONFIG_TEMPLATE_IDS = ("pyinstaller",)

# iter-84 各模板的依赖声明
ITER84_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "pygame": ("pygame",),
    "snake": ("pygame",),
    "matplotlib": ("matplotlib",),
    "numpy": ("numpy",),
    "scipy": ("numpy", "scipy"),
    "flask": ("flask",),
    "fastapi": ("fastapi", "uvicorn"),
    "pyinstaller": (),
}

# iter-84 各模板的 app_type（pygame/snake/matplotlib 因 import 在 _GUI_HINTS 中→gui）
ITER84_APP_TYPES: dict[str, str] = {
    "pygame": "gui",
    "snake": "gui",
    "matplotlib": "gui",
    "numpy": "cli",
    "scipy": "cli",
    "flask": "cli",
    "fastapi": "cli",
    "pyinstaller": "cli",
}

ITER84_ALL_IDS = GAME_TEMPLATE_IDS + SCI_TEMPLATE_IDS + WEB_TEMPLATE_IDS + CONFIG_TEMPLATE_IDS


@pytest.mark.parametrize("tpl_id", ITER84_ALL_IDS)
def test_iter84_templates_registered(tpl_id: str) -> None:
    """iter-84 的 8 个模板都已注册."""
    assert get_template(tpl_id) is not None, f"模板 {tpl_id!r} 未注册"


@pytest.mark.parametrize("tpl_id", ITER84_ALL_IDS)
def test_iter84_template_dependencies(tpl_id: str) -> None:
    """iter-84 模板依赖声明与设计一致."""
    tpl = get_template(tpl_id)
    assert tpl is not None
    assert tpl.dependencies == ITER84_DEPENDENCIES[tpl_id]


@pytest.mark.parametrize("tpl_id", ITER84_ALL_IDS)
def test_iter84_template_entry_script_syntax_valid(tpl_id: str) -> None:
    """iter-84 模板渲染后的入口脚本能被 AST 解析（语法正确）."""
    rendered = _render_template_files(tpl_id)
    entry = rendered[Path("test_app.py")]
    ast.parse(entry)


@pytest.mark.parametrize("tpl_id", ITER84_ALL_IDS)
def test_iter84_template_app_type_inferred(tpl_id: str, tmp_path: Path) -> None:
    """iter-84 模板入口脚本被 infer_app_type 识别为预期类型（gui/cli）."""
    from fspack.config import infer_app_type

    rendered = _render_template_files(tpl_id)
    entry_content = rendered[Path("test_app.py")]
    entry_file = tmp_path / "test_app.py"
    entry_file.write_text(entry_content, encoding="utf-8")

    declared = ITER84_DEPENDENCIES[tpl_id]
    app_type = infer_app_type(entry_file, declared)
    expected = ITER84_APP_TYPES[tpl_id]
    assert app_type.value == expected, f"模板 {tpl_id} 应识别为 {expected}，实际 {app_type.value}"


def test_pyinstaller_template_contains_tool_fspack_config() -> None:
    """pyinstaller 模板的 pyproject.toml 包含 [tool.fspack] 完整配置示例."""
    rendered = _render_template_files("pyinstaller")
    pyproject = rendered[Path("pyproject.toml")]
    assert "[tool.fspack]" in pyproject
    assert "nuitka" in pyproject
    assert "pyc_strip" in pyproject
    assert "pyc_optimize" in pyproject
    assert "exclude" in pyproject
    # 多入口声明示例（注释形式）
    assert "[tool.fspack.entries]" in pyproject


def test_init_project_pygame(tmp_path: Path) -> None:
    """init_project 用 pygame 模板创建项目，结构正确."""
    target = init_project("game-app", template_id="pygame", directory=tmp_path)
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pygame"' in pyproject
    entry = (target / "game_app.py").read_text(encoding="utf-8")
    assert "import pygame" in entry
    assert "pygame.init" in entry


def test_init_project_snake(tmp_path: Path) -> None:
    """init_project 用 snake 模板创建项目，含完整游戏逻辑."""
    target = init_project("snake-app", template_id="snake", directory=tmp_path)
    entry = (target / "snake_app.py").read_text(encoding="utf-8")
    assert "pygame" in entry
    assert "_spawn_food" in entry
    assert "K_UP" in entry


def test_init_project_matplotlib(tmp_path: Path) -> None:
    """init_project 用 matplotlib 模板创建项目."""
    target = init_project("chart-app", template_id="matplotlib", directory=tmp_path)
    entry = (target / "chart_app.py").read_text(encoding="utf-8")
    assert "import matplotlib.pyplot" in entry
    assert "plt.show" in entry


def test_init_project_numpy(tmp_path: Path) -> None:
    """init_project 用 numpy 模板创建项目."""
    target = init_project("np-app", template_id="numpy", directory=tmp_path)
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert '"numpy"' in pyproject
    entry = (target / "np_app.py").read_text(encoding="utf-8")
    assert "import numpy" in entry


def test_init_project_flask(tmp_path: Path) -> None:
    """init_project 用 flask 模板创建项目."""
    target = init_project("web-app", template_id="flask", directory=tmp_path)
    entry = (target / "web_app.py").read_text(encoding="utf-8")
    assert "from flask" in entry
    assert "Flask(__name__)" in entry
    assert "app.run" in entry


def test_init_project_fastapi(tmp_path: Path) -> None:
    """init_project 用 fastapi 模板创建项目."""
    target = init_project("api-app", template_id="fastapi", directory=tmp_path)
    entry = (target / "api_app.py").read_text(encoding="utf-8")
    assert "from fastapi" in entry
    assert "FastAPI(" in entry
    assert "uvicorn.run" in entry


def test_init_project_pyinstaller(tmp_path: Path) -> None:
    """init_project 用 pyinstaller 模板创建项目，pyproject 含 [tool.fspack] 配置."""
    target = init_project("pi-app", template_id="pyinstaller", directory=tmp_path)
    # pyinstaller 模板无依赖，pyproject 不含 dependencies
    pyproject_text = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.fspack]" in pyproject_text
    assert "dependencies" not in pyproject_text


# ---- iter-85 多入口/完整配置模板 ----

ITER85_TEMPLATE_IDS = ("multi-entry", "full-config")


@pytest.mark.parametrize("tpl_id", ITER85_TEMPLATE_IDS)
def test_iter85_templates_registered(tpl_id: str) -> None:
    """iter-85 的 2 个模板都已注册."""
    assert get_template(tpl_id) is not None, f"模板 {tpl_id!r} 未注册"


def test_multi_entry_template_contains_entries_config() -> None:
    """multi-entry 模板的 pyproject.toml 含 [tool.fspack.entries] 声明."""
    tpl = get_template("multi-entry")
    assert tpl is not None
    rel_paths = [f.rel_path for f in tpl.files]
    assert "src/cli.py" in rel_paths
    assert "src/gui.py" in rel_paths
    rendered = _render_template_files("multi-entry")
    pyproject = rendered[Path("pyproject.toml")]
    assert "[tool.fspack.entries]" in pyproject
    assert 'cli = "src/cli.py"' in pyproject
    assert 'gui = "src/gui.py"' in pyproject


def test_multi_entry_template_scripts_syntax_valid() -> None:
    """multi-entry 模板的 cli.py 与 gui.py 渲染后能被 AST 解析."""
    rendered = _render_template_files("multi-entry")
    ast.parse(rendered[Path("src/cli.py")])
    ast.parse(rendered[Path("src/gui.py")])


def test_full_config_template_contains_extra_files() -> None:
    """full-config 模板含 README.md、.gitignore、tests/test_main.py."""
    tpl = get_template("full-config")
    assert tpl is not None
    rel_paths = [f.rel_path for f in tpl.files]
    assert "README.md" in rel_paths
    assert ".gitignore" in rel_paths
    assert "tests/test_main.py" in rel_paths


def test_full_config_template_pyproject_contains_tool_fspack() -> None:
    """full-config 模板的 pyproject.toml 含 [tool.fspack] 实际配置."""
    rendered = _render_template_files("full-config")
    pyproject = rendered[Path("pyproject.toml")]
    assert "[tool.fspack]" in pyproject
    assert "pyc_strip = true" in pyproject
    assert "pyc_optimize = 2" in pyproject
    assert "exclude" in pyproject


def test_full_config_template_all_files_syntax_valid() -> None:
    """full-config 模板的入口脚本与测试文件能被 AST 解析."""
    rendered = _render_template_files("full-config")
    ast.parse(rendered[Path("test_app.py")])
    ast.parse(rendered[Path("tests/test_main.py")])


def test_full_config_template_readme_contains_project_name() -> None:
    """full-config 模板的 README.md 含项目名与打包命令."""
    rendered = _render_template_files("full-config")
    readme = rendered[Path("README.md")]
    assert "test-app" in readme
    assert "fspack b" in readme
    assert "fspack p" in readme


def test_init_project_multi_entry(tmp_path: Path) -> None:
    """init_project 用 multi-entry 模板创建项目，含 src/cli.py 与 src/gui.py."""
    target = init_project("me-app", template_id="multi-entry", directory=tmp_path)
    assert (target / "src" / "cli.py").is_file()
    assert (target / "src" / "gui.py").is_file()
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.fspack.entries]" in pyproject


def test_init_project_full_config(tmp_path: Path) -> None:
    """init_project 用 full-config 模板创建项目，含 README/tests/.gitignore."""
    target = init_project("fc-app", template_id="full-config", directory=tmp_path)
    assert (target / "README.md").is_file()
    assert (target / ".gitignore").is_file()
    assert (target / "tests" / "test_main.py").is_file()
    assert (target / "fc_app.py").is_file()


# ---- 模板总数统计 ----


def test_template_count_final() -> None:
    """iter-85 后模板总数 = 22（6 CLI + 6 GUI + 2 game + 3 sci + 2 web + 3 config）.

    满足"不少于 20 项"要求。10 轮迭代计划全部交付。
    """
    templates = list_templates()
    assert len(templates) == 22, f"iter-85 后应有 22 个模板，实际 {len(templates)}"
    # 分类统计
    categories: dict[str, int] = {}
    for tpl in templates:
        categories[tpl.category] = categories.get(tpl.category, 0) + 1
    assert categories.get("cli", 0) == 6
    assert categories.get("gui", 0) == 6
    assert categories.get("game", 0) == 2
    assert categories.get("sci", 0) == 3
    assert categories.get("web", 0) == 2
    assert categories.get("config", 0) == 3
