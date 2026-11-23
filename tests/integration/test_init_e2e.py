"""init → ProjectInfo 解析端到端测试.

验证 ``fsp init`` 生成的各模板项目能被 :meth:`ProjectInfo.from_dir` 正确解析，
确保模板生成的 pyproject.toml 格式正确、入口脚本可识别、[tool.fspack] 配置可解析。

不实际执行 build（需 mingw/网络），仅验证 init → 项目元信息解析环节。
实际 build 测试在 ``test_e2e_slow.py``（标记 ``slow``）中。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.cli_init import init_project
from fspack.config import ProjectInfo


def test_init_helloworld_then_parse(tmp_path: Path) -> None:
    """init helloworld → ProjectInfo.from_dir 解析：name/version/app_type=cli."""
    target = init_project("e2e-hello", template_id="helloworld", directory=tmp_path, description="e2e test")
    info = ProjectInfo.from_dir(target)
    assert info.name == "e2e-hello"
    assert info.version == "0.1.0"
    assert info.app_type.value == "cli"
    assert info.entry_module == "e2e_hello"
    assert info.entry_file.is_file()


def test_init_pyside2_then_parse(tmp_path: Path) -> None:
    """init pyside2 → ProjectInfo.from_dir 解析：app_type=gui（PySide2 import 触发）."""
    target = init_project("e2e-gui", template_id="pyside2", directory=tmp_path)
    info = ProjectInfo.from_dir(target)
    assert info.name == "e2e-gui"
    assert info.app_type.value == "gui"
    assert "PySide2" in info.dependencies


def test_init_pyinstaller_then_parse(tmp_path: Path) -> None:
    """init pyinstaller → ProjectInfo.from_dir 解析：build_defaults 含 [tool.fspack] 配置."""
    target = init_project("e2e-pi", template_id="pyinstaller", directory=tmp_path)
    info = ProjectInfo.from_dir(target)
    assert info.name == "e2e-pi"
    # [tool.fspack] 配置项解析
    assert info.build_defaults.pyc_strip is True
    assert info.build_defaults.pyc_optimize == 2
    assert info.build_defaults.no_stdlib_trim is True
    assert info.build_defaults.nuitka is False
    # exclude 配置
    assert "tests" in info.exclude_dirs
    assert "docs" in info.exclude_dirs
    assert ".github" in info.exclude_dirs


def test_init_multi_entry_then_parse(tmp_path: Path) -> None:
    """init multi-entry → ProjectInfo.from_dir 解析：entries 含 cli 与 gui 两个入口."""
    target = init_project("e2e-me", template_id="multi-entry", directory=tmp_path)
    info = ProjectInfo.from_dir(target)
    assert info.name == "e2e-me"
    # 多入口声明 [project.scripts]（PEP 621 标准，dotted module 经 src layout 解析）
    assert len(info.entries) == 2
    entry_names = {e.name for e in info.entries}
    assert entry_names == {"cli", "gui"}
    # cli 入口 → CLI 类型
    cli_entry = next(e for e in info.entries if e.name == "cli")
    assert cli_entry.app_type.value == "cli"
    # gui 入口 → GUI 类型（import tkinter 触发）
    gui_entry = next(e for e in info.entries if e.name == "gui")
    assert gui_entry.app_type.value == "gui"


def test_init_full_config_then_parse(tmp_path: Path) -> None:
    """init full-config → ProjectInfo.from_dir 解析：build_defaults + exclude_dirs."""
    target = init_project("e2e-fc", template_id="full-config", directory=tmp_path, description="full config")
    info = ProjectInfo.from_dir(target)
    assert info.name == "e2e-fc"
    assert info.app_type.value == "cli"
    # [tool.fspack] 实际配置
    assert info.build_defaults.pyc_strip is True
    assert info.build_defaults.pyc_optimize == 2
    assert info.build_defaults.no_stdlib_trim is True
    # exclude 配置
    assert "tests" in info.exclude_dirs
    assert "docs" in info.exclude_dirs


@pytest.mark.parametrize(
    "template_id",
    ["helloworld", "pyside2", "pyside6", "tkinter", "snake", "matplotlib", "numpy", "flask"],
)
def test_init_various_templates_parseable(tmp_path: Path, template_id: str) -> None:
    """各分类代表模板 init 后都能被 ProjectInfo.from_dir 解析（格式正确）."""
    target = init_project(f"e2e-{template_id}", template_id=template_id, directory=tmp_path)
    info = ProjectInfo.from_dir(target)
    assert info.name == f"e2e-{template_id}"
    assert info.version == "0.1.0"
    assert info.entry_file.is_file()
