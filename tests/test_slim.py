"""slim 精简打包测试：wheel 文件归属分类与按需解压."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from fspack.exceptions import DependencyError
from fspack.slim import classify_entry, slim_unpack

if sys.version_info >= (3, 12):  # pragma: no cover
    from typing import override
else:
    from typing_extensions import override  # type: ignore[import-not-found]


class TestClassifyEntry:
    """wheel 条目归属分类."""

    def test_dist_info(self) -> None:
        assert classify_entry("PySide2-5.15.2.1.dist-info/METADATA", "PySide2") == ("metadata", None)

    def test_init_py(self) -> None:
        assert classify_entry("PySide2/__init__.py", "PySide2") == ("shared", None)

    def test_private_module(self) -> None:
        assert classify_entry("PySide2/_config.py", "PySide2") == ("shared", None)

    def test_pyd_file(self) -> None:
        """Qt 库 .pyd 归一化子模块名：QtCore.pyd → Core."""
        assert classify_entry("PySide2/QtCore.pyd", "PySide2") == ("submodule", "Core")

    def test_pyi_file(self) -> None:
        assert classify_entry("PySide2/QtCore.pyi", "PySide2") == ("submodule", "Core")

    def test_pyd_file_3d(self) -> None:
        """Qt3DCore.pyd 归一化为 3DCore."""
        assert classify_entry("PySide2/Qt3DCore.pyd", "PySide2") == ("submodule", "3DCore")

    def test_so_file(self) -> None:
        """子目录下的 .so 文件归类为 shared（len(parts) > 2）."""
        assert classify_entry("numpy/core/multiarray.so", "numpy") == ("shared", None)

    def test_qt5_dll(self) -> None:
        """Qt5Core.dll 归一化为子模块 Core（按需保留）."""
        assert classify_entry("PySide2/Qt5Core.dll", "PySide2") == ("submodule", "Core")

    def test_qt5_3d_dll(self) -> None:
        """Qt53DAnimation.dll 归一化为子模块 3DAnimation."""
        assert classify_entry("PySide2/Qt53DAnimation.dll", "PySide2") == ("submodule", "3DAnimation")

    def test_qt6_dll(self) -> None:
        """PySide6 的 Qt6Gui.dll 归一化为子模块 Gui."""
        assert classify_entry("PySide6/Qt6Gui.dll", "PySide6") == ("submodule", "Gui")

    def test_other_dll(self) -> None:
        """非 Qt5/Qt6 前缀的 DLL（VC++ 运行时等）归 shared 始终保留."""
        assert classify_entry("PySide2/concrt140.dll", "PySide2") == ("shared", None)

    def test_pyside_abi_dll(self) -> None:
        """pyside2.abi3.dll 归 shared（绑定层，始终保留）."""
        assert classify_entry("PySide2/pyside2.abi3.dll", "PySide2") == ("shared", None)

    def test_qt_exe_excluded(self) -> None:
        """Qt 自带开发工具 exe 归 exclude（运行时不需要）."""
        assert classify_entry("PySide2/designer.exe", "PySide2") == ("exclude", None)

    def test_subdir_platforms(self) -> None:
        """plugins/platforms 始终保留（窗口系统必需）."""
        assert classify_entry("PySide2/plugins/platforms/qwindows.dll", "PySide2") == ("shared", None)

    def test_subdir_imageformats(self) -> None:
        """plugins/imageformats 始终保留."""
        assert classify_entry("PySide2/plugins/imageformats/qsvg.dll", "PySide2") == ("shared", None)

    def test_subdir_mediaservice_no_dep(self) -> None:
        """plugins/mediaservice 无 Multimedia 依赖时剥离."""
        assert classify_entry("PySide2/plugins/mediaservice/wmfengine.dll", "PySide2") == ("exclude", None)

    def test_subdir_mediaservice_with_dep(self) -> None:
        """plugins/mediaservice 有 Multimedia 依赖时保留."""
        result = classify_entry("PySide2/plugins/mediaservice/wmfengine.dll", "PySide2", {"Multimedia"})
        assert result == ("shared", None)

    def test_subdir_sqldrivers_with_dep(self) -> None:
        """plugins/sqldrivers 有 Sql 依赖时保留."""
        result = classify_entry("PySide2/plugins/sqldrivers/qsqlite.dll", "PySide2", {"Sql"})
        assert result == ("shared", None)

    def test_subdir_unknown_plugin_excluded(self) -> None:
        """未知 plugins 子目录白名单制剥离."""
        assert classify_entry("PySide2/plugins/unknown/x.dll", "PySide2") == ("exclude", None)

    def test_examples_excluded(self) -> None:
        """examples 目录始终剥离."""
        assert classify_entry("PySide2/examples/charts/linechart.py", "PySide2") == ("exclude", None)

    def test_translations_excluded(self) -> None:
        """translations 目录始终剥离."""
        assert classify_entry("PySide2/translations/qtbase_ar.qm", "PySide2") == ("exclude", None)

    def test_include_excluded(self) -> None:
        """include 目录（C 头文件）始终剥离."""
        assert classify_entry("PySide2/include/QtGui/qguiapplication.h", "PySide2") == ("exclude", None)

    def test_resources_no_dep_excluded(self) -> None:
        """resources 目录无 WebEngine 依赖时剥离."""
        assert classify_entry("PySide2/resources/icudtl.dat", "PySide2") == ("exclude", None)

    def test_resources_with_dep_kept(self) -> None:
        """resources 目录有 WebEngine 依赖时保留."""
        result = classify_entry("PySide2/resources/icudtl.dat", "PySide2", {"WebEngineCore"})
        assert result == ("shared", None)

    def test_qml_no_dep_excluded(self) -> None:
        """qml 目录无 Qml/Quick 依赖时剥离."""
        assert classify_entry("PySide2/qml/QtQuick.2/qmldir", "PySide2") == ("exclude", None)

    def test_qml_with_dep_kept(self) -> None:
        """qml 目录有 Quick 依赖时保留."""
        result = classify_entry("PySide2/qml/QtQuick.2/qmldir", "PySide2", {"Quick"})
        assert result == ("shared", None)

    def test_other_pkg(self) -> None:
        assert classify_entry("shiboken2/shiboken2.pyd", "PySide2") == ("shared", None)

    def test_top_level_file(self) -> None:
        assert classify_entry("PySide2/py.typed", "PySide2") == ("shared", None)

    def test_non_qt_pyd(self) -> None:
        """非 Qt 库的 .pyd 按原始文件名归类（不归一化）."""
        assert classify_entry("numpy/_core/multiarray.pyd", "numpy") == ("shared", None)
        assert classify_entry("mypkg/core.pyd", "mypkg") == ("submodule", "core")


class TestQtModuleClosure:
    """Qt 模块依赖闭包计算（归一化名）."""

    def test_core_only(self) -> None:
        from fspack.slim.qt import _qt_module_closure

        assert _qt_module_closure({"Core"}) == {"Core"}

    def test_widgets_closure(self) -> None:
        """QtWidgets → Gui → Core（C 层链接依赖链）."""
        from fspack.slim.qt import _qt_module_closure

        assert _qt_module_closure({"Widgets"}) == {"Widgets", "Gui", "Core"}

    def test_quick_transitive(self) -> None:
        """QtQuick → QtQml → QtNetwork → QtCore + QtGui."""
        from fspack.slim.qt import _qt_module_closure

        result = _qt_module_closure({"Quick"})
        assert {"Quick", "Qml", "Network", "Gui", "Core"}.issubset(result)

    def test_qt3d_extras_transitive(self) -> None:
        """Qt3DExtras 闭包含 3DRender/3DInput/3DLogic/3DCore/Core/Gui/Network."""
        from fspack.slim.qt import _qt_module_closure

        result = _qt_module_closure({"3DExtras"})
        assert result == {
            "3DExtras",
            "3DRender",
            "3DInput",
            "3DLogic",
            "3DCore",
            "Gui",
            "Core",
            "Network",
        }

    def test_unknown_module_kept(self) -> None:
        """未知模块名原样保留，不触发额外依赖推导."""
        from fspack.slim.qt import _qt_module_closure

        assert _qt_module_closure({"UnknownMod"}) == {"UnknownMod"}

    def test_mixed_known_unknown(self) -> None:
        """已知与未知模块混合时，已知模块触发闭包，未知模块原样保留."""
        from fspack.slim.qt import _qt_module_closure

        result = _qt_module_closure({"Widgets", "Foo"})
        assert result == {"Widgets", "Gui", "Core", "Foo"}

    def test_empty_set(self) -> None:
        from fspack.slim.qt import _qt_module_closure

        assert _qt_module_closure(set()) == set()

    def test_idempotent(self) -> None:
        """闭包计算幂等：对已闭包集合再次计算结果不变."""
        from fspack.slim.qt import _qt_module_closure

        once = _qt_module_closure({"Widgets"})
        twice = _qt_module_closure(once)
        assert once == twice


class TestQtDllClassification:
    """Qt5/Qt6*.dll 文件名与 Qt 子模块名归一化."""

    def test_qt5core_to_core(self) -> None:
        from fspack.slim.qt import _qt_dll_submodule

        assert _qt_dll_submodule("Qt5Core") == "Core"

    def test_qt6widgets_to_widgets(self) -> None:
        from fspack.slim.qt import _qt_dll_submodule

        assert _qt_dll_submodule("Qt6Widgets") == "Widgets"

    def test_qt5_3d_animation(self) -> None:
        """Qt53DAnimation.dll → 3DAnimation（去掉 5 后保留 3DAnimation）."""
        from fspack.slim.qt import _qt_dll_submodule

        assert _qt_dll_submodule("Qt53DAnimation") == "3DAnimation"

    def test_non_qt_dll_returns_none(self) -> None:
        from fspack.slim.qt import _qt_dll_submodule

        assert _qt_dll_submodule("pyside2.abi3") is None
        assert _qt_dll_submodule("concrt140") is None
        assert _qt_dll_submodule("msvcp140") is None

    def test_normalize_qtcore(self) -> None:
        from fspack.slim.qt import _normalize_qt_sub

        assert _normalize_qt_sub("QtCore") == "Core"
        assert _normalize_qt_sub("Qt5Core") == "Core"
        assert _normalize_qt_sub("Qt6Core") == "Core"

    def test_normalize_qt3dcore(self) -> None:
        """Qt3DCore 归一化为 3DCore."""
        from fspack.slim.qt import _normalize_qt_sub

        assert _normalize_qt_sub("Qt3DCore") == "3DCore"

    def test_normalize_non_qt(self) -> None:
        """非 Qt 前缀原样返回."""
        from fspack.slim.qt import _normalize_qt_sub

        assert _normalize_qt_sub("requests") == "requests"
        assert _normalize_qt_sub("os") == "os"


def _make_wheel(whl: Path, entries: dict[str, bytes]) -> None:
    """构造测试用 wheel 文件."""
    with zipfile.ZipFile(whl, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


class TestSlimUnpack:
    """按需解压 wheel."""

    def test_selective_unpack(self, tmp_path: Path) -> None:
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide2/__init__.py": b"",
                "PySide2/QtCore.pyd": b"core",
                "PySide2/QtWidgets.pyd": b"widgets",
                "PySide2/QtGui.pyd": b"gui",
                "PySide2/Qt5Core.dll": b"qt5core",
                "PySide2/Qt5Widgets.dll": b"qt5widgets",
                "PySide2/Qt5Gui.dll": b"qt5gui",
                # abi3.dll 隐式依赖 Qml/Network 的 DLL → 归 shared 始终保留
                "PySide2/Qt5Network.dll": b"net",
                "PySide2/Qt5Qml.dll": b"qml",
                # 非 abi3 依赖且未 import → 剥离
                "PySide2/Qt5Sql.dll": b"sql",
                "PySide2/plugins/platforms/qwindows.dll": b"plugin",
                "PySide2/plugins/mediaservice/wmf.dll": b"media",
                "PySide2/examples/dummy.py": b"example",
                "PySide2/designer.exe": b"tool",
                "PySide2-5.15.2.1.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        # 用户只 import QtCore/QtWidgets，Qt 闭包自动加入 Gui（C 层依赖）
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore", "QtWidgets"})})
        assert count == 1
        assert (dest / "PySide2" / "__init__.py").is_file()
        # 闭包内（Core/Widgets/Gui）→ 对应 .pyd 与 Qt5*.dll 保留
        assert (dest / "PySide2" / "QtCore.pyd").is_file()
        assert (dest / "PySide2" / "QtWidgets.pyd").is_file()
        assert (dest / "PySide2" / "QtGui.pyd").is_file()  # 闭包自动加入
        assert (dest / "PySide2" / "Qt5Core.dll").is_file()
        assert (dest / "PySide2" / "Qt5Widgets.dll").is_file()
        assert (dest / "PySide2" / "Qt5Gui.dll").is_file()  # 闭包自动加入
        # abi3.dll 隐式依赖的 Qml/Network DLL → 归 shared 始终保留（.pyd 仍按需）
        assert (dest / "PySide2" / "Qt5Network.dll").is_file()
        assert (dest / "PySide2" / "Qt5Qml.dll").is_file()
        # 未 import 且非 abi3 依赖的 Qt5Sql.dll → 剥离
        assert not (dest / "PySide2" / "Qt5Sql.dll").exists()
        # platforms 基础插件始终保留
        assert (dest / "PySide2" / "plugins" / "platforms" / "qwindows.dll").is_file()
        # mediaservice 无 Multimedia 依赖 → 剥离
        assert not (dest / "PySide2" / "plugins" / "mediaservice" / "wmf.dll").exists()
        # examples 与开发工具 exe 始终剥离
        assert not (dest / "PySide2" / "examples" / "dummy.py").exists()
        assert not (dest / "PySide2" / "designer.exe").exists()
        assert (dest / "PySide2-5.15.2.1.dist-info" / "METADATA").is_file()

    def test_no_usage_full_unpack(self, tmp_path: Path) -> None:
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide2/__init__.py": b"",
                "PySide2/QtGui.pyd": b"gui",
                "PySide2/Qt5Gui.dll": b"qt5gui",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest)
        assert count == 1
        assert (dest / "PySide2" / "QtGui.pyd").is_file()
        assert (dest / "PySide2" / "Qt5Gui.dll").is_file()

    def test_empty_usage_full_unpack(self, tmp_path: Path) -> None:
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtGui.pyd": b"gui"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {})
        assert count == 1
        assert (dest / "PySide2" / "QtGui.pyd").is_file()

    def test_keep_module_merged(self, tmp_path: Path) -> None:
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide2/QtCore.pyd": b"core",
                "PySide2/QtGui.pyd": b"gui",
                "PySide2/QtWidgets.pyd": b"widgets",
                "PySide2/Qt5Core.dll": b"c",
                "PySide2/Qt5Gui.dll": b"g",
                "PySide2/Qt5Widgets.dll": b"w",
            },
        )
        dest = tmp_path / "sp"
        # submodule_usage(QtCore) + keep_modules(QtGui) 合并后保留 {Core, Gui}
        count = slim_unpack(
            [whl],
            dest,
            {"PySide2": frozenset({"QtCore"})},
            keep_modules={"PySide2.QtGui"},
        )
        assert count == 1
        assert (dest / "PySide2" / "QtCore.pyd").is_file()
        assert (dest / "PySide2" / "QtGui.pyd").is_file()
        assert (dest / "PySide2" / "Qt5Core.dll").is_file()
        assert (dest / "PySide2" / "Qt5Gui.dll").is_file()
        # QtWidgets 未在保留集合中 → .pyd 与 Qt5Widgets.dll 均剥离
        assert not (dest / "PySide2" / "QtWidgets.pyd").exists()
        assert not (dest / "PySide2" / "Qt5Widgets.dll").exists()

    def test_unparseable_wheel_full_unpack(self, tmp_path: Path) -> None:
        whl = tmp_path / "wh" / "not-a-wheel.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtGui.pyd": b"gui"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore"})})
        assert count == 1
        assert (dest / "PySide2" / "QtGui.pyd").is_file()

    def test_bad_zip_raises(self, tmp_path: Path) -> None:
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        whl.write_bytes(b"not a zip")
        dest = tmp_path / "sp"
        with pytest.raises(DependencyError, match="wheel 损坏"):
            slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore"})})

    def test_no_matching_pkg_full_unpack(self, tmp_path: Path) -> None:
        """submodule_usage 有 numpy 但 wheel 是 PySide2 → 全量解压."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtGui.pyd": b"gui"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"numpy": frozenset({"core"})})
        assert count == 1
        assert (dest / "PySide2" / "QtGui.pyd").is_file()

    def test_full_unpack_bad_zip_no_usage(self, tmp_path: Path) -> None:
        """无 submodule_usage 时坏 zip 走 _full_unpack 路径抛错."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        whl.write_bytes(b"not a zip")
        with pytest.raises(DependencyError, match="wheel 损坏"):
            slim_unpack([whl], tmp_path / "sp")

    def test_slim_extract_with_dir_entries(self, tmp_path: Path) -> None:
        """wheel 含目录条目时正确提取目录与文件."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        with zipfile.ZipFile(whl, "w") as zf:
            zf.writestr("PySide2/", "")
            zf.writestr("PySide2/QtCore.pyd", b"core")
            zf.writestr("PySide2/plugins/", "")
            zf.writestr("PySide2/plugins/platforms/qwindows.dll", b"plugin")
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore"})})
        assert count == 1
        assert (dest / "PySide2" / "QtCore.pyd").is_file()
        assert (dest / "PySide2" / "plugins" / "platforms" / "qwindows.dll").is_file()

    def test_slim_extract_no_skip(self, tmp_path: Path) -> None:
        """所有子模块都在保留集合中时不跳过任何文件."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtCore.pyd": b"core", "PySide2/QtGui.pyd": b"gui"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore", "QtGui"})})
        assert count == 1
        assert (dest / "PySide2" / "QtCore.pyd").is_file()
        assert (dest / "PySide2" / "QtGui.pyd").is_file()

    def test_detect_top_pkg_skips_non_matching(self, tmp_path: Path) -> None:
        """_detect_top_pkg 跳过不匹配的顶层目录后找到匹配项."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        with zipfile.ZipFile(whl, "w") as zf:
            zf.writestr("shiboken2/something.py", b"")
            zf.writestr("PySide2-5.15.2.1.dist-info/METADATA", b"")
            zf.writestr("PySide2/QtCore.pyd", b"core")
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore"})})
        assert count == 1
        assert (dest / "PySide2" / "QtCore.pyd").is_file()

    def test_detect_top_pkg_no_match_full_unpack(self, tmp_path: Path) -> None:
        """wheel 顶层目录与包名不匹配时全量解压."""
        whl = tmp_path / "wh" / "numpy-1.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"different_pkg/core.pyd": b"core"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"numpy": frozenset({"core"})})
        assert count == 1
        assert (dest / "different_pkg" / "core.pyd").is_file()

    def test_keep_module_without_dot_skipped(self, tmp_path: Path) -> None:
        """keep_modules 中无 '.' 的条目被跳过，走全量解压."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtGui.pyd": b"gui"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, keep_modules={"PySide2"})
        assert count == 1
        assert (dest / "PySide2" / "QtGui.pyd").is_file()

    def test_slim_extract_bad_zip(self, tmp_path: Path) -> None:
        """精简解压路径遇到坏 zip 抛 DependencyError."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        whl.write_bytes(b"not a zip")
        with pytest.raises(DependencyError, match="wheel 损坏"):
            slim_unpack([whl], tmp_path / "sp", {"PySide2": frozenset({"QtCore"})})

    def test_default_unpack_excludes_runtime_dirs(self, tmp_path: Path) -> None:
        """非 Qt 库的 examples/docs/tests 子目录在精简模式下剥离。."""
        whl = tmp_path / "wh" / "mypkg-1.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "mypkg/__init__.py": b"",
                "mypkg/core.pyd": b"core",
                "mypkg/utils.py": b"u",
                # 非必要子目录 → 剥离
                "mypkg/examples/demo.py": b"demo",
                "mypkg/examples/nested/deep.py": b"deep",
                "mypkg/docs/index.md": b"docs",
                "mypkg/tests/test_core.py": b"test",
                "mypkg/testing/helper.py": b"helper",
                # 运行时子目录 → 保留
                "mypkg/sub/x.py": b"sub",
                "mypkg-1.0.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        # 保留集合非空 → 进入 _slim_extract 路径
        count = slim_unpack([whl], dest, {"mypkg": frozenset({"core"})})
        assert count == 1
        # 保留子模块 .pyd 与顶层文件
        assert (dest / "mypkg" / "core.pyd").is_file()
        assert (dest / "mypkg" / "__init__.py").is_file()
        assert (dest / "mypkg" / "utils.py").is_file()
        # 运行时子目录保留
        assert (dest / "mypkg" / "sub" / "x.py").is_file()
        # 非必要子目录剥离
        assert not (dest / "mypkg" / "examples").exists()
        assert not (dest / "mypkg" / "docs").exists()
        assert not (dest / "mypkg" / "tests").exists()
        assert not (dest / "mypkg" / "testing").exists()
        # 元数据保留
        assert (dest / "mypkg-1.0.dist-info" / "METADATA").is_file()

    def test_no_usage_still_excludes_dirs(self, tmp_path: Path) -> None:
        """无 submodule_usage 时仍剥离 examples/docs/tests（顶层 import 场景）.

        回归：源码 ``import pygame`` 顶层导入时 AST 不收集子模块使用信息，
        ``keep_subs`` 为空集，``_slim_extract`` 应剥离非必要子目录但保留
        所有运行时文件（等价于全量解压 + 应用剥离规则）。
        """
        whl = tmp_path / "wh" / "mypkg-1.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "mypkg/__init__.py": b"",
                "mypkg/core.pyd": b"core",
                "mypkg/utils.py": b"u",
                # 非必要子目录 → 应剥离
                "mypkg/examples/demo.py": b"demo",
                "mypkg/docs/index.md": b"docs",
                "mypkg/tests/test_core.py": b"test",
                # 运行时子目录 → 保留
                "mypkg/sub/x.py": b"sub",
                "mypkg-1.0.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        # 不传 submodule_usage：keep_subs 为空集 → 走 _slim_extract 应用剥离规则
        count = slim_unpack([whl], dest)
        assert count == 1
        # 运行时文件全保留（含子模块 .pyd）
        assert (dest / "mypkg" / "__init__.py").is_file()
        assert (dest / "mypkg" / "core.pyd").is_file()
        assert (dest / "mypkg" / "utils.py").is_file()
        assert (dest / "mypkg" / "sub" / "x.py").is_file()
        # 非必要子目录剥离
        assert not (dest / "mypkg" / "examples").exists()
        assert not (dest / "mypkg" / "docs").exists()
        assert not (dest / "mypkg" / "tests").exists()
        # 元数据保留
        assert (dest / "mypkg-1.0.dist-info" / "METADATA").is_file()

    def test_no_usage_qt_still_excludes_dirs(self, tmp_path: Path) -> None:
        """Qt 库无 submodule_usage 时仍剥离 examples/translations/include 等。."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide2/__init__.py": b"",
                "PySide2/QtGui.pyd": b"gui",
                "PySide2/Qt5Gui.dll": b"qt5gui",
                "PySide2/examples/dummy.py": b"ex",
                "PySide2/translations/qtbase_ar.qm": b"tr",
                "PySide2/include/QtGui/qguiapplication.h": b"h",
                "PySide2/designer.exe": b"tool",
                "PySide2-5.15.2.1.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        # 不传 submodule_usage → keep_subs 为空集，应用 Qt 剥离规则
        count = slim_unpack([whl], dest)
        assert count == 1
        # 运行时文件保留
        assert (dest / "PySide2" / "__init__.py").is_file()
        assert (dest / "PySide2" / "QtGui.pyd").is_file()
        assert (dest / "PySide2" / "Qt5Gui.dll").is_file()
        # Qt 剥离目录/工具始终剥离（无 usage 时也应用）
        assert not (dest / "PySide2" / "examples").exists()
        assert not (dest / "PySide2" / "translations").exists()
        assert not (dest / "PySide2" / "include").exists()
        assert not (dest / "PySide2" / "designer.exe").exists()
        # 元数据保留
        assert (dest / "PySide2-5.15.2.1.dist-info" / "METADATA").is_file()

    def test_qt_multimedia_dynamic_expansion(self, tmp_path: Path) -> None:
        """import PySide2.QtMultimedia 时联动保留 mediaservice plugins 与 Qt5Multimedia.dll."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide2/__init__.py": b"",
                "PySide2/QtCore.pyd": b"core",
                "PySide2/QtMultimedia.pyd": b"mm",
                "PySide2/Qt5Core.dll": b"c",
                "PySide2/Qt5Multimedia.dll": b"mm-dll",
                "PySide2/plugins/platforms/qwindows.dll": b"plat",
                "PySide2/plugins/mediaservice/wmfengine.dll": b"media",
                "PySide2/plugins/audio/audio.dll": b"audio",
                "PySide2/plugins/sqldrivers/qsqlite.dll": b"sql",
                "PySide2/examples/charts/linechart.py": b"ex",
                "PySide2/resources/icudtl.dat": b"res",
                "PySide2/qml/QtQuick.2/qmldir": b"qml",
                "PySide2-5.15.2.1.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        # 源码 import PySide2.QtCore 与 PySide2.QtMultimedia
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore", "QtMultimedia"})})
        assert count == 1
        # 基础子模块保留
        assert (dest / "PySide2" / "QtCore.pyd").is_file()
        assert (dest / "PySide2" / "Qt5Core.dll").is_file()
        # Multimedia 子模块保留
        assert (dest / "PySide2" / "QtMultimedia.pyd").is_file()
        assert (dest / "PySide2" / "Qt5Multimedia.dll").is_file()
        # platforms 基础插件始终保留
        assert (dest / "PySide2" / "plugins" / "platforms" / "qwindows.dll").is_file()
        # mediaservice/audio 依赖 Multimedia，保留集合含 Multimedia → 保留
        assert (dest / "PySide2" / "plugins" / "mediaservice" / "wmfengine.dll").is_file()
        assert (dest / "PySide2" / "plugins" / "audio" / "audio.dll").is_file()
        # sqldrivers 依赖 Sql，保留集合无 Sql → 剥离
        assert not (dest / "PySide2" / "plugins" / "sqldrivers" / "qsqlite.dll").exists()
        # examples 始终剥离
        assert not (dest / "PySide2" / "examples" / "charts" / "linechart.py").exists()
        # resources 依赖 WebEngine，保留集合无 WebEngine → 剥离
        assert not (dest / "PySide2" / "resources" / "icudtl.dat").exists()
        # qml 依赖 Qml/Quick，保留集合无 Qml/Quick → 剥离
        assert not (dest / "PySide2" / "qml" / "QtQuick.2" / "qmldir").exists()
        # 元数据保留
        assert (dest / "PySide2-5.15.2.1.dist-info" / "METADATA").is_file()

    def test_qt_webengine_dynamic_expansion(self, tmp_path: Path) -> None:
        """import PySide2.QtWebEngineWidgets 时联动保留 resources 与 qtwebengine plugins."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide2/__init__.py": b"",
                "PySide2/QtCore.pyd": b"core",
                "PySide2/QtWebEngineWidgets.pyd": b"we",
                "PySide2/Qt5Core.dll": b"c",
                "PySide2/Qt5WebEngineCore.dll": b"we-core",
                "PySide2/plugins/platforms/qwindows.dll": b"plat",
                "PySide2/plugins/qtwebengine/qwebengine.dll": b"qtwe-plugin",
                "PySide2/resources/icudtl.dat": b"res",
                "PySide2/resources/qtwebengine_resources.pak": b"pak",
                "PySide2/qml/QtQuick.2/qmldir": b"qml",
                "PySide2/translations/qtbase_ar.qm": b"tr",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore", "WebEngineWidgets"})})
        assert count == 1
        # WebEngine 子模块保留
        assert (dest / "PySide2" / "QtWebEngineWidgets.pyd").is_file()
        assert (dest / "PySide2" / "Qt5WebEngineCore.dll").is_file()
        # qtwebengine plugins 依赖 WebEngineWidgets → 保留
        assert (dest / "PySide2" / "plugins" / "qtwebengine" / "qwebengine.dll").is_file()
        # resources 依赖 WebEngineCore/WebEngineWidgets → 保留
        assert (dest / "PySide2" / "resources" / "icudtl.dat").is_file()
        assert (dest / "PySide2" / "resources" / "qtwebengine_resources.pak").is_file()
        # qml 依赖 Qml/Quick，保留集合无 → 剥离
        assert not (dest / "PySide2" / "qml" / "QtQuick.2" / "qmldir").exists()
        # translations 始终剥离
        assert not (dest / "PySide2" / "translations" / "qtbase_ar.qm").exists()

    def test_qt_qml_dynamic_expansion(self, tmp_path: Path) -> None:
        """import PySide2.QtQml 与 PySide2.QtQuick 时保留 qml 目录与 scenegraph plugins."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide2/__init__.py": b"",
                "PySide2/QtCore.pyd": b"core",
                "PySide2/QtQml.pyd": b"qml",
                "PySide2/QtQuick.pyd": b"quick",
                "PySide2/Qt5Core.dll": b"c",
                "PySide2/plugins/platforms/qwindows.dll": b"plat",
                "PySide2/plugins/scenegraph/opengl.dll": b"sg",
                "PySide2/plugins/mediaservice/wmfengine.dll": b"media",
                "PySide2/qml/QtQuick.2/qmldir": b"qml",
                "PySide2/qml/QtQml/Models.2/qmldir": b"qml2",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore", "Qml", "Quick"})})
        assert count == 1
        # Qml/Quick 子模块保留
        assert (dest / "PySide2" / "QtQml.pyd").is_file()
        assert (dest / "PySide2" / "QtQuick.pyd").is_file()
        # scenegraph plugins 依赖 Quick → 保留
        assert (dest / "PySide2" / "plugins" / "scenegraph" / "opengl.dll").is_file()
        # mediaservice 依赖 Multimedia，保留集合无 → 剥离
        assert not (dest / "PySide2" / "plugins" / "mediaservice" / "wmfengine.dll").exists()
        # qml 目录依赖 Qml/Quick → 保留
        assert (dest / "PySide2" / "qml" / "QtQuick.2" / "qmldir").is_file()
        assert (dest / "PySide2" / "qml" / "QtQml" / "Models.2" / "qmldir").is_file()


class TestDefaultSlimSpec:
    """默认精简规则（非 Qt 库兜底）。."""

    def test_match_always_true(self) -> None:
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.match("numpy") is True
        assert DefaultSlimSpec.match("requests") is True
        assert DefaultSlimSpec.match("unknown-pkg") is True

    def test_normalize_submodule_noop(self) -> None:
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.normalize_submodule("core") == "core"
        assert DefaultSlimSpec.normalize_submodule("QtCore") == "QtCore"

    def test_expand_closure_noop(self) -> None:
        from fspack.slim.default import DefaultSlimSpec

        result = DefaultSlimSpec.expand_closure({"core", "fft"})
        assert result == {"core", "fft"}
        # 不就地修改输入
        src = {"a"}
        DefaultSlimSpec.expand_closure(src)
        assert src == {"a"}

    def test_classify_dist_info(self) -> None:
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.classify_entry("numpy-1.0.dist-info/METADATA", "numpy", set()) == (
            "metadata",
            None,
        )

    def test_classify_cross_pkg_shared(self) -> None:
        """跨包文件归 shared（如 shiboken2 在 PySide2 wheel 中）。."""
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.classify_entry("shiboken2/something.py", "numpy", set()) == (
            "shared",
            None,
        )

    def test_classify_init_and_private(self) -> None:
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.classify_entry("mypkg/__init__.py", "mypkg", set()) == ("shared", None)
        assert DefaultSlimSpec.classify_entry("mypkg/_config.py", "mypkg", set()) == ("shared", None)

    def test_classify_other_top_level_shared(self) -> None:
        """非 .pyd/.pyi/.so 的顶层文件归 shared（.dll/.py/.py.typed 等）。."""
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.classify_entry("mypkg/py.typed", "mypkg", set()) == ("shared", None)
        assert DefaultSlimSpec.classify_entry("mypkg/foo.dll", "mypkg", set()) == ("shared", None)
        assert DefaultSlimSpec.classify_entry("mypkg/utils.py", "mypkg", set()) == ("shared", None)

    def test_classify_top_pyd_as_submodule(self) -> None:
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.classify_entry("mypkg/core.pyd", "mypkg", set()) == ("submodule", "core")
        assert DefaultSlimSpec.classify_entry("mypkg/fft.so", "mypkg", set()) == ("submodule", "fft")

    def test_classify_subdir_shared(self) -> None:
        """非剥离子目录归 shared（不细分子模块）。."""
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.classify_entry("numpy/core/multiarray.pyd", "numpy", set()) == (
            "shared",
            None,
        )
        assert DefaultSlimSpec.classify_entry("mypkg/sub/x.py", "mypkg", set()) == ("shared", None)

    def test_classify_excluded_subdirs(self) -> None:
        """示例/文档/测试等非必要子目录归 exclude 始终剥离。."""
        from fspack.slim.default import DefaultSlimSpec

        for subdir in ("examples", "docs", "doc", "tests", "test", "testing"):
            assert DefaultSlimSpec.classify_entry(f"mypkg/{subdir}/dummy.py", "mypkg", set()) == ("exclude", None), (
                f"{subdir} 应当剥离"
            )
            # 嵌套子目录同样剥离
            assert DefaultSlimSpec.classify_entry(f"mypkg/{subdir}/nested/deep.py", "mypkg", set()) == (
                "exclude",
                None,
            ), f"{subdir}/nested 应当剥离"

    def test_classify_runtime_subdir_kept(self) -> None:
        """运行时子目录（如 numpy/core、pandas/io）归 shared 始终保留。."""
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.classify_entry("numpy/core/_internal.py", "numpy", set()) == (
            "shared",
            None,
        )
        assert DefaultSlimSpec.classify_entry("pandas/io/formats.py", "pandas", set()) == (
            "shared",
            None,
        )


class TestNumpySlimSpec:
    """numpy 精简规则：通用剥离 + 库专属剥离（distutils/_pyinstaller）。."""

    def test_match_numpy_only(self) -> None:
        from fspack.slim.libs import NumpySlimSpec

        assert NumpySlimSpec.match("numpy") is True
        assert NumpySlimSpec.match("numpy-1") is False  # 归一化后应为 numpy1
        assert NumpySlimSpec.match("scipy") is False
        assert NumpySlimSpec.match("pandas") is False

    def test_normalize_submodule_noop(self) -> None:
        from fspack.slim.libs import NumpySlimSpec

        assert NumpySlimSpec.normalize_submodule("core") == "core"
        assert NumpySlimSpec.normalize_submodule("fft") == "fft"

    def test_expand_closure_noop(self) -> None:
        from fspack.slim.libs import NumpySlimSpec

        result = NumpySlimSpec.expand_closure({"core", "fft"})
        assert result == {"core", "fft"}
        src = {"a"}
        NumpySlimSpec.expand_closure(src)
        assert src == {"a"}

    def test_classify_dist_info(self) -> None:
        from fspack.slim.libs import NumpySlimSpec

        assert NumpySlimSpec.classify_entry("numpy-1.24.4.dist-info/METADATA", "numpy", set()) == (
            "metadata",
            None,
        )

    def test_classify_runtime_subdir_kept(self) -> None:
        """numpy 运行时子目录（core/lib/random/fft/linalg/typing 等）归 shared 保留."""
        from fspack.slim.libs import NumpySlimSpec

        for subdir in ("core", "lib", "random", "fft", "linalg", "typing", "ma", "polynomial"):
            assert NumpySlimSpec.classify_entry(f"numpy/{subdir}/_internal.py", "numpy", set()) == ("shared", None), (
                f"{subdir} 应当保留"
            )

    def test_classify_common_excluded(self) -> None:
        """numpy 的通用剥离目录（examples/docs/tests 等）归 exclude。."""
        from fspack.slim.libs import NumpySlimSpec

        for subdir in ("examples", "docs", "doc", "tests", "test"):
            assert NumpySlimSpec.classify_entry(f"numpy/{subdir}/dummy.py", "numpy", set()) == ("exclude", None), (
                f"{subdir} 应当剥离"
            )

    def test_classify_numpy_extra_excluded(self) -> None:
        """numpy 专属剥离目录：distutils/_pyinstaller 归 exclude。."""
        from fspack.slim.libs import NumpySlimSpec

        for subdir in ("distutils", "_pyinstaller"):
            assert NumpySlimSpec.classify_entry(f"numpy/{subdir}/dummy.py", "numpy", set()) == ("exclude", None), (
                f"{subdir} 应当剥离"
            )
            # 嵌套子目录同样剥离
            assert NumpySlimSpec.classify_entry(f"numpy/{subdir}/nested/deep.py", "numpy", set()) == (
                "exclude",
                None,
            ), f"{subdir}/nested 应当剥离"

    def test_classify_numpy_f2py_kept(self) -> None:
        """numpy f2py 不剥离（scipy 运行时通过 from numpy import * 触发导入）."""
        from fspack.slim.libs import NumpySlimSpec

        # f2py 目录文件归 shared（始终保留），不归 exclude
        assert NumpySlimSpec.classify_entry("numpy/f2py/__init__.py", "numpy", set()) == ("shared", None)
        assert NumpySlimSpec.classify_entry("numpy/f2py/auxfuncs.py", "numpy", set()) == ("shared", None)
        assert NumpySlimSpec.classify_entry("numpy/f2py/f90mod_rules.py", "numpy", set()) == ("shared", None)

    def test_classify_numpy_testing_kept(self) -> None:
        """numpy testing 不剥离（numpy.testing 是运行时公共 API，非测试代码）."""
        from fspack.slim.libs import NumpySlimSpec

        # numpy/testing/ 是 numpy.testing 公共 API 模块，归 shared 保留
        assert NumpySlimSpec.classify_entry("numpy/testing/__init__.py", "numpy", set()) == ("shared", None)
        assert NumpySlimSpec.classify_entry("numpy/testing/assertions.py", "numpy", set()) == ("shared", None)
        # numpy/tests/（复数）是真正的测试代码，仍剥离
        assert NumpySlimSpec.classify_entry("numpy/tests/test_core.py", "numpy", set()) == ("exclude", None)

    def test_classify_top_pyd_as_submodule(self) -> None:
        """numpy 顶层 .pyd 分类：``_`` 前缀私有 C 扩展归 shared 始终保留。

        numpy 的 C 扩展几乎都以 ``_`` 开头（``_multiarray_umath``、``_fft_helper``
        等），属运行时核心，按 :meth:`_default_classify` 规则归 shared 始终保留。
        """
        from fspack.slim.libs import NumpySlimSpec

        # _ 前缀私有 C 扩展归 shared（始终保留）
        assert NumpySlimSpec.classify_entry("numpy/_multiarray_umath.cp38-win_amd64.pyd", "numpy", set()) == (
            "shared",
            None,
        )
        # 非 _ 前缀的 .pyd 按原文件名归类为 submodule（选择性保留）
        assert NumpySlimSpec.classify_entry("numpy/multiarray.pyd", "numpy", set()) == ("submodule", "multiarray")

    def test_classify_init_and_private(self) -> None:
        """numpy 顶层 __init__/私有文件归 shared."""
        from fspack.slim.libs import NumpySlimSpec

        assert NumpySlimSpec.classify_entry("numpy/__init__.py", "numpy", set()) == ("shared", None)
        assert NumpySlimSpec.classify_entry("numpy/_config.py", "numpy", set()) == ("shared", None)
        assert NumpySlimSpec.classify_entry("numpy/version.py", "numpy", set()) == ("shared", None)


class TestLxmlSlimSpec:
    """lxml 精简规则：剥离 includes C 头文件目录。."""

    def test_match_lxml_only(self) -> None:
        from fspack.slim.libs import LxmlSlimSpec

        assert LxmlSlimSpec.match("lxml") is True
        assert LxmlSlimSpec.match("lxml-html") is False  # 归一化后不同
        assert LxmlSlimSpec.match("bs4") is False
        assert LxmlSlimSpec.match("numpy") is False

    def test_normalize_submodule_noop(self) -> None:
        from fspack.slim.libs import LxmlSlimSpec

        assert LxmlSlimSpec.normalize_submodule("etree") == "etree"
        assert LxmlSlimSpec.normalize_submodule("html") == "html"

    def test_expand_closure_noop(self) -> None:
        from fspack.slim.libs import LxmlSlimSpec

        result = LxmlSlimSpec.expand_closure({"etree", "html"})
        assert result == {"etree", "html"}

    def test_classify_includes_excluded(self) -> None:
        """lxml/includes C 头文件目录归 exclude 始终剥离."""
        from fspack.slim.libs import LxmlSlimSpec

        # libxml/libxslt/c14n/etc 子目录均剥离
        for sub in ("libxml", "libxslt", "libexslt", "c14n", "relaxng"):
            assert LxmlSlimSpec.classify_entry(f"lxml/includes/{sub}/xmlreader.h", "lxml", set()) == (
                "exclude",
                None,
            ), f"includes/{sub} 应当剥离"

    def test_classify_runtime_subdir_kept(self) -> None:
        """lxml 运行时子目录（html/isoschematron 等）归 shared 保留."""
        from fspack.slim.libs import LxmlSlimSpec

        assert LxmlSlimSpec.classify_entry("lxml/html/clean.py", "lxml", set()) == ("shared", None)
        assert LxmlSlimSpec.classify_entry("lxml/isoschematron/rng.xsl", "lxml", set()) == (
            "shared",
            None,
        )

    def test_classify_top_pyd_as_submodule(self) -> None:
        """lxml 顶层 .pyd 按原文件名归类（如 etree.cpython-38.pyd）."""
        from fspack.slim.libs import LxmlSlimSpec

        result = LxmlSlimSpec.classify_entry("lxml/etree.cpython-38-x86_64-linux-gnu.so", "lxml", set())
        assert result == ("submodule", "etree.cpython-38-x86_64-linux-gnu")

    def test_classify_common_excluded(self) -> None:
        """lxml 通用剥离目录（tests/examples 等）归 exclude."""
        from fspack.slim.libs import LxmlSlimSpec

        for subdir in ("examples", "docs", "tests"):
            assert LxmlSlimSpec.classify_entry(f"lxml/{subdir}/dummy.py", "lxml", set()) == ("exclude", None), (
                f"{subdir} 应当剥离"
            )


class TestMatplotlibSlimSpec:
    """matplotlib 精简规则：剥离 sphinxext 与跨包/嵌套 tests 目录。."""

    def test_match_matplotlib_only(self) -> None:
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.match("matplotlib") is True
        assert MatplotlibSlimSpec.match("matplotlib-inline") is False  # 归一化后不同
        assert MatplotlibSlimSpec.match("numpy") is False
        assert MatplotlibSlimSpec.match("scipy") is False

    def test_normalize_submodule_noop(self) -> None:
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.normalize_submodule("pyplot") == "pyplot"
        assert MatplotlibSlimSpec.normalize_submodule("backends") == "backends"

    def test_expand_closure_noop(self) -> None:
        from fspack.slim.libs import MatplotlibSlimSpec

        result = MatplotlibSlimSpec.expand_closure({"pyplot", "backends"})
        assert result == {"pyplot", "backends"}
        src = {"a"}
        MatplotlibSlimSpec.expand_closure(src)
        assert src == {"a"}

    def test_classify_dist_info(self) -> None:
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("matplotlib-3.7.0.dist-info/METADATA", "matplotlib", set()) == (
            "metadata",
            None,
        )

    def test_classify_runtime_subdir_kept(self) -> None:
        """matplotlib 运行时子目录（mpl-data/backends/tri/style 等）归 shared 保留."""
        from fspack.slim.libs import MatplotlibSlimSpec

        for subdir in ("mpl-data", "backends", "tri", "style", "axes", "colors", "image"):
            assert MatplotlibSlimSpec.classify_entry(f"matplotlib/{subdir}/_internal.py", "matplotlib", set()) == (
                "shared",
                None,
            ), f"{subdir} 应当保留"

    def test_classify_sphinxext_excluded(self) -> None:
        """matplotlib/sphinxext/ 文档构建扩展归 exclude."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("matplotlib/sphinxext/plot_directive.py", "matplotlib", set()) == (
            "exclude",
            None,
        )
        # 嵌套子目录同样剥离
        assert MatplotlibSlimSpec.classify_entry("matplotlib/sphinxext/nested/deep.py", "matplotlib", set()) == (
            "exclude",
            None,
        )

    def test_classify_matplotlib_tests_excluded(self) -> None:
        """matplotlib/tests/ 归 exclude（与 COMMON_EXCLUDE_SUBDIRS 冗余但无害）."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("matplotlib/tests/test_figure.py", "matplotlib", set()) == (
            "exclude",
            None,
        )

    def test_classify_mpl_toolkits_tests_excluded(self) -> None:
        """mpl_toolkits/tests/ 跨包嵌套 tests 归 exclude（nested_excludes 核心场景）."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("mpl_toolkits/tests/test_mplot3d.py", "matplotlib", set()) == (
            "exclude",
            None,
        )
        # 三级嵌套同样剥离
        assert MatplotlibSlimSpec.classify_entry("mpl_toolkits/mplot3d/tests/test_axes3d.py", "matplotlib", set()) == (
            "exclude",
            None,
        )

    def test_classify_mpl_toolkits_runtime_kept(self) -> None:
        """mpl_toolkits/ 非 tests 部分归 shared 保留（跨包运行时模块）."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("mpl_toolkits/mplot3d/axes3d.py", "matplotlib", set()) == (
            "shared",
            None,
        )
        assert MatplotlibSlimSpec.classify_entry("mpl_toolkits/__init__.py", "matplotlib", set()) == ("shared", None)

    def test_classify_matplotlib_libs_kept(self) -> None:
        """matplotlib.libs/ 跨包共享 DLL 归 shared 保留."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("matplotlib.libs/libopenblas.dll", "matplotlib", set()) == (
            "shared",
            None,
        )

    def test_classify_pylab_kept(self) -> None:
        """pylab.py 跨包顶层模块归 shared 保留."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("pylab.py", "matplotlib", set()) == ("shared", None)

    def test_classify_common_excluded(self) -> None:
        """matplotlib 通用剥离目录（examples/docs 等）归 exclude."""
        from fspack.slim.libs import MatplotlibSlimSpec

        for subdir in ("examples", "docs", "doc"):
            assert MatplotlibSlimSpec.classify_entry(f"matplotlib/{subdir}/dummy.py", "matplotlib", set()) == (
                "exclude",
                None,
            ), f"{subdir} 应当剥离"

    def test_classify_init_and_private(self) -> None:
        """matplotlib 顶层 __init__/私有文件归 shared."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("matplotlib/__init__.py", "matplotlib", set()) == (
            "shared",
            None,
        )
        assert MatplotlibSlimSpec.classify_entry("matplotlib/_cmocean.py", "matplotlib", set()) == (
            "shared",
            None,
        )

    def test_classify_top_pyd_always_shared(self) -> None:
        """matplotlib 顶层 .pyd（ft2font 等）归 shared 始终保留，不按子模块剥离.

        ft2font.pyd 是 __init__._check_versions() 硬依赖（from . import ft2font），
        剥离即 ImportError。top_ext_always_shared=True 确保始终保留。
        """
        from fspack.slim.libs import MatplotlibSlimSpec

        # keep_subs 为空（用户只 import matplotlib.pyplot）时 ft2font 仍归 shared
        assert MatplotlibSlimSpec.classify_entry(
            "matplotlib/ft2font.cp311-win_amd64.pyd", "matplotlib", {"pyplot"}
        ) == ("shared", None)
        # keep_subs 含 ft2font 时也归 shared（不归 submodule）
        assert MatplotlibSlimSpec.classify_entry(
            "matplotlib/ft2font.cp311-win_amd64.pyd", "matplotlib", {"ft2font"}
        ) == ("shared", None)
        # 其他顶层 .pyd 同理
        for pyd in ("_image.cp311-win_amd64.pyd", "_path.cp311-win_amd64.pyd", "_tri.cp311-win_amd64.pyd"):
            assert MatplotlibSlimSpec.classify_entry(f"matplotlib/{pyd}", "matplotlib", set()) == ("shared", None), (
                f"{pyd} 应当归 shared"
            )

    def test_classify_top_pyi_always_shared(self) -> None:
        """matplotlib 顶层 .pyi 归 shared（top_ext_always_shared 覆盖所有 SUBMODULE_EXTS）."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("matplotlib/pyplot.pyi", "matplotlib", set()) == (
            "shared",
            None,
        )


class TestScipySlimSpec:
    """scipy 精简规则：剥离各子模块下的嵌套 tests 目录。."""

    def test_match_scipy_only(self) -> None:
        from fspack.slim.libs import ScipySlimSpec

        assert ScipySlimSpec.match("scipy") is True
        assert ScipySlimSpec.match("scipy-1") is False  # 归一化后不同
        assert ScipySlimSpec.match("numpy") is False
        assert ScipySlimSpec.match("matplotlib") is False

    def test_normalize_submodule_noop(self) -> None:
        from fspack.slim.libs import ScipySlimSpec

        assert ScipySlimSpec.normalize_submodule("linalg") == "linalg"
        assert ScipySlimSpec.normalize_submodule("fft") == "fft"

    def test_expand_closure_noop(self) -> None:
        from fspack.slim.libs import ScipySlimSpec

        result = ScipySlimSpec.expand_closure({"linalg", "fft"})
        assert result == {"linalg", "fft"}
        src = {"a"}
        ScipySlimSpec.expand_closure(src)
        assert src == {"a"}

    def test_classify_dist_info(self) -> None:
        from fspack.slim.libs import ScipySlimSpec

        assert ScipySlimSpec.classify_entry("scipy-1.10.0.dist-info/METADATA", "scipy", set()) == (
            "metadata",
            None,
        )

    def test_classify_nested_tests_excluded(self) -> None:
        """scipy/<sub>/tests/ 嵌套 tests 归 exclude（nested_excludes 核心场景）."""
        from fspack.slim.libs import ScipySlimSpec

        for sub in ("linalg", "fft", "optimize", "stats", "integrate", "interpolate", "ndimage", "signal"):
            assert ScipySlimSpec.classify_entry(f"scipy/{sub}/tests/test_basic.py", "scipy", set()) == (
                "exclude",
                None,
            ), f"{sub}/tests 应当剥离"
        # 更深层嵌套同样剥离
        assert ScipySlimSpec.classify_entry("scipy/fft/_pocketfft/tests/test_pocketfft.py", "scipy", set()) == (
            "exclude",
            None,
        )

    def test_classify_runtime_subdir_kept(self) -> None:
        """scipy 运行时子目录（_lib/linalg/fft/optimize 等非 tests）归 shared 保留."""
        from fspack.slim.libs import ScipySlimSpec

        for subdir in ("_lib", "linalg", "fft", "optimize", "stats", "constants", "io"):
            assert ScipySlimSpec.classify_entry(f"scipy/{subdir}/_internal.py", "scipy", set()) == ("shared", None), (
                f"{subdir} 应当保留"
            )

    def test_classify_scipy_libs_kept(self) -> None:
        """scipy.libs/ 跨包共享 DLL 归 shared 保留."""
        from fspack.slim.libs import ScipySlimSpec

        assert ScipySlimSpec.classify_entry("scipy.libs/libopenblas.dll", "scipy", set()) == (
            "shared",
            None,
        )

    def test_classify_top_tests_excluded(self) -> None:
        """scipy/tests/ 顶层 tests 归 exclude（与 COMMON_EXCLUDE_SUBDIRS 冗余但无害）."""
        from fspack.slim.libs import ScipySlimSpec

        assert ScipySlimSpec.classify_entry("scipy/tests/test_dummy.py", "scipy", set()) == (
            "exclude",
            None,
        )

    def test_classify_common_excluded(self) -> None:
        """scipy 通用剥离目录（examples/docs 等）归 exclude."""
        from fspack.slim.libs import ScipySlimSpec

        for subdir in ("examples", "docs", "doc"):
            assert ScipySlimSpec.classify_entry(f"scipy/{subdir}/dummy.py", "scipy", set()) == ("exclude", None), (
                f"{subdir} 应当剥离"
            )

    def test_classify_init_and_private(self) -> None:
        """scipy 顶层 __init__/私有文件归 shared."""
        from fspack.slim.libs import ScipySlimSpec

        assert ScipySlimSpec.classify_entry("scipy/__init__.py", "scipy", set()) == ("shared", None)
        assert ScipySlimSpec.classify_entry("scipy/_lib/_util.py", "scipy", set()) == ("shared", None)


class TestNestedExcludesBehavior:
    """_default_classify 的 nested_excludes 参数行为。."""

    def test_nested_excludes_cross_pkg(self) -> None:
        """nested_excludes 在跨包路径上生效（mpl_toolkits/tests/）."""
        from fspack.slim.base import SlimSpec

        # nested_excludes={"tests"} 应剥离跨包 mpl_toolkits/tests/
        assert SlimSpec._default_classify(
            "mpl_toolkits/tests/x.py", "matplotlib", set(), frozenset(), frozenset({"tests"})
        ) == ("exclude", None)

    def test_nested_excludes_deep_level(self) -> None:
        """nested_excludes 在任意深层级生效（scipy/fft/_pocketfft/tests/）."""
        from fspack.slim.base import SlimSpec

        assert SlimSpec._default_classify(
            "scipy/fft/_pocketfft/tests/x.py", "scipy", set(), frozenset(), frozenset({"tests"})
        ) == ("exclude", None)

    def test_nested_excludes_empty_no_strip(self) -> None:
        """nested_excludes 为空时不剥离嵌套 tests（向后兼容）."""
        from fspack.slim.base import SlimSpec

        # 默认行为：scipy/linalg/tests/ 归 shared（parts[1]=linalg 非 tests）
        assert SlimSpec._default_classify("scipy/linalg/tests/x.py", "scipy", set(), frozenset(), frozenset()) == (
            "shared",
            None,
        )

    def test_nested_excludes_not_affect_top_pkg_name(self) -> None:
        """nested_excludes 不检查 parts[0]（顶层包名），避免误伤同名包."""
        from fspack.slim.base import SlimSpec

        # 假设有包名为 tests（极端情况），不应被 nested_excludes 误剥离
        assert SlimSpec._default_classify("tests/__init__.py", "tests", set(), frozenset(), frozenset({"tests"})) == (
            "shared",
            None,
        )


class TestTopExtAlwaysSharedBehavior:
    """_default_classify 的 top_ext_always_shared 参数行为。."""

    def test_default_top_pyd_as_submodule(self) -> None:
        """默认（top_ext_always_shared=False）顶层 .pyd 归 submodule（向后兼容）."""
        from fspack.slim.base import SlimSpec

        assert SlimSpec._default_classify("numpy/ft2font.cp311-win_amd64.pyd", "numpy", set()) == (
            "submodule",
            "ft2font.cp311-win_amd64",  # Path.stem 只去最后后缀
        )

    def test_top_ext_always_shared_pyd_to_shared(self) -> None:
        """top_ext_always_shared=True 时顶层 .pyd 归 shared（不归 submodule）."""
        from fspack.slim.base import SlimSpec

        assert SlimSpec._default_classify(
            "matplotlib/ft2font.cp311-win_amd64.pyd",
            "matplotlib",
            set(),
            frozenset(),
            frozenset(),
            True,
        ) == ("shared", None)

    def test_top_ext_always_shared_not_affect_private(self) -> None:
        """top_ext_always_shared 不影响 _ 前缀文件（本就归 shared）."""
        from fspack.slim.base import SlimSpec

        # _ 前缀无论 top_ext_always_shared 与否都归 shared
        assert SlimSpec._default_classify(
            "matplotlib/_image.pyd", "matplotlib", set(), frozenset(), frozenset(), True
        ) == ("shared", None)
        assert SlimSpec._default_classify(
            "matplotlib/_image.pyd", "matplotlib", set(), frozenset(), frozenset(), False
        ) == ("shared", None)

    def test_top_ext_always_shared_not_affect_subdir_pyd(self) -> None:
        """top_ext_always_shared 仅影响顶层 .pyd，子目录 .pyd 仍归 shared（子目录本就 shared）."""
        from fspack.slim.base import SlimSpec

        # 子目录下的 .pyd 归 shared（子目录逻辑，不受 top_ext_always_shared 影响）
        assert SlimSpec._default_classify(
            "matplotlib/backends/_backend_agg.pyd", "matplotlib", set(), frozenset(), frozenset(), True
        ) == ("shared", None)


class TestSlimSpecRegistry:
    """spec 注册表分发。."""

    def test_get_spec_qt(self) -> None:
        from fspack.slim import get_spec
        from fspack.slim.qt import QtSlimSpec

        for pkg in ("pyside2", "pyside6", "pyqt5", "pyqt6"):
            assert get_spec(pkg) is QtSlimSpec

    def test_get_spec_default_fallback(self) -> None:
        from fspack.slim import get_spec
        from fspack.slim.default import DefaultSlimSpec

        # numpy/lxml 有专门 spec，不走默认
        assert get_spec("requests") is DefaultSlimSpec
        assert get_spec("unknown") is DefaultSlimSpec

    def test_get_spec_numpy_lxml(self) -> None:
        """numpy/lxml 走专门的 spec（非默认兜底）."""
        from fspack.slim import get_spec
        from fspack.slim.libs import LxmlSlimSpec, NumpySlimSpec

        assert get_spec("numpy") is NumpySlimSpec
        assert get_spec("lxml") is LxmlSlimSpec

    def test_get_spec_sci_libs(self) -> None:
        """matplotlib/scipy 走专门的 spec（非默认兜底）."""
        from fspack.slim import get_spec
        from fspack.slim.libs import MatplotlibSlimSpec, ScipySlimSpec

        assert get_spec("matplotlib") is MatplotlibSlimSpec
        assert get_spec("scipy") is ScipySlimSpec

    def test_classify_entry_dispatches_to_matplotlib(self) -> None:
        """classify_entry 按 top_pkg 归一化名分发到 MatplotlibSlimSpec。."""
        # mpl_toolkits/tests/ 跨包嵌套剥离验证分发到 matplotlib spec
        assert classify_entry("mpl_toolkits/tests/x.py", "matplotlib") == ("exclude", None)

    def test_classify_entry_dispatches_to_scipy(self) -> None:
        """classify_entry 按 top_pkg 归一化名分发到 ScipySlimSpec。."""
        # scipy/linalg/tests/ 嵌套剥离验证分发到 scipy spec
        assert classify_entry("scipy/linalg/tests/x.py", "scipy") == ("exclude", None)

    def test_classify_entry_dispatches_to_qt(self) -> None:
        """classify_entry 按 top_pkg 归一化名分发到 QtSlimSpec。."""
        # PySide2/PySide6/PyQt5/PyQt6 都走 Qt 规则（.pyd 归一化）
        assert classify_entry("PySide2/QtCore.pyd", "PySide2") == ("submodule", "Core")
        assert classify_entry("PySide6/Qt6Gui.dll", "PySide6") == ("submodule", "Gui")
        assert classify_entry("PyQt5/QtWidgets.pyd", "PyQt5") == ("submodule", "Widgets")

    def test_classify_entry_dispatches_to_default(self) -> None:
        """非 Qt 库走默认规则（.pyd 不归一化）。."""
        assert classify_entry("mypkg/core.pyd", "mypkg") == ("submodule", "core")

    def test_register_spec_custom(self) -> None:
        """自定义 spec 注册后能被 get_spec 命中。."""
        from fspack.slim import get_spec
        from fspack.slim.base import SlimSpec

        class MySpec(SlimSpec):
            @classmethod
            @override
            def match(cls, whl_pkg: str) -> bool:
                return whl_pkg == "mylib"

            @classmethod
            @override
            def normalize_submodule(cls, sub: str) -> str:
                return sub

            @classmethod
            @override
            def expand_closure(cls, subs: set[str]) -> set[str]:
                return set(subs)

            @classmethod
            @override
            def classify_entry(
                cls,
                entry: str,
                top_pkg: str,
                keep_subs: set[str],
            ) -> tuple[str, str | None]:
                return ("shared", None)

        # 注册到 DefaultSlimSpec 之前，否则会被兜底规则提前命中
        from fspack.slim.base import _SPECS

        _SPECS.insert(0, MySpec)
        try:
            assert get_spec("mylib") is MySpec
            # 其他包仍走默认
            from fspack.slim.default import DefaultSlimSpec

            assert get_spec("requests") is DefaultSlimSpec
        finally:
            _SPECS.remove(MySpec)


class TestQtSubdirSharedFallback:
    """Qt 库非 plugins/resources/qml 子目录归 shared 兜底。."""

    def test_qt_other_subdir_shared(self) -> None:
        """Qt 库非白名单子目录（如 lib/）归 shared 始终保留。."""
        assert classify_entry("PySide2/lib/fonts/times.ttf", "PySide2") == ("shared", None)
        assert classify_entry("PySide2/PySide2/__init__.py", "PySide2") == ("shared", None)


class TestSlimUnpackStageCallback:
    """slim_unpack 的 stage 回调参数。."""

    def test_stage_set_detail_called(self, tmp_path: Path) -> None:
        from fspack.progress import BuildTracker

        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtCore.pyd": b"core"})
        dest = tmp_path / "sp"
        tracker = BuildTracker()
        with tracker.stage("解压 wheel") as stage:
            count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore"})}, stage=stage)
        assert count == 1
        # tracker.records[0].items 反映 wheel 解压数（iter_with_progress 调用 stage.processed()）
        assert tracker.records[0].items == 1
        # slim_unpack 末尾调用 stage.set_detail 设置备注
        assert tracker.records[0].detail == "1 wheels 解压"

    def test_stage_none_no_error(self, tmp_path: Path) -> None:
        """stage=None 时不报错。."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtCore.pyd": b"core"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore"})})
        assert count == 1


class TestWheelInfoFromFilename:
    """WheelInfo.from_filename：从 wheel 文件名构造元信息."""

    def test_standard_wheel(self) -> None:
        from fspack.slim.base import WheelInfo

        info = WheelInfo.from_filename("requests-2.31.0-py3-none-any.whl")
        assert info is not None
        assert info.name == "requests"
        assert info.version == "2.31.0"
        assert info.python_tags == ("py3",)
        assert info.abi_tag == "none"
        assert info.platform_tags == ("any",)

    def test_pyside2_nonstandard_build_tag(self) -> None:
        from fspack.slim.base import WheelInfo

        info = WheelInfo.from_filename("PySide2-5.15.2.1-5.15.2-cp35.cp36.cp37.cp38.cp39.cp310-none-win_amd64.whl")
        assert info is not None
        assert info.name == "PySide2"
        assert info.version == "5.15.2.1"
        assert info.python_tags == ("cp35", "cp36", "cp37", "cp38", "cp39", "cp310")
        assert info.platform_tags == ("win_amd64",)

    def test_multi_platform_wheel(self) -> None:
        from fspack.slim.base import WheelInfo

        info = WheelInfo.from_filename("numpy-1.24.0-cp39-cp39-manylinux2014_x86_64.manylinux_2_28_x86_64.whl")
        assert info is not None
        assert info.name == "numpy"
        assert info.python_tags == ("cp39",)
        assert info.platform_tags == ("manylinux2014_x86_64", "manylinux_2_28_x86_64")

    def test_invalid_filename_returns_none(self) -> None:
        from fspack.slim.base import WheelInfo

        assert WheelInfo.from_filename("not-a-wheel.txt") is None
        assert WheelInfo.from_filename("missing-tags-1.0.whl") is None


class TestNormalizeName:
    """PEP 503 名称归一化."""

    def test_basic(self) -> None:
        from fspack.slim.base import normalize_name

        assert normalize_name("PySide2") == "pyside2"
        assert normalize_name("Jinja2") == "jinja2"

    def test_separators(self) -> None:
        from fspack.slim.base import normalize_name

        assert normalize_name("my_pkg.name") == "my-pkg-name"
        assert normalize_name("multi__sep") == "multi-sep"


class TestStripExts:
    """STRIP_EXTS 扩展名剥离：编译时/调试/缓存文件运行时不需要。"""

    def test_is_strip_ext_method(self) -> None:
        """_is_strip_ext 识别 STRIP_EXTS 中的扩展名."""
        from fspack.slim.base import SlimSpec

        strip_exts = (
            ".h",
            ".hpp",
            ".hxx",
            ".hh",
            ".cpp",
            ".cc",
            ".cxx",
            ".c",
            ".lib",
            ".a",
            ".pdb",
            ".exp",
            ".ilk",
            ".pyc",
            ".pyo",
            ".exe",
        )
        for ext in strip_exts:
            assert SlimSpec._is_strip_ext(f"foo{ext}") is True, f"{ext} 应当剥离"
        # 不在 STRIP_EXTS 中的扩展名
        for ext in (".py", ".pyd", ".pyi", ".dll", ".so", ".json", ".txt"):
            assert SlimSpec._is_strip_ext(f"foo{ext}") is False, f"{ext} 不应剥离"

    def test_is_strip_ext_case_insensitive(self) -> None:
        """扩展名大小写不敏感."""
        from fspack.slim.base import SlimSpec

        assert SlimSpec._is_strip_ext("foo.H") is True
        assert SlimSpec._is_strip_ext("foo.CPP") is True
        assert SlimSpec._is_strip_ext("foo.Lib") is True
        assert SlimSpec._is_strip_ext("foo.PDB") is True

    def test_is_strip_ext_directory_not_affected(self) -> None:
        """目录条目（以 / 结尾）不被剥离."""
        from fspack.slim.base import SlimSpec

        assert SlimSpec._is_strip_ext("mypkg/sub.h/") is False
        assert SlimSpec._is_strip_ext("mypkg/") is False
        assert SlimSpec._is_strip_ext("PySide2/include/") is False

    def test_is_strip_ext_no_extension(self) -> None:
        """无扩展名文件不被剥离."""
        from fspack.slim.base import SlimSpec

        assert SlimSpec._is_strip_ext("README") is False
        assert SlimSpec._is_strip_ext("py.typed") is False
        assert SlimSpec._is_strip_ext("LICENSE") is False

    def test_default_spec_strip_h(self) -> None:
        """默认 spec 顶层 .h 剥离."""
        assert classify_entry("mypkg/foo.h", "mypkg") == ("exclude", None)

    def test_default_spec_strip_cpp_in_subdir(self) -> None:
        """默认 spec 子目录内 .cpp 剥离."""
        assert classify_entry("mypkg/core/foo.cpp", "mypkg") == ("exclude", None)

    def test_default_spec_strip_lib(self) -> None:
        """默认 spec .lib 剥离."""
        assert classify_entry("mypkg/foo.lib", "mypkg") == ("exclude", None)

    def test_default_spec_strip_a_unix_static_lib(self) -> None:
        """默认 spec .a（Unix 静态库）剥离."""
        assert classify_entry("mypkg/foo.a", "mypkg") == ("exclude", None)

    def test_default_spec_strip_pdb(self) -> None:
        """默认 spec .pdb 剥离."""
        assert classify_entry("mypkg/foo.pdb", "mypkg") == ("exclude", None)

    def test_default_spec_strip_pyc(self) -> None:
        """默认 spec .pyc 剥离."""
        assert classify_entry("mypkg/foo.pyc", "mypkg") == ("exclude", None)

    def test_default_spec_strip_pyc_in_subdir(self) -> None:
        """默认 spec 子目录内 .pyc 剥离."""
        assert classify_entry("mypkg/sub/foo.pyc", "mypkg") == ("exclude", None)

    def test_default_spec_strip_exe(self) -> None:
        """默认 spec 顶层 .exe 剥离（非 Qt 库也剥离辅助 exe）."""
        assert classify_entry("mypkg/tool.exe", "mypkg") == ("exclude", None)

    def test_default_spec_pyd_not_stripped(self) -> None:
        """.pyd 不受 STRIP_EXTS 影响（仍归 submodule）."""
        assert classify_entry("mypkg/core.pyd", "mypkg") == ("submodule", "core")

    def test_default_spec_dll_not_stripped(self) -> None:
        """非 Qt 库 .dll 不受 STRIP_EXTS 影响（仍归 shared）."""
        assert classify_entry("mypkg/foo.dll", "mypkg") == ("shared", None)

    def test_default_spec_py_not_stripped(self) -> None:
        """.py 不受 STRIP_EXTS 影响."""
        assert classify_entry("mypkg/foo.py", "mypkg") == ("shared", None)

    def test_default_spec_pyi_not_stripped(self) -> None:
        """.pyi 不受 STRIP_EXTS 影响（仍归 submodule）."""
        assert classify_entry("mypkg/core.pyi", "mypkg") == ("submodule", "core")

    def test_qt_spec_strip_h_in_subdir(self) -> None:
        """Qt spec 子目录内 .h 剥离（如 PySide2/plugins/foo.h）."""
        assert classify_entry("PySide2/plugins/foo.h", "PySide2") == ("exclude", None)

    def test_qt_spec_strip_lib(self) -> None:
        """Qt spec 顶层 .lib 剥离."""
        assert classify_entry("PySide2/foo.lib", "PySide2") == ("exclude", None)

    def test_qt_spec_exe_still_excluded(self) -> None:
        """Qt spec 顶层 .exe 仍剥离（通过 STRIP_EXTS，不再需要专用分支）."""
        assert classify_entry("PySide2/designer.exe", "PySide2") == ("exclude", None)

    def test_qt_spec_pyd_not_stripped(self) -> None:
        """Qt spec 顶层 .pyd 不受 STRIP_EXTS 影响（仍归 submodule）."""
        assert classify_entry("PySide2/QtCore.pyd", "PySide2") == ("submodule", "Core")

    def test_qt_spec_dll_not_stripped(self) -> None:
        """Qt spec Qt5*.dll 不受 STRIP_EXTS 影响（仍归 submodule）."""
        assert classify_entry("PySide2/Qt5Core.dll", "PySide2") == ("submodule", "Core")

    def test_qt_spec_non_qt_dll_not_stripped(self) -> None:
        """Qt spec 非 Qt5/6 前缀 DLL 不受 STRIP_EXTS 影响（VC++ 运行时归 shared）."""
        assert classify_entry("PySide2/concrt140.dll", "PySide2") == ("shared", None)

    def test_qt_spec_cross_pkg_h_stripped(self) -> None:
        """Qt spec 跨包 .h 文件剥离（如 shiboken2 中的 .h）."""
        assert classify_entry("shiboken2/foo.h", "PySide2") == ("exclude", None)

    def test_qt_spec_cross_pkg_exe_stripped(self) -> None:
        """Qt spec 跨包 .exe 文件剥离."""
        assert classify_entry("shiboken2/tool.exe", "PySide2") == ("exclude", None)

    def test_numpy_spec_strip_h_in_include(self) -> None:
        """numpy spec 子目录内 .h 剥离（如 numpy/core/include/foo.h）."""
        assert classify_entry("numpy/core/include/foo.h", "numpy") == ("exclude", None)

    def test_numpy_spec_pyd_not_stripped(self) -> None:
        """numpy spec _ 前缀 .pyd 不受 STRIP_EXTS 影响（仍归 shared）."""
        assert classify_entry("numpy/_multiarray_umath.cp38-win_amd64.pyd", "numpy") == ("shared", None)

    def test_lxml_spec_strip_h_outside_includes(self) -> None:
        """lxml spec includes 目录外的 .h 也剥离（STRIP_EXTS 兜底）."""
        assert classify_entry("lxml/foo.h", "lxml") == ("exclude", None)

    def test_matplotlib_spec_strip_pdb(self) -> None:
        """matplotlib spec .pdb 剥离."""
        assert classify_entry("matplotlib/foo.pdb", "matplotlib") == ("exclude", None)

    def test_scipy_spec_strip_cpp_in_subdir(self) -> None:
        """scipy spec 子目录内 .cpp 剥离."""
        assert classify_entry("scipy/_lib/foo.cpp", "scipy") == ("exclude", None)

    def test_dist_info_not_stripped(self) -> None:
        """.dist-info 内运行时无用文件剥离，必要文件保留."""
        # RECORD/WHEEL/entry_points.txt/top_level.txt 等运行时无用 → exclude
        assert classify_entry("PySide2-5.15.2.1.dist-info/RECORD", "PySide2") == ("exclude", None)
        assert classify_entry("PySide2-5.15.2.1.dist-info/WHEEL", "PySide2") == ("exclude", None)
        assert classify_entry("PySide2-5.15.2.1.dist-info/entry_points.txt", "PySide2") == ("exclude", None)
        assert classify_entry("PySide2-5.15.2.1.dist-info/top_level.txt", "PySide2") == ("exclude", None)
        assert classify_entry("PySide2-5.15.2.1.dist-info/INSTALLER", "PySide2") == ("exclude", None)
        assert classify_entry("PySide2-5.15.2.1.dist-info/REQUESTED", "PySide2") == ("exclude", None)
        # METADATA/LICENSE 等必要文件 → metadata（保留）
        assert classify_entry("PySide2-5.15.2.1.dist-info/METADATA", "PySide2") == ("metadata", None)
        assert classify_entry("PySide2-5.15.2.1.dist-info/LICENSE", "PySide2") == ("metadata", None)
        assert classify_entry("PySide2-5.15.2.1.dist-info/LICENSE.GPLv3", "PySide2") == ("metadata", None)
        assert classify_entry("mypkg-1.0.dist-info/PKG-INFO", "mypkg") == ("metadata", None)
        assert classify_entry("mypkg-1.0.dist-info/COPYING.txt", "mypkg") == ("metadata", None)
        assert classify_entry("mypkg-1.0.dist-info/NOTICE", "mypkg") == ("metadata", None)
        # 未知文件兜底保留（避免误剥离）
        assert classify_entry("mypkg-1.0.dist-info/UNKNOWN_FILE", "mypkg") == ("metadata", None)
        # dist-info 目录自身条目保留（zip 中目录条目可能以 / 结尾或为纯目录名）
        assert classify_entry("PySide2-5.15.2.1.dist-info/", "PySide2") == ("metadata", None)
        assert classify_entry("PySide2-5.15.2.1.dist-info", "PySide2") == ("metadata", None)

    def test_dist_info_stripped_across_specs(self) -> None:
        """dist-info 精简在所有 spec（Qt/Default/numpy/lxml/matplotlib/scipy）中一致生效."""
        # Qt spec（_classify_top_or_meta 路径）
        from fspack.slim.qt import QtSlimSpec

        assert QtSlimSpec.classify_entry("PySide2-5.15.2.1.dist-info/RECORD", "PySide2", set()) == (
            "exclude",
            None,
        )
        assert QtSlimSpec.classify_entry("PySide2-5.15.2.1.dist-info/METADATA", "PySide2", set()) == (
            "metadata",
            None,
        )
        # Default spec（_default_classify 路径）
        from fspack.slim.default import DefaultSlimSpec

        assert DefaultSlimSpec.classify_entry("mypkg-1.0.dist-info/RECORD", "mypkg", set()) == (
            "exclude",
            None,
        )
        assert DefaultSlimSpec.classify_entry("mypkg-1.0.dist-info/METADATA", "mypkg", set()) == (
            "metadata",
            None,
        )
        # 库专属 spec（numpy 走 _default_classify）
        from fspack.slim.libs import NumpySlimSpec

        assert NumpySlimSpec.classify_entry("numpy-1.24.4.dist-info/WHEEL", "numpy", set()) == (
            "exclude",
            None,
        )
        assert NumpySlimSpec.classify_entry("numpy-1.24.4.dist-info/LICENSE.txt", "numpy", set()) == (
            "metadata",
            None,
        )

    def test_strip_exts_end_to_end_unpack(self, tmp_path: Path) -> None:
        """端到端：含 .h/.cpp/.lib/.pyc 的 wheel 解压后这些文件被剥离."""
        whl = tmp_path / "wh" / "mypkg-1.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "mypkg/__init__.py": b"",
                "mypkg/core.pyd": b"core",
                "mypkg/utils.py": b"u",
                # 编译时/调试/缓存文件 → 剥离
                "mypkg/foo.h": b"h",
                "mypkg/sub/bar.cpp": b"cpp",
                "mypkg/baz.lib": b"lib",
                "mypkg/debug.pdb": b"pdb",
                "mypkg/__pycache__/utils.cpython-38.pyc": b"pyc",
                "mypkg/tool.exe": b"exe",
                # 运行时文件 → 保留
                "mypkg/sub/x.py": b"sub",
                "mypkg-1.0.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"mypkg": frozenset({"core"})})
        assert count == 1
        # 运行时文件保留
        assert (dest / "mypkg" / "__init__.py").is_file()
        assert (dest / "mypkg" / "core.pyd").is_file()
        assert (dest / "mypkg" / "utils.py").is_file()
        assert (dest / "mypkg" / "sub" / "x.py").is_file()
        # 编译时/调试/缓存文件剥离
        assert not (dest / "mypkg" / "foo.h").exists()
        assert not (dest / "mypkg" / "sub" / "bar.cpp").exists()
        assert not (dest / "mypkg" / "baz.lib").exists()
        assert not (dest / "mypkg" / "debug.pdb").exists()
        assert not (dest / "mypkg" / "__pycache__").exists()
        assert not (dest / "mypkg" / "tool.exe").exists()
        # 元数据保留
        assert (dest / "mypkg-1.0.dist-info" / "METADATA").is_file()

    def test_dist_info_end_to_end_unpack(self, tmp_path: Path) -> None:
        """端到端：dist-info 中 RECORD/WHEEL 等剥离，METADATA/LICENSE 保留."""
        whl = tmp_path / "wh" / "mypkg-1.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "mypkg/__init__.py": b"",
                "mypkg/core.pyd": b"core",
                # dist-info 运行时无用文件 → 应剥离
                "mypkg-1.0.dist-info/RECORD": b"r" * 1024,
                "mypkg-1.0.dist-info/WHEEL": b"w",
                "mypkg-1.0.dist-info/entry_points.txt": b"e",
                "mypkg-1.0.dist-info/top_level.txt": b"t",
                "mypkg-1.0.dist-info/INSTALLER": b"i",
                "mypkg-1.0.dist-info/REQUESTED": b"",
                # dist-info 必要文件 → 应保留
                "mypkg-1.0.dist-info/METADATA": b"meta",
                "mypkg-1.0.dist-info/LICENSE": b"lic",
                "mypkg-1.0.dist-info/LICENSE.GPLv3": b"gpl",
                "mypkg-1.0.dist-info/NOTICE": b"n",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"mypkg": frozenset({"core"})})
        assert count == 1
        dist_info = dest / "mypkg-1.0.dist-info"
        # 运行时无用的元数据文件剥离
        assert not (dist_info / "RECORD").exists()
        assert not (dist_info / "WHEEL").exists()
        assert not (dist_info / "entry_points.txt").exists()
        assert not (dist_info / "top_level.txt").exists()
        assert not (dist_info / "INSTALLER").exists()
        assert not (dist_info / "REQUESTED").exists()
        # 必要文件保留
        assert (dist_info / "METADATA").is_file()
        assert (dist_info / "LICENSE").is_file()
        assert (dist_info / "LICENSE.GPLv3").is_file()
        assert (dist_info / "NOTICE").is_file()


class TestCommonExcludeSubdirsExtended:
    """COMMON_EXCLUDE_SUBDIRS 扩展：benchmarks/__pycache__ 目录剥离."""

    def test_benchmarks_excluded_default_spec(self) -> None:
        """默认 spec benchmarks 目录剥离."""
        assert classify_entry("mypkg/benchmarks/bench.py", "mypkg") == ("exclude", None)
        assert classify_entry("mypkg/benchmarks/nested/deep.py", "mypkg") == ("exclude", None)

    def test_benchmarks_excluded_numpy_spec(self) -> None:
        """numpy spec benchmarks 目录剥离."""
        assert classify_entry("numpy/benchmarks/bench_core.py", "numpy") == ("exclude", None)

    def test_pycache_excluded_default_spec(self) -> None:
        """默认 spec __pycache__ 目录剥离."""
        assert classify_entry("mypkg/__pycache__/foo.cpython-38.pyc", "mypkg") == ("exclude", None)

    def test_pycache_excluded_numpy_spec(self) -> None:
        """numpy spec __pycache__ 目录剥离."""
        assert classify_entry("numpy/__pycache__/foo.cpython-38.pyc", "numpy") == ("exclude", None)

    def test_qt_benchmarks_excluded(self) -> None:
        """Qt spec benchmarks 目录剥离（Qt spec 现在也应用 COMMON_EXCLUDE_SUBDIRS）."""
        assert classify_entry("PySide2/benchmarks/bench.py", "PySide2") == ("exclude", None)

    def test_qt_pycache_excluded(self) -> None:
        """Qt spec __pycache__ 目录剥离."""
        assert classify_entry("PySide2/__pycache__/foo.cpython-38.pyc", "PySide2") == ("exclude", None)

    def test_qt_tests_excluded(self) -> None:
        """Qt spec tests 目录剥离（COMMON_EXCLUDE_SUBDIRS 现在也覆盖 Qt）."""
        assert classify_entry("PySide2/tests/test_core.py", "PySide2") == ("exclude", None)

    def test_qt_docs_excluded(self) -> None:
        """Qt spec docs 目录剥离（COMMON_EXCLUDE_SUBDIRS 覆盖 Qt，与 _QT_EXCLUDE_SUBDIRS 的 doc 冗余但无害）."""
        assert classify_entry("PySide2/docs/index.md", "PySide2") == ("exclude", None)
