"""analyzer AST 依赖分析测试."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fspack.analyzer import (
    STDLIB_FALLBACK,
    _qml_module_to_qt_sub,
    analyze_dependencies,
    collect_imports,
    collect_submodule_imports,
    parse_qml_imports,
)


def _tree(src: str) -> ast.AST:
    return ast.parse(src)


def test_collect_imports_basic() -> None:
    tree = _tree("import os\nfrom sys import path\nimport numpy as np\nfrom numpy import array\nimport os.path\n")
    assert collect_imports(tree) == ["os", "sys", "numpy"]


def test_collect_imports_relative_skipped() -> None:
    tree = _tree("from . import foo\nfrom .sub import bar\nimport json\n")
    assert collect_imports(tree) == ["json"]


def test_collect_imports_dedup() -> None:
    tree = _tree("import os\nimport os\nimport os\n")
    assert collect_imports(tree) == ["os"]


def test_collect_imports_empty() -> None:
    assert collect_imports(_tree("x = 1\n")) == []


def test_collect_submodule_imports_dotted() -> None:
    """import X.Y 收集 {X: {Y}}."""
    tree = _tree("import os.path\nimport numpy.core\n")
    result = collect_submodule_imports(tree)
    assert result == {"os": frozenset({"path"}), "numpy": frozenset({"core"})}


def test_collect_submodule_imports_from_dotted() -> None:
    """from X.Y import Z 收集 {X: {Y}}."""
    tree = _tree("from PySide2.QtWidgets import QApplication\n")
    assert collect_submodule_imports(tree) == {"PySide2": frozenset({"QtWidgets"})}


def test_collect_submodule_imports_from_simple() -> None:
    """from X import Y 收集 {X: {Y}}（Y 可能是类名，不匹配 wheel 文件时自然忽略）."""
    tree = _tree("from flask import Flask\n")
    assert collect_submodule_imports(tree) == {"flask": frozenset({"Flask"})}


def test_collect_submodule_imports_relative_skipped() -> None:
    """相对导入跳过."""
    tree = _tree("from .sub import bar\nfrom . import foo\n")
    assert collect_submodule_imports(tree) == {}


def test_collect_submodule_imports_star_skipped() -> None:
    """星号导入跳过."""
    tree = _tree("from numpy import *\n")
    assert collect_submodule_imports(tree) == {}


def test_collect_submodule_imports_empty() -> None:
    """无 import 返回空字典."""
    assert collect_submodule_imports(_tree("x = 1\n")) == {}


def test_analyze_dependencies_classification(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import numpy\n")
    (tmp_path / "main.py").write_text("import os\nimport numpy\nimport pkg\nfrom json import loads\nimport requests\n")
    r = analyze_dependencies(tmp_path, "main", ("numpy>=1.0",))
    assert "os" in r.ast_stdlib
    assert "json" in r.ast_stdlib
    assert "pkg" in r.ast_local
    assert "numpy" in r.ast_third_party
    assert "requests" in r.ast_third_party
    assert "requests" in r.missing
    assert "numpy" not in r.missing


def test_analyze_dependencies_syntax_error_skipped(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("import os\ndef bad(:\n")
    (tmp_path / "good.py").write_text("import sys\n")
    r = analyze_dependencies(tmp_path, "good", ())
    assert "sys" in r.ast_stdlib
    assert r.ast_third_party == ()


def test_stdlib_fallback_contents() -> None:
    assert "os" in STDLIB_FALLBACK
    assert "json" in STDLIB_FALLBACK
    assert "numpy" not in STDLIB_FALLBACK


def test_analyze_dependencies_excludes_build_artifacts(tmp_path: Path) -> None:
    """dist/build/.venv 等目录下的 .py 不应被扫描，避免误报标准库内部模块为第三方依赖."""
    (tmp_path / "main.py").write_text("import os\n")
    # 模拟构建产物：dist/runtime/python/lib/ 下有 Python 标准库源码
    stdlib_dir = tmp_path / "dist" / "runtime" / "python" / "lib" / "python3.11"
    stdlib_dir.mkdir(parents=True)
    (stdlib_dir / "_weakref.py").write_text("import _weakrefset\n")
    # 模拟 .venv 下的第三方包
    venv_dir = tmp_path / ".venv" / "lib" / "site-packages" / "tornado"
    venv_dir.mkdir(parents=True)
    (venv_dir / "__init__.py").write_text("import cryptography\n")
    r = analyze_dependencies(tmp_path, "main", ())
    assert "os" in r.ast_stdlib
    assert "_weakrefset" not in r.ast_third_party
    assert "cryptography" not in r.ast_third_party
    assert r.ast_third_party == ()


def test_analyze_dependencies_excludes_dev_directories(tmp_path: Path) -> None:
    """examples/tests/docs/templates 等开发期目录不应被扫描，避免误报依赖."""
    (tmp_path / "main.py").write_text("import os\n")
    # examples 下含 tkinter/PySide2 等 import（非项目自身依赖）
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "tk_app.py").write_text("import tkinter\n")
    (tmp_path / "examples" / "gui.py").write_text("import PySide2\n")
    # tests 下含 pytest import
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("import pytest\n")
    # docs 下含 sphinx import
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "conf.py").write_text("import sphinx\n")
    # templates 下含 yaml import
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "gen.py").write_text("import yaml\n")
    r = analyze_dependencies(tmp_path, "main", ())
    assert "tkinter" not in r.ast_stdlib
    assert "PySide2" not in r.ast_third_party
    assert "pytest" not in r.ast_third_party
    assert "sphinx" not in r.ast_third_party
    assert "yaml" not in r.ast_third_party
    assert r.ast_third_party == ()


def test_analyze_dependencies_submodules(tmp_path: Path) -> None:
    """第三方包的子模块 import 被收集到 ast_submodules."""
    (tmp_path / "main.py").write_text("from PySide2.QtCore import QTimer\nfrom PySide2.QtWidgets import QApplication\n")
    r = analyze_dependencies(tmp_path, "main", ())
    assert r.ast_submodules["PySide2"] == frozenset({"QtCore", "QtWidgets"})


def test_analyze_dependencies_submodules_stdlib_filtered(tmp_path: Path) -> None:
    """标准库的子模块 import 不进入 ast_submodules."""
    (tmp_path / "main.py").write_text("import os.path\nfrom json import loads\n")
    r = analyze_dependencies(tmp_path, "main", ())
    assert "os" not in r.ast_submodules
    assert "json" not in r.ast_submodules


def test_analyze_dependencies_parallel_matches_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并行解析路径与串行路径结果一致.

    通过 monkeypatch 调低 ``_PARALLEL_THRESHOLD`` 强制走并行路径，
    验证 ``ProcessPoolExecutor`` 分发与结果合并的正确性。
    """
    from fspack import analyzer

    # 构造 10 个 .py 文件（足够触发并行路径，阈值调到 2）
    pkg = tmp_path / "myproj"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for i in range(10):
        (pkg / f"mod_{i}.py").write_text(
            "import os\nimport sys\nimport numpy as np\nfrom PySide2.QtWidgets import QApplication\n",
            encoding="utf-8",
        )
    (tmp_path / "main.py").write_text("import os\nimport myproj\n", encoding="utf-8")

    # 串行路径
    monkeypatch.setattr(analyzer, "_PARALLEL_THRESHOLD", 10000)
    serial = analyze_dependencies(tmp_path, "main", ())

    # 并行路径
    monkeypatch.setattr(analyzer, "_PARALLEL_THRESHOLD", 2)
    parallel = analyze_dependencies(tmp_path, "main", ())

    assert serial == parallel


def test_parse_file_worker_skips_syntax_error(tmp_path: Path) -> None:
    """worker 函数对语法错误文件返回空结果."""
    from fspack.analyzer import _parse_file_worker

    bad = tmp_path / "bad.py"
    bad.write_text("def bad(:\n", encoding="utf-8")
    tops, subs = _parse_file_worker(str(bad))
    assert tops == []
    assert subs == {}


def test_parse_file_worker_normal(tmp_path: Path) -> None:
    """worker 函数正常解析返回顶层导入与子模块."""
    from fspack.analyzer import _parse_file_worker

    py = tmp_path / "ok.py"
    py.write_text("import os\nfrom PySide2.QtWidgets import QApplication\n", encoding="utf-8")
    tops, subs = _parse_file_worker(str(py))
    assert "os" in tops
    assert "PySide2" in tops
    assert subs["PySide2"] == frozenset({"QtWidgets"})


# ---------- QML 文件扫描测试 ----------


def test_qml_module_to_qt_sub_default_rule() -> None:
    """默认规则：去掉 Qt 前缀（QtQuick → Quick）."""
    assert _qml_module_to_qt_sub("QtQuick") == "Quick"
    assert _qml_module_to_qt_sub("QtCharts") == "Charts"
    assert _qml_module_to_qt_sub("QtMultimedia") == "Multimedia"
    assert _qml_module_to_qt_sub("QtDataVisualization") == "DataVisualization"
    assert _qml_module_to_qt_sub("QtWebSockets") == "WebSockets"
    assert _qml_module_to_qt_sub("QtQuick3D") == "Quick3D"


def test_qml_module_to_qt_sub_explicit_mapping() -> None:
    """显式映射：QML 模块名与 DLL 子模块名不一致的特殊情况."""
    assert _qml_module_to_qt_sub("QtQuick.Controls") == "QuickControls2"
    assert _qml_module_to_qt_sub("QtQuick.Templates") == "QuickTemplates2"
    assert _qml_module_to_qt_sub("QtQuick.Layouts") == "QuickLayouts"
    assert _qml_module_to_qt_sub("QtQuick.Shapes") == "QuickShapes"
    # QtQuick 子模块对应同一 Qt5Quick.dll
    assert _qml_module_to_qt_sub("QtQuick.Window") == "Quick"
    assert _qml_module_to_qt_sub("QtQuick.Particles") == "Quick"
    assert _qml_module_to_qt_sub("QtQuick.LocalStorage") == "Quick"
    # WebEngine QML 模块对应 WebEngineCore DLL
    assert _qml_module_to_qt_sub("QtWebEngine") == "WebEngineCore"


def test_qml_module_to_qt_sub_non_qt_returns_none() -> None:
    """非 Qt 前缀返回 None."""
    assert _qml_module_to_qt_sub("Qt") is None  # 仅 "Qt" 无后续字符
    assert _qml_module_to_qt_sub("numpy") is None
    assert _qml_module_to_qt_sub("") is None


def test_parse_qml_imports_basic(tmp_path: Path) -> None:
    """基本 QML import 解析：import QtQuick 2.15 → Quick."""
    qml = tmp_path / "Main.qml"
    qml.write_text(
        "import QtQuick 2.15\nimport QtQuick.Controls 2.15\nimport QtQuick.Layouts 1.15\n",
        encoding="utf-8",
    )
    subs = parse_qml_imports(qml)
    assert subs == {"Quick", "QuickControls2", "QuickLayouts"}


def test_parse_qml_imports_relative_skipped(tmp_path: Path) -> None:
    """相对导入（import "."）被忽略."""
    qml = tmp_path / "Main.qml"
    qml.write_text('import "."\nimport ".."\nimport QtQuick 2.15\n', encoding="utf-8")
    subs = parse_qml_imports(qml)
    assert subs == {"Quick"}


def test_parse_qml_imports_js_skipped(tmp_path: Path) -> None:
    """JS 文件导入（import "scripts.js" as Scripts）被忽略."""
    qml = tmp_path / "Main.qml"
    qml.write_text(
        'import "scripts.js" as Scripts\nimport QtQuick 2.15\n',
        encoding="utf-8",
    )
    subs = parse_qml_imports(qml)
    assert subs == {"Quick"}


def test_parse_qml_imports_no_version(tmp_path: Path) -> None:
    """无版本号的 import 也能解析."""
    qml = tmp_path / "Main.qml"
    qml.write_text("import QtQuick\nimport QtQuick.Controls\n", encoding="utf-8")
    subs = parse_qml_imports(qml)
    assert subs == {"Quick", "QuickControls2"}


def test_parse_qml_imports_empty_file(tmp_path: Path) -> None:
    """空文件返回空集合."""
    qml = tmp_path / "empty.qml"
    qml.write_text("", encoding="utf-8")
    assert parse_qml_imports(qml) == set()


def test_parse_qml_imports_unreadable_returns_empty(tmp_path: Path) -> None:
    """文件读取失败返回空集合（不抛异常）."""
    # 使用不存在路径触发 OSError
    qml = tmp_path / "nonexistent.qml"
    assert parse_qml_imports(qml) == set()


def test_analyze_dependencies_qml_merges_to_qt_pkg(tmp_path: Path) -> None:
    """QML 文件扫描结果合并到 Qt 绑定包的 ast_submodules.

    Python 入口仅 import PySide2.QtQml，但 QML 文件 import QtQuick/QtQuick.Controls/
    QtQuick.Layouts，应将 Quick/QuickControls2/QuickLayouts 加入 PySide2 子模块集合。
    """
    (tmp_path / "main.py").write_text(
        "from PySide2.QtQml import QQmlApplicationEngine\n",
        encoding="utf-8",
    )
    (tmp_path / "Main.qml").write_text(
        "import QtQuick 2.15\nimport QtQuick.Controls 2.15\nimport QtQuick.Layouts 1.15\n",
        encoding="utf-8",
    )
    r = analyze_dependencies(tmp_path, "main", ())
    # Python 层收集的 QtQml + QML 层补充的 Quick/QuickControls2/QuickLayouts
    assert r.ast_submodules["PySide2"] >= frozenset({"QtQml", "Quick", "QuickControls2", "QuickLayouts"})


def test_analyze_dependencies_qml_with_multiple_qt_pkgs(tmp_path: Path) -> None:
    """项目同时 import 多个 Qt 绑定包时，QML 依赖加入所有 Qt 包的子模块集合.

    实际项目不会同时用 PySide2 和 PySide6，但代码逻辑应正确处理此场景。
    """
    (tmp_path / "main.py").write_text(
        "import PySide2.QtQml\nimport PySide6.QtQml\n",
        encoding="utf-8",
    )
    (tmp_path / "Main.qml").write_text("import QtQuick 2.15\n", encoding="utf-8")
    r = analyze_dependencies(tmp_path, "main", ())
    assert "Quick" in r.ast_submodules["PySide2"]
    assert "Quick" in r.ast_submodules["PySide6"]


def test_analyze_dependencies_no_qt_pkg_skips_qml_scan(tmp_path: Path) -> None:
    """项目未 import 任何 Qt 绑定包时，QML 文件不被扫描."""
    (tmp_path / "main.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "Main.qml").write_text("import QtQuick 2.15\n", encoding="utf-8")
    r = analyze_dependencies(tmp_path, "main", ())
    # 无 Qt 包，QML 扫描不触发，PySide2 不在 ast_submodules 中
    assert "PySide2" not in r.ast_submodules


def test_analyze_dependencies_qml_excluded_dirs_skipped(tmp_path: Path) -> None:
    """examples/tests 等开发目录下的 QML 文件不被扫描."""
    (tmp_path / "main.py").write_text(
        "from PySide2.QtQml import QQmlApplicationEngine\n",
        encoding="utf-8",
    )
    # examples 下的 QML 不应被扫描
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "demo.qml").write_text(
        "import QtQuick 2.15\nimport QtCharts 2.15\n",
        encoding="utf-8",
    )
    # 项目根目录的 QML 应被扫描
    (tmp_path / "Main.qml").write_text("import QtQuick 2.15\n", encoding="utf-8")
    r = analyze_dependencies(tmp_path, "main", ())
    # QtCharts 仅在 examples 下，不应被收集
    assert "Charts" not in r.ast_submodules.get("PySide2", frozenset())
    # Quick 在项目根 QML 中，应被收集
    assert "Quick" in r.ast_submodules["PySide2"]
