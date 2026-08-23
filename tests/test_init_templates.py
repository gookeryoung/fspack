"""项目模板内容测试.

验证各分类模板（cli/gui/game/sci/web/config）的：

- 模板元数据正确（id/name/category/app_type/dependencies）
- 文件列表完整（入口脚本、pyproject.toml、资源文件如 QML）
- 模板渲染后生成正确文件树，入口脚本能被 AST 解析（语法正确）
- 入口脚本 import 触发正确的 app_type 推断（GUI 模板识别为 GUI）
- init_project 创建项目后目录结构与文件内容正确

覆盖 6 个 GUI 模板：pyside2/pyside6/pyside2-qml/pyside6-qml/pyqt5/tkinter。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fspack.cli_init import init_project
from fspack.templates import default_variables, get_template, list_templates, render_template

# ---- GUI 模板注册与元数据 ----

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
    # PySide2 不支持 Python 3.10+，模板约束 requires-python
    assert 'requires-python = ">=3.8,<3.10"' in pyproject
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
    # PySide2 不支持 Python 3.10+，模板约束 requires-python
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.8,<3.10"' in pyproject
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


# ---- 游戏/科学/Web/配置模板 ----

GAME_TEMPLATE_IDS = ("pygame", "snake")
SCI_TEMPLATE_IDS = ("matplotlib", "numpy", "scipy")
WEB_TEMPLATE_IDS = ("flask", "fastapi")
CONFIG_TEMPLATE_IDS = ("pyinstaller",)

# 各模板的依赖声明
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

# 各模板的 app_type（pygame/snake/matplotlib 因 import 在 _GUI_HINTS 中→gui）
ITER84_APP_TYPES: dict[str, str] = {
    "pygame": "gui",
    "snake": "gui",
    "matplotlib": "gui",
    "numpy": "cli",
    "scipy": "cli",
    "flask": "web",
    "fastapi": "web",
    "pyinstaller": "cli",
}

ITER84_ALL_IDS = GAME_TEMPLATE_IDS + SCI_TEMPLATE_IDS + WEB_TEMPLATE_IDS + CONFIG_TEMPLATE_IDS


@pytest.mark.parametrize("tpl_id", ITER84_ALL_IDS)
def test_iter84_templates_registered(tpl_id: str) -> None:
    """8 个模板都已注册."""
    assert get_template(tpl_id) is not None, f"模板 {tpl_id!r} 未注册"


@pytest.mark.parametrize("tpl_id", ITER84_ALL_IDS)
def test_iter84_template_dependencies(tpl_id: str) -> None:
    """模板依赖声明与设计一致."""
    tpl = get_template(tpl_id)
    assert tpl is not None
    assert tpl.dependencies == ITER84_DEPENDENCIES[tpl_id]


@pytest.mark.parametrize("tpl_id", ITER84_ALL_IDS)
def test_iter84_template_entry_script_syntax_valid(tpl_id: str) -> None:
    """模板渲染后的入口脚本能被 AST 解析（语法正确）."""
    rendered = _render_template_files(tpl_id)
    entry = rendered[Path("test_app.py")]
    ast.parse(entry)


@pytest.mark.parametrize("tpl_id", ITER84_ALL_IDS)
def test_iter84_template_app_type_inferred(tpl_id: str, tmp_path: Path) -> None:
    """模板入口脚本被 infer_app_type 识别为预期类型（gui/cli/web）."""
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
    # 多入口声明示例（注释形式）：[project.scripts] 推荐 + [tool.fspack.entries] 可选覆盖
    assert "# [project.scripts]" in pyproject
    assert "# [tool.fspack.entries]" in pyproject


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
    """init_project 用 matplotlib 模板创建项目.

    模板显式 ``import tkinter`` 触发 fspack 打包 Tcl/Tk 运行时，并
    ``matplotlib.use("TkAgg")`` 强制交互后端，避免 embed python 下
    ``plt.show`` 抛 ``FigureCanvasAgg is non-interactive`` 错误。
    """
    target = init_project("chart-app", template_id="matplotlib", directory=tmp_path)
    entry = (target / "chart_app.py").read_text(encoding="utf-8")
    assert "import tkinter" in entry
    assert "matplotlib.use" in entry
    assert "TkAgg" in entry
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


# ---- 多入口/完整配置模板 ----

ITER85_TEMPLATE_IDS = ("multi-entry", "full-config")


@pytest.mark.parametrize("tpl_id", ITER85_TEMPLATE_IDS)
def test_iter85_templates_registered(tpl_id: str) -> None:
    """2 个模板都已注册."""
    assert get_template(tpl_id) is not None, f"模板 {tpl_id!r} 未注册"


def test_multi_entry_template_contains_entries_config() -> None:
    """multi-entry 模板的 pyproject.toml 含 [project.scripts] 声明（推荐默认）."""
    tpl = get_template("multi-entry")
    assert tpl is not None
    rel_paths = [f.rel_path for f in tpl.files]
    assert "src/cli.py" in rel_paths
    assert "src/gui.py" in rel_paths
    rendered = _render_template_files("multi-entry")
    pyproject = rendered[Path("pyproject.toml")]
    assert "[project.scripts]" in pyproject
    assert 'cli = "cli:main"' in pyproject
    assert 'gui = "gui:main"' in pyproject


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
    assert "[project.scripts]" in pyproject


def test_init_project_full_config(tmp_path: Path) -> None:
    """init_project 用 full-config 模板创建项目，含 README/tests/.gitignore."""
    target = init_project("fc-app", template_id="full-config", directory=tmp_path)
    assert (target / "README.md").is_file()
    assert (target / ".gitignore").is_file()
    assert (target / "tests" / "test_main.py").is_file()
    assert (target / "fc_app.py").is_file()


# ---- 模板总数统计 ----


def test_template_count_final() -> None:
    """init 模板总数 = 24（6 CLI + 6 GUI + 2 game + 3 sci + 4 web + 3 config）."""
    templates = list_templates(role="init")
    assert len(templates) == 24, f"应有 24 个 init 模板，实际 {len(templates)}"
    # 分类统计
    categories: dict[str, int] = {}
    for tpl in templates:
        categories[tpl.category] = categories.get(tpl.category, 0) + 1
    assert categories.get("cli", 0) == 6
    assert categories.get("gui", 0) == 6
    assert categories.get("game", 0) == 2
    assert categories.get("sci", 0) == 3
    assert categories.get("web", 0) == 4
    assert categories.get("config", 0) == 3


# --- iter-148 前后端分离 Web 模板（web-flask-vue / web-fastapi-react）---


WEB_SEPARATED_TEMPLATE_IDS = ("web-flask-vue", "web-fastapi-react")


@pytest.mark.parametrize("tpl_id", WEB_SEPARATED_TEMPLATE_IDS)
def test_web_separated_templates_registered(tpl_id: str) -> None:
    """前后端分离 Web 模板已注册到 registry."""
    assert get_template(tpl_id) is not None, f"Web 模板 {tpl_id!r} 未注册"


@pytest.mark.parametrize("tpl_id", WEB_SEPARATED_TEMPLATE_IDS)
def test_web_separated_template_metadata(tpl_id: str) -> None:
    """前后端分离 Web 模板元数据：category=web, app_type=web."""
    tpl = get_template(tpl_id)
    assert tpl is not None
    assert tpl.category == "web"
    assert tpl.app_type == "web"


def test_web_flask_vue_template_dependencies() -> None:
    """web-flask-vue 模板依赖为 flask."""
    tpl = get_template("web-flask-vue")
    assert tpl is not None
    assert tpl.dependencies == ("flask",)


def test_web_fastapi_react_template_dependencies() -> None:
    """web-fastapi-react 模板依赖为 fastapi + uvicorn."""
    tpl = get_template("web-fastapi-react")
    assert tpl is not None
    assert tpl.dependencies == ("fastapi", "uvicorn")


def test_web_separated_templates_contain_frontend_index_html() -> None:
    """前后端分离 Web 模板含 frontend/index.html 前端入口文件."""
    for tpl_id in WEB_SEPARATED_TEMPLATE_IDS:
        tpl = get_template(tpl_id)
        assert tpl is not None, f"模板 {tpl_id!r} 不存在"
        rel_paths = [f.rel_path for f in tpl.files]
        assert "frontend/index.html" in rel_paths, f"{tpl_id} 缺少 frontend/index.html"


def test_web_separated_templates_pyproject_contains_web_static_dirs() -> None:
    """前后端分离 Web 模板 pyproject.toml 含 web-static-dirs = ['frontend']."""
    for tpl_id in WEB_SEPARATED_TEMPLATE_IDS:
        rendered = _render_template_files(tpl_id)
        pyproject = rendered[Path("pyproject.toml")]
        assert 'web-static-dirs = ["frontend"]' in pyproject, f"{tpl_id} pyproject 缺少 web-static-dirs"


def test_web_separated_template_entry_script_syntax_valid() -> None:
    """前后端分离 Web 模板入口脚本能被 AST 解析（语法正确）."""
    for tpl_id in WEB_SEPARATED_TEMPLATE_IDS:
        rendered = _render_template_files(tpl_id)
        entry_path = Path("test_app.py")
        assert entry_path in rendered, f"{tpl_id} 缺少入口脚本"
        ast.parse(rendered[entry_path])


def test_init_project_web_flask_vue(tmp_path: Path) -> None:
    """init_project 用 web-flask-vue 模板创建项目."""
    target = init_project("wf-app", template_id="web-flask-vue", directory=tmp_path)
    assert (target / "wf_app.py").is_file()
    assert (target / "frontend" / "index.html").is_file()
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert 'web-static-dirs = ["frontend"]' in pyproject


# --- ``--python-version`` 覆盖 requires-python 的三层兜底 ---


def _init_with_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pyproject: str) -> str:
    """monkeypatch render_template 返回指定 pyproject 内容，执行带版本覆盖的 init_project.

    绕过真实模板（均有 requires-python 行），用于构造缺键的兜底场景。
    """
    from fspack import cli_init

    monkeypatch.setattr(
        cli_init,
        "render_template",
        lambda template, variables: {Path("pyproject.toml"): pyproject},
    )
    target = init_project("ver-app", template_id="helloworld", directory=tmp_path, python_version="3.10")
    return (target / "pyproject.toml").read_text(encoding="utf-8")


def test_init_project_python_version_replaces_existing_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pyproject 已有 requires-python 行时：直接整行替换为用户指定版本."""
    pyproject = '[project]\nname = "ver-app"\ndescription = "d"\nrequires-python = ">=3.8"\nversion = "0.1.0"\n'
    result = _init_with_pyproject(tmp_path, monkeypatch, pyproject)
    assert 'requires-python = ">=3.10"' in result
    assert ">=3.8" not in result  # 旧约束被替换，不得残留


def test_init_project_python_version_appends_after_description(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 requires-python 行但有 description 行时：追加到 description 行后."""
    pyproject = '[project]\nname = "ver-app"\ndescription = "demo"\nversion = "0.1.0"\n'
    result = _init_with_pyproject(tmp_path, monkeypatch, pyproject)
    # 追加位置：description 行之后（TOML 键仍位于 [project] 节内）
    desc_idx = result.index('description = "demo"')
    req_idx = result.index('requires-python = ">=3.10"')
    assert req_idx > desc_idx
    assert result.count("requires-python") == 1


def test_init_project_python_version_inserts_after_project_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """requires-python 与 description 行均缺失时：兜底插入 [project] 节头后."""
    pyproject = '[project]\nname = "ver-app"\nversion = "0.1.0"\n'
    result = _init_with_pyproject(tmp_path, monkeypatch, pyproject)
    lines = result.splitlines()
    assert lines[0] == "[project]"
    assert lines[1] == 'requires-python = ">=3.10"'  # 紧随节头插入，保持在 [project] 节内


def test_init_project_python_version_no_project_section_keeps_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 [project] 节时无法定位插入点：保持原样（不强行追加避免 TOML 错位）."""
    pyproject = 'name = "ver-app"\nversion = "0.1.0"\n'
    result = _init_with_pyproject(tmp_path, monkeypatch, pyproject)
    assert "requires-python" not in result


# ---- 加载器错误处理与边界场景 ----


def test_load_template_without_template_toml_returns_none(tmp_path: Path) -> None:
    """无 template.toml 的目录返回 None（跳过无效模板）."""
    from fspack.templates.registry import _load_template

    tpl_dir = tmp_path / "no_manifest"
    tpl_dir.mkdir()
    (tpl_dir / "main.py").write_text('print("hi")', encoding="utf-8")
    assert _load_template(tpl_dir) is None


def test_load_template_with_invalid_toml_returns_none(tmp_path: Path) -> None:
    """template.toml 解析失败返回 None（不抛异常）."""
    from fspack.templates.registry import _load_template

    tpl_dir = tmp_path / "bad_toml"
    tpl_dir.mkdir()
    (tpl_dir / "template.toml").write_text("invalid [toml", encoding="utf-8")
    (tpl_dir / "main.py").write_text('print("hi")', encoding="utf-8")
    assert _load_template(tpl_dir) is None


def test_load_template_without_source_files_returns_none(tmp_path: Path) -> None:
    """有 template.toml 但无源文件的目录返回 None."""
    from fspack.templates.registry import _load_template

    tpl_dir = tmp_path / "empty_tpl"
    tpl_dir.mkdir()
    (tpl_dir / "template.toml").write_text(
        'id = "empty"\nname = "Empty"\ndescription = ""\ncategory = "cli"\n',
        encoding="utf-8",
    )
    assert _load_template(tpl_dir) is None


def test_load_template_skips_non_utf8_binary_file(tmp_path: Path) -> None:
    """模板目录含非 UTF-8 二进制文件时跳过该文件而非崩溃（保留有效文本文件）.

    复现打包环境下模板目录混入图标/图片等二进制文件的场景：
    ``read_text(encoding="utf-8")`` 抛 UnicodeDecodeError，加载器应跳过并
    继续处理其余文本模板，而非让整个 fsp init/--list 中断。
    """
    from fspack.templates.registry import _load_template

    tpl_dir = tmp_path / "with_binary"
    tpl_dir.mkdir()
    (tpl_dir / "template.toml").write_text(
        'id = "with_binary"\nname = "Binary"\ndescription = ""\ncategory = "cli"\n',
        encoding="utf-8",
    )
    (tpl_dir / "main.py").write_text('print("hi")', encoding="utf-8")
    # 0xa7 起始的非 UTF-8 字节序列（复现用户 traceback 中的 0xa7 解码错误）
    (tpl_dir / "icon.ico").write_bytes(b"\xa7\x00\x01\x02\xff")

    tpl = _load_template(tpl_dir)
    assert tpl is not None
    rel_paths = [f.rel_path for f in tpl.files]
    assert "main.py" in rel_paths
    assert "icon.ico" not in rel_paths


def test_load_template_skips_pycache_without_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """模板目录含 ``__pycache__/*.pyc`` 编译产物时扫描阶段直接过滤，不刷警告.

    复现安装后模板目录被 ``python -O`` 触碰生成 ``__pycache__/*.opt-2.pyc``
    的场景：这些二进制字节码非模板源文件，此前会逐个触发 UnicodeDecodeError
    兜底并刷出"跳过非 UTF-8 模板文件"警告刷屏。加载器应在扫描阶段按目录名/
    后缀过滤，既不读取也不产生任何警告日志。
    """
    import logging

    from fspack.templates.registry import _load_template

    tpl_dir = tmp_path / "with_pycache"
    tpl_dir.mkdir()
    (tpl_dir / "template.toml").write_text(
        'id = "with_pycache"\nname = "Pycache"\ndescription = ""\ncategory = "cli"\n',
        encoding="utf-8",
    )
    (tpl_dir / "main.py").write_text('print("hi")', encoding="utf-8")
    # 模拟 python -O 生成的字节码缓存（0xa7 起始复现用户日志中的解码错误字节）
    pycache = tpl_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "main.cpython-311.opt-2.pyc").write_bytes(b"\xa7\x00\x01\x02\xff")
    # 顶层散落的 .pyc 也应被后缀过滤
    (tpl_dir / "stray.pyc").write_bytes(b"\xa7\xff")

    with caplog.at_level(logging.WARNING, logger="fspack.templates.registry"):
        tpl = _load_template(tpl_dir)

    assert tpl is not None
    rel_paths = [f.rel_path for f in tpl.files]
    assert "main.py" in rel_paths
    # __pycache__ 下文件与顶层 .pyc 均不应出现在模板文件列表
    assert not any("__pycache__" in p for p in rel_paths)
    assert not any(p.endswith(".pyc") for p in rel_paths)
    # 关键：扫描阶段过滤，不产生任何"跳过非 UTF-8 模板文件"警告
    assert not any("非 UTF-8" in record.message for record in caplog.records)


def test_load_all_skips_non_category_directories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_load_all 跳过非分类目录（如 README.md 文件、非分类名目录）."""
    from fspack.templates import registry as registry_mod

    # 构造 tmp_path/cli/helloworld/ 有效模板 + tmp_path/non_category/xxx/ 非分类
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    tpl_dir = cli_dir / "helloworld"
    tpl_dir.mkdir()
    (tpl_dir / "template.toml").write_text(
        'id = "helloworld"\nname = "Hello"\ndescription = ""\ncategory = "cli"\n',
        encoding="utf-8",
    )
    (tpl_dir / "main.py").write_text('print("hi")', encoding="utf-8")
    # 非分类目录
    other_dir = tmp_path / "non_category"
    other_dir.mkdir()
    (other_dir / "xxx").mkdir()
    monkeypatch.setattr(registry_mod, "_init_templates_root", lambda: tmp_path)
    # doctor 模板根目录指向不存在路径，避免污染测试
    monkeypatch.setattr(registry_mod, "_doctor_templates_root", lambda: tmp_path / "nonexistent_doctor")
    # _load_all 进程内缓存：注入自定义根目录后须清缓存强制重扫，结束时再清
    # 恢复真实资产目录，避免污染后续依赖真实模板的测试
    registry_mod.clear_template_cache()
    try:
        templates = registry_mod._load_all()
        assert len(templates) == 1
        assert templates[0].id == "helloworld"
    finally:
        registry_mod.clear_template_cache()


def test_load_all_when_root_missing_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """两个模板根目录都不存在时返回空元组."""
    from fspack.templates import registry as registry_mod

    monkeypatch.setattr(registry_mod, "_init_templates_root", lambda: tmp_path / "nonexistent")
    monkeypatch.setattr(registry_mod, "_doctor_templates_root", lambda: tmp_path / "nonexistent_doctor")
    registry_mod.clear_template_cache()
    try:
        assert registry_mod._load_all() == ()
    finally:
        registry_mod.clear_template_cache()


def test_get_template_nonexistent_returns_none() -> None:
    """get_template 对不存在的 id 返回 None."""
    assert get_template("nonexistent_template_xyz") is None
