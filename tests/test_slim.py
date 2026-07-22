"""slim 精简打包测试：wheel 文件归属分类与按需解压."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from fspack.exceptions import DependencyError
from fspack.slim import classify_entry, slim_unpack


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
        from fspack.slim import _qt_module_closure

        assert _qt_module_closure({"Core"}) == {"Core"}

    def test_widgets_closure(self) -> None:
        """QtWidgets → Gui → Core（C 层链接依赖链）."""
        from fspack.slim import _qt_module_closure

        assert _qt_module_closure({"Widgets"}) == {"Widgets", "Gui", "Core"}

    def test_quick_transitive(self) -> None:
        """QtQuick → QtQml → QtNetwork → QtCore + QtGui."""
        from fspack.slim import _qt_module_closure

        result = _qt_module_closure({"Quick"})
        assert {"Quick", "Qml", "Network", "Gui", "Core"}.issubset(result)

    def test_qt3d_extras_transitive(self) -> None:
        """Qt3DExtras 闭包含 3DRender/3DInput/3DLogic/3DCore/Core/Gui/Network."""
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
        """未知模块名原样保留，不触发额外依赖推导."""
        from fspack.slim import _qt_module_closure

        assert _qt_module_closure({"UnknownMod"}) == {"UnknownMod"}

    def test_mixed_known_unknown(self) -> None:
        """已知与未知模块混合时，已知模块触发闭包，未知模块原样保留."""
        from fspack.slim import _qt_module_closure

        result = _qt_module_closure({"Widgets", "Foo"})
        assert result == {"Widgets", "Gui", "Core", "Foo"}

    def test_empty_set(self) -> None:
        from fspack.slim import _qt_module_closure

        assert _qt_module_closure(set()) == set()

    def test_idempotent(self) -> None:
        """闭包计算幂等：对已闭包集合再次计算结果不变."""
        from fspack.slim import _qt_module_closure

        once = _qt_module_closure({"Widgets"})
        twice = _qt_module_closure(once)
        assert once == twice


class TestQtDllClassification:
    """Qt5/Qt6*.dll 文件名与 Qt 子模块名归一化."""

    def test_qt5core_to_core(self) -> None:
        from fspack.slim import _qt_dll_submodule

        assert _qt_dll_submodule("Qt5Core") == "Core"

    def test_qt6widgets_to_widgets(self) -> None:
        from fspack.slim import _qt_dll_submodule

        assert _qt_dll_submodule("Qt6Widgets") == "Widgets"

    def test_qt5_3d_animation(self) -> None:
        """Qt53DAnimation.dll → 3DAnimation（去掉 5 后保留 3DAnimation）."""
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
        """Qt3DCore 归一化为 3DCore."""
        from fspack.slim import _normalize_qt_sub

        assert _normalize_qt_sub("Qt3DCore") == "3DCore"

    def test_normalize_non_qt(self) -> None:
        """非 Qt 前缀原样返回."""
        from fspack.slim import _normalize_qt_sub

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

    def test_slim_extract_bad_zip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_slim_extract 遇到坏 zip 抛 DependencyError."""
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
    """numpy 精简规则：通用剥离 + 库专属剥离（f2py/distutils/_pyinstaller）。."""

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
        """numpy 的通用剥离目录（examples/docs/tests/testing 等）归 exclude."""
        from fspack.slim.libs import NumpySlimSpec

        for subdir in ("examples", "docs", "doc", "tests", "test", "testing"):
            assert NumpySlimSpec.classify_entry(f"numpy/{subdir}/dummy.py", "numpy", set()) == ("exclude", None), (
                f"{subdir} 应当剥离"
            )

    def test_classify_numpy_extra_excluded(self) -> None:
        """numpy 专属剥离目录：f2py/distutils/_pyinstaller 归 exclude."""
        from fspack.slim.libs import NumpySlimSpec

        for subdir in ("f2py", "distutils", "_pyinstaller"):
            assert NumpySlimSpec.classify_entry(f"numpy/{subdir}/dummy.py", "numpy", set()) == ("exclude", None), (
                f"{subdir} 应当剥离"
            )
            # 嵌套子目录同样剥离
            assert NumpySlimSpec.classify_entry(f"numpy/{subdir}/nested/deep.py", "numpy", set()) == (
                "exclude",
                None,
            ), f"{subdir}/nested 应当剥离"

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
        from fspack.slim.base import SlimSpec, override

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
                entry: str,  # noqa: ARG003
                top_pkg: str,  # noqa: ARG003
                keep_subs: set[str],  # noqa: ARG003
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
