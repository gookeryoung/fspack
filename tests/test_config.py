"""config 模块测试：镜像源、项目解析、入口识别、版本解析、依赖分析、构建配置."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from fspack.config import (
    DEFAULT_MIRROR,
    DEFAULT_PY_VERSION,
    MIRRORS,
    AppType,
    BuildConfig,
    BuildDefaults,
    BuildOptions,
    DependencyReport,
    EntryPoint,
    MirrorConfig,
    ProjectInfo,
    _satisfies,
    _split_t_suffix,
    _ver_key,
    build_options_from_defaults,
    clear_project_cache,
    detect_entry,
    expand_extras,
    get_mirror,
    infer_app_type,
    nuitka_version_for,
    parse_project,
    resolve_py_version,
)
from fspack.exceptions import ProjectError
from fspack.platform import Platform
from fspack.templates.project_template import ProjectTemplate

_EXAMPLES = ProjectTemplate.root_dir()


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


def test_project_info_from_dir_tk_app() -> None:
    """from_dir 类方法解析 tk_app 示例（无第三方依赖的 GUI 模板）."""
    info = ProjectInfo.from_dir(_EXAMPLES / "gui" / "tk_app")
    assert info.name == "tk_app"
    assert info.entry_module == "tk_app"
    assert info.entry_file.name == "tk_app.py"
    assert info.app_type is AppType.GUI
    assert info.exe_name == "tk_app.exe"
    assert info.py_xy == "python311"


def test_project_info_from_dir_with_explicit_py_version(tmp_path: Path) -> None:
    """from_dir 接受 py_version 参数透传给 parse_project."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1"\n')
    (tmp_path / "p.py").write_text("def main():\n    pass\n")
    info = ProjectInfo.from_dir(tmp_path, "3.10.0")
    assert info.py_version == "3.10.0"


def test_project_info_from_dir_pyside2_app() -> None:
    """from_dir 解析 GUI 示例并读取 requires-python 约束."""
    info = ProjectInfo.from_dir(_EXAMPLES / "gui" / "pyside2_app")
    assert info.requires_python == ">=3.8,<3.10"
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


def test_dependency_report_missing_import_alias_static() -> None:
    """导入名 ≠ PyPI 分发名时经静态映射表消除误报（如 yaml↔PyYAML）."""
    r = DependencyReport(
        declared=("PyYAML>=6.0", "Pillow", "scikit-learn", "opencv-python-headless"),
        ast_third_party=("yaml", "PIL", "sklearn", "cv2"),
        ast_stdlib=(),
        ast_local=(),
    )
    assert r.missing == ()


def test_dependency_report_missing_alias_not_swallow_unrelated() -> None:
    """映射命中不吞掉真正的缺失依赖：声明 PyYAML 时 requests 仍报 missing."""
    r = DependencyReport(
        declared=("PyYAML",),
        ast_third_party=("yaml", "requests"),
        ast_stdlib=(),
        ast_local=(),
    )
    assert r.missing == ("requests",)


def test_dependency_report_missing_alias_common_entries() -> None:
    """静态映射表覆盖高频无歧义别名：声明名与导入名不一致的分发不误报 missing."""
    cases: list[tuple[str, str]] = [
        ("djangorestframework", "rest_framework"),
        ("ruamel.yaml", "ruamel"),
        ("python-json-logger", "pythonjsonlogger"),
        ("pywin32", "win32api"),
        ("opencv-contrib-python-headless", "cv2"),
    ]
    for declared, imported in cases:
        r = DependencyReport(
            declared=(declared,),
            ast_third_party=(imported,),
            ast_stdlib=(),
            ast_local=(),
        )
        assert r.missing == (), f"声明 {declared} 时导入 {imported} 不应报 missing"


def test_dependency_report_missing_runtime_top_level_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """静态表未覆盖的分发经当前环境 top_level.txt 兜底消除误报.

    patch 定义所在子模块 fspack.config.models 的 ``_installed_top_level_imports``
    （经 facade patch 无法拦截 models 内部调用）。
    """
    monkeypatch.setattr(
        "fspack.config.models._installed_top_level_imports",
        lambda dist_name: ("mymod",) if dist_name == "fake-dist" else (),
    )
    r = DependencyReport(
        declared=("fake-dist",),
        ast_third_party=("mymod", "other"),
        ast_stdlib=(),
        ast_local=(),
    )
    assert r.missing == ("other",)


def test_installed_top_level_imports_not_installed_returns_empty() -> None:
    """未安装分发的 top_level.txt 兜底返回空元组且不抛异常."""
    from fspack.config.models import _installed_top_level_imports

    assert _installed_top_level_imports("fspack-definitely-not-a-dist-xyz") == ()


def test_missing_lazy_runtime_fallback_not_triggered_when_static_covers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """静态匹配已覆盖全部导入时不得触发运行时兜底（importlib.metadata 导入 ~220ms）.

    回归防护：iter 前版本对每个静态表未覆盖的声明无条件调用兜底，
    dry-run 链路实测 ~58% 耗时浪费在 importlib.metadata 首次导入上。
    """
    called: list[str] = []
    monkeypatch.setattr(
        "fspack.config.models._installed_top_level_imports",
        lambda dist_name: (called.append(dist_name), ())[1],
    )
    # 声明 PyYAML（静态表覆盖 yaml）+ requests（声明名=导入名，归一化直配），
    # 全部导入已被前两层匹配：兜底不应被调用
    r = DependencyReport(
        declared=("PyYAML", "requests"),
        ast_third_party=("yaml", "requests"),
        ast_stdlib=(),
        ast_local=(),
    )
    assert r.missing == ()
    assert called == []


def test_missing_lazy_runtime_fallback_triggered_on_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存在未匹配导入时才触发运行时兜底，且兜底可消除误报."""
    called: list[str] = []
    monkeypatch.setattr(
        "fspack.config.models._installed_top_level_imports",
        lambda dist_name: (called.append(dist_name), ("mymod",))[1],
    )
    r = DependencyReport(
        declared=("fake-dist",),
        ast_third_party=("mymod",),
        ast_stdlib=(),
        ast_local=(),
    )
    assert r.missing == ()
    assert called == ["fake-dist"]


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
    info = ProjectInfo.from_dir(_EXAMPLES / "config" / "multi_entry")
    assert len(info.entries) == 3
    assert info.all_entries == info.entries
    assert info.all_entries[0].name == "cli"


def test_project_info_exe_name_multi_entry_uses_default_entry() -> None:
    """多入口模式 exe_name 取默认入口名（GUI 优先），与构建侧 exe 命名一致."""
    ep_cli = EntryPoint(name="cli", module="cli", file=Path("cli.py"), app_type=AppType.CLI)
    ep_gui = EntryPoint(name="gui", module="gui", file=Path("gui.py"), app_type=AppType.GUI)
    info = ProjectInfo(
        name="multi",
        version="0.1",
        src_dir=Path(),
        entry_module="cli",
        entry_file=Path("cli.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.10.11",
        entries=(ep_cli, ep_gui),
    )
    # default_entry GUI 优先于 CLI，exe 名跟随默认入口而非项目名
    assert info.default_entry is ep_gui
    assert info.exe_name == "gui.exe"


def test_scripts_exe_name_matches_build_output(tmp_path: Path) -> None:
    """[project.scripts] 声明入口后 exe_name 与构建产物名一致（入口名而非项目名）."""
    _write_script(tmp_path / "cli.py", content="def main():\n    pass\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[project.scripts]\nwebview_app = "cli:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 1
    assert info.exe_name == "webview_app.exe"


# --- 项目解析（parse_project）测试 ---


def test_parse_project_tk_app() -> None:
    """解析 tk_app 示例：无 requires-python 时用默认版本."""
    info = parse_project(_EXAMPLES / "gui" / "tk_app")
    assert info.name == "tk_app"
    assert info.entry_module == "tk_app"
    assert info.entry_file.name == "tk_app.py"
    assert info.app_type is AppType.GUI
    assert info.exe_name == "tk_app.exe"
    assert info.py_xy == "python311"
    assert info.py_version == DEFAULT_PY_VERSION
    assert info.requires_python is None


def test_parse_project_pyside2app_requires_python() -> None:
    """pyside2app 示例的 requires-python 约束正确解析."""
    info = parse_project(_EXAMPLES / "gui" / "pyside2_app")
    assert info.requires_python == ">=3.8,<3.10"
    assert info.app_type is AppType.GUI


def test_parse_project_missing_pyproject(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match=r"未找到 pyproject\.toml"):
        parse_project(tmp_path)


def test_parse_project_bad_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("this is = = not valid {{{")
    with pytest.raises(ProjectError, match="语法错误"):
        parse_project(tmp_path)


def test_parse_project_non_utf8_pyproject_raises_project_error(tmp_path: Path) -> None:
    """非 UTF-8 编码的 pyproject.toml 抛 ProjectError（中文提示）而非原始 UnicodeDecodeError.

    回归：用户曾遇到含非法起始字节的文件导致命令以原始 traceback 崩溃。
    此处写入含 0xa7（GBK "§"）的非法 UTF-8 起始字节，验证被包装为 ProjectError。
    """
    # 0xa7 是 UTF-8 的非法起始字节（continuation byte 无前导字节），必抛 UnicodeDecodeError
    (tmp_path / "pyproject.toml").write_bytes(b"\xa7[project]\nname = 'x'\n")
    with pytest.raises(ProjectError, match="编码错误"):
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


def test_detect_entry_skips_non_utf8_file(tmp_path: Path) -> None:
    """非 UTF-8 编码的候选入口脚本被跳过（不崩溃），选中后续合法入口.

    回归：``_has_entry`` 读取候选脚本时若文件非 UTF-8 会抛 UnicodeDecodeError
    （ValueError 子类，非 OSError），原先仅 catch (SyntaxError, OSError) 无法覆盖，
    导致 detect_entry 以原始 traceback 崩溃。
    """
    # 0xa7 为非法 UTF-8 起始字节，read_text(encoding="utf-8") 必抛 UnicodeDecodeError
    (tmp_path / "app.py").write_bytes(b"\xa7def main():\n    pass\n")
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


def test_resolve_py_version_explicit_short_maps_to_full(tmp_path: Path) -> None:
    """显式短版本号（如 3.13）按目标平台映射到完整版本号.

    Windows 用 KNOWN_EMBED_VERSIONS（3.11→3.11.9），Linux 用 KNOWN_STANDALONE_VERSIONS
    （3.11→3.11.15）。避免拼出 ``python/3.13/python-3.13-embed-amd64.zip`` 这样不存在的 URL。
    """
    assert resolve_py_version(tmp_path, "3.13", None) == "3.13.14"
    assert resolve_py_version(tmp_path, "3.11", None) == "3.11.9"
    assert resolve_py_version(tmp_path, "3.11", None, target=Platform.LINUX) == "3.11.15"
    assert resolve_py_version(tmp_path, "3.10", None, target=Platform.LINUX) == "3.10.20"


def test_resolve_py_version_explicit_full_version_passes_through(tmp_path: Path) -> None:
    """显式完整版本号（>=3 段）原样使用."""
    assert resolve_py_version(tmp_path, "3.13.1", None) == "3.13.1"


def test_resolve_py_version_explicit_unknown_short_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """显式未知短版本号告警并原样返回（向后兼容）."""
    with caplog.at_level("WARNING", logger="fspack.config"):
        result = resolve_py_version(tmp_path, "3.99", None)
    assert result == "3.99"
    assert "不在已知版本映射中" in caplog.text


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


def test_resolve_py_version_python_version_file_utf8_bom(tmp_path: Path) -> None:
    """.python-version 带 UTF-8 BOM 时正确解码."""
    (tmp_path / ".python-version").write_bytes(b"\xef\xbb\xbf3.9\n")
    assert resolve_py_version(tmp_path, None, None) == "3.9.13"


def test_resolve_py_version_python_version_file_utf16_le_bom(tmp_path: Path) -> None:
    """.python-version 为 UTF-16 LE（带 BOM）时正确解码.

    某些 Windows 编辑器默认以 UTF-16 保存文件，需通过 BOM 自动识别。
    """
    (tmp_path / ".python-version").write_bytes(b"\xff\xfe" + "3.9\r\n".encode("utf-16-le"))
    assert resolve_py_version(tmp_path, None, None) == "3.9.13"


def test_resolve_py_version_python_version_file_utf16_be_bom(tmp_path: Path) -> None:
    """.python-version 为 UTF-16 BE（带 BOM）时正确解码."""
    (tmp_path / ".python-version").write_bytes(b"\xfe\xff" + "3.9\r\n".encode("utf-16-be"))
    assert resolve_py_version(tmp_path, None, None) == "3.9.13"


def test_resolve_py_version_python_version_file_non_utf8_no_bom(tmp_path: Path) -> None:
    """.python-version 无 BOM 且含非法 UTF-8 字节时宽松解码不崩溃.

    回归：``_read_python_version`` 无 BOM 分支原先 ``data.decode("utf-8")`` 严格
    解码，文件为 GBK/含非法字节时抛 UnicodeDecodeError 导致 resolve_py_version
    以原始 traceback 崩溃（build/package 命令热路径）。现退回 errors="replace"，
    非法字节替换为占位符后版本号无法匹配已知映射，走告警回退而非崩溃。
    """
    # 版本号前混入非法 UTF-8 字节（无 BOM），严格 utf-8 解码必抛 UnicodeDecodeError
    (tmp_path / ".python-version").write_bytes(b"\xa73.99\n")
    # 不抛异常：宽松解码后 "\ufffd3.99" 不在已知映射，回退到默认版本
    result = resolve_py_version(tmp_path, None, None)
    assert result == DEFAULT_PY_VERSION


def test_resolve_py_version_python_version_file_full_version(tmp_path: Path) -> None:
    """.python-version 含完整版本号时直接使用."""
    (tmp_path / ".python-version").write_text("3.10.5")
    assert resolve_py_version(tmp_path, None, None) == "3.10.5"


def test_resolve_py_version_python_version_313_mapping(tmp_path: Path) -> None:
    """.python-version=3.13 映射到 3.13.14（KNOWN_EMBED_VERSIONS 已收录）."""
    (tmp_path / ".python-version").write_text("3.13")
    assert resolve_py_version(tmp_path, None, None) == "3.13.14"


def test_resolve_py_version_python_version_314_mapping(tmp_path: Path) -> None:
    """.python-version=3.14 映射到 3.14.6（KNOWN_EMBED_VERSIONS 已收录）."""
    (tmp_path / ".python-version").write_text("3.14")
    assert resolve_py_version(tmp_path, None, None) == "3.14.6"


def test_resolve_py_version_python_version_freethreaded_313t(tmp_path: Path) -> None:
    """.python-version=3.13t 映射到 3.13.14t（free-threaded build，PEP 703/779）."""
    (tmp_path / ".python-version").write_text("3.13t")
    assert resolve_py_version(tmp_path, None, None) == "3.13.14t"


def test_resolve_py_version_python_version_freethreaded_314t(tmp_path: Path) -> None:
    """.python-version=3.14t 映射到 3.14.6t（free-threaded build，PEP 779 正式支持）."""
    (tmp_path / ".python-version").write_text("3.14t")
    assert resolve_py_version(tmp_path, None, None) == "3.14.6t"


def test_resolve_py_version_python_version_freethreaded_full_passes_through(tmp_path: Path) -> None:
    """.python-version=3.13.14t 完整版本号直接透传（无映射）."""
    (tmp_path / ".python-version").write_text("3.13.14t")
    assert resolve_py_version(tmp_path, None, None) == "3.13.14t"


def test_split_t_suffix() -> None:
    """_split_t_suffix 剥离 free-threaded build 的 t 后缀."""
    assert _split_t_suffix("3.13.14") == ("3.13.14", False)
    assert _split_t_suffix("3.13.14t") == ("3.13.14", True)
    assert _split_t_suffix("3.13t") == ("3.13", True)
    assert _split_t_suffix("3.14") == ("3.14", False)


def test_ver_key_freethreaded_t_suffix() -> None:
    """_ver_key 处理 free-threaded 版本号末尾 t 后缀（int('14t') 会抛 ValueError）."""
    assert _ver_key("3.13.14") == (3, 13, 14)
    assert _ver_key("3.13.14t") == (3, 13, 14)
    assert _ver_key("3.13t") == (3, 13)


def test_nuitka_version_for_freethreaded() -> None:
    """nuitka_version_for 识别 t 后缀查表 3.13t/3.14t 键."""
    assert nuitka_version_for("3.13.14t") == "4.1.3"
    assert nuitka_version_for("3.14.6t") == "4.1.3"
    assert nuitka_version_for("3.13.14") == "4.1.3"  # 标准版仍命中
    assert nuitka_version_for("3.14.6") == "4.1.3"


def test_resolve_py_version_python_version_unknown_short_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """.python-version 为未知短版本号（无映射）时告警并回退到自动选择.

    防止返回短版本号（如 "3.99"）导致 embed 下载 URL 拼接错误。
    """
    (tmp_path / ".python-version").write_text("3.99")
    with caplog.at_level("WARNING", logger="fspack.config"):
        result = resolve_py_version(tmp_path, None, ">=3.8")
    # 回退到自动选择最高兼容已知版本
    assert result == "3.14.6"
    assert "不在已知版本映射中" in caplog.text


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
    """无 .python-version 时按 requires-python 自动选最高兼容版本（平台感知）."""
    # Windows（默认）：用 KNOWN_EMBED_VERSIONS
    assert resolve_py_version(tmp_path, None, ">=3.8,<3.11") == "3.10.11"
    assert resolve_py_version(tmp_path, None, ">=3.8") == "3.14.6"
    assert resolve_py_version(tmp_path, None, "<3.10") == "3.9.13"
    # Linux：用 KNOWN_STANDALONE_VERSIONS
    assert resolve_py_version(tmp_path, None, ">=3.8,<3.11", target=Platform.LINUX) == "3.10.20"
    assert resolve_py_version(tmp_path, None, ">=3.8", target=Platform.LINUX) == "3.14.6"


def test_resolve_py_version_no_constraints(tmp_path: Path) -> None:
    """无任何约束时返回 default."""
    assert resolve_py_version(tmp_path, None, None) == DEFAULT_PY_VERSION


def test_resolve_py_version_custom_default(tmp_path: Path) -> None:
    """无约束时使用自定义 default."""
    assert resolve_py_version(tmp_path, None, None, default="3.11.10") == "3.11.10"


def test_resolve_py_version_unsatisfiable_requires_python(tmp_path: Path) -> None:
    """requires-python 无法满足时抛 ProjectError."""
    with pytest.raises(ProjectError, match="无已知兼容 python 版本"):
        resolve_py_version(tmp_path, None, ">=4.0")


def test_resolve_py_version_complex_specifier(tmp_path: Path) -> None:
    """复杂规范符 >=3.9,<3.12 选 3.11.9（Windows embed 最新 3.11.x）."""
    assert resolve_py_version(tmp_path, None, ">=3.9,<3.12") == "3.11.9"
    assert resolve_py_version(tmp_path, None, ">=3.9,<3.12", target=Platform.LINUX) == "3.11.15"


def test_resolve_py_version_pyside2app_example() -> None:
    """pyside2app 示例：.python-version=3.9 + requires-python<3.10 解析到 3.9.13（Windows embed）."""
    info = parse_project(_EXAMPLES / "gui" / "pyside2_app")
    resolved = resolve_py_version(_EXAMPLES / "gui" / "pyside2_app", None, info.requires_python)
    assert resolved == "3.9.13"


# --- _satisfies PEP 440 规范符匹配测试 ---


def test_satisfies_wildcard_prefix_match() -> None:
    """``==3.12.*`` 前缀匹配任意 3.12.x 版本（PEP 440 version prefix match）."""
    assert _satisfies("3.12.0", "==3.12.*") is True
    assert _satisfies("3.12.10", "==3.12.*") is True
    assert _satisfies("3.12", "==3.12.*") is True
    assert _satisfies("3.13.0", "==3.12.*") is False
    assert _satisfies("3.11.9", "==3.12.*") is False


def test_satisfies_wildcard_not_match() -> None:
    """``!=3.12.*`` 排除所有 3.12.x 版本."""
    assert _satisfies("3.12.10", "!=3.12.*") is False
    assert _satisfies("3.13.0", "!=3.12.*") is True


def test_satisfies_wildcard_multi_segment_prefix() -> None:
    """``==3.12.1.*`` 支持多段前缀匹配."""
    assert _satisfies("3.12.1", "==3.12.1.*") is True
    assert _satisfies("3.12.1.5", "==3.12.1.*") is True
    assert _satisfies("3.12.2", "==3.12.1.*") is False


def test_satisfies_wildcard_combined_with_other_specifiers() -> None:
    """``==3.12.*`` 与其他规范符组合使用."""
    assert _satisfies("3.12.10", ">=3.11,==3.12.*") is True
    assert _satisfies("3.11.9", ">=3.11,==3.12.*") is False
    assert _satisfies("3.13.0", ">=3.11,==3.12.*") is False


def test_satisfies_exact_match_no_wildcard() -> None:
    """无通配符 ``==3.12.0`` 走精确匹配（向后兼容）."""
    assert _satisfies("3.12.0", "==3.12.0") is True
    assert _satisfies("3.12.10", "==3.12.0") is False


def test_satisfies_less_equal_operator() -> None:
    """``<=`` 操作符：小于或等于都满足."""
    assert _satisfies("3.11.9", "<=3.12") is True
    assert _satisfies("3.12.0", "<=3.12") is True
    assert _satisfies("3.13.0", "<=3.12") is False


def test_satisfies_greater_than_operator() -> None:
    """``>`` 操作符：仅大于满足（不含等于）."""
    assert _satisfies("3.13.0", ">3.12") is True
    assert _satisfies("3.12.0", ">3.12") is False
    assert _satisfies("3.11.9", ">3.12") is False


def test_satisfies_not_equal_operator() -> None:
    """``!=`` 操作符（无通配符）：不等于即满足."""
    assert _satisfies("3.12.0", "!=3.12.0") is False
    assert _satisfies("3.12.10", "!=3.12.0") is True
    assert _satisfies("3.11.9", "!=3.12.0") is True


def test_satisfies_combined_operators() -> None:
    """组合多个操作符：``>=3.11,<3.13,!=3.12.0``."""
    assert _satisfies("3.11.9", ">=3.11,<3.13,!=3.12.0") is True
    assert _satisfies("3.12.0", ">=3.11,<3.13,!=3.12.0") is False  # 被 != 排除
    assert _satisfies("3.12.10", ">=3.11,<3.13,!=3.12.0") is True
    assert _satisfies("3.13.0", ">=3.11,<3.13,!=3.12.0") is False  # 被 < 排除


# --- ``~=`` 兼容发行符（PEP 440 compatible release）测试 ---


def test_satisfies_compatible_release_two_segments() -> None:
    """``~=3.11`` 匹配 3.11 系列（``3.11 <= ver < 3.12``）."""
    assert _satisfies("3.11", "~=3.11") is True
    assert _satisfies("3.11.0", "~=3.11") is True
    assert _satisfies("3.11.9", "~=3.11") is True
    assert _satisfies("3.12.0", "~=3.11") is False  # 越 minor 上界
    assert _satisfies("3.10.13", "~=3.11") is False  # 低于下界


def test_satisfies_compatible_release_three_segments() -> None:
    """``~=3.11.5`` 匹配 ``3.11.5 <= ver < 3.12.0``（下界含补丁号，上界仍限 minor）."""
    assert _satisfies("3.11.5", "~=3.11.5") is True
    assert _satisfies("3.11.13", "~=3.11.5") is True
    assert _satisfies("3.11.5.1", "~=3.11.5") is True
    assert _satisfies("3.11.4", "~=3.11.5") is False  # 低于补丁下界
    assert _satisfies("3.12.0", "~=3.11.5") is False  # 越 minor 上界


def test_satisfies_compatible_release_single_segment_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """``~=3`` 单段非法（PEP 440 要求至少两段）：warning 后宽松放行."""
    with caplog.at_level("WARNING", logger="fspack.config.versions"):
        assert _satisfies("3.12.0", "~=3") is True
    assert "单段兼容发行符" in caplog.text


def test_satisfies_compatible_release_combined_with_other_specifiers() -> None:
    """``~=`` 与其他规范符组合：全部满足才通过."""
    assert _satisfies("3.11.8", "~=3.11,!=3.11.9") is True
    assert _satisfies("3.11.9", "~=3.11,!=3.11.9") is False  # 被 != 排除
    assert _satisfies("3.12.1", "~=3.11,!=3.11.9") is False  # 被 ~= 上界排除


def test_satisfies_unparseable_specifiers_warns_and_passes(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """整串无可识别规范符（如 ``"abc"``）：warning 后宽松放行."""
    with caplog.at_level("WARNING", logger="fspack.config.versions"):
        assert _satisfies("3.11.9", "abc") is True
    assert "无法解析" in caplog.text


def test_satisfies_freethreaded_t_suffix() -> None:
    """free-threaded 版本号末尾 't' 后缀：剥离后按纯数字版本判定.

    requires-python 规范符不区分 t 变体（标准版与 free-threaded 版本号主体相同）。
    """
    assert _satisfies("3.13.14t", ">=3.13") is True
    assert _satisfies("3.13.14t", ">=3.14") is False
    assert _satisfies("3.13.14t", "==3.13.*") is True
    assert _satisfies("3.14.6t", ">=3.13,<3.15") is True
    assert _satisfies("3.14.6t", "~=3.14") is True
    assert _satisfies("3.12.10", "~=3.13") is False  # 标准版回归测试


def test_resolve_py_version_wildcard_requires_python(tmp_path: Path) -> None:
    """``requires-python: ==3.12.*`` 自动选最高兼容 3.12.x 版本."""
    # Windows embed 最高 3.12.x 为 3.12.10
    assert resolve_py_version(tmp_path, None, "==3.12.*") == "3.12.10"
    # .python-version=3.12 满足 ==3.12.* 直接使用
    (tmp_path / ".python-version").write_text("3.12")
    assert resolve_py_version(tmp_path, None, "==3.12.*") == "3.12.10"


# --- 多入口解析测试 ---


def test_parse_project_multi_entry_example() -> None:
    """multi_entry 示例：[tool.fspack.entries] 解析为三个入口."""
    info = parse_project(_EXAMPLES / "config" / "multi_entry")
    assert len(info.entries) == 3
    assert [ep.name for ep in info.entries] == ["cli", "gui", "web"]
    # 首个入口作为主入口（向后兼容）
    assert info.entry_module == "cli"
    assert info.entry_file.name == "cli.py"
    assert info.app_type is AppType.CLI
    # 每个入口按自身 import 推断类型（不看项目级 declared PySide2）
    assert info.entries[0].app_type is AppType.CLI
    assert info.entries[1].app_type is AppType.GUI
    # web.py 入口 import flask → AppType.WEB（iter-148 新增 WEB 类型）
    assert info.entries[2].app_type is AppType.WEB


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


# --- [project.scripts] 入口点解析测试 ---
#
# PEP 621 标准入口点：name = "module:function"。
# 自动识别 flat/src layout 将 dotted module 解析为脚本路径。
# 与 [tool.fspack.entries] 同名时以 fspack 为准覆盖。


def _write_script(path: Path, content: str = "def main():\n    pass\n") -> None:
    """写入入口脚本（默认含 def main() 使 detect_entry 兜底可识别）."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_resolve_module_script_flat_multileg(tmp_path: Path) -> None:
    """flat layout：多段 module（pkg.cli）解析为 <project>/pkg/cli.py."""
    from fspack.config import _resolve_module_script

    _write_script(tmp_path / "mypkg" / "cli.py")
    result = _resolve_module_script(tmp_path, "mypkg.cli")
    assert result is not None
    assert result == (tmp_path / "mypkg" / "cli.py").resolve()


def test_resolve_module_script_flat_single_seg_top_file(tmp_path: Path) -> None:
    """flat layout：单段 module（app）优先解析为 <project>/app.py."""
    from fspack.config import _resolve_module_script

    _write_script(tmp_path / "app.py")
    result = _resolve_module_script(tmp_path, "app")
    assert result is not None
    assert result == (tmp_path / "app.py").resolve()


def test_resolve_module_script_flat_single_seg_pkg_main(tmp_path: Path) -> None:
    """flat layout：单段 module 无 app.py 时回退到 app/__main__.py."""
    from fspack.config import _resolve_module_script

    _write_script(tmp_path / "app" / "__main__.py")
    result = _resolve_module_script(tmp_path, "app")
    assert result is not None
    assert result == (tmp_path / "app" / "__main__.py").resolve()


def test_resolve_module_script_src_layout_multileg(tmp_path: Path) -> None:
    """src layout：多段 module（pkg.cli）解析为 <project>/src/pkg/cli.py."""
    from fspack.config import _resolve_module_script

    _write_script(tmp_path / "src" / "mypkg" / "cli.py")
    result = _resolve_module_script(tmp_path, "mypkg.cli")
    assert result is not None
    assert result == (tmp_path / "src" / "mypkg" / "cli.py").resolve()


def test_resolve_module_script_src_layout_single_seg(tmp_path: Path) -> None:
    """src layout：单段 module（app）解析为 <project>/src/app.py."""
    from fspack.config import _resolve_module_script

    _write_script(tmp_path / "src" / "app.py")
    result = _resolve_module_script(tmp_path, "app")
    assert result is not None
    assert result == (tmp_path / "src" / "app.py").resolve()


def test_resolve_module_script_flat_preferred_over_src(tmp_path: Path) -> None:
    """flat 与 src layout 同时存在时优先 flat（首个命中即返回）."""
    from fspack.config import _resolve_module_script

    _write_script(tmp_path / "pkg" / "cli.py")
    _write_script(tmp_path / "src" / "pkg" / "cli.py")
    result = _resolve_module_script(tmp_path, "pkg.cli")
    assert result is not None
    assert result == (tmp_path / "pkg" / "cli.py").resolve()


def test_resolve_module_script_not_found_returns_none(tmp_path: Path) -> None:
    """模块对应脚本不存在时返回 None."""
    from fspack.config import _resolve_module_script

    assert _resolve_module_script(tmp_path, "nonexistent.module") is None
    assert _resolve_module_script(tmp_path, "nonexistent") is None


def test_parse_project_scripts_single_entry_flat(tmp_path: Path) -> None:
    """[project.scripts] 单入口 flat layout：name = "module:function"."""
    from fspack.config import _parse_project_scripts

    _write_script(tmp_path / "app.py")
    tbl = {"app": "app:main"}
    entries = _parse_project_scripts(tmp_path, tbl)
    assert len(entries) == 1
    assert entries[0].name == "app"
    assert entries[0].module == "app"
    assert entries[0].file == (tmp_path / "app.py").resolve()


def test_parse_project_scripts_multileg_src_layout(tmp_path: Path) -> None:
    """[project.scripts] src layout：dotted module 解析为 src/<pkg>/<mod>.py."""
    from fspack.config import _parse_project_scripts

    _write_script(tmp_path / "src" / "mypkg" / "cli.py")
    _write_script(tmp_path / "src" / "mypkg" / "gui.py")
    tbl = {"cli": "mypkg.cli:main", "gui": "mypkg.gui:main"}
    entries = _parse_project_scripts(tmp_path, tbl)
    assert [ep.name for ep in entries] == ["cli", "gui"]
    assert entries[0].file == (tmp_path / "src" / "mypkg" / "cli.py").resolve()
    assert entries[1].file == (tmp_path / "src" / "mypkg" / "gui.py").resolve()


def test_parse_project_scripts_function_part_ignored(tmp_path: Path) -> None:
    """[project.scripts] 的 :function 部分被忽略（fspack 用 runpy 运行整个模块）."""
    from fspack.config import _parse_project_scripts

    _write_script(tmp_path / "app.py")
    # 不同 function 名（main vs run vs cli）应解析到同一脚本
    entries = _parse_project_scripts(tmp_path, {"app": "app:run"})
    assert len(entries) == 1
    assert entries[0].file == (tmp_path / "app.py").resolve()


def test_parse_project_scripts_pure_module_name_without_function(tmp_path: Path) -> None:
    """缺少 :function 时整段作为 module 名（向后兼容纯模块名写法）."""
    from fspack.config import _parse_project_scripts

    _write_script(tmp_path / "app.py")
    entries = _parse_project_scripts(tmp_path, {"app": "app"})
    assert len(entries) == 1
    assert entries[0].file == (tmp_path / "app.py").resolve()


def test_parse_project_scripts_preserves_insertion_order(tmp_path: Path) -> None:
    """[project.scripts] 按 dict 插入序返回 EntryPoint."""
    from fspack.config import _parse_project_scripts

    _write_script(tmp_path / "a.py")
    _write_script(tmp_path / "b.py")
    _write_script(tmp_path / "c.py")
    tbl = {"c": "c:main", "a": "a:main", "b": "b:main"}
    entries = _parse_project_scripts(tmp_path, tbl)
    assert [ep.name for ep in entries] == ["c", "a", "b"]


def test_parse_project_scripts_empty_name_raises(tmp_path: Path) -> None:
    """[project.scripts] 入口名为空时报错."""
    from fspack.config import _parse_project_scripts

    _write_script(tmp_path / "app.py")
    with pytest.raises(ProjectError, match="入口名无效"):
        _parse_project_scripts(tmp_path, {"": "app:main"})


def test_parse_project_scripts_empty_spec_raises(tmp_path: Path) -> None:
    """[project.scripts] 入口规范为空时报错."""
    from fspack.config import _parse_project_scripts

    with pytest.raises(ProjectError, match="入口规范为空"):
        _parse_project_scripts(tmp_path, {"app": ""})


def test_parse_project_scripts_module_not_found_raises(tmp_path: Path) -> None:
    """[project.scripts] 模块未找到对应脚本时报错."""
    from fspack.config import _parse_project_scripts

    with pytest.raises(ProjectError, match="未找到对应脚本"):
        _parse_project_scripts(tmp_path, {"app": "nonexistent.module:main"})


def test_parse_project_scripts_empty_table_raises(tmp_path: Path) -> None:
    """[project.scripts] 空表报错."""
    from fspack.config import _parse_project_scripts

    with pytest.raises(ProjectError, match=r"\[project\.scripts\] 为空"):
        _parse_project_scripts(tmp_path, {})


def test_parse_project_scripts_invalid_name_type_raises(tmp_path: Path) -> None:
    """[project.scripts] 入口名非字符串时报错."""
    from fspack.config import _parse_project_scripts

    _write_script(tmp_path / "app.py")
    with pytest.raises(ProjectError, match="入口名无效"):
        _parse_project_scripts(tmp_path, {123: "app:main"})  # type: ignore[dict-item]


def test_parse_project_scripts_invalid_spec_type_raises(tmp_path: Path) -> None:
    """[project.scripts] 入口规范非字符串时报错."""
    from fspack.config import _parse_project_scripts

    with pytest.raises(ProjectError, match="入口规范为空"):
        _parse_project_scripts(tmp_path, {"app": 123})  # type: ignore[dict-item]


def test_merge_entries_fspack_overrides_scripts_same_name(tmp_path: Path) -> None:
    """fspack entries 覆盖 scripts 同名入口（fspack 优先级更高）."""
    from fspack.config import _merge_entries

    scripts = (
        EntryPoint(name="cli", module="cli", file=Path("scripts_cli.py"), app_type=AppType.CLI),
        EntryPoint(name="gui", module="gui", file=Path("scripts_gui.py"), app_type=AppType.GUI),
    )
    fspack = (EntryPoint(name="cli", module="cli", file=Path("fspack_cli.py"), app_type=AppType.CLI),)
    merged = _merge_entries(scripts, fspack)
    assert [ep.name for ep in merged] == ["cli", "gui"]
    # cli 来自 fspack（覆盖）
    assert merged[0].file == Path("fspack_cli.py")
    # gui 来自 scripts（未覆盖）
    assert merged[1].file == Path("scripts_gui.py")


def test_merge_entries_preserves_order_and_appends_fspack_only(tmp_path: Path) -> None:
    """合并保持 scripts 原序，fspack 独有入口追加在末尾."""
    from fspack.config import _merge_entries

    scripts = (
        EntryPoint(name="cli", module="cli", file=Path("s_cli.py"), app_type=AppType.CLI),
        EntryPoint(name="gui", module="gui", file=Path("s_gui.py"), app_type=AppType.GUI),
    )
    fspack = (
        EntryPoint(name="cli", module="cli", file=Path("f_cli.py"), app_type=AppType.CLI),
        EntryPoint(name="web", module="web", file=Path("f_web.py"), app_type=AppType.CLI),
    )
    merged = _merge_entries(scripts, fspack)
    assert [ep.name for ep in merged] == ["cli", "gui", "web"]
    assert merged[0].file == Path("f_cli.py")  # cli 覆盖
    assert merged[1].file == Path("s_gui.py")  # gui 保留 scripts
    assert merged[2].file == Path("f_web.py")  # web 新增


def test_merge_entries_scripts_only(tmp_path: Path) -> None:
    """仅 scripts 有入口时返回 scripts."""
    from fspack.config import _merge_entries

    scripts = (EntryPoint(name="cli", module="cli", file=Path("s.py"), app_type=AppType.CLI),)
    merged = _merge_entries(scripts, ())
    assert merged == scripts


def test_merge_entries_fspack_only(tmp_path: Path) -> None:
    """仅 fspack 有入口时返回 fspack."""
    from fspack.config import _merge_entries

    fspack = (EntryPoint(name="cli", module="cli", file=Path("f.py"), app_type=AppType.CLI),)
    merged = _merge_entries((), fspack)
    assert merged == fspack


def test_merge_entries_both_empty(tmp_path: Path) -> None:
    """两者都空时返回空元组."""
    from fspack.config import _merge_entries

    assert _merge_entries((), ()) == ()


# --- parse_project 端到端：[project.scripts] 集成测试 ---


def test_parse_project_scripts_only_flat(tmp_path: Path) -> None:
    """仅 [project.scripts] flat layout：解析为多入口 entries."""
    _write_script(tmp_path / "cli.py")
    _write_script(tmp_path / "gui.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[project.scripts]\ncli = "cli:main"\ngui = "gui:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 2
    assert [ep.name for ep in info.entries] == ["cli", "gui"]
    # 首个入口作为主入口（向后兼容）
    assert info.entry_module == "cli"
    assert info.entry_file == (tmp_path / "cli.py").resolve()


def test_parse_project_scripts_src_layout(tmp_path: Path) -> None:
    """[project.scripts] src layout：dotted module 解析到 src/<pkg>/."""
    _write_script(tmp_path / "src" / "mypkg" / "cli.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\nversion = "0.1"\n\n[project.scripts]\nmycli = "mypkg.cli:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 1
    assert info.entries[0].name == "mycli"
    assert info.entries[0].file == (tmp_path / "src" / "mypkg" / "cli.py").resolve()


def test_parse_project_scripts_and_fspack_entries_merge(tmp_path: Path) -> None:
    """[project.scripts] + [tool.fspack.entries] 混合：fspack 覆盖同名入口."""
    _write_script(tmp_path / "scripts_cli.py")
    _write_script(tmp_path / "fspack_cli.py")
    _write_script(tmp_path / "scripts_gui.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[project.scripts]\ncli = "scripts_cli:main"\ngui = "scripts_gui:main"\n\n'
        '[tool.fspack.entries]\ncli = "fspack_cli.py"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 2
    assert [ep.name for ep in info.entries] == ["cli", "gui"]
    # cli 被 fspack 覆盖
    assert info.entries[0].file == (tmp_path / "fspack_cli.py").resolve()
    # gui 保留 scripts
    assert info.entries[1].file == (tmp_path / "scripts_gui.py").resolve()


def test_parse_project_scripts_and_fspack_entries_no_overlap(tmp_path: Path) -> None:
    """[project.scripts] 与 [tool.fspack.entries] 无重叠时合并所有入口."""
    _write_script(tmp_path / "cli.py")
    _write_script(tmp_path / "gui.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[project.scripts]\ncli = "cli:main"\n\n'
        '[tool.fspack.entries]\ngui = "gui.py"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 2
    assert [ep.name for ep in info.entries] == ["cli", "gui"]
    assert info.entries[0].file == (tmp_path / "cli.py").resolve()
    assert info.entries[1].file == (tmp_path / "gui.py").resolve()


def test_parse_project_fspack_entries_only_still_works(tmp_path: Path) -> None:
    """仅有 [tool.fspack.entries] 时走原路径（向后兼容）."""
    _write_script(tmp_path / "cli.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack.entries]\ncli = "cli.py"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 1
    assert info.entries[0].name == "cli"
    assert info.entries[0].file == (tmp_path / "cli.py").resolve()


def test_parse_project_no_scripts_no_entries_falls_back_to_detect(tmp_path: Path) -> None:
    """无 [project.scripts] 与 [tool.fspack.entries] 时走 detect_entry 兜底."""
    _write_script(tmp_path / "app.py")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    clear_project_cache()
    info = parse_project(tmp_path)
    assert info.entries == ()
    assert info.entry_module == "app"
    assert info.entry_file == (tmp_path / "app.py").resolve()


def test_parse_project_empty_scripts_table_falls_back_to_detect(tmp_path: Path) -> None:
    """[project.scripts] 为空表时走 detect_entry 兜底."""
    _write_script(tmp_path / "app.py")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n\n[project.scripts]\n')
    clear_project_cache()
    info = parse_project(tmp_path)
    assert info.entries == ()
    assert info.entry_module == "app"


# --- [project.scripts] 典型场景测试 ---
#
# 贴近真实项目结构的端到端测试：包式项目、GUI 类型推断、深层 dotted module、
# 混合声明、真实 fspack 自身场景等。区别于上方的单元/错误场景测试。


def test_scripts_flat_layout_package_project(tmp_path: Path) -> None:
    """典型：flat layout 包项目（mypkg/__init__.py + mypkg/cli.py）.

    真实项目常见结构：包目录在项目根下，[project.scripts] 用 dotted module。
    """
    _write_script(tmp_path / "mypkg" / "__init__.py", content="")
    _write_script(tmp_path / "mypkg" / "cli.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\nversion = "0.1"\n\n[project.scripts]\nmycli = "mypkg.cli:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 1
    assert info.entries[0].name == "mycli"
    assert info.entries[0].file == (tmp_path / "mypkg" / "cli.py").resolve()
    assert info.entries[0].app_type is AppType.CLI


def test_scripts_src_layout_package_project(tmp_path: Path) -> None:
    """典型：src layout 包项目（src/mypkg/__init__.py + src/mypkg/cli.py）.

    PEP 628 推荐的 src layout，包在 src/ 下，src/ 本身不是包。
    """
    _write_script(tmp_path / "src" / "mypkg" / "__init__.py", content="")
    _write_script(tmp_path / "src" / "mypkg" / "cli.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\nversion = "0.1"\n\n[project.scripts]\nmycli = "mypkg.cli:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 1
    assert info.entries[0].file == (tmp_path / "src" / "mypkg" / "cli.py").resolve()


def test_scripts_gui_app_type_inferred(tmp_path: Path) -> None:
    """典型：[project.scripts] 脚本 import PySide2 推断为 GUI.

    验证 app_type 按 [project.scripts] 入口脚本自身 import 推断，
    与 [tool.fspack.entries] 行为一致。
    """
    _write_script(tmp_path / "app.py", content="import PySide2\ndef main():\n    pass\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[project.scripts]\napp = "app:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 1
    assert info.entries[0].app_type is AppType.GUI
    assert info.app_type is AppType.GUI  # 首个入口作为主入口


def test_scripts_deep_dotted_module(tmp_path: Path) -> None:
    """典型：深层 dotted module（a.b.c:main → a/b/c.py）.

    多层包嵌套场景，验证 dotted module 正确解析为深层文件路径。
    """
    _write_script(tmp_path / "a" / "b" / "c.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[project.scripts]\napp = "a.b.c:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 1
    assert info.entries[0].file == (tmp_path / "a" / "b" / "c.py").resolve()


def test_scripts_single_seg_package_main_entry(tmp_path: Path) -> None:
    """典型：单段 module 指向包入口（pkg → pkg/__main__.py）.

    当 pkg.py 不存在但 pkg/__main__.py 存在时，单段 module 解析为包入口。
    与 :func:`detect_entry` 的 ``<name>/__main__.py`` 兜底逻辑一致。
    """
    _write_script(tmp_path / "mypkg" / "__init__.py", content="")
    _write_script(tmp_path / "mypkg" / "__main__.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\nversion = "0.1"\n\n[project.scripts]\nmycli = "mypkg:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 1
    assert info.entries[0].file == (tmp_path / "mypkg" / "__main__.py").resolve()


def test_scripts_mixed_cli_gui_entries(tmp_path: Path) -> None:
    """典型：[project.scripts] 多入口混合 CLI/GUI 类型.

    不同入口脚本按自身 import 推断类型，CLI 与 GUI 共存。
    """
    _write_script(tmp_path / "cli.py", content="def main():\n    pass\n")
    _write_script(tmp_path / "gui.py", content="import tkinter\ndef main():\n    pass\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[project.scripts]\ncli = "cli:main"\ngui = "gui:main"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 2
    assert info.entries[0].name == "cli"
    assert info.entries[0].app_type is AppType.CLI
    assert info.entries[1].name == "gui"
    assert info.entries[1].app_type is AppType.GUI


def test_scripts_real_fspack_self_scenario(tmp_path: Path) -> None:
    """典型：真实 fspack 自身场景.

    fspack 的 pyproject.toml 同时声明：
    - [project.scripts] fspack = "fspack.cli:main" / fsp = "fspack.cli:main"
    - [tool.fspack.entries] fsp = "src/fspack/cli.py" / fspack = "src/fspack/cli.py"

    fsp 与 fspack 同名，fspack entries 覆盖 scripts；
    合并后 2 个入口，文件路径指向 src/fspack/cli.py（src layout 解析）。
    """
    _write_script(tmp_path / "src" / "fspack" / "__init__.py", content="")
    _write_script(tmp_path / "src" / "fspack" / "cli.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fspack"\nversion = "0.3.10"\n\n'
        '[project.scripts]\nfspack = "fspack.cli:main"\nfsp = "fspack.cli:main"\n\n'
        '[tool.fspack.entries]\nfsp = "src/fspack/cli.py"\nfspack = "src/fspack/cli.py"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 2
    assert [ep.name for ep in info.entries] == ["fspack", "fsp"]
    # 两者都被 fspack entries 覆盖，指向 src/fspack/cli.py
    assert info.entries[0].file == (tmp_path / "src" / "fspack" / "cli.py").resolve()
    assert info.entries[1].file == (tmp_path / "src" / "fspack" / "cli.py").resolve()


def test_scripts_partial_overlap_with_unique_entries(tmp_path: Path) -> None:
    """典型：scripts 与 fspack 部分重叠、部分独有.

    scripts: cli, gui, web
    fspack:  cli, admin
    合并后:  cli(fspack覆盖), gui(scripts), web(scripts), admin(fspack新增)
    """
    _write_script(tmp_path / "scripts_cli.py")
    _write_script(tmp_path / "scripts_gui.py")
    _write_script(tmp_path / "scripts_web.py")
    _write_script(tmp_path / "fspack_cli.py")
    _write_script(tmp_path / "admin.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[project.scripts]\ncli = "scripts_cli:main"\ngui = "scripts_gui:main"\nweb = "scripts_web:main"\n\n'
        '[tool.fspack.entries]\ncli = "fspack_cli.py"\nadmin = "admin.py"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert [ep.name for ep in info.entries] == ["cli", "gui", "web", "admin"]
    # cli 被 fspack 覆盖
    assert info.entries[0].file == (tmp_path / "fspack_cli.py").resolve()
    # gui/web 保留 scripts
    assert info.entries[1].file == (tmp_path / "scripts_gui.py").resolve()
    assert info.entries[2].file == (tmp_path / "scripts_web.py").resolve()
    # admin 是 fspack 新增
    assert info.entries[3].file == (tmp_path / "admin.py").resolve()


def test_multi_entry_template_backward_compat() -> None:
    """典型：multi_entry 模板（仅 [tool.fspack.entries]）向后兼容.

    现有模板不使用 [project.scripts]，仅用 [tool.fspack.entries]，
    新逻辑不应破坏其行为。
    """
    clear_project_cache()
    info = parse_project(_EXAMPLES / "config" / "multi_entry")
    assert len(info.entries) == 3
    assert [ep.name for ep in info.entries] == ["cli", "gui", "web"]
    # cli 是 CLI，gui 是 GUI（import PySide2），web 是 WEB（import flask）
    assert info.entries[0].app_type is AppType.CLI
    assert info.entries[1].app_type is AppType.GUI
    assert info.entries[2].app_type is AppType.WEB


def test_scripts_with_fspack_supplements_extra_entry(tmp_path: Path) -> None:
    """典型：[project.scripts] 主入口 + [tool.fspack.entries] 补充额外入口.

    常见场景：[project.scripts] 声明标准入口（pip install 时生成），
    [tool.fspack.entries] 补充打包专属入口（如调试脚本）。
    """
    _write_script(tmp_path / "src" / "mypkg" / "cli.py")
    _write_script(tmp_path / "debug.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\nversion = "0.1"\n\n'
        '[project.scripts]\nmycli = "mypkg.cli:main"\n\n'
        '[tool.fspack.entries]\ndebug = "debug.py"\n'
    )
    clear_project_cache()
    info = parse_project(tmp_path)
    assert len(info.entries) == 2
    assert [ep.name for ep in info.entries] == ["mycli", "debug"]
    # mycli 来自 scripts（src layout 解析）
    assert info.entries[0].file == (tmp_path / "src" / "mypkg" / "cli.py").resolve()
    # debug 来自 fspack entries（相对路径）
    assert info.entries[1].file == (tmp_path / "debug.py").resolve()


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


def test_infer_app_type_non_utf8_falls_back_to_declared(tmp_path: Path) -> None:
    """非 UTF-8 入口脚本时 infer_app_type 跳过 import 分析，按声明依赖回退（不崩溃）.

    回归：``infer_app_type`` 原先无 try/except，读取非 UTF-8 脚本会抛
    UnicodeDecodeError 导致命令崩溃。此路径经 ``EntryPoint.from_script`` 直达，
    不经 ``_has_entry`` 守护，故必须独立防御。
    """
    # 0xa7 为非法 UTF-8 起始字节，read_text(encoding="utf-8") 必抛 UnicodeDecodeError
    script = tmp_path / "app.py"
    script.write_bytes(b"\xa7import PySide2\ndef main():\n    pass\n")
    # import 分析被跳过（读取失败），仅按声明依赖推断
    assert infer_app_type(script, ("PyQt5>=5",)) is AppType.GUI
    # 无声明依赖时回退 CLI（保留控制台最安全）
    assert infer_app_type(script, ()) is AppType.CLI


def test_parse_project_pygame_snake_is_gui() -> None:
    """pygame_snake 示例被识别为 GUI."""
    info = parse_project(_EXAMPLES / "game" / "pygame_snake")
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


# --- exclude 与 build_defaults 配置测试 ---


def test_parse_project_no_exclude_returns_empty(tmp_path: Path) -> None:
    """无 [tool.fspack] exclude 配置时 exclude_dirs 为空元组."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.exclude_dirs == ()


def test_parse_project_with_exclude(tmp_path: Path) -> None:
    """[tool.fspack] exclude 配置解析为排除模式元组."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nexclude = ["examples", "docs"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.exclude_dirs == ("examples", "docs")


def test_parse_project_exclude_not_list_raises(tmp_path: Path) -> None:
    """exclude 非列表时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nexclude = "examples"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="exclude 必须是字符串列表"):
        parse_project(tmp_path)


def test_parse_project_exclude_empty_string_element_raises(tmp_path: Path) -> None:
    """exclude 元素为空字符串时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nexclude = [""]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="exclude 元素必须是非空字符串"):
        parse_project(tmp_path)


# --- data-dirs 配置测试 ---


def test_parse_project_no_data_dirs_returns_empty(tmp_path: Path) -> None:
    """无 [tool.fspack] data-dirs 配置时 data_dirs 为空元组（默认行为不变）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.data_dirs == ()


def test_parse_project_with_data_dirs(tmp_path: Path) -> None:
    """[tool.fspack] data-dirs 配置解析为路径元组."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[tool.fspack]\ndata-dirs = ["src/fspack/assets/templates", "data/projects"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.data_dirs == ("src/fspack/assets/templates", "data/projects")


def test_parse_project_data_dirs_not_list_raises(tmp_path: Path) -> None:
    """data-dirs 非列表时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\ndata-dirs = "templates"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="data-dirs 必须是字符串列表"):
        parse_project(tmp_path)


def test_parse_project_data_dirs_empty_string_element_raises(tmp_path: Path) -> None:
    """data-dirs 元素为空字符串时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\ndata-dirs = [""]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="data-dirs 元素必须是非空字符串"):
        parse_project(tmp_path)


def test_parse_project_no_build_defaults_returns_all_none(tmp_path: Path) -> None:
    """无 [tool.fspack] 构建默认值时 build_defaults 所有字段为 None."""
    from fspack.config import BuildDefaults

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.build_defaults == BuildDefaults()


def test_parse_project_with_build_defaults(tmp_path: Path) -> None:
    """[tool.fspack] 构建默认值正确解析."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        "[tool.fspack]\nnuitka = true\npyc_strip = true\nno_site = true\npyc_optimize = 1\n"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.build_defaults.nuitka is True
    assert info.build_defaults.pyc_strip is True
    assert info.build_defaults.no_site is True
    assert info.build_defaults.pyc_optimize == 1
    assert info.build_defaults.no_pyc is None
    assert info.build_defaults.no_stdlib_trim is None


def test_parse_project_lazy_imports_config(tmp_path: Path) -> None:
    """[tool.fspack] lazy_imports 解析为模块名元组."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nlazy_imports = ["numpy", "pandas"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.build_defaults.lazy_imports == ("numpy", "pandas")
    # build_options_from_defaults 透传到 BuildOptions.lazy_imports
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.lazy_imports == ("numpy", "pandas")


def test_parse_project_lazy_imports_default_empty(tmp_path: Path) -> None:
    """未配置 lazy_imports 时默认空元组."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.build_defaults.lazy_imports == ()


def test_parse_project_lazy_imports_invalid_type_raises(tmp_path: Path) -> None:
    """lazy_imports 非字符串列表时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nlazy_imports = "numpy"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="lazy_imports"):
        parse_project(tmp_path)


def test_parse_project_build_defaults_invalid_bool_raises(tmp_path: Path) -> None:
    """构建默认值布尔字段传非布尔值时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nnuitka = "yes"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="nuitka 必须是布尔值"):
        parse_project(tmp_path)


def test_parse_project_build_defaults_invalid_pyc_optimize_raises(tmp_path: Path) -> None:
    """pyc_optimize 非 0/1/2 报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\npyc_optimize = 3\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="pyc_optimize 必须是 0/1/2"):
        parse_project(tmp_path)


def test_parse_project_exclude_and_defaults_in_multi_entry(tmp_path: Path) -> None:
    """多入口项目也正确解析 exclude 与 build_defaults."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[tool.fspack]\nexclude = ["tests"]\nnuitka = true\n\n'
        '[tool.fspack.entries]\ncli = "cli.py"\n'
    )
    (tmp_path / "cli.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.exclude_dirs == ("tests",)
    assert info.build_defaults.nuitka is True


# ---------- 私有包源（extra-index-urls / find-links）----------


def test_parse_project_no_private_sources_returns_empty(tmp_path: Path) -> None:
    """无 [tool.fspack] 私有包源配置时 extra_index_urls/find_links 为空元组."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.extra_index_urls == ()
    assert info.find_links == ()


def test_parse_project_with_extra_index_urls(tmp_path: Path) -> None:
    """[tool.fspack] extra-index-urls 解析为元组."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[tool.fspack]\nextra-index-urls = ["https://pypi.company.com/simple/", "https://mirror.example.com/pypi"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.extra_index_urls == ("https://pypi.company.com/simple/", "https://mirror.example.com/pypi")


def test_parse_project_with_find_links(tmp_path: Path) -> None:
    """[tool.fspack] find-links 解析为元组，支持本地路径与 URL."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[tool.fspack]\nfind-links = ["./wheels", "https://example.com/wheels/"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.find_links == ("./wheels", "https://example.com/wheels/")


def test_parse_project_extra_index_urls_strips_whitespace(tmp_path: Path) -> None:
    """extra-index-urls 元素首尾空白被 strip，空字符串元素被过滤."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[tool.fspack]\nextra-index-urls = ["  https://pypi.company.com/simple/  ", "", "  "]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.extra_index_urls == ("https://pypi.company.com/simple/",)


def test_parse_project_extra_index_urls_not_list_raises(tmp_path: Path) -> None:
    """extra-index-urls 非列表时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[tool.fspack]\nextra-index-urls = "https://pypi.company.com/simple/"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="extra-index-urls 必须是字符串列表"):
        parse_project(tmp_path)


def test_parse_project_find_links_non_string_element_raises(tmp_path: Path) -> None:
    """find-links 元素非字符串时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nfind-links = [123]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="find-links 元素必须是字符串"):
        parse_project(tmp_path)


def test_parse_project_with_slim_include_exclude(tmp_path: Path) -> None:
    """[tool.fspack] slim-include/slim-exclude 解析为 SlimRules."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        "[tool.fspack]\n"
        'slim-include = ["PySide6/Qt6Charts.dll"]\n'
        'slim-exclude = ["PySide6/opengl32sw.dll", "PySide6/translations/*"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.slim_rules.include == ("PySide6/Qt6Charts.dll",)
    assert info.slim_rules.exclude == ("PySide6/opengl32sw.dll", "PySide6/translations/*")
    assert info.slim_rules.has_rules


def test_parse_project_slim_include_not_list_raises(tmp_path: Path) -> None:
    """slim-include 非列表时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nslim-include = "PySide6/Qt6Charts.dll"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="slim-include 必须是字符串列表"):
        parse_project(tmp_path)


def test_parse_project_slim_exclude_empty_element_raises(tmp_path: Path) -> None:
    """slim-exclude 元素为空字符串时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nslim-exclude = [""]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="slim-exclude 元素必须是非空字符串"):
        parse_project(tmp_path)


def test_parse_project_slim_rules_defaults_empty(tmp_path: Path) -> None:
    """未配置 slim-include/slim-exclude 时 SlimRules 为空规则."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.slim_rules.include == ()
    assert info.slim_rules.exclude == ()
    assert not info.slim_rules.has_rules


def test_parse_project_private_sources_in_multi_entry(tmp_path: Path) -> None:
    """多入口项目也正确解析私有包源配置."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n'
        '[tool.fspack]\nextra-index-urls = ["https://pypi.company.com/simple/"]\n'
        'find-links = ["./wheels"]\n\n'
        '[tool.fspack.entries]\ncli = "cli.py"\n'
    )
    (tmp_path / "cli.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.extra_index_urls == ("https://pypi.company.com/simple/",)
    assert info.find_links == ("./wheels",)


# --- 解析缓存（lru_cache）测试 ---
#
# parse_project 按 (project_dir, py_version, mtime_ns) 缓存
# ProjectInfo。以下测试验证缓存命中、mtime 失效、显式清空、不同参数隔离。


def _make_minimal_project(project_dir: Path, name: str = "app", version: str = "0.1") -> None:
    """构造最小可解析项目（pyproject.toml + 入口脚本）."""
    (project_dir / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "{version}"\n')
    (project_dir / f"{name}.py").write_text("def main():\n    pass\n")


def test_parse_project_cache_hit_returns_same_object(tmp_path: Path) -> None:
    """同一目录连续调用 parse_project 返回同一缓存对象（identity 相等）."""
    clear_project_cache()
    _make_minimal_project(tmp_path)
    first = parse_project(tmp_path)
    second = parse_project(tmp_path)
    assert first is second  # lru_cache 命中返回同一实例


def test_parse_project_cache_hit_via_from_dir(tmp_path: Path) -> None:
    """ProjectInfo.from_dir 同样命中缓存（委托 parse_project）."""
    clear_project_cache()
    _make_minimal_project(tmp_path)
    first = ProjectInfo.from_dir(tmp_path)
    second = ProjectInfo.from_dir(tmp_path)
    assert first is second


def test_parse_project_cache_invalidates_on_mtime_change(tmp_path: Path) -> None:
    """pyproject.toml mtime 变化后下次调用获取新解析结果."""
    clear_project_cache()
    _make_minimal_project(tmp_path, name="v1")
    first = parse_project(tmp_path)
    assert first.name == "v1"

    # 修改 pyproject.toml 并强制推进 mtime（覆盖部分平台 mtime 分辨率不足）
    pp = tmp_path / "pyproject.toml"
    pp.write_text('[project]\nname = "v2"\nversion = "0.2"\n')

    # 强制 mtime 推进 1 秒，确保跨平台稳定触发缓存失效
    new_mtime = time.time() + 1.0
    os.utime(pp, (new_mtime, new_mtime))

    second = parse_project(tmp_path)
    assert second is not first  # 缓存未命中，新实例
    assert second.name == "v2"
    assert second.version == "0.2"


def test_clear_project_cache_empties_cache(tmp_path: Path) -> None:
    """clear_project_cache 后下次调用是新解析（不复用缓存）."""
    clear_project_cache()
    _make_minimal_project(tmp_path)
    first = parse_project(tmp_path)
    clear_project_cache()
    second = parse_project(tmp_path)
    assert first is not second  # 清空后重新解析
    # 内容仍相等（frozen dataclass __eq__ 按字段比较）
    assert first == second


def test_clear_project_cache_idempotent() -> None:
    """多次清空缓存不报错（空缓存再清仍安全）."""
    clear_project_cache()
    clear_project_cache()
    clear_project_cache()


def test_parse_project_cache_separates_different_py_version(tmp_path: Path) -> None:
    """不同 py_version 参数分别缓存，互不影响."""
    clear_project_cache()
    _make_minimal_project(tmp_path)
    a = parse_project(tmp_path, "3.10.0")
    b = parse_project(tmp_path, "3.11.9")
    c = parse_project(tmp_path, "3.10.0")
    assert a is not b  # 不同 py_version 不同缓存条目
    assert a is c  # 相同 py_version 命中同一缓存
    assert a.py_version == "3.10.0"
    assert b.py_version == "3.11.9"


def test_parse_project_cache_separates_different_project_dirs(tmp_path: Path) -> None:
    """不同项目目录分别缓存."""
    clear_project_cache()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    _make_minimal_project(proj_a, name="appa")
    _make_minimal_project(proj_b, name="appb")
    info_a = parse_project(proj_a)
    info_b = parse_project(proj_b)
    assert info_a is not info_b
    assert info_a.name == "appa"
    assert info_b.name == "appb"


def test_parse_project_cache_error_not_cached(tmp_path: Path) -> None:
    """解析失败时不缓存异常（lru_cache 仅缓存成功返回值）.

    修复 pyproject.toml 后再次调用应能成功，而非复用失败的异常。
    """
    clear_project_cache()
    pp = tmp_path / "pyproject.toml"
    pp.write_text("invalid toml [")  # 语法错误
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="语法错误"):
        parse_project(tmp_path)
    # 修复后再次调用应成功（异常未被缓存）
    pp.write_text('[project]\nname = "app"\nversion = "0.1"\n')
    info = parse_project(tmp_path)
    assert info.name == "app"


# --- [project.optional-dependencies] 解析测试 ---
#
# 覆盖 PEP 621 可选依赖分组解析、[tool.fspack] extras 配置默认、
# expand_extras 自引用展开与第三方 extras 透传等场景。


def _make_project_with_optional_deps(
    project_dir: Path,
    *,
    name: str = "myapp",
    base_deps: str = "",
    optional_block: str = "",
    fspack_extras: str = "",
) -> None:
    """构造带 [project.optional-dependencies] 的项目.

    Args:
        base_deps: ``[project] dependencies`` 内容（多行字符串，含缩进）
        optional_block: ``[project.optional-dependencies]`` 整段内容
        fspack_extras: ``[tool.fspack] extras`` 行（如 ``extras = ["gui"]``）
    """
    parts = [f'[project]\nname = "{name}"\nversion = "0.1"\n']
    if base_deps:
        parts.append("dependencies = [\n")
        parts.append(base_deps)
        parts.append("]\n")
    if optional_block:
        parts.append("\n[project.optional-dependencies]\n")
        parts.append(optional_block)
    if fspack_extras:
        parts.append("\n[tool.fspack]\n")
        parts.append(fspack_extras)
    (project_dir / "pyproject.toml").write_text("".join(parts))
    (project_dir / f"{name}.py").write_text("def main():\n    pass\n")


def test_parse_optional_dependencies_basic(tmp_path: Path) -> None:
    """解析简单分组：gui/web 两个 extras."""
    clear_project_cache()
    _make_project_with_optional_deps(
        tmp_path,
        base_deps='    "rich>=13",\n',
        optional_block='gui = ["PySide2"]\nweb = ["flask", "uvicorn"]\n',
    )
    info = parse_project(tmp_path)
    assert info.dependencies == ("rich>=13",)
    assert info.optional_dependencies == {
        "gui": ("PySide2",),
        "web": ("flask", "uvicorn"),
    }


def test_parse_optional_dependencies_none_when_absent(tmp_path: Path) -> None:
    """无 [project.optional-dependencies] 时 optional_dependencies 为空字典."""
    clear_project_cache()
    _make_minimal_project(tmp_path, name="app")
    info = parse_project(tmp_path)
    assert info.optional_dependencies == {}


def test_parse_optional_dependencies_invalid_type(tmp_path: Path) -> None:
    """optional-dependencies 非 dict 报错."""
    clear_project_cache()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\noptional-dependencies = ["not", "a", "dict"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match=r"\[project\.optional-dependencies\] 必须是表"):
        parse_project(tmp_path)


def test_parse_optional_dependencies_invalid_dep_list(tmp_path: Path) -> None:
    """分组值非字符串列表报错."""
    clear_project_cache()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[project.optional-dependencies]\ngui = "PySide2"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="必须是字符串列表"):
        parse_project(tmp_path)


def test_parse_optional_dependencies_empty_name(tmp_path: Path) -> None:
    """分组名为空字符串报错."""
    clear_project_cache()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[project.optional-dependencies]\n"" = ["rich"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="分组名无效"):
        parse_project(tmp_path)


def test_parse_fspack_extras_config_default(tmp_path: Path) -> None:
    """[tool.fspack] extras 配置默认启用分组."""
    clear_project_cache()
    _make_project_with_optional_deps(
        tmp_path,
        optional_block='gui = ["PySide2"]\nweb = ["flask"]\n',
        fspack_extras='extras = ["gui"]\n',
    )
    info = parse_project(tmp_path)
    assert info.build_defaults.extras == ("gui",)
    # build_options_from_defaults 透传到 BuildOptions.extras
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.extras == frozenset({"gui"})


def test_parse_fspack_extras_reject_empty_element(tmp_path: Path) -> None:
    """[tool.fspack] extras 含空字符串元素报错（reject_empty=True）."""
    clear_project_cache()
    _make_project_with_optional_deps(
        tmp_path,
        optional_block='gui = ["PySide2"]\n',
        fspack_extras='extras = ["gui", ""]\n',
    )
    with pytest.raises(ProjectError, match="必须是非空字符串"):
        parse_project(tmp_path)


def test_build_options_extras_defaults_to_empty() -> None:
    """BuildOptions.extras 默认空 frozenset；BuildDefaults.extras 默认空 tuple."""
    opts = BuildOptions()
    assert opts.extras == frozenset()
    defaults = BuildDefaults()
    assert defaults.extras == ()


def test_expand_extras_simple() -> None:
    """单个 extra 展开：base + extra 依赖."""
    result = expand_extras(
        ("rich",),
        {"gui": ("PySide2",)},
        frozenset({"gui"}),
        "myapp",
    )
    assert result == ("rich", "PySide2")


def test_expand_extras_multiple_merge_dedup() -> None:
    """多 extra 合并去重，保留首次出现顺序."""
    result = expand_extras(
        ("rich",),
        {"gui": ("PySide2",), "web": ("flask", "rich")},  # rich 重复
        frozenset({"gui", "web"}),
        "myapp",
    )
    # rich 首次出现，web 中的 rich 去重
    assert "rich" in result
    assert result.count("rich") == 1
    assert "PySide2" in result
    assert "flask" in result


def test_expand_extras_self_reference_recursion() -> None:
    """自引用 my-pkg[extra] 递归展开对应 extras 依赖."""
    optional: dict[str, tuple[str, ...]] = {
        "gui": ("PySide2",),
        "web": ("flask",),
        "full": ("myapp[gui]", "myapp[web]", "numpy"),
    }
    result = expand_extras(
        ("rich",),
        optional,
        frozenset({"full"}),
        "myapp",
    )
    # rich + PySide2 + flask + numpy（自引用展开）
    assert result == ("rich", "PySide2", "flask", "numpy")


def test_expand_extras_self_reference_with_hyphen_normalized() -> None:
    """项目名含连字符，自引用归一化匹配（my-app == my_app）."""
    optional: dict[str, tuple[str, ...]] = {"gui": ("PySide2",)}
    result = expand_extras(
        ("my-app[gui]",),
        optional,
        frozenset(),
        "my_app",  # 项目名用下划线，依赖名用连字符
    )
    assert result == ("PySide2",)


def test_expand_extras_self_reference_cycle_safe() -> None:
    """循环自引用安全终止（visited 去重避免无限递归）."""
    optional: dict[str, tuple[str, ...]] = {
        "a": ("myapp[b]",),
        "b": ("myapp[a]", "rich"),
    }
    # 启用 a → 展开 myapp[b] → 展开 myapp[a]（已访问，跳过）+ rich
    result = expand_extras(
        (),
        optional,
        frozenset({"a"}),
        "myapp",
    )
    assert "rich" in result
    # 不会无限递归（测试通过即证明）


def test_expand_extras_third_party_with_extra_preserved() -> None:
    """第三方 extras（pandas[performance]）原样保留，不展开."""
    result = expand_extras(
        ("rich",),
        {"sci": ("pandas[performance]", "numpy")},
        frozenset({"sci"}),
        "myapp",
    )
    assert "pandas[performance]" in result  # 原样保留
    assert "numpy" in result


def test_expand_extras_self_reference_unknown_extra_skipped() -> None:
    """自引用不存在的 extra 跳过（pip 行为，构建期报错更友好）."""
    optional: dict[str, tuple[str, ...]] = {"gui": ("PySide2",)}
    result = expand_extras(
        ("myapp[unknown]",),
        optional,
        frozenset(),
        "myapp",
    )
    # unknown extra 不存在，跳过；无其他依赖，结果为空
    assert result == ()


def test_expand_extras_unknown_enabled_raises() -> None:
    """enabled_extras 含未知分组名报错."""
    with pytest.raises(ProjectError, match="未知的 extras 分组"):
        expand_extras(
            ("rich",),
            {"gui": ("PySide2",)},
            frozenset({"unknown"}),
            "myapp",
        )


def test_expand_extras_empty_enabled_returns_base() -> None:
    """enabled_extras 为空时返回 base_deps（去重）."""
    result = expand_extras(
        ("rich", "rich", "numpy"),
        {"gui": ("PySide2",)},
        frozenset(),
        "myapp",
    )
    assert result == ("rich", "numpy")


def test_expand_extras_preserves_version_constraints() -> None:
    """依赖含版本约束与环境标记时原样保留."""
    optional: dict[str, tuple[str, ...]] = {
        "gui": ("PySide2>=5.15; platform_system=='Windows'",),
    }
    result = expand_extras(
        ("rich>=13; python_version<'3.11'",),
        optional,
        frozenset({"gui"}),
        "myapp",
    )
    assert "rich>=13; python_version<'3.11'" in result
    assert "PySide2>=5.15; platform_system=='Windows'" in result


# ---------- 安全加固：require_hashes / no_sbom 配置 ----------


def test_parse_project_require_hashes_config(tmp_path: Path) -> None:
    """[tool.fspack] require_hashes = true 解析为布尔值并透传到 BuildOptions."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nrequire_hashes = true\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.require_hashes is True
    # build_options_from_defaults 透传到 BuildOptions.require_hashes
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.require_hashes is True


def test_parse_project_require_hashes_default_none(tmp_path: Path) -> None:
    """未配置 require_hashes 时默认 None."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.require_hashes is None
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.require_hashes is False


def test_parse_project_require_hashes_false(tmp_path: Path) -> None:
    """[tool.fspack] require_hashes = false 显式关闭."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nrequire_hashes = false\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.require_hashes is False
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.require_hashes is False


def test_parse_project_require_hashes_invalid_type_raises(tmp_path: Path) -> None:
    """require_hashes 非布尔值时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nrequire_hashes = "yes"\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="require_hashes 必须是布尔值"):
        parse_project(tmp_path)


def test_parse_project_no_sbom_config(tmp_path: Path) -> None:
    """[tool.fspack] no_sbom = true 解析为布尔值并透传到 BuildOptions."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nno_sbom = true\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.no_sbom is True
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.no_sbom is True


def test_parse_project_no_sbom_default_none(tmp_path: Path) -> None:
    """未配置 no_sbom 时默认 None."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.no_sbom is None
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.no_sbom is False


def test_parse_project_no_sbom_invalid_type_raises(tmp_path: Path) -> None:
    """no_sbom 非布尔值时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nno_sbom = 1\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="no_sbom 必须是布尔值"):
        parse_project(tmp_path)


# ---------- 安全加固：sign-exe-certificate / sign-exe-password / sign-deb-key 配置 ----------


def test_parse_project_sign_exe_certificate_config(tmp_path: Path) -> None:
    """[tool.fspack] sign-exe-certificate 解析为字符串并透传到 BuildOptions（Path 类型）."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-exe-certificate = "cert.pfx"\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_exe_certificate == "cert.pfx"
    # build_options_from_defaults 转为 Path 类型
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.sign_exe_certificate == Path("cert.pfx")


def test_parse_project_sign_exe_certificate_default_none(tmp_path: Path) -> None:
    """未配置 sign-exe-certificate 时默认 None."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_exe_certificate is None
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.sign_exe_certificate is None


def test_parse_project_sign_exe_certificate_strips_whitespace(tmp_path: Path) -> None:
    """sign-exe-certificate 前后空白被 strip."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-exe-certificate = "  cert.pfx  "\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_exe_certificate == "cert.pfx"


def test_parse_project_sign_exe_certificate_empty_raises(tmp_path: Path) -> None:
    """sign-exe-certificate 为空字符串报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-exe-certificate = ""\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="sign-exe-certificate 必须是非空字符串"):
        parse_project(tmp_path)


def test_parse_project_sign_exe_certificate_non_string_raises(tmp_path: Path) -> None:
    """sign-exe-certificate 非字符串报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-exe-certificate = 123\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="sign-exe-certificate 必须是非空字符串"):
        parse_project(tmp_path)


def test_parse_project_sign_exe_password_config(tmp_path: Path) -> None:
    """[tool.fspack] sign-exe-password 解析为字符串并透传到 BuildOptions."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-exe-password = "s3cret"\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_exe_password == "s3cret"
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.sign_exe_password == "s3cret"


def test_parse_project_sign_exe_password_default_none(tmp_path: Path) -> None:
    """未配置 sign-exe-password 时默认 None."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_exe_password is None
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.sign_exe_password is None


def test_parse_project_sign_exe_password_empty_string_allowed(tmp_path: Path) -> None:
    """sign-exe-password 允许空字符串（与 sign-exe-certificate 不同，不要求非空）."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-exe-password = ""\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_exe_password == ""
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.sign_exe_password == ""


def test_parse_project_sign_exe_password_non_string_raises(tmp_path: Path) -> None:
    """sign-exe-password 非字符串报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-exe-password = 123\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="sign-exe-password 必须是字符串"):
        parse_project(tmp_path)


def test_parse_project_sign_deb_key_config(tmp_path: Path) -> None:
    """[tool.fspack] sign-deb-key 解析为字符串并透传到 BuildOptions."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-deb-key = "0x12345678"\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_deb_key == "0x12345678"
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.sign_deb_key == "0x12345678"


def test_parse_project_sign_deb_key_default_none(tmp_path: Path) -> None:
    """未配置 sign-deb-key 时默认 None."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_deb_key is None
    opts = build_options_from_defaults(info.build_defaults)
    assert opts.sign_deb_key is None


def test_parse_project_sign_deb_key_strips_whitespace(tmp_path: Path) -> None:
    """sign-deb-key 前后空白被 strip."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-deb-key = "  0xABCD1234  "\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    info = parse_project(tmp_path)
    assert info.build_defaults.sign_deb_key == "0xABCD1234"


def test_parse_project_sign_deb_key_empty_raises(tmp_path: Path) -> None:
    """sign-deb-key 为空字符串报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-deb-key = ""\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="sign-deb-key 必须是非空字符串"):
        parse_project(tmp_path)


def test_parse_project_sign_deb_key_non_string_raises(tmp_path: Path) -> None:
    """sign-deb-key 非字符串报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nsign-deb-key = 123\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="sign-deb-key 必须是非空字符串"):
        parse_project(tmp_path)


def test_build_options_security_defaults() -> None:
    """BuildOptions 安全加固字段默认值：require_hashes/no_sbom=False，签名相关=None."""
    opts = BuildOptions()
    assert opts.require_hashes is False
    assert opts.no_sbom is False
    assert opts.sign_exe_certificate is None
    assert opts.sign_exe_password is None
    assert opts.sign_deb_key is None


def test_build_defaults_security_defaults_all_none() -> None:
    """BuildDefaults 安全加固字段默认全为 None（未配置时回退到 BuildOptions 默认值）."""
    defaults = BuildDefaults()
    assert defaults.require_hashes is None
    assert defaults.no_sbom is None
    assert defaults.sign_exe_certificate is None
    assert defaults.sign_exe_password is None
    assert defaults.sign_deb_key is None


# --- iter-148 前后端分离 Web 打包：AppType.WEB 推断与配置 ---


def test_apptype_web_enum_value() -> None:
    """AppType.WEB 枚举值为 'web'（与 CLI/GUI 并列）."""
    assert AppType.WEB.value == "web"
    assert AppType.WEB is not AppType.CLI
    assert AppType.WEB is not AppType.GUI


def test_infer_app_type_flask_import(tmp_path: Path) -> None:
    """入口脚本 import flask 推断为 WEB 类型."""
    script = tmp_path / "app.py"
    script.write_text("from flask import Flask\ndef main():\n    pass\n")
    assert infer_app_type(script, ()) is AppType.WEB


def test_infer_app_type_fastapi_import(tmp_path: Path) -> None:
    """入口脚本 import fastapi 推断为 WEB 类型."""
    script = tmp_path / "app.py"
    script.write_text("from fastapi import FastAPI\ndef main():\n    pass\n")
    assert infer_app_type(script, ()) is AppType.WEB


def test_infer_app_type_uvicorn_import(tmp_path: Path) -> None:
    """入口脚本 import uvicorn 推断为 WEB 类型（ASGI 服务器）."""
    script = tmp_path / "app.py"
    script.write_text("import uvicorn\ndef main():\n    pass\n")
    assert infer_app_type(script, ()) is AppType.WEB


def test_infer_app_type_gui_priority_over_web(tmp_path: Path) -> None:
    """GUI 优先级高于 WEB：同时 import GUI 与 Web 框架时判为 GUI.

    matplotlib 等可视化库偶尔与 web 框架共存，按 GUI 处理关闭控制台更合理。
    """
    script = tmp_path / "app.py"
    script.write_text("import flask\nimport tkinter\ndef main():\n    pass\n")
    assert infer_app_type(script, ()) is AppType.GUI


def test_infer_app_type_web_from_declared_dependency(tmp_path: Path) -> None:
    """入口脚本未直接 import 但声明依赖 flask 时推断为 WEB 类型."""
    script = tmp_path / "app.py"
    script.write_text("def main():\n    pass\n")
    assert infer_app_type(script, ("flask>=2",)) is AppType.WEB


def test_parse_web_static_dirs_valid(tmp_path: Path) -> None:
    """[tool.fspack] web-static-dirs 配置解析为路径元组."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nweb-static-dirs = ["dist", "static"]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.web_static_dirs == ("dist", "static")


def test_parse_web_static_dirs_empty(tmp_path: Path) -> None:
    """无 [tool.fspack] web-static-dirs 配置时 web_static_dirs 为空元组."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.web_static_dirs == ()


def test_parse_web_static_dirs_invalid_type(tmp_path: Path) -> None:
    """web-static-dirs 非列表时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nweb-static-dirs = "dist"\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="web-static-dirs 必须是字符串列表"):
        parse_project(tmp_path)


def test_parse_web_static_dirs_empty_element(tmp_path: Path) -> None:
    """web-static-dirs 元素为空字符串时报错."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nweb-static-dirs = [""]\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(ProjectError, match="web-static-dirs 元素必须是非空字符串"):
        parse_project(tmp_path)


def test_build_defaults_open_browser(tmp_path: Path) -> None:
    """[tool.fspack] open_browser = true 解析到 BuildDefaults.open_browser."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\nopen_browser = true\n'
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = parse_project(tmp_path)
    assert info.build_defaults.open_browser is True


def test_build_options_open_browser() -> None:
    """BuildOptions.open_browser 默认 False（WEB 类型在 stages 层自动启用）."""
    opts = BuildOptions()
    assert opts.open_browser is False
    # build_options_from_defaults: None 回退到 BuildOptions 默认值
    defaults = BuildDefaults()
    assert build_options_from_defaults(defaults).open_browser is False
    # 非 None 时覆盖
    defaults_true = BuildDefaults(open_browser=True)
    assert build_options_from_defaults(defaults_true).open_browser is True


def test_default_entry_web_priority_over_cli() -> None:
    """多入口项目默认入口：WEB 优先于 CLI（GUI > WEB > CLI）."""
    ep_cli = EntryPoint(name="cli", module="cli", file=Path("cli.py"), app_type=AppType.CLI)
    ep_web = EntryPoint(name="web", module="web", file=Path("web.py"), app_type=AppType.WEB)
    info = ProjectInfo(
        name="multi",
        version="0.1",
        src_dir=Path(),
        entry_module="cli",
        entry_file=Path("cli.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
        entries=(ep_cli, ep_web),
    )
    assert info.default_entry is ep_web
