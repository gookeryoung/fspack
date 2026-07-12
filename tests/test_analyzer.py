"""analyzer AST 依赖分析测试。."""

from __future__ import annotations

import ast
from pathlib import Path

from fspack.analyzer import STDLIB_FALLBACK, analyze_dependencies, collect_imports


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
