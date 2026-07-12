"""project pyproject.toml 解析与入口识别测试。."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.config import AppType
from fspack.exceptions import ProjectError
from fspack.project import DEFAULT_PY_VERSION, detect_entry, parse_project

_EXAMPLES = Path(__file__).parent / "examples"


def test_parse_project_helloworld() -> None:
    info = parse_project(_EXAMPLES / "helloworld")
    assert info.name == "helloworld"
    assert info.entry_module == "helloworld"
    assert info.entry_file.name == "helloworld.py"
    assert info.app_type is AppType.CLI
    assert info.exe_name == "helloworld.exe"
    assert info.py_xy == "python311"
    assert info.py_version == DEFAULT_PY_VERSION


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
