"""slim 精简打包测试：wheel 文件归属分类与按需解压。."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from fspack.exceptions import DependencyError
from fspack.slim import classify_entry, slim_unpack


class TestClassifyEntry:
    """wheel 条目归属分类。."""

    def test_dist_info(self) -> None:
        assert classify_entry("PySide2-5.15.2.1.dist-info/METADATA", "PySide2") == ("metadata", None)

    def test_init_py(self) -> None:
        assert classify_entry("PySide2/__init__.py", "PySide2") == ("shared", None)

    def test_private_module(self) -> None:
        assert classify_entry("PySide2/_config.py", "PySide2") == ("shared", None)

    def test_pyd_file(self) -> None:
        """Qt 库 .pyd 归一化子模块名：QtCore.pyd → Core。."""
        assert classify_entry("PySide2/QtCore.pyd", "PySide2") == ("submodule", "Core")

    def test_pyi_file(self) -> None:
        assert classify_entry("PySide2/QtCore.pyi", "PySide2") == ("submodule", "Core")

    def test_pyd_file_3d(self) -> None:
        """Qt3DCore.pyd 归一化为 3DCore。."""
        assert classify_entry("PySide2/Qt3DCore.pyd", "PySide2") == ("submodule", "3DCore")

    def test_so_file(self) -> None:
        """子目录下的 .so 文件归类为 shared（len(parts) > 2）。."""
        assert classify_entry("numpy/core/multiarray.so", "numpy") == ("shared", None)

    def test_qt5_dll(self) -> None:
        """Qt5Core.dll 归一化为子模块 Core（按需保留）。."""
        assert classify_entry("PySide2/Qt5Core.dll", "PySide2") == ("submodule", "Core")

    def test_qt5_3d_dll(self) -> None:
        """Qt53DAnimation.dll 归一化为子模块 3DAnimation。."""
        assert classify_entry("PySide2/Qt53DAnimation.dll", "PySide2") == ("submodule", "3DAnimation")

    def test_qt6_dll(self) -> None:
        """PySide6 的 Qt6Gui.dll 归一化为子模块 Gui。."""
        assert classify_entry("PySide6/Qt6Gui.dll", "PySide6") == ("submodule", "Gui")

    def test_other_dll(self) -> None:
        """非 Qt5/Qt6 前缀的 DLL（VC++ 运行时等）归 shared 始终保留。."""
        assert classify_entry("PySide2/concrt140.dll", "PySide2") == ("shared", None)

    def test_pyside_abi_dll(self) -> None:
        """pyside2.abi3.dll 归 shared（绑定层，始终保留）。."""
        assert classify_entry("PySide2/pyside2.abi3.dll", "PySide2") == ("shared", None)

    def test_qt_exe_excluded(self) -> None:
        """Qt 自带开发工具 exe 归 exclude（运行时不需要）。."""
        assert classify_entry("PySide2/designer.exe", "PySide2") == ("exclude", None)

    def test_subdir_platforms(self) -> None:
        """plugins/platforms 始终保留（窗口系统必需）。."""
        assert classify_entry("PySide2/plugins/platforms/qwindows.dll", "PySide2") == ("shared", None)

    def test_subdir_imageformats(self) -> None:
        """plugins/imageformats 始终保留。."""
        assert classify_entry("PySide2/plugins/imageformats/qsvg.dll", "PySide2") == ("shared", None)

    def test_subdir_mediaservice_no_dep(self) -> None:
        """plugins/mediaservice 无 Multimedia 依赖时剥离。."""
        assert classify_entry("PySide2/plugins/mediaservice/wmfengine.dll", "PySide2") == ("exclude", None)

    def test_subdir_mediaservice_with_dep(self) -> None:
        """plugins/mediaservice 有 Multimedia 依赖时保留。."""
        result = classify_entry("PySide2/plugins/mediaservice/wmfengine.dll", "PySide2", {"Multimedia"})
        assert result == ("shared", None)

    def test_subdir_sqldrivers_with_dep(self) -> None:
        """plugins/sqldrivers 有 Sql 依赖时保留。."""
        result = classify_entry("PySide2/plugins/sqldrivers/qsqlite.dll", "PySide2", {"Sql"})
        assert result == ("shared", None)

    def test_subdir_unknown_plugin_excluded(self) -> None:
        """未知 plugins 子目录白名单制剥离。."""
        assert classify_entry("PySide2/plugins/unknown/x.dll", "PySide2") == ("exclude", None)

    def test_examples_excluded(self) -> None:
        """examples 目录始终剥离。."""
        assert classify_entry("PySide2/examples/charts/linechart.py", "PySide2") == ("exclude", None)

    def test_translations_excluded(self) -> None:
        """translations 目录始终剥离。."""
        assert classify_entry("PySide2/translations/qtbase_ar.qm", "PySide2") == ("exclude", None)

    def test_include_excluded(self) -> None:
        """include 目录（C 头文件）始终剥离。."""
        assert classify_entry("PySide2/include/QtGui/qguiapplication.h", "PySide2") == ("exclude", None)

    def test_resources_no_dep_excluded(self) -> None:
        """resources 目录无 WebEngine 依赖时剥离。."""
        assert classify_entry("PySide2/resources/icudtl.dat", "PySide2") == ("exclude", None)

    def test_resources_with_dep_kept(self) -> None:
        """resources 目录有 WebEngine 依赖时保留。."""
        result = classify_entry("PySide2/resources/icudtl.dat", "PySide2", {"WebEngineCore"})
        assert result == ("shared", None)

    def test_qml_no_dep_excluded(self) -> None:
        """qml 目录无 Qml/Quick 依赖时剥离。."""
        assert classify_entry("PySide2/qml/QtQuick.2/qmldir", "PySide2") == ("exclude", None)

    def test_qml_with_dep_kept(self) -> None:
        """qml 目录有 Quick 依赖时保留。."""
        result = classify_entry("PySide2/qml/QtQuick.2/qmldir", "PySide2", {"Quick"})
        assert result == ("shared", None)

    def test_other_pkg(self) -> None:
        assert classify_entry("shiboken2/shiboken2.pyd", "PySide2") == ("shared", None)

    def test_top_level_file(self) -> None:
        assert classify_entry("PySide2/py.typed", "PySide2") == ("shared", None)

    def test_non_qt_pyd(self) -> None:
        """非 Qt 库的 .pyd 按原始文件名归类（不归一化）。."""
        assert classify_entry("numpy/_core/multiarray.pyd", "numpy") == ("shared", None)
        assert classify_entry("mypkg/core.pyd", "mypkg") == ("submodule", "core")


class TestQtModuleClosure:
    """Qt 模块依赖闭包计算（归一化名）。."""

    def test_core_only(self) -> None:
        from fspack.slim import _qt_module_closure

        assert _qt_module_closure({"Core"}) == {"Core"}

    def test_widgets_closure(self) -> None:
        """QtWidgets → Gui → Core（C 层链接依赖链）。."""
        from fspack.slim import _qt_module_closure

        assert _qt_module_closure({"Widgets"}) == {"Widgets", "Gui", "Core"}

    def test_quick_transitive(self) -> None:
        """QtQuick → QtQml → QtNetwork → QtCore + QtGui。."""
        from fspack.slim import _qt_module_closure

        result = _qt_module_closure({"Quick"})
        assert {"Quick", "Qml", "Network", "Gui", "Core"}.issubset(result)

    def test_qt3d_extras_transitive(self) -> None:
        """Qt3DExtras 闭包含 3DRender/3DInput/3DLogic/3DCore/Core/Gui/Network。."""
        from fspack.slim import _qt_module_closure

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
        """未知模块名原样保留，不触发额外依赖推导。."""
        from fspack.slim import _qt_module_closure

        assert _qt_module_closure({"UnknownMod"}) == {"UnknownMod"}

    def test_mixed_known_unknown(self) -> None:
        """已知与未知模块混合时，已知模块触发闭包，未知模块原样保留。."""
        from fspack.slim import _qt_module_closure

        result = _qt_module_closure({"Widgets", "Foo"})
        assert result == {"Widgets", "Gui", "Core", "Foo"}

    def test_empty_set(self) -> None:
        from fspack.slim import _qt_module_closure

        assert _qt_module_closure(set()) == set()

    def test_idempotent(self) -> None:
        """闭包计算幂等：对已闭包集合再次计算结果不变。."""
        from fspack.slim import _qt_module_closure

        once = _qt_module_closure({"Widgets"})
        twice = _qt_module_closure(once)
        assert once == twice


class TestQtDllClassification:
    """Qt5/Qt6*.dll 文件名与 Qt 子模块名归一化。."""

    def test_qt5core_to_core(self) -> None:
        from fspack.slim import _qt_dll_submodule

        assert _qt_dll_submodule("Qt5Core") == "Core"

    def test_qt6widgets_to_widgets(self) -> None:
        from fspack.slim import _qt_dll_submodule

        assert _qt_dll_submodule("Qt6Widgets") == "Widgets"

    def test_qt5_3d_animation(self) -> None:
        """Qt53DAnimation.dll → 3DAnimation（去掉 5 后保留 3DAnimation）。."""
        from fspack.slim import _qt_dll_submodule

        assert _qt_dll_submodule("Qt53DAnimation") == "3DAnimation"

    def test_non_qt_dll_returns_none(self) -> None:
        from fspack.slim import _qt_dll_submodule

        assert _qt_dll_submodule("pyside2.abi3") is None
        assert _qt_dll_submodule("concrt140") is None
        assert _qt_dll_submodule("msvcp140") is None

    def test_normalize_qtcore(self) -> None:
        from fspack.slim import _normalize_qt_sub

        assert _normalize_qt_sub("QtCore") == "Core"
        assert _normalize_qt_sub("Qt5Core") == "Core"
        assert _normalize_qt_sub("Qt6Core") == "Core"

    def test_normalize_qt3dcore(self) -> None:
        """Qt3DCore 归一化为 3DCore。."""
        from fspack.slim import _normalize_qt_sub

        assert _normalize_qt_sub("Qt3DCore") == "3DCore"

    def test_normalize_non_qt(self) -> None:
        """非 Qt 前缀原样返回。."""
        from fspack.slim import _normalize_qt_sub

        assert _normalize_qt_sub("requests") == "requests"
        assert _normalize_qt_sub("os") == "os"


def _make_wheel(whl: Path, entries: dict[str, bytes]) -> None:
    """构造测试用 wheel 文件。."""
    with zipfile.ZipFile(whl, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


class TestSlimUnpack:
    """按需解压 wheel。."""

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
        """submodule_usage 有 numpy 但 wheel 是 PySide2 → 全量解压。."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtGui.pyd": b"gui"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"numpy": frozenset({"core"})})
        assert count == 1
        assert (dest / "PySide2" / "QtGui.pyd").is_file()

    def test_full_unpack_bad_zip_no_usage(self, tmp_path: Path) -> None:
        """无 submodule_usage 时坏 zip 走 _full_unpack 路径抛错。."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        whl.write_bytes(b"not a zip")
        with pytest.raises(DependencyError, match="wheel 损坏"):
            slim_unpack([whl], tmp_path / "sp")

    def test_slim_extract_with_dir_entries(self, tmp_path: Path) -> None:
        """wheel 含目录条目时正确提取目录与文件。."""
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
        """所有子模块都在保留集合中时不跳过任何文件。."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtCore.pyd": b"core", "PySide2/QtGui.pyd": b"gui"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"PySide2": frozenset({"QtCore", "QtGui"})})
        assert count == 1
        assert (dest / "PySide2" / "QtCore.pyd").is_file()
        assert (dest / "PySide2" / "QtGui.pyd").is_file()

    def test_detect_top_pkg_skips_non_matching(self, tmp_path: Path) -> None:
        """_detect_top_pkg 跳过不匹配的顶层目录后找到匹配项。."""
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
        """wheel 顶层目录与包名不匹配时全量解压。."""
        whl = tmp_path / "wh" / "numpy-1.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"different_pkg/core.pyd": b"core"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"numpy": frozenset({"core"})})
        assert count == 1
        assert (dest / "different_pkg" / "core.pyd").is_file()

    def test_keep_module_without_dot_skipped(self, tmp_path: Path) -> None:
        """keep_modules 中无 '.' 的条目被跳过，走全量解压。."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtGui.pyd": b"gui"})
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, keep_modules={"PySide2"})
        assert count == 1
        assert (dest / "PySide2" / "QtGui.pyd").is_file()

    def test_slim_extract_bad_zip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_slim_extract 遇到坏 zip 抛 DependencyError。."""
        whl = tmp_path / "wh" / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"PySide2/QtCore.pyd": b"core"})
        original_zipfile = zipfile.ZipFile
        call_count = [0]

        def fake_zipfile(file: Path) -> zipfile.ZipFile:
            call_count[0] += 1
            if call_count[0] >= 2:
                raise zipfile.BadZipFile("corrupt on second open")
            return original_zipfile(file)

        monkeypatch.setattr("fspack.slim.zipfile.ZipFile", fake_zipfile)
        with pytest.raises(DependencyError, match="wheel 损坏"):
            slim_unpack([whl], tmp_path / "sp", {"PySide2": frozenset({"QtCore"})})

    def test_qt_multimedia_dynamic_expansion(self, tmp_path: Path) -> None:
        """import PySide2.QtMultimedia 时联动保留 mediaservice plugins 与 Qt5Multimedia.dll。."""
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
        """import PySide2.QtWebEngineWidgets 时联动保留 resources 与 qtwebengine plugins。."""
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
        """import PySide2.QtQml 与 PySide2.QtQuick 时保留 qml 目录与 scenegraph plugins。."""
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
