"""config 模块测试：镜像源、项目解析、入口识别、版本解析、依赖分析、构建配置."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.config import (
    DEFAULT_MIRROR,
    DEFAULT_PY_VERSION,
    MIRRORS,
    AppType,
    BuildConfig,
    DependencyReport,
    EntryPoint,
    MirrorConfig,
    ProjectInfo,
    detect_entry,
    get_mirror,
    infer_app_type,
    parse_project,
    resolve_py_version,
)
from fspack.exceptions import ProjectError
from fspack.platform import Platform

_EXAMPLES = Path(__file__).parent.parent / "examples"


# --- 镜像源测试 ---


def test_default_mirror_is_aliyun() -> None:
    assert DEFAULT_MIRROR == "aliyun"
    assert {"huawei", "aliyun", "tsinghua"} <= set(MIRRORS)


def test_get_mirror_default() -> None:
    assert get_mirror().name == "阿里云"


def test_get_mirror_by_name() -> None:
    assert get_mirror("aliyun").name == "阿里云"
    assert get_mirror("tsinghua").name == "清华"


def test_get_mirror_invalid() -> None:
    with pytest.raises(KeyError, match="未知镜像源"):
        get_mirror("nope")


def test_huawei_embed_url() -> None:
    m = get_mirror("huawei")
    assert m.embed_url("3.11.9") == "https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip"


def test_huawei_pypi_index() -> None:
    assert get_mirror("huawei").pypi_index == "https://mirrors.huaweicloud.com/pypi/simple/"


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


# --- 项目解析（parse_project）测试 ---


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
    """pyside2app 示例的 requires-python 约束正确解析."""
    info = parse_project(_EXAMPLES / "pyside2_app")
    assert info.requires_python == ">=3.8,<3.11"
    assert info.app_type is AppType.GUI


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


def test_parse_project_project_section_not_dict(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('project = "not a dict"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match=r"\[project\] 节格式异常"):
        parse_project(tmp_path)


# --- 入口识别（detect_entry）测试 ---


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


# --- Python 版本解析（resolve_py_version）测试 ---


def test_resolve_py_version_explicit(tmp_path: Path) -> None:
    """显式 --py-version 始终优先."""
    assert resolve_py_version(tmp_path, "3.10.0", None) == "3.10.0"


def test_resolve_py_version_explicit_overrides_requires_python(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """显式版本不满足 requires-python 时告警但仍使用."""
    with caplog.at_level("WARNING", logger="fspack.config"):
        result = resolve_py_version(tmp_path, "3.12.0", ">=3.8,<3.11")
    assert result == "3.12.0"
    assert "不满足 requires-python" in caplog.text


def test_resolve_py_version_python_version_file(tmp_path: Path) -> None:
    """有 .python-version 文件时映射到完整版本."""
    (tmp_path / ".python-version").write_text("3.9")
    assert resolve_py_version(tmp_path, None, None) == "3.9.13"


def test_resolve_py_version_python_version_file_full_version(tmp_path: Path) -> None:
    """.python-version 含完整版本号时直接使用."""
    (tmp_path / ".python-version").write_text("3.10.5")
    assert resolve_py_version(tmp_path, None, None) == "3.10.5"


def test_resolve_py_version_python_version_satisfies_requires_python(tmp_path: Path) -> None:
    """.python-version 满足 requires-python 时直接使用."""
    (tmp_path / ".python-version").write_text("3.9")
    assert resolve_py_version(tmp_path, None, ">=3.8,<3.11") == "3.9.13"


def test_resolve_py_version_python_version_violates_requires_python(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """.python-version 不满足 requires-python 时告警并自动选择."""
    (tmp_path / ".python-version").write_text("3.12")
    with caplog.at_level("WARNING", logger="fspack.config"):
        result = resolve_py_version(tmp_path, None, ">=3.8,<3.11")
    assert result == "3.10.11"
    assert "不满足 requires-python" in caplog.text


def test_resolve_py_version_auto_select_highest_compatible(tmp_path: Path) -> None:
    """无 .python-version 时按 requires-python 自动选最高兼容版本."""
    assert resolve_py_version(tmp_path, None, ">=3.8,<3.11") == "3.10.11"
    assert resolve_py_version(tmp_path, None, ">=3.8") == "3.12.0"
    assert resolve_py_version(tmp_path, None, "<3.10") == "3.9.13"


def test_resolve_py_version_no_constraints(tmp_path: Path) -> None:
    """无任何约束时返回 default."""
    assert resolve_py_version(tmp_path, None, None) == DEFAULT_PY_VERSION


def test_resolve_py_version_custom_default(tmp_path: Path) -> None:
    """无约束时使用自定义 default."""
    assert resolve_py_version(tmp_path, None, None, default="3.11.10") == "3.11.10"


def test_resolve_py_version_unsatisfiable_requires_python(tmp_path: Path) -> None:
    """requires-python 无法满足时抛 ProjectError."""
    with pytest.raises(ProjectError, match="无已知兼容 embed python 版本"):
        resolve_py_version(tmp_path, None, ">=4.0")


def test_resolve_py_version_complex_specifier(tmp_path: Path) -> None:
    """复杂规范符 >=3.9,<3.12 选 3.11.9."""
    assert resolve_py_version(tmp_path, None, ">=3.9,<3.12") == "3.11.9"


def test_resolve_py_version_pyside2app_example() -> None:
    """pyside2app 示例：.python-version=3.11 不满足 requires-python<3.11，自动选择 3.10.11."""
    info = parse_project(_EXAMPLES / "pyside2_app")
    resolved = resolve_py_version(_EXAMPLES / "pyside2_app", None, info.requires_python)
    assert resolved == "3.10.11"


# --- 多入口解析测试 ---


def test_parse_project_multi_entry_example() -> None:
    """multi_entry 示例：[tool.fspack.entries] 解析为三个入口."""
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
    """无 [tool.fspack.entries] 时走单入口 detect_entry 路径，entries 为空."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.entries == ()
    assert info.entry_module == "app"


def test_parse_project_multi_entry_missing_script(tmp_path: Path) -> None:
    """[tool.fspack.entries] 中脚本不存在时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack.entries]\nmain = "missing.py"\n'
    )
    with pytest.raises(ProjectError, match="脚本不存在"):
        parse_project(tmp_path)


def test_parse_project_multi_entry_empty_path(tmp_path: Path) -> None:
    """[tool.fspack.entries] 中脚本路径为空时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack.entries]\nmain = ""\n'
    )
    with pytest.raises(ProjectError, match="脚本路径为空"):
        parse_project(tmp_path)


# --- 应用类型推断（infer_app_type / EntryPoint）测试 ---


def test_infer_app_type_by_import(tmp_path: Path) -> None:
    """infer_app_type 按脚本 import 推断类型."""
    gui = tmp_path / "gui.py"
    gui.write_text("import PySide2\ndef main():\n    pass\n")
    assert infer_app_type(gui, ()) is AppType.GUI

    cli = tmp_path / "cli.py"
    cli.write_text("import sys\ndef main():\n    pass\n")
    assert infer_app_type(cli, ()) is AppType.CLI


def test_infer_app_type_by_declared(tmp_path: Path) -> None:
    """infer_app_type 按声明依赖推断类型（单入口模式）."""
    cli = tmp_path / "cli.py"
    cli.write_text("def main():\n    pass\n")
    assert infer_app_type(cli, ("PyQt5>=5",)) is AppType.GUI


def test_infer_app_type_pygame_is_gui(tmp_path: Path) -> None:
    """import pygame 的脚本推断为 GUI（无控制台）."""
    script = tmp_path / "game.py"
    script.write_text("import pygame\ndef main():\n    pass\n")
    assert infer_app_type(script, ()) is AppType.GUI


def test_parse_project_pygame_example_is_gui() -> None:
    """pygame_cli 示例被识别为 GUI."""
    info = parse_project(_EXAMPLES / "pygame_cli")
    assert info.app_type is AppType.GUI


def test_parse_project_pygame_snake_is_gui() -> None:
    """pygame_snake 示例被识别为 GUI."""
    info = parse_project(_EXAMPLES / "pygame_snake")
    assert info.app_type is AppType.GUI


def test_entry_point_from_script(tmp_path: Path) -> None:
    """EntryPoint.from_script 按 import 推断 app_type（多入口模式不看 declared）."""
    script = tmp_path / "gui.py"
    script.write_text("import PySide2\ndef main():\n    pass\n")
    ep = EntryPoint.from_script("gui", script)
    assert ep.name == "gui"
    assert ep.module == "gui"
    assert ep.file == script
    assert ep.app_type is AppType.GUI


def test_entry_point_entry_rel(tmp_path: Path) -> None:
    """EntryPoint.entry_rel 返回相对源码目录的 POSIX 路径."""
    sub = tmp_path / "sub"
    sub.mkdir()
    script = sub / "app.py"
    script.write_text("def main():\n    pass\n")
    ep = EntryPoint.from_script("app", script)
    assert ep.entry_rel(tmp_path) == "sub/app.py"


# --- icon 配置测试 ---


def test_resolve_icon_none_returns_none(tmp_path: Path) -> None:
    """icon_rel 为 None/空时返回 None."""
    from fspack.config import _resolve_icon

    assert _resolve_icon(tmp_path, None) is None
    assert _resolve_icon(tmp_path, "") is None


def test_resolve_icon_invalid_type_raises(tmp_path: Path) -> None:
    """icon_rel 非字符串时报错."""
    from fspack.config import _resolve_icon

    with pytest.raises(ProjectError, match="icon 配置无效"):
        _resolve_icon(tmp_path, 123)


def test_resolve_icon_blank_string_raises(tmp_path: Path) -> None:
    """icon_rel 为纯空白字符串时报错."""
    from fspack.config import _resolve_icon

    with pytest.raises(ProjectError, match="icon 配置无效"):
        _resolve_icon(tmp_path, "   ")


def test_resolve_icon_missing_file_raises(tmp_path: Path) -> None:
    """icon 文件不存在时报错."""
    from fspack.config import _resolve_icon

    with pytest.raises(ProjectError, match="icon 文件不存在"):
        _resolve_icon(tmp_path, "missing.ico")


def test_resolve_icon_valid_returns_absolute(tmp_path: Path) -> None:
    """icon 文件存在时返回绝对路径."""
    from fspack.config import _resolve_icon

    icon = tmp_path / "custom.ico"
    icon.write_bytes(b"ico")
    result = _resolve_icon(tmp_path, "custom.ico")
    assert result is not None
    assert result == icon.resolve()
    assert result.is_absolute()


def test_resolve_icon_strips_whitespace(tmp_path: Path) -> None:
    """icon 路径两侧空白被剥离."""
    from fspack.config import _resolve_icon

    icon = tmp_path / "custom.ico"
    icon.write_bytes(b"ico")
    assert _resolve_icon(tmp_path, "  custom.ico  ") == icon.resolve()


def test_parse_project_no_icon_returns_none(tmp_path: Path) -> None:
    """无 [tool.fspack] icon 配置时 ProjectInfo.icon 为 None."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.icon is None


def test_parse_project_with_icon_returns_path(tmp_path: Path) -> None:
    """[tool.fspack] icon 配置存在时 ProjectInfo.icon 为绝对路径."""
    icon = tmp_path / "my.ico"
    icon.write_bytes(b"ico")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nicon = "my.ico"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.icon == icon.resolve()


def test_parse_project_with_missing_icon_raises(tmp_path: Path) -> None:
    """[tool.fspack] icon 指向不存在文件时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nicon = "missing.ico"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="icon 文件不存在"):
        parse_project(tmp_path)


def test_parse_project_with_icon_in_multi_entry(tmp_path: Path) -> None:
    """多入口项目也正确解析 icon."""
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nicon = "icon.ico"\n\n[tool.fspack.entries]\ncli = "cli.py"\n'
    )
    (tmp_path / "cli.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.icon == icon.resolve()
