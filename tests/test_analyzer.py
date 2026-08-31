"""analyzer AST 依赖分析测试."""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
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


def test_analyze_dependencies_excludes_data_dirs(tmp_path: Path) -> None:
    """data-dirs 配置的数据资源目录树不应被扫描，避免模板/前端产物误报依赖.

    模拟 fspack 打包自身场景：src/fspack/assets/init_templates/ 下的 tkinter
    模板含 ``import tkinter``，但这是模板数据资源而非项目自身依赖，应被排除。
    用 ``init_templates`` 路径而非 ``templates``（后者已在 ``_EXCLUDED_DIRS`` 中）。
    """
    (tmp_path / "main.py").write_text("import os\n")
    # 模拟 data-dirs：src/fspack/assets/init_templates/gui/tkinter/
    data_dir = tmp_path / "src" / "fspack" / "assets" / "init_templates" / "gui" / "tkinter"
    data_dir.mkdir(parents=True)
    (data_dir / "entry.py").write_text("import tkinter as tk\nfrom tkinter import ttk\nimport PySide2\n")

    # 不传 data_dirs → tkinter/PySide2 被误扫到（init_templates 不在 _EXCLUDED_DIRS）
    r_no_exclude = analyze_dependencies(tmp_path, "main", ())
    assert "tkinter" in r_no_exclude.ast_stdlib
    assert "PySide2" in r_no_exclude.ast_third_party

    # 传 data_dirs → 数据资源目录被排除
    r_excluded = analyze_dependencies(
        tmp_path,
        "main",
        (),
        data_dirs=("src/fspack/assets/init_templates",),
    )
    assert "tkinter" not in r_excluded.ast_stdlib
    assert "PySide2" not in r_excluded.ast_third_party
    assert r_excluded.ast_third_party == ()


def test_source_fingerprint_excludes_data_dirs(tmp_path: Path) -> None:
    """source_fingerprint 传入 data_dirs 后排除数据资源目录，与 AST 扫描一致.

    指纹必须与 AST 扫描使用相同的排除逻辑，否则 data-dirs 内 .py 变化会
    导致缓存键变化但 AST 结果不变（缓存命中后跳过扫描，浪费），或反之
    （缓存未命中但 .py 未变，重复扫描）。
    用 ``assets/init_templates`` 路径（不在 ``_EXCLUDED_DIRS`` 中）验证。
    """
    from fspack.analyzer import source_fingerprint

    (tmp_path / "main.py").write_text("import os\n")
    data_dir = tmp_path / "assets" / "init_templates"
    data_dir.mkdir(parents=True)
    (data_dir / "tpl.py").write_text("import tkinter\n")

    # 不排除 data-dirs 的指纹（init_templates 不在 _EXCLUDED_DIRS，会被扫描）
    fp_no_exclude = source_fingerprint(tmp_path)
    # 排除 data-dirs 的指纹
    fp_excluded = source_fingerprint(tmp_path, data_dirs=("assets/init_templates",))

    # 两者不同（data-dirs 内 .py 被排除后指纹不同）
    assert fp_no_exclude != fp_excluded

    # 修改 data-dirs 内 .py 后，排除指纹不变（证明被排除）
    # 用不同字节数内容确保 size 变化（避免 mtime 精度问题）
    (data_dir / "tpl.py").write_text("import PySide2 as ps2\nimport asyncio\n")
    fp_after_change = source_fingerprint(tmp_path, data_dirs=("assets/init_templates",))
    assert fp_excluded == fp_after_change

    # 但不排除的指纹会变（size 变化触发指纹变化）
    fp_no_exclude_after = source_fingerprint(tmp_path)
    assert fp_no_exclude != fp_no_exclude_after


def test_is_excluded_venv_prefix_variants(tmp_path: Path) -> None:
    """.venv 前缀目录（.venv38/.venv310 等多版本 venv）被排除，普通 venv 命名不受影响."""
    from fspack.analyzer.fingerprint import _is_excluded, _is_excluded_name

    assert _is_excluded_name(".venv") is True
    assert _is_excluded_name(".venv38") is True
    assert _is_excluded_name(".venv310") is True
    # 非 venv 的点开头目录不误伤（如 .github/.idea）；无点前缀的 venv38 不匹配
    assert _is_excluded_name(".github") is False
    assert _is_excluded_name("venv38") is False

    venv_dir = tmp_path / ".venv38" / "lib" / "site-packages" / "tornado"
    venv_dir.mkdir(parents=True)
    assert _is_excluded(venv_dir / "__init__.py", tmp_path) is True


def test_analyze_dependencies_excludes_versioned_venv_dirs(tmp_path: Path) -> None:
    """.venv38 等多版本 venv 目录下的第三方包 .py 不被扫描（AST 分析口径）."""
    (tmp_path / "main.py").write_text("import os\n")
    venv_dir = tmp_path / ".venv38" / "lib" / "site-packages" / "tornado"
    venv_dir.mkdir(parents=True)
    (venv_dir / "__init__.py").write_text("import cryptography\n")
    r = analyze_dependencies(tmp_path, "main", ())
    assert "cryptography" not in r.ast_third_party
    assert r.ast_third_party == ()


def test_source_fingerprint_excludes_versioned_venv_dirs(tmp_path: Path) -> None:
    """.venv38 等多版本 venv 目录下的 .py 不参与指纹计算（指纹口径与分析一致）."""
    from fspack.analyzer.fingerprint import source_fingerprint

    (tmp_path / "main.py").write_text("import os\n")
    venv_dir = tmp_path / ".venv38" / "lib" / "site-packages" / "tornado"
    venv_dir.mkdir(parents=True)
    (venv_dir / "__init__.py").write_text("import cryptography\n")
    fp_before = source_fingerprint(tmp_path)
    # venv 内文件变化不改变指纹（未参与计算）
    (venv_dir / "extra.py").write_text("import flask\n")
    fp_after = source_fingerprint(tmp_path)
    assert fp_before == fp_after


def test_parallel_spawn_ok_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """loader exe（非 python 解释器）下强制回退串行解析.

    fspack 自举场景：安装版 fsp.exe 是 C loader，无法承载 spawn 的
    ``-c`` 引导命令，进程池 worker 全部立即崩溃。守卫按
    ``sys.executable`` 基名是否 python* 判断，非 python 时走串行。
    """
    from fspack.analyzer import analysis

    pkg = tmp_path / "myproj"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("import os\nimport myproj\n", encoding="utf-8")

    # 基线：python 解释器下守卫通过
    assert analysis._parallel_spawn_ok() is True

    # 模拟 fsp.exe loader：守卫拦截，走串行且结果正确
    monkeypatch.setattr(analysis.sys, "executable", r"C:\Program Files\fspack\fsp.exe")
    assert analysis._parallel_spawn_ok() is False

    calls: list[str] = []
    real_serial = analysis._parse_serial
    real_parallel = analysis._parse_parallel

    def spy_serial(
        py_files: list[Path],
        all_imports_ord: dict[str, None],
        all_stdlib_ord: dict[str, None],
        all_submodules: dict[str, list[str]],
        all_errors: list[tuple[str, str]],
    ) -> None:
        calls.append("serial")
        real_serial(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    def spy_parallel(
        py_files: list[Path],
        all_imports_ord: dict[str, None],
        all_stdlib_ord: dict[str, None],
        all_submodules: dict[str, list[str]],
        all_errors: list[tuple[str, str]],
    ) -> None:
        calls.append("parallel")
        real_parallel(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    monkeypatch.setattr(analysis, "_parse_serial", spy_serial)
    monkeypatch.setattr(analysis, "_parse_parallel", spy_parallel)
    monkeypatch.setattr(analysis, "_PARALLEL_THRESHOLD", 1)
    r = analyze_dependencies(tmp_path, "main", ())
    assert calls == ["serial"]
    assert "os" in r.ast_stdlib


def test_is_excluded_build_and_egg_info_dirs(tmp_path: Path) -> None:
    """构建产物、缓存目录与 egg-info 目录下的文件被排除，普通源码不排除."""
    from fspack.analyzer.fingerprint import _is_excluded

    assert _is_excluded(tmp_path / "build" / "a.py", tmp_path) is True
    assert _is_excluded(tmp_path / "dist" / "a.py", tmp_path) is True
    assert _is_excluded(tmp_path / "pkg.egg-info" / "a.py", tmp_path) is True
    # 排除判断只看目录段（parts[:-1]），不误伤同名源文件
    assert _is_excluded(tmp_path / "build.py", tmp_path) is False
    assert _is_excluded(tmp_path / "src" / "a.py", tmp_path) is False


def test_is_excluded_data_dirs_tree(tmp_path: Path) -> None:
    """data-dirs 目录树内的文件被排除（含子目录），树外文件不受影响."""
    from fspack.analyzer.fingerprint import _is_excluded, _is_in_data_dirs

    data_dir = tmp_path / "assets"
    assert _is_in_data_dirs(data_dir / "sub" / "x.py", (data_dir,)) is True
    assert _is_in_data_dirs(data_dir / "top.py", (data_dir,)) is True  # 含 data-dir 自身
    assert _is_in_data_dirs(tmp_path / "src" / "a.py", (data_dir,)) is False

    assert _is_excluded(data_dir / "sub" / "x.py", tmp_path, (data_dir,)) is True
    assert _is_excluded(tmp_path / "src" / "a.py", tmp_path, (data_dir,)) is False
    # data_dirs 为空元组时不启用 data-dirs 排除（bool(()) 短路）
    assert _is_excluded(data_dir / "x.py", tmp_path, ()) is False


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


def test_fingerprint_excluded_and_data_dirs(tmp_path: Path) -> None:
    """_is_excluded 排除构建产物目录与 data-dirs；_is_in_data_dirs 命中/未命中."""
    from fspack.analyzer.fingerprint import _is_excluded, _is_in_data_dirs

    (tmp_path / "build").mkdir()
    (tmp_path / "app.egg-info").mkdir()
    assets = tmp_path / "assets"
    assets.mkdir()
    assert _is_excluded(tmp_path / "build" / "m.py", tmp_path) is True
    assert _is_excluded(tmp_path / "app.egg-info" / "m.py", tmp_path) is True
    assert _is_excluded(tmp_path / "main.py", tmp_path) is False
    assert _is_excluded(assets / "t.py", tmp_path, (assets.resolve(),)) is True
    assert _is_excluded(tmp_path / "main.py", tmp_path, (assets.resolve(),)) is False
    assert _is_in_data_dirs(assets / "t.py", (assets.resolve(),)) is True
    assert _is_in_data_dirs(tmp_path / "main.py", (assets.resolve(),)) is False


def test_iter_py_entries_prunes_and_drops_out_of_tree_data_dir(tmp_path: Path) -> None:
    """_iter_py_entries 排除 .egg-info/数据目录树；root 树外的 data-dir 被丢弃不报错."""
    from fspack.analyzer.fingerprint import _iter_py_entries

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app.egg-info").mkdir()
    (tmp_path / "app.egg-info" / "meta.py").write_text("x = 1\n", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "tpl.py").write_text("x = 1\n", encoding="utf-8")
    # root 树外的 data-dir：relative_to 触发 ValueError 被丢弃（不参与剪枝也不报错）
    outside = (tmp_path / "..").resolve() / "fsp-out-of-tree-data"

    entries = list(_iter_py_entries(tmp_path, tmp_path, (assets, outside)))
    assert [rel for rel, _, _ in entries] == ["main.py"]


def test_analyze_dependencies_parallel_matches_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并行解析路径与串行路径结果一致.

    通过 monkeypatch 调低 ``_PARALLEL_THRESHOLD`` 强制走并行路径，
    验证 ``ProcessPoolExecutor`` 分发与结果合并的正确性。
    """
    from fspack.analyzer import analysis

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
    monkeypatch.setattr(analysis, "_PARALLEL_THRESHOLD", 10000)
    serial = analyze_dependencies(tmp_path, "main", ())

    # 并行路径
    monkeypatch.setattr(analysis, "_PARALLEL_THRESHOLD", 2)
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


def test_init_parse_worker_sets_stdlib(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_init_parse_worker`` 设置 worker 状态 ``_WORKER_STATE["stdlib"]``."""
    from fspack.analyzer import analysis

    monkeypatch.setattr(analysis, "_WORKER_STATE", {"stdlib": frozenset()})
    custom = frozenset({"os", "sys", "json"})
    analysis._init_parse_worker(custom)
    assert custom == analysis._WORKER_STATE["stdlib"]


def test_parse_file_worker_uses_worker_stdlib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_parse_file_worker`` 用 ``_WORKER_STATE["stdlib"]`` 分离标准库（worker 路径）.

    设置自定义 ``_WORKER_STATE["stdlib"]`` 后，其中的模块应进入 stdlib_tops。
    """
    from fspack.analyzer import analysis

    py = tmp_path / "ok.py"
    py.write_text("import os\nimport numpy\n", encoding="utf-8")
    # os 在自定义集合中，numpy 不在
    monkeypatch.setattr(analysis, "_WORKER_STATE", {"stdlib": frozenset({"os"})})
    non_stdlib_tops, stdlib_tops, subs, errors = analysis._parse_file_worker(str(py))
    assert "os" in stdlib_tops
    assert "os" not in non_stdlib_tops
    assert "numpy" in non_stdlib_tops
    assert "numpy" not in stdlib_tops
    assert subs == {}
    assert errors == []


def test_parse_file_worker_falls_back_to_module_stdlib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_WORKER_STATE["stdlib"]`` 为空时回退到模块级 ``_STDLIB``（主进程直接调用）."""
    from fspack.analyzer import analysis

    py = tmp_path / "ok.py"
    py.write_text("import os\n", encoding="utf-8")
    monkeypatch.setattr(analysis, "_WORKER_STATE", {"stdlib": frozenset()})
    non_stdlib_tops, stdlib_tops, _subs, _errors = analysis._parse_file_worker(str(py))
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
    from fspack.analyzer import analysis

    (tmp_path / "bad.py").write_text("def bad(:\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("import os\n", encoding="utf-8")
    # 强制走并行路径
    monkeypatch.setattr(analysis, "_PARALLEL_THRESHOLD", 1)
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

    monkeypatch.setattr("fspack.analyzer.analysis.parse_qml_imports", raise_oserror)

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


# ---------- AST 解析异常容错增强测试（ValueError/RecursionError） ----------


def test_analyze_dependencies_nul_byte_records_ast_error(tmp_path: Path) -> None:
    """源码含 NUL 字节触发 ValueError，记入 ast_errors 而非崩溃."""
    (tmp_path / "bad.py").write_bytes(b"import os\n\x00def f(:\n")
    (tmp_path / "good.py").write_text("import sys\n", encoding="utf-8")
    r = analyze_dependencies(tmp_path, "good", ())
    assert "sys" in r.ast_stdlib
    assert len(r.ast_errors) == 1
    assert "bad.py" in r.ast_errors[0]


def test_analyze_dependencies_deeply_nested_records_ast_error(tmp_path: Path) -> None:
    """深度嵌套源码触发 RecursionError，记入 ast_errors 而非崩溃."""
    (tmp_path / "deep.py").write_text("x = " + "(" * 50000 + "1" + ")" * 50000, encoding="utf-8")
    (tmp_path / "good.py").write_text("import sys\n", encoding="utf-8")
    r = analyze_dependencies(tmp_path, "good", ())
    assert "sys" in r.ast_stdlib
    assert len(r.ast_errors) == 1
    assert "deep.py" in r.ast_errors[0]


def test_parse_file_worker_catches_value_and_recursion_error(tmp_path: Path) -> None:
    """worker 函数对 NUL 字节与深度嵌套源码返回错误记录（并行路径同等容错）."""
    from fspack.analyzer import _parse_file_worker

    nul = tmp_path / "nul.py"
    nul.write_bytes(b"\x00import os\n")
    _non_stdlib, _stdlib, _subs, errors = _parse_file_worker(str(nul))
    assert len(errors) == 1

    deep = tmp_path / "deep.py"
    deep.write_text("y = " + "[" * 50000 + "]" * 50000, encoding="utf-8")
    _non_stdlib, _stdlib, _subs, errors = _parse_file_worker(str(deep))
    assert len(errors) == 1


def test_stdlib_fallback_underscore_modules() -> None:
    """3.8/3.9 回退集合含常见下划线 C 模块，避免误判为第三方依赖."""
    for mod in (
        "_io",
        "_thread",
        "_weakref",
        "_collections",
        "_functools",
        "_socket",
        "_json",
        "__main__",
        "_ast",
    ):
        assert mod in STDLIB_FALLBACK, mod


# ---------- 指纹纳入 QML 测试 ----------


def test_source_fingerprint_includes_qml_changes(tmp_path: Path) -> None:
    """QML 文件参与源码指纹：修改 .qml 触发指纹变化（与 analyze_dependencies 范围一致）.

    QML 修改会改变依赖产物（Qt 子模块保留集合），指纹若不含 .qml 会导致
    deps 缓存不失效、产物静默缺 DLL。
    """
    from fspack.analyzer import source_fingerprint

    (tmp_path / "main.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "Main.qml").write_text("import QtQuick 2.15\n", encoding="utf-8")
    fp_before = source_fingerprint(tmp_path)

    # 修改 QML（内容长度不同确保 size 变化，避免 mtime 精度问题）
    (tmp_path / "Main.qml").write_text("import QtQuick 2.15\nimport QtCharts 2.15\n", encoding="utf-8")
    fp_after = source_fingerprint(tmp_path)
    assert fp_before != fp_after

    # 新增 QML 文件同样触发指纹变化
    (tmp_path / "Other.qml").write_text("import QtQuick 2.15\n", encoding="utf-8")
    assert fp_after != source_fingerprint(tmp_path)


# ---------- 并行解析容错测试（BrokenProcessPool / 超时 shutdown） ----------


class _StubFuture:
    """测试桩：预置 result 返回值或异常，done/cancel 固定返回（仅供 ``_parse_parallel`` 消费）."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def done(self) -> bool:
        return True

    def cancel(self) -> bool:
        return False

    def result(self) -> object:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _StubExecutor:
    """测试桩：submit 按序弹回预置 payload，记录 shutdown 的 wait 参数."""

    def __init__(self, payloads: list[object]) -> None:
        self._payloads = list(payloads)
        self.shutdown_waits: list[bool] = []

    def submit(self, fn: object, arg: object) -> _StubFuture:
        # 空 payload 默认值：与 _parse_file_worker 返回结构一致的空结果（注解供类型检查）
        empty: tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]] = ([], [], {}, [])
        payload = self._payloads.pop(0) if self._payloads else empty
        return _StubFuture(payload)

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_waits.append(wait)


def _fake_as_completed(futures: list[_StubFuture], timeout: float | None = None) -> Iterator[_StubFuture]:
    """测试桩：直接按序 yield futures，替代真实 as_completed 的完成顺序调度."""
    yield from futures


def test_parse_parallel_broken_pool_preserves_aggregated_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 崩溃（BrokenProcessPool，如 OOM）时保留已聚合结果，不整单失败."""
    from concurrent.futures.process import BrokenProcessPool

    from fspack.analyzer import analysis

    stub = _StubExecutor([(["os"], [], {}, []), BrokenProcessPool("simulated worker OOM")])
    monkeypatch.setattr(analysis, "ProcessPoolExecutor", lambda **kwargs: stub)
    monkeypatch.setattr(analysis, "as_completed", _fake_as_completed)

    imports_ord: dict[str, None] = {}
    stdlib_ord: dict[str, None] = {}
    submodules: dict[str, list[str]] = {}
    errors: list[tuple[str, str]] = []
    analysis._parse_parallel([Path("a.py"), Path("b.py")], imports_ord, stdlib_ord, submodules, errors)
    # 第一个 worker 的结果已聚合保留，第二个崩溃不吞掉已完成部分
    assert "os" in imports_ord
    # 正常结束路径：finally 中 shutdown(wait=True)
    assert stub.shutdown_waits == [True]


def test_parse_parallel_timeout_shutdowns_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行超时分支 cancel 后 shutdown(wait=False) 立即返回，不再无限等待卡死 worker."""
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from fspack.analyzer import analysis

    def _as_completed_timeout(futures: list[_StubFuture], timeout: float | None = None) -> Iterator[_StubFuture]:
        yield futures[0]
        raise FuturesTimeoutError

    stub = _StubExecutor([(["os"], [], {}, []), ([], [], {}, [])])
    monkeypatch.setattr(analysis, "ProcessPoolExecutor", lambda **kwargs: stub)
    monkeypatch.setattr(analysis, "as_completed", _as_completed_timeout)

    imports_ord: dict[str, None] = {}
    stdlib_ord: dict[str, None] = {}
    submodules: dict[str, list[str]] = {}
    errors: list[tuple[str, str]] = []
    analysis._parse_parallel([Path("a.py"), Path("b.py")], imports_ord, stdlib_ord, submodules, errors)
    # 超时前已完成的结果保留
    assert "os" in imports_ord
    # 超时分支 shutdown(wait=False)，timed_out 标志使 finally 跳过重复 shutdown
    assert stub.shutdown_waits == [False]


# ---- _parse_parallel 超时防护测试（iter-127） ----


def test_parse_parallel_timeout_warns_on_slow_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_parse_parallel`` 整体超时后 warning 提示，未完成 future 被 cancel（iter-138 改 submit+as_completed）.

    用 fake ``as_completed`` 抛 ``TimeoutError`` 模拟超时。验证 warning 日志输出
    与未完成 future 的 cancel 调用（fake future 的 ``done()`` 返回 False 触发 cancel）。
    """
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _parse_parallel

    # 构造 5 个 .py 文件
    for i in range(5):
        (tmp_path / f"mod_{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    cancel_calls: list[bool] = []
    shutdown_calls: list[bool] = []

    class _FakeFuture:
        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            cancel_calls.append(True)
            return True

        def result(
            self,
        ) -> tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]:
            return [], [], {}, []

    class _FakePool:
        def __init__(self, *args: object, **kw: object) -> None:
            pass

        def __enter__(self) -> _FakePool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> _FakeFuture:
            return _FakeFuture()

        def shutdown(self, wait: bool = True) -> None:
            shutdown_calls.append(wait)

    monkeypatch.setattr(analysis, "ProcessPoolExecutor", _FakePool)

    def fake_as_completed(futures: object, timeout: float | None = None) -> object:
        raise FuturesTimeoutError("simulated timeout")

    monkeypatch.setattr(analysis, "as_completed", fake_as_completed)

    all_imports_ord: dict[str, None] = {}
    all_stdlib_ord: dict[str, None] = {}
    all_submodules: dict[str, list[str]] = {}
    all_errors: list[tuple[str, str]] = []

    with caplog.at_level(logging.WARNING, logger="fspack.analyzer"):
        _parse_parallel(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    # 超时 warning
    timeout_logs = [r for r in caplog.records if "超时" in r.message]
    assert len(timeout_logs) == 1
    assert "AST 并行解析" in timeout_logs[0].message
    # 5 个 future 都被 cancel（done() 返回 False）
    assert len(cancel_calls) == 5
    # 超时后 imports/submodules/errors 为空（fake as_completed 抛异常未返回结果）
    assert all_imports_ord == {}
    assert all_stdlib_ord == {}
    assert all_submodules == {}
    assert all_errors == []


def test_parse_parallel_normal_completes_without_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正常完成的并行解析不触发超时，结果完整."""
    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _parse_parallel

    for i in range(5):
        (tmp_path / f"mod_{i}.py").write_text(f"import os\nx = {i}\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    # 设较长 timeout 确保正常完成
    monkeypatch.setattr(analysis, "_PARSE_TOTAL_TIMEOUT", 60.0)

    all_imports_ord: dict[str, None] = {}
    all_stdlib_ord: dict[str, None] = {}
    all_submodules: dict[str, list[str]] = {}
    all_errors: list[tuple[str, str]] = []

    _parse_parallel(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    # 5 个文件都 import os（dict 去重保序，"os" 只出现一次）
    assert "os" in all_stdlib_ord
    assert all_imports_ord == {}
    assert all_errors == []


def test_parse_parallel_timeout_constant_default() -> None:
    """``_PARSE_TOTAL_TIMEOUT`` 默认 300s."""
    from fspack.analyzer import _PARSE_TOTAL_TIMEOUT

    assert _PARSE_TOTAL_TIMEOUT == 300.0


def test_parse_parallel_uses_initializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_parse_parallel`` 用 ``initializer`` 预加载 ``_STDLIB`` 传给 worker（iter-134）."""
    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _init_parse_worker, _parse_parallel
    from fspack.analyzer.ast_scan import _STDLIB

    for i in range(5):
        (tmp_path / f"mod_{i}.py").write_text("x = 0\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    captured: dict[str, object] = {}

    class _FakeFuture:
        def done(self) -> bool:
            return True

        def cancel(self) -> bool:
            return False

        def result(
            self,
        ) -> tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]:
            return [], [], {}, []

    class _Pool:
        def __init__(self, *args: object, **kw: object) -> None:
            captured.update(kw)

        def __enter__(self) -> _Pool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> _FakeFuture:
            return _FakeFuture()

        def shutdown(self, wait: bool = True) -> None:
            pass

    monkeypatch.setattr(analysis, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(analysis, "as_completed", lambda futures, timeout=None: iter(futures))
    _parse_parallel(py_files, {}, {}, {}, [])

    assert captured.get("initializer") is _init_parse_worker
    assert captured.get("initargs") == (_STDLIB,)


def test_parse_parallel_interleave_and_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_parse_parallel`` 对每个文件 submit 一个 future（iter-138 改 submit 逐文件提交）.

    旧 ``_interleave_by_size`` 为 ``map(chunksize=)`` 连续分块设计，submit 模式下
    进程池 FIFO 队列天然负载均衡，已删除——本测试验证 submit 调用次数等于文件数。
    """
    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _parse_parallel

    for i in range(20):
        (tmp_path / f"mod_{i}.py").write_text("x = 0\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    submit_calls: list[str] = []

    class _FakeFuture:
        def done(self) -> bool:
            return True

        def cancel(self) -> bool:
            return False

        def result(
            self,
        ) -> tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]:
            return [], [], {}, []

    class _Pool:
        def __init__(self, *args: object, **kw: object) -> None:
            pass

        def __enter__(self) -> _Pool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> _FakeFuture:
            # args[0] 是文件路径 str
            submit_calls.append(str(args[0]) if args else "")
            return _FakeFuture()

        def shutdown(self, wait: bool = True) -> None:
            pass

    monkeypatch.setattr(analysis, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(analysis, "as_completed", lambda futures, timeout=None: iter(futures))
    _parse_parallel(py_files, {}, {}, {}, [])

    # 20 个文件每个 submit 一次（submit 替代 map+chunksize，无需 interleave 重排）
    assert len(submit_calls) == 20


def test_parse_parallel_partial_timeout_aggregates_completed_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_parse_parallel`` 部分 worker 超时时，已完成 worker 的结果仍被聚合（iter-138）.

    ``map(timeout=)`` 在首个 future 卡死时丢弃后续已完成结果；``submit`` + ``as_completed``
    按完成顺序 yield，超时前已完成的 future 结果被聚合，未完成的被 cancel。
    """
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _parse_parallel

    for i in range(5):
        (tmp_path / f"mod_{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    completed_results: list[tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]] = [
        (["numpy"], ["os"], {}, []),
        (["requests"], ["sys"], {}, []),
        (["flask"], [], {}, []),
    ]

    class _DoneFuture:
        def __init__(self, result: object) -> None:
            self._result = result

        def done(self) -> bool:
            return True

        def cancel(self) -> bool:
            return False

        def result(self) -> object:
            return self._result

    class _PendingFuture:
        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            return True

    futures_chain: list[object] = [
        _DoneFuture(completed_results[0]),
        _DoneFuture(completed_results[1]),
        _DoneFuture(completed_results[2]),
        _PendingFuture(),
        _PendingFuture(),
    ]

    class _FakePool:
        def __init__(self, *args: object, **kw: object) -> None:
            pass

        def __enter__(self) -> _FakePool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> object:
            return futures_chain.pop(0)

        def shutdown(self, wait: bool = True) -> None:
            pass

    monkeypatch.setattr(analysis, "ProcessPoolExecutor", _FakePool)

    def fake_as_completed(futures: object, timeout: float | None = None) -> Iterator[object]:
        # 前 3 个已完成的 yield，然后抛 TimeoutError 模拟后 2 个超时
        futures_list = list(futures)  # type: ignore[arg-type]
        yield from futures_list[:3]
        raise FuturesTimeoutError("partial timeout")

    monkeypatch.setattr(analysis, "as_completed", fake_as_completed)

    all_imports_ord: dict[str, None] = {}
    all_stdlib_ord: dict[str, None] = {}
    all_submodules: dict[str, list[str]] = {}
    all_errors: list[tuple[str, str]] = []

    _parse_parallel(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    # 已完成的 3 个 future 结果被聚合（关键改进：map(timeout=) 会丢失这些结果）
    assert "numpy" in all_imports_ord
    assert "requests" in all_imports_ord
    assert "flask" in all_imports_ord
    assert "os" in all_stdlib_ord
    assert "sys" in all_stdlib_ord
