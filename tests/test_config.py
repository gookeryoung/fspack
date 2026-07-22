"""config 数据结构测试."""

from __future__ import annotations

from pathlib import Path

from fspack.config import AppType, BuildConfig, DependencyReport, EntryPoint, MirrorConfig, ProjectInfo
from fspack.mirror import MIRRORS
from fspack.platform import Platform

_EXAMPLES = Path(__file__).parent.parent / "examples"


def test_mirror_config_embed_url() -> None:
    m = MirrorConfig(name="t", python_base="https://x/py", pypi_index="https://x/simple")
    assert m.embed_url("3.11.9") == "https://x/py/3.11.9/python-3.11.9-embed-amd64.zip"


def test_project_info_from_dir_helloworld() -> None:
    """from_dir 类方法解析 cli_helloworld 示例."""
    info = ProjectInfo.from_dir(_EXAMPLES / "cli_helloworld")
    assert info.name == "cli_helloworld"
    assert info.entry_module == "helloworld"
    assert info.entry_file.name == "helloworld.py"
    assert info.app_type is AppType.CLI
    assert info.exe_name == "cli_helloworld.exe"
    assert info.py_xy == "python311"


def test_project_info_from_dir_with_explicit_py_version(tmp_path: Path) -> None:
    """from_dir 接受 py_version 参数透传给 parse_project."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1"\n')
    (tmp_path / "p.py").write_text("def main():\n    pass\n")
    info = ProjectInfo.from_dir(tmp_path, "3.10.0")
    assert info.py_version == "3.10.0"


def test_project_info_from_dir_pyside2_app() -> None:
    """from_dir 解析 GUI 示例并读取 requires-python 约束."""
    info = ProjectInfo.from_dir(_EXAMPLES / "pyside2_app")
    assert info.requires_python == ">=3.8,<3.11"
    assert info.app_type is AppType.GUI


def test_dependency_report_from_src_classification(tmp_path: Path) -> None:
    """from_src 类方法扫描源码并分类依赖."""
    (tmp_path / "main.py").write_text("import os\nimport numpy\nimport requests\nfrom json import loads\n")
    r = DependencyReport.from_src(tmp_path, "main", ("numpy>=1.0",))
    assert "os" in r.ast_stdlib
    assert "json" in r.ast_stdlib
    assert "numpy" in r.ast_third_party
    assert "requests" in r.ast_third_party
    assert "requests" in r.missing
    assert "numpy" not in r.missing


def test_dependency_report_from_src_submodules(tmp_path: Path) -> None:
    """from_src 收集子模块 import."""
    (tmp_path / "main.py").write_text("from PySide2.QtCore import QTimer\nfrom PySide2.QtWidgets import QApplication\n")
    r = DependencyReport.from_src(tmp_path, "main", ())
    assert r.ast_submodules["PySide2"] == frozenset({"QtCore", "QtWidgets"})


def test_project_info_exe_and_pyxy() -> None:
    info = ProjectInfo(
        name="hw",
        version="0.1",
        src_dir=Path(),
        entry_module="hw",
        entry_file=Path("hw.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
    )
    assert info.exe_name == "hw.exe"
    assert info.py_xy == "python311"


def test_project_info_pyxy_312() -> None:
    info = ProjectInfo(
        name="hw",
        version="0.1",
        src_dir=Path(),
        entry_module="hw",
        entry_file=Path("hw.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.12.3",
    )
    assert info.py_xy == "python312"


def test_dependency_report_missing() -> None:
    r = DependencyReport(
        declared=("numpy>=1.0", "requests"),
        ast_third_party=("numpy", "Flask"),
        ast_stdlib=(),
        ast_local=(),
    )
    assert r.missing == ("Flask",)


def test_dependency_report_missing_case_insensitive() -> None:
    r = DependencyReport(
        declared=("Flask",),
        ast_third_party=("flask",),
        ast_stdlib=(),
        ast_local=(),
    )
    assert r.missing == ()


def test_dependency_report_missing_empty() -> None:
    r = DependencyReport(declared=(), ast_third_party=(), ast_stdlib=(), ast_local=())
    assert r.missing == ()


def test_build_config_defaults() -> None:
    cfg = BuildConfig(
        project_dir=Path("/p"),
        dist_dir=Path("/p/dist"),
        embed_cache_dir=Path("/c"),
        mirror=MIRRORS["huawei"],
    )
    assert cfg.target == Platform.WINDOWS


def test_apptype_values() -> None:
    assert AppType.CLI.value == "cli"
    assert AppType.GUI.value == "gui"


# --- 多入口 all_entries 测试 ---


def test_project_info_all_entries_single() -> None:
    """单入口模式（entries 空）all_entries 构造单一入口."""
    info = ProjectInfo(
        name="app",
        version="0.1",
        src_dir=Path(),
        entry_module="app",
        entry_file=Path("app.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
    )
    entries = info.all_entries
    assert len(entries) == 1
    assert entries[0].name == "app"
    assert entries[0].module == "app"
    assert entries[0].app_type is AppType.CLI


def test_project_info_all_entries_multi() -> None:
    """多入口模式 all_entries 返回 entries 字段."""
    ep1 = EntryPoint(name="cli", module="cli", file=Path("cli.py"), app_type=AppType.CLI)
    ep2 = EntryPoint(name="gui", module="gui", file=Path("gui.py"), app_type=AppType.GUI)
    info = ProjectInfo(
        name="multi",
        version="0.1",
        src_dir=Path(),
        entry_module="cli",
        entry_file=Path("cli.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.10.11",
        entries=(ep1, ep2),
    )
    assert info.all_entries == (ep1, ep2)


def test_project_info_from_dir_multi_entry() -> None:
    """from_dir 解析 multi_entry 示例返回多个入口."""
    info = ProjectInfo.from_dir(_EXAMPLES / "multi_entry")
    assert len(info.entries) == 3
    assert info.all_entries == info.entries
    assert info.all_entries[0].name == "cli"
