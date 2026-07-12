"""config 数据结构测试。."""

from __future__ import annotations

from pathlib import Path

from fspack.config import AppType, BuildConfig, DependencyReport, MirrorConfig, ProjectInfo
from fspack.mirror import MIRRORS
from fspack.platform import Platform


def test_mirror_config_embed_url() -> None:
    m = MirrorConfig(name="t", python_base="https://x/py", pypi_index="https://x/simple")
    assert m.embed_url("3.11.9") == "https://x/py/3.11.9/python-3.11.9-embed-amd64.zip"


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
