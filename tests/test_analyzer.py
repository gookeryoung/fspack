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


def test_analyze_dependencies_excludes_cache_and_tool_dirs(tmp_path: Path) -> None:
    """.uv-cache/node_modules/.pyrefly_cache/htmlcov 等缓存与工具目录不应被扫描.

    这些目录可能含第三方包 .py 源码（如 ``.uv-cache`` 内的 wheel 解包源码），
    误扫描会导致依赖分析多出第三方包内部依赖、源码指纹变化触发缓存失效。
    """
    (tmp_path / "main.py").write_text("import os\n")
    # .uv-cache 内模拟第三方包源码（uv 缓存解包后的 wheel）
    uv_cache_dir = tmp_path / ".uv-cache" / "archive-v0" / "hash"
    uv_cache_dir.mkdir(parents=True)
    (uv_cache_dir / "tornado.py").write_text("import cryptography\n")
    # node_modules 内模拟 py2js 项目残留 .py
    nm_dir = tmp_path / "node_modules" / "pkg"
    nm_dir.mkdir(parents=True)
    (nm_dir / "index.py").write_text("import flask\n")
    # .pyrefly_cache 内模拟 pyrefly 缓存
    pr_dir = tmp_path / ".pyrefly_cache"
    pr_dir.mkdir()
    (pr_dir / "stub.py").write_text("import typing\n")
    # htmlcov 内模拟覆盖率报告残留 .py
    hc_dir = tmp_path / "htmlcov"
    hc_dir.mkdir()
    (hc_dir / "helper.py").write_text("import coverage\n")
    r = analyze_dependencies(tmp_path, "main", ())
    assert "cryptography" not in r.ast_third_party
    assert "flask" not in r.ast_third_party
    assert "typing" not in r.ast_stdlib
    assert "coverage" not in r.ast_third_party
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
    """worker 函数对语法错误文件返回空结果与错误记录（iter-138 记录 ast_errors）."""
    from fspack.analyzer import _parse_file_worker

    bad = tmp_path / "bad.py"
    bad.write_text("def bad(:\n", encoding="utf-8")
    non_stdlib_tops, stdlib_tops, subs, errors = _parse_file_worker(str(bad))
    assert non_stdlib_tops == []
    assert stdlib_tops == []
    assert subs == {}
    # iter-138：错误记录为 (abs_path, error_msg) 元组
    assert len(errors) == 1
    assert errors[0][0] == str(bad)
    assert "bad" in errors[0][1].lower() or "syntax" in errors[0][1].lower() or errors[0][1]


def test_parse_file_worker_normal(tmp_path: Path) -> None:
    """worker 函数正常解析返回非标准库/标准库分离的顶层导入与子模块."""
    from fspack.analyzer import _parse_file_worker

    py = tmp_path / "ok.py"
    py.write_text("import os\nfrom PySide2.QtWidgets import QApplication\n", encoding="utf-8")
    non_stdlib_tops, stdlib_tops, subs, errors = _parse_file_worker(str(py))
    assert "os" in stdlib_tops
    assert "os" not in non_stdlib_tops
    assert "PySide2" in non_stdlib_tops
    assert "PySide2" not in stdlib_tops
    assert subs["PySide2"] == frozenset({"QtWidgets"})
    assert errors == []


# ---------- iter-134 AST 并行解析调优测试 ----------


def test_interleave_by_size_distributes_large_files(tmp_path: Path) -> None:
    """``_interleave_by_size`` 将大文件分散到不同 chunk，避免扎堆.

    构造 8 个文件（4 大 4 小），``num_chunks=4``，验证每个 chunk 都含至少一个大文件。
    """
    from fspack.analyzer import _interleave_by_size

    files: list[Path] = []
    for i in range(4):
        big = tmp_path / f"big_{i}.py"
        big.write_text("x = 0\n" * 1000, encoding="utf-8")
        files.append(big)
    for i in range(4):
        small = tmp_path / f"small_{i}.py"
        small.write_text("x = 0\n", encoding="utf-8")
        files.append(small)

    num_chunks = 4
    interleaved = _interleave_by_size(files, num_chunks)
    chunk_size = len(interleaved) // num_chunks
    # 每个 chunk 应含至少一个大文件（大文件分散，不扎堆）
    for i in range(num_chunks):
        chunk = interleaved[i * chunk_size : (i + 1) * chunk_size]
        big_count = sum(1 for p in chunk if p.name.startswith("big_"))
        assert big_count >= 1, f"chunk {i} 无大文件: {[p.name for p in chunk]}"


def test_interleave_by_size_preserves_all_files(tmp_path: Path) -> None:
    """``_interleave_by_size`` 重排后文件集合不变."""
    from fspack.analyzer import _interleave_by_size

    files = [(tmp_path / f"f{i}.py") for i in range(10)]
    for f in files:
        f.write_text("x = 0\n", encoding="utf-8")

    interleaved = _interleave_by_size(files, 4)
    assert set(interleaved) == set(files)
    assert len(interleaved) == len(files)


def test_interleave_by_size_empty_and_single_chunk(tmp_path: Path) -> None:
    """``_interleave_by_size`` 空列表返回空，``num_chunks<=1`` 原序返回."""
    from fspack.analyzer import _interleave_by_size

    assert _interleave_by_size([], 4) == []
    f1 = tmp_path / "a.py"
    f1.write_text("x = 0\n", encoding="utf-8")
    f2 = tmp_path / "b.py"
    f2.write_text("y = 0\n", encoding="utf-8")
    single = _interleave_by_size([f1, f2], 1)
    assert single == [f1, f2]


def test_init_parse_worker_sets_stdlib(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_init_parse_worker`` 设置 worker 状态 ``_WORKER_STATE["stdlib"]``."""
    from fspack import analyzer

    monkeypatch.setattr(analyzer, "_WORKER_STATE", {"stdlib": frozenset()})
    custom = frozenset({"os", "sys", "json"})
    analyzer._init_parse_worker(custom)
    assert custom == analyzer._WORKER_STATE["stdlib"]


def test_parse_file_worker_uses_worker_stdlib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_parse_file_worker`` 用 ``_WORKER_STATE["stdlib"]`` 分离标准库（worker 路径）.

    设置自定义 ``_WORKER_STATE["stdlib"]`` 后，其中的模块应进入 stdlib_tops。
    """
    from fspack import analyzer

    py = tmp_path / "ok.py"
    py.write_text("import os\nimport numpy\n", encoding="utf-8")
    # os 在自定义集合中，numpy 不在
    monkeypatch.setattr(analyzer, "_WORKER_STATE", {"stdlib": frozenset({"os"})})
    non_stdlib_tops, stdlib_tops, subs, errors = analyzer._parse_file_worker(str(py))
    assert "os" in stdlib_tops
    assert "os" not in non_stdlib_tops
    assert "numpy" in non_stdlib_tops
    assert "numpy" not in stdlib_tops
    assert subs == {}
    assert errors == []


def test_parse_file_worker_falls_back_to_module_stdlib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_WORKER_STATE["stdlib"]`` 为空时回退到模块级 ``_STDLIB``（主进程直接调用）."""
    from fspack import analyzer

    py = tmp_path / "ok.py"
    py.write_text("import os\n", encoding="utf-8")
    monkeypatch.setattr(analyzer, "_WORKER_STATE", {"stdlib": frozenset()})
    non_stdlib_tops, stdlib_tops, _subs, _errors = analyzer._parse_file_worker(str(py))
    # 回退到模块级 _STDLIB（含 os）
    assert "os" in stdlib_tops
    assert non_stdlib_tops == []


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


# ---------- iter-138 依赖分析异常容错测试 ----------


def test_analyze_dependencies_records_ast_errors(tmp_path: Path) -> None:
    """``analyze_dependencies`` 将 AST 解析失败记录到 ``ast_errors`` 字段（iter-138）.

    语法错误文件不静默跳过，记录 ``"<相对路径>: <错误信息>"`` 供用户诊断。
    """
    (tmp_path / "bad.py").write_text("def bad(:\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("import sys\n", encoding="utf-8")
    r = analyze_dependencies(tmp_path, "good", ())
    assert "sys" in r.ast_stdlib
    assert r.ast_third_party == ()
    assert len(r.ast_errors) == 1
    assert "bad.py" in r.ast_errors[0]


def test_analyze_dependencies_records_multiple_ast_errors(tmp_path: Path) -> None:
    """多个语法错误文件都记录到 ``ast_errors``（iter-138）."""
    (tmp_path / "bad1.py").write_text("def bad1(:\n", encoding="utf-8")
    (tmp_path / "bad2.py").write_text("import (\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("import os\n", encoding="utf-8")
    r = analyze_dependencies(tmp_path, "good", ())
    assert len(r.ast_errors) == 2
    error_files = {e.split(":")[0] for e in r.ast_errors}
    assert "bad1.py" in error_files
    assert "bad2.py" in error_files


def test_analyze_dependencies_parallel_records_ast_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并行路径下 AST 解析失败也记录到 ``ast_errors``（iter-138）."""
    from fspack import analyzer

    (tmp_path / "bad.py").write_text("def bad(:\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("import os\n", encoding="utf-8")
    # 强制走并行路径
    monkeypatch.setattr(analyzer, "_PARALLEL_THRESHOLD", 1)
    r = analyze_dependencies(tmp_path, "good", ())
    assert "os" in r.ast_stdlib
    assert len(r.ast_errors) == 1
    assert "bad.py" in r.ast_errors[0]


def test_analyze_dependencies_qml_parse_failure_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """QML 文件解析失败（OSError）不阻塞依赖分析主流程（iter-138）.

    ``parse_qml_imports`` 内部已 catch OSError 返回空集合，但 ``analyze_dependencies``
    循环外加防御性 try/except 兜底其他异常场景。
    """
    (tmp_path / "main.py").write_text("from PySide2.QtQml import QQmlApplicationEngine\n", encoding="utf-8")
    (tmp_path / "Main.qml").write_text("import QtQuick 2.15\n", encoding="utf-8")

    def raise_oserror(qml_file: Path) -> set[str]:
        raise OSError("simulated permission error")

    monkeypatch.setattr("fspack.analyzer.parse_qml_imports", raise_oserror)

    # 不抛异常，PySide2.QtQml 仍被收集
    r = analyze_dependencies(tmp_path, "main", ())
    assert "PySide2" in r.ast_third_party
    assert r.ast_submodules.get("PySide2", frozenset()) >= frozenset({"QtQml"})


def test_format_ast_errors_converts_to_relative_path(tmp_path: Path) -> None:
    """``_format_ast_errors`` 将绝对路径转为相对 src_dir 的 POSIX 路径（iter-138）."""
    from fspack.analyzer import _format_ast_errors

    src_dir = tmp_path
    bad_abs = str(tmp_path / "subdir" / "bad.py")
    errors = [(bad_abs, "invalid syntax")]
    formatted = _format_ast_errors(src_dir, errors)
    assert formatted == ["subdir/bad.py: invalid syntax"]


def test_format_ast_errors_falls_back_to_abs_path(tmp_path: Path) -> None:
    """``_format_ast_errors`` 路径不在 src_dir 下时回退到绝对路径（iter-138，不同盘符场景）."""
    from fspack.analyzer import _format_ast_errors

    src_dir = tmp_path
    # ``Z:/...`` 在 Windows 是不同盘符，在 Linux 是相对路径，两者都会让 relative_to 抛 ValueError
    outside_abs = "Z:/nonexistent/bad.py"
    errors = [(outside_abs, "syntax error")]
    formatted = _format_ast_errors(src_dir, errors)
    # 回退到绝对路径（不抛 ValueError）
    assert len(formatted) == 1
    assert "syntax error" in formatted[0]
    assert outside_abs in formatted[0]
