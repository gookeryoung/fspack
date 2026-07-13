"""analyzer AST 依赖分析测试。."""

from __future__ import annotations

import ast
from pathlib import Path

from fspack.analyzer import STDLIB_FALLBACK, analyze_dependencies, collect_imports, collect_submodule_imports


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
    """import X.Y 收集 {X: {Y}}。."""
    tree = _tree("import os.path\nimport numpy.core\n")
    result = collect_submodule_imports(tree)
    assert result == {"os": frozenset({"path"}), "numpy": frozenset({"core"})}


def test_collect_submodule_imports_from_dotted() -> None:
    """from X.Y import Z 收集 {X: {Y}}。."""
    tree = _tree("from PySide2.QtWidgets import QApplication\n")
    assert collect_submodule_imports(tree) == {"PySide2": frozenset({"QtWidgets"})}


def test_collect_submodule_imports_from_simple() -> None:
    """from X import Y 收集 {X: {Y}}（Y 可能是类名，不匹配 wheel 文件时自然忽略）。."""
    tree = _tree("from flask import Flask\n")
    assert collect_submodule_imports(tree) == {"flask": frozenset({"Flask"})}


def test_collect_submodule_imports_relative_skipped() -> None:
    """相对导入跳过。."""
    tree = _tree("from .sub import bar\nfrom . import foo\n")
    assert collect_submodule_imports(tree) == {}


def test_collect_submodule_imports_star_skipped() -> None:
    """星号导入跳过。."""
    tree = _tree("from numpy import *\n")
    assert collect_submodule_imports(tree) == {}


def test_collect_submodule_imports_empty() -> None:
    """无 import 返回空字典。."""
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
    """dist/build/.venv 等目录下的 .py 不应被扫描，避免误报标准库内部模块为第三方依赖。."""
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


def test_analyze_dependencies_submodules(tmp_path: Path) -> None:
    """第三方包的子模块 import 被收集到 ast_submodules。."""
    (tmp_path / "main.py").write_text("from PySide2.QtCore import QTimer\nfrom PySide2.QtWidgets import QApplication\n")
    r = analyze_dependencies(tmp_path, "main", ())
    assert r.ast_submodules["PySide2"] == frozenset({"QtCore", "QtWidgets"})


def test_analyze_dependencies_submodules_stdlib_filtered(tmp_path: Path) -> None:
    """标准库的子模块 import 不进入 ast_submodules。."""
    (tmp_path / "main.py").write_text("import os.path\nfrom json import loads\n")
    r = analyze_dependencies(tmp_path, "main", ())
    assert "os" not in r.ast_submodules
    assert "json" not in r.ast_submodules
