"""入口包装器源码生成测试：dotted_module_name 与 generate_wrapper_source。."""

from __future__ import annotations

from pathlib import Path

from fspack.entry_wrapper import dotted_module_name, generate_wrapper_source


def test_dotted_module_name_entry_outside_src_dir(tmp_path: Path) -> None:
    """入口脚本不在 src_dir 内时返回 None。."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    entry = tmp_path / "other.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) is None


def test_dotted_module_name_entry_equals_src_dir(tmp_path: Path) -> None:
    """入口路径等于 src_dir 自身时返回 None（rel.parts 为空）。."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    # entry_file 即 src_dir 本身（边界场景，relative_to 返回 "."）
    assert dotted_module_name(src_dir, src_dir) is None


def test_dotted_module_name_top_level_no_init(tmp_path: Path) -> None:
    """入口在 src_dir 顶层且 src_dir 无 __init__.py：返回 None（顶层模式）。."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    entry = src_dir / "main.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) is None


def test_dotted_module_name_top_level_with_init(tmp_path: Path) -> None:
    """入口在 src_dir 顶层且 src_dir 有 __init__.py：返回 ('src.<stem>', '.')。."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    entry = src_dir / "game.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) == ("src.game", ".")


def test_dotted_module_name_subdir_chain_with_init_prefix_src(tmp_path: Path) -> None:
    """入口在子目录且目录链都有 __init__.py，src_dir 也有：返回 ('src.<pkg>.<stem>', '.')。."""
    src_dir = tmp_path / "src"
    pkg = src_dir / "pkg"
    pkg.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    entry = pkg / "main.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) == ("src.pkg.main", ".")


def test_dotted_module_name_subdir_chain_with_init_no_prefix(tmp_path: Path) -> None:
    """入口在子目录且目录链都有 __init__.py，src_dir 无：返回 ('<pkg>.<stem>', 'src')。."""
    src_dir = tmp_path / "src"
    pkg = src_dir / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    entry = pkg / "main.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) == ("pkg.main", "src")


def test_dotted_module_name_subdir_chain_broken(tmp_path: Path) -> None:
    """入口在子目录但某级目录无 __init__.py（src_dir 是包）：返回 None。."""
    src_dir = tmp_path / "src"
    pkg = src_dir / "pkg"
    pkg.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    # pkg 目录无 __init__.py，src_dir 是包时不允许容器跳过
    entry = pkg / "main.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) is None


def test_dotted_module_name_nested_subdir_chain(tmp_path: Path) -> None:
    """入口在多层嵌套子目录且目录链都为包：返回完整 dotted 路径。."""
    src_dir = tmp_path / "src"
    nested = src_dir / "pkg" / "sub"
    nested.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    (src_dir / "pkg" / "__init__.py").write_text("")
    (nested / "__init__.py").write_text("")
    entry = nested / "main.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) == ("src.pkg.sub.main", ".")


def test_dotted_module_name_non_py_extension(tmp_path: Path) -> None:
    """入口文件无 .py 后缀时直接用文件名作为模块名。."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    # 不以 .py 结尾的文件（理论上 fspack 入口都是 .py，此处覆盖分支）
    entry = src_dir / "main"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) == ("src.main", ".")


def test_dotted_module_name_src_layout(tmp_path: Path) -> None:
    """src-layout：src_dir 非包，src/ 容器无 __init__.py，其下 pkg 有。

    模拟 fuscan 项目结构：project_root/src/fuscan/__main__.py。
    返回 ("fuscan.__main__", "src/src")——包在 dist/src/src/fuscan/。
    """
    src_dir = tmp_path  # 项目根
    container = src_dir / "src"  # src-layout 容器（无 __init__.py）
    pkg = container / "fuscan"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    entry = pkg / "__main__.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) == ("fuscan.__main__", "src/src")


def test_dotted_module_name_src_layout_nested_entry(tmp_path: Path) -> None:
    """src-layout 下嵌套入口（如 fuscan.gui.__main__）。."""
    src_dir = tmp_path  # 项目根
    container = src_dir / "src"
    pkg = container / "fuscan"
    sub = pkg / "gui"
    sub.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    entry = sub / "__main__.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) == ("fuscan.gui.__main__", "src/src")


def test_dotted_module_name_src_layout_multi_container(tmp_path: Path) -> None:
    """多层容器前缀：src_dir/outer/inner/pkg/main.py，outer/inner 均无 __init__.py。."""
    src_dir = tmp_path
    pkg = src_dir / "outer" / "inner" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    entry = pkg / "main.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) == ("pkg.main", "src/outer/inner")


def test_dotted_module_name_no_pkg_after_container(tmp_path: Path) -> None:
    """src-layout 容器下无包目录：返回 None（全无 __init__.py）。."""
    src_dir = tmp_path
    container = src_dir / "src"
    pkg = container / "pkg"  # pkg 也无 __init__.py
    pkg.mkdir(parents=True)
    entry = pkg / "main.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) is None


def test_dotted_module_name_pkg_after_container_broken(tmp_path: Path) -> None:
    """src-layout 容器后首个包之后某级目录无 __init__.py：返回 None。."""
    src_dir = tmp_path
    container = src_dir / "src"
    pkg = container / "pkg"
    broken = pkg / "broken"  # broken 无 __init__.py，在 pkg 之后
    broken.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    entry = broken / "main.py"
    entry.write_text("")
    assert dotted_module_name(src_dir, entry) is None


def test_generate_wrapper_source_top_level_mode() -> None:
    """顶层模式（module_dotted=None）生成 run_path 分支。."""
    source = generate_wrapper_source("app", None, "app.py")
    assert "fspack 生成的入口包装器（app）" in source
    assert "_ENTRY_MODULE = None" in source
    assert "_ENTRY_REL = 'app.py'" in source
    # 模板里两个 runpy 调用都在，靠 if _ENTRY_MODULE 控制流；此处验证 None 字面量
    assert "runpy.run_path" in source


def test_generate_wrapper_source_package_mode() -> None:
    """包模式（module_dotted='src.game'）生成 run_module 分支。."""
    source = generate_wrapper_source("gktetris", "src.game", "game.py")
    assert "fspack 生成的入口包装器（gktetris）" in source
    assert "_ENTRY_MODULE = 'src.game'" in source
    assert "_ENTRY_REL = 'game.py'" in source
    assert "_PKG_ROOT_REL = '.'" in source
    # 模板里两个 runpy 调用都在，靠 if _ENTRY_MODULE 控制流；此处验证模块名已注入
    assert "runpy.run_module(_ENTRY_MODULE" in source


def test_generate_wrapper_source_src_layout_pkg_root() -> None:
    """src-layout 包模式：pkg_root_rel='src/src' 注入 _PKG_ROOT_REL。."""
    source = generate_wrapper_source("fuscan", "fuscan.__main__", "src/fuscan/__main__.py", "src/src")
    assert "_ENTRY_MODULE = 'fuscan.__main__'" in source
    assert "_ENTRY_REL = 'src/fuscan/__main__.py'" in source
    assert "_PKG_ROOT_REL = 'src/src'" in source
    assert "runpy.run_module(_ENTRY_MODULE" in source


def test_generate_wrapper_source_qt_plugin_paths() -> None:
    """wrapper 源码含 Qt 插件路径设置代码（PySide2/PySide6/PyQt5/6）。."""
    source = generate_wrapper_source("app", None, "app.py")
    for qt_pkg in ("PySide2", "PySide6", "PyQt5", "PyQt6"):
        assert qt_pkg in source
    assert "QT_PLUGIN_PATH" in source
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" in source
