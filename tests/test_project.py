"""project pyproject.toml 解析与入口识别测试。."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.config import AppType, EntryPoint
from fspack.exceptions import ProjectError
from fspack.project import DEFAULT_PY_VERSION, detect_entry, infer_app_type, parse_project, resolve_py_version

_EXAMPLES = Path(__file__).parent.parent / "examples"


def test_parse_project_helloworld() -> None:
    info = parse_project(_EXAMPLES / "cli_helloworld")
    assert info.name == "cli_helloworld"
    assert info.entry_module == "helloworld"
    assert info.entry_file.name == "helloworld.py"
    assert info.app_type is AppType.CLI
    assert info.exe_name == "cli_helloworld.exe"
    assert info.py_xy == "python311"
    assert info.py_version == DEFAULT_PY_VERSION
    assert info.requires_python is None


def test_parse_project_pyside2app_requires_python() -> None:
    """pyside2app 示例的 requires-python 约束正确解析。."""
    info = parse_project(_EXAMPLES / "pyside2_app")
    assert info.requires_python == ">=3.8,<3.11"
    assert info.app_type is AppType.GUI


def test_resolve_py_version_pyside2app_example() -> None:
    """pyside2app 示例：.python-version=3.9 + requires-python 解析到 3.9.13。."""
    info = parse_project(_EXAMPLES / "pyside2_app")
    resolved = resolve_py_version(_EXAMPLES / "pyside2_app", None, info.requires_python)
    assert resolved == "3.9.13"


def test_parse_project_missing_pyproject(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match=r"未找到 pyproject\.toml"):
        parse_project(tmp_path)


def test_parse_project_bad_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("this is = = not valid {{{")
    with pytest.raises(ProjectError, match="语法错误"):
        parse_project(tmp_path)


def test_parse_project_uses_dir_name_when_no_name(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0"\n')
    (tmp_path / "myproj.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path, "3.10.0")
    assert info.name == tmp_path.name
    assert info.py_version == "3.10.0"


def test_detect_entry_main_func(tmp_path: Path) -> None:
    f = tmp_path / "app.py"
    f.write_text("def main():\n    print('hi')\n")
    mod, path, app = detect_entry(tmp_path, "app")
    assert mod == "app"
    assert path == f
    assert app is AppType.CLI


def test_detect_entry_main_block(tmp_path: Path) -> None:
    f = tmp_path / "app.py"
    f.write_text('if __name__ == "__main__":\n    print("hi")\n')
    mod, _, _ = detect_entry(tmp_path, "app")
    assert mod == "app"


def test_detect_entry_no_entry(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("print('no main')\n")
    with pytest.raises(ProjectError, match="未识别到入口"):
        detect_entry(tmp_path, "x")


def test_detect_entry_package_main(tmp_path: Path) -> None:
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text("def main():\n    pass\n")
    mod, path, _ = detect_entry(tmp_path, "app")
    assert mod == "app"
    assert path == pkg / "__main__.py"


def test_detect_entry_gui_via_tkinter(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import tkinter\ndef main():\n    pass\n")
    _, _, app = detect_entry(tmp_path, "app")
    assert app is AppType.GUI


def test_detect_entry_gui_via_declared_dep(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    _, _, app = detect_entry(tmp_path, "app", ("PyQt5>=5",))
    assert app is AppType.GUI


def test_detect_entry_prefers_name_match(tmp_path: Path) -> None:
    (tmp_path / "other.py").write_text("def main():\n    pass\n")
    named = tmp_path / "app.py"
    named.write_text("def main():\n    pass\n")
    _, path, _ = detect_entry(tmp_path, "app")
    assert path == named


def test_parse_project_project_section_not_dict(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('project = "not a dict"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match=r"\[project\] 节格式异常"):
        parse_project(tmp_path)


def test_detect_entry_skips_syntax_error_file(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def bad(:\n    pass\n")
    (tmp_path / "other.py").write_text("def main():\n    pass\n")
    mod, path, _ = detect_entry(tmp_path, "app")
    assert mod == "other"
    assert path.name == "other.py"


def test_detect_entry_dedup_same_name_no_entry(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "other.py").write_text("def main():\n    pass\n")
    mod, path, _ = detect_entry(tmp_path, "app")
    assert mod == "other"
    assert path.name == "other.py"


def test_detect_entry_cli_with_multiple_non_gui_deps(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    _, _, app = detect_entry(tmp_path, "app", ("requests>=2", "numpy>=1"))
    assert app is AppType.CLI


# --- resolve_py_version 测试 ---


def test_resolve_py_version_explicit(tmp_path: Path) -> None:
    """显式 --py-version 始终优先。."""
    assert resolve_py_version(tmp_path, "3.10.0", None) == "3.10.0"


def test_resolve_py_version_explicit_overrides_requires_python(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """显式版本不满足 requires-python 时告警但仍使用。."""
    with caplog.at_level("WARNING", logger="fspack.project"):
        result = resolve_py_version(tmp_path, "3.12.0", ">=3.8,<3.11")
    assert result == "3.12.0"
    assert "不满足 requires-python" in caplog.text


def test_resolve_py_version_python_version_file(tmp_path: Path) -> None:
    """有 .python-version 文件时映射到完整版本。."""
    (tmp_path / ".python-version").write_text("3.9")
    assert resolve_py_version(tmp_path, None, None) == "3.9.13"


def test_resolve_py_version_python_version_file_full_version(tmp_path: Path) -> None:
    """.python-version 含完整版本号时直接使用。."""
    (tmp_path / ".python-version").write_text("3.10.5")
    assert resolve_py_version(tmp_path, None, None) == "3.10.5"


def test_resolve_py_version_python_version_satisfies_requires_python(tmp_path: Path) -> None:
    """.python-version 满足 requires-python 时直接使用。."""
    (tmp_path / ".python-version").write_text("3.9")
    assert resolve_py_version(tmp_path, None, ">=3.8,<3.11") == "3.9.13"


def test_resolve_py_version_python_version_violates_requires_python(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """.python-version 不满足 requires-python 时告警并自动选择。."""
    (tmp_path / ".python-version").write_text("3.12")
    with caplog.at_level("WARNING", logger="fspack.project"):
        result = resolve_py_version(tmp_path, None, ">=3.8,<3.11")
    assert result == "3.10.11"
    assert "不满足 requires-python" in caplog.text


def test_resolve_py_version_auto_select_highest_compatible(tmp_path: Path) -> None:
    """无 .python-version 时按 requires-python 自动选最高兼容版本。."""
    assert resolve_py_version(tmp_path, None, ">=3.8,<3.11") == "3.10.11"
    assert resolve_py_version(tmp_path, None, ">=3.8") == "3.12.0"
    assert resolve_py_version(tmp_path, None, "<3.10") == "3.9.13"


def test_resolve_py_version_no_constraints(tmp_path: Path) -> None:
    """无任何约束时返回 default。."""
    assert resolve_py_version(tmp_path, None, None) == DEFAULT_PY_VERSION


def test_resolve_py_version_custom_default(tmp_path: Path) -> None:
    """无约束时使用自定义 default。."""
    assert resolve_py_version(tmp_path, None, None, default="3.11.10") == "3.11.10"


def test_resolve_py_version_unsatisfiable_requires_python(tmp_path: Path) -> None:
    """requires-python 无法满足时抛 ProjectError。."""
    with pytest.raises(ProjectError, match="无已知兼容 embed python 版本"):
        resolve_py_version(tmp_path, None, ">=4.0")


def test_resolve_py_version_complex_specifier(tmp_path: Path) -> None:
    """复杂规范符 >=3.9,<3.12 选 3.11.9。."""
    assert resolve_py_version(tmp_path, None, ">=3.9,<3.12") == "3.11.9"


# --- 多入口解析测试 ---


def test_parse_project_multi_entry_example() -> None:
    """multi_entry 示例：[tool.fspack.entries] 解析为三个入口。."""
    info = parse_project(_EXAMPLES / "multi_entry")
    assert len(info.entries) == 3
    assert [ep.name for ep in info.entries] == ["cli", "gui", "web"]
    # 首个入口作为主入口（向后兼容）
    assert info.entry_module == "cli"
    assert info.entry_file.name == "cli.py"
    assert info.app_type is AppType.CLI
    # 每个入口按自身 import 推断类型（不看项目级 declared PySide2）
    assert info.entries[0].app_type is AppType.CLI
    assert info.entries[1].app_type is AppType.GUI
    assert info.entries[2].app_type is AppType.CLI


def test_parse_project_multi_entry_single_declared_compat(tmp_path: Path) -> None:
    """无 [tool.fspack.entries] 时走单入口 detect_entry 路径，entries 为空。."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.entries == ()
    assert info.entry_module == "app"


def test_parse_project_multi_entry_missing_script(tmp_path: Path) -> None:
    """[tool.fspack.entries] 中脚本不存在时报错。."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack.entries]\nmain = "missing.py"\n'
    )
    with pytest.raises(ProjectError, match="脚本不存在"):
        parse_project(tmp_path)


def test_parse_project_multi_entry_empty_path(tmp_path: Path) -> None:
    """[tool.fspack.entries] 中脚本路径为空时报错。."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack.entries]\nmain = ""\n'
    )
    with pytest.raises(ProjectError, match="脚本路径为空"):
        parse_project(tmp_path)


def test_infer_app_type_by_import(tmp_path: Path) -> None:
    """infer_app_type 按脚本 import 推断类型。."""
    gui = tmp_path / "gui.py"
    gui.write_text("import PySide2\ndef main():\n    pass\n")
    assert infer_app_type(gui, ()) is AppType.GUI

    cli = tmp_path / "cli.py"
    cli.write_text("import sys\ndef main():\n    pass\n")
    assert infer_app_type(cli, ()) is AppType.CLI


def test_infer_app_type_by_declared(tmp_path: Path) -> None:
    """infer_app_type 按声明依赖推断类型（单入口模式）。."""
    cli = tmp_path / "cli.py"
    cli.write_text("def main():\n    pass\n")
    assert infer_app_type(cli, ("PyQt5>=5",)) is AppType.GUI


def test_entry_point_from_script(tmp_path: Path) -> None:
    """EntryPoint.from_script 按 import 推断 app_type（多入口模式不看 declared）。."""
    script = tmp_path / "gui.py"
    script.write_text("import PySide2\ndef main():\n    pass\n")
    ep = EntryPoint.from_script("gui", script)
    assert ep.name == "gui"
    assert ep.module == "gui"
    assert ep.file == script
    assert ep.app_type is AppType.GUI


def test_entry_point_entry_rel(tmp_path: Path) -> None:
    """EntryPoint.entry_rel 返回相对源码目录的 POSIX 路径。."""
    sub = tmp_path / "sub"
    sub.mkdir()
    script = sub / "app.py"
    script.write_text("def main():\n    pass\n")
    ep = EntryPoint.from_script("app", script)
    assert ep.entry_rel(tmp_path) == "sub/app.py"
