"""slim 精简打包测试：wheel 文件归属分类与按需解压."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from fspack.config import DEFAULT_SLIM_RULES, SlimRules
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
        """Qt 库 .pyi 类型存根归 exclude（运行时不需要，仅类型检查工具用）."""
        assert classify_entry("PySide2/QtCore.pyi", "PySide2") == ("exclude", None)

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

    def test_resources_debug_pak_excluded(self) -> None:
        """resources 目录中 *.debug.pak 是 DevTools 调试资源，始终剥离.

        回归测试：ref/RimSort 此项浪费 74MB，fspack 通过此规则剥离。
        """
        # 有 WebEngine 依赖时 .debug.pak 仍剥离
        result = classify_entry(
            "PySide6/resources/qtwebengine_devtools_resources.debug.pak",
            "PySide6",
            {"WebEngineCore"},
        )
        assert result == ("exclude", None)
        result = classify_entry(
            "PySide6/resources/qtwebengine_resources.debug.pak",
            "PySide6",
            {"WebEngineWidgets"},
        )
        assert result == ("exclude", None)
        result = classify_entry(
            "PySide6/resources/qtwebengine_resources_100p.debug.pak",
            "PySide6",
            {"WebEngine"},
        )
        assert result == ("exclude", None)

    def test_resources_non_debug_pak_kept(self) -> None:
        """resources 目录中非 .debug.pak 资源（如 icudtl.dat、qtwebengine_resources.pak）保留."""
        result = classify_entry(
            "PySide6/resources/qtwebengine_resources.pak",
            "PySide6",
            {"WebEngineCore"},
        )
        assert result == ("shared", None)
        result = classify_entry(
            "PySide6/resources/icudtl.dat",
            "PySide6",
            {"WebEngineCore"},
        )
        assert result == ("shared", None)

    def test_top_icudtl_dat_no_dep_excluded(self) -> None:
        """顶层 icudtl.dat 无 WebEngine 依赖时剥离（ICU 数据仅 WebEngine 必需）."""
        result = classify_entry("PySide6/icudtl.dat", "PySide6")
        assert result == ("exclude", None)

    def test_top_icudtl_dat_with_dep_kept(self) -> None:
        """顶层 icudtl.dat 有 WebEngine 依赖时保留."""
        result = classify_entry("PySide6/icudtl.dat", "PySide6", {"WebEngineCore"})
        assert result == ("shared", None)

    def test_qtwebengineprocess_exe_no_dep_excluded(self) -> None:
        """顶层 QtWebEngineProcess.exe 无 WebEngine 依赖时剥离.

        回归测试：.exe 在 STRIP_EXTS 中会被默认剥离，但 QtWebEngineProcess.exe
        是 WebEngine 子进程宿主，WebEngine 应用必需。须在 STRIP_EXTS 之前拦截，
        按 WebEngine 子模块依赖判断。
        """
        result = classify_entry("PySide6/QtWebEngineProcess.exe", "PySide6")
        assert result == ("exclude", None)

    def test_qtwebengineprocess_exe_with_dep_kept(self) -> None:
        """顶层 QtWebEngineProcess.exe 有 WebEngine 依赖时保留（绕过 STRIP_EXTS .exe 规则）."""
        result = classify_entry("PySide6/QtWebEngineProcess.exe", "PySide6", {"WebEngineCore"})
        assert result == ("shared", None)

    def test_qtwebengineprocess_linux_no_dep_excluded(self) -> None:
        """Linux 平台 QtWebEngineProcess（无后缀 ELF）同样按 WebEngine 依赖判断."""
        result = classify_entry("PySide6/QtWebEngineProcess", "PySide6")
        assert result == ("exclude", None)

    def test_qtwebengineprocess_linux_with_dep_kept(self) -> None:
        """Linux 平台 QtWebEngineProcess 有 WebEngine 依赖时保留."""
        result = classify_entry("PySide6/QtWebEngineProcess", "PySide6", {"WebEngineWidgets"})
        assert result == ("shared", None)

    def test_designer_exe_still_excluded(self) -> None:
        """非 QtWebEngineProcess 的 .exe 仍归 STRIP_EXTS 剥离（如 designer.exe）."""
        result = classify_entry("PySide6/designer.exe", "PySide6", {"WebEngineCore"})
        assert result == ("exclude", None)

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

    def test_metatypes_excluded(self) -> None:
        """metatypes 目录（Qt 元类型 JSON）始终剥离（编译期用，运行时不需要，约 14MB）."""
        assert classify_entry("PySide6/metatypes/qt6core_metatypes.json", "PySide6") == ("exclude", None)
        assert classify_entry("PySide6/metatypes/qt6gui_metatypes.json", "PySide6", {"Gui"}) == ("exclude", None)

    def test_lib_cmake_excluded(self) -> None:
        """lib/cmake/ 三级子目录剥离（cmake 配置文件，构建系统用，运行时不需要）."""
        assert classify_entry("PySide6/lib/cmake/PySide6/PySide6Config.cmake", "PySide6") == ("exclude", None)
        assert classify_entry("PySide6/lib/cmake/PySide6/PySide6Targets.cmake", "PySide6", {"Core"}) == (
            "exclude",
            None,
        )

    def test_lib_fonts_kept(self) -> None:
        """lib/fonts/ 等非 cmake 内容保留（PySide2 lib/fonts/ 含 Qt 内嵌字体，运行时需要）."""
        assert classify_entry("PySide2/lib/fonts/times.ttf", "PySide2") == ("shared", None)
        assert classify_entry("PySide6/lib/some_other_file.dat", "PySide6") == ("shared", None)

    def test_qtasyncio_excluded(self) -> None:
        """QtAsyncio 目录（asyncio 集成模块）始终剥离（非 asyncio 应用不需要）."""
        assert classify_entry("PySide6/QtAsyncio/__init__.py", "PySide6") == ("exclude", None)
        assert classify_entry("PySide6/QtAsyncio/tasks.py", "PySide6", {"Core"}) == ("exclude", None)

    def test_ffmpeg_dll_classified_as_multimedia(self) -> None:
        """FFmpeg 系列 DLL 按 Multimedia 子模块分类（由 _slim_extract 按闭包选择性保留）.

        回归测试：ref/RimSort 闭包内无 Multimedia，avcodec-61.dll 等 FFmpeg 系列
        合计约 18MB 应被剥离。classify_entry 返回 ``("submodule", "Multimedia")``，
        当 keep_subs 非空且不含 Multimedia 时由 :func:`_slim_extract` 剥离；
        keep_subs 为空时（全量解压）保留。
        """
        # classify_entry 总是返回 ("submodule", "Multimedia")，剥离由 _slim_extract 决定
        assert classify_entry("PySide6/avcodec-61.dll", "PySide6") == ("submodule", "Multimedia")
        assert classify_entry("PySide6/avformat-61.dll", "PySide6") == ("submodule", "Multimedia")
        assert classify_entry("PySide6/avutil-59.dll", "PySide6") == ("submodule", "Multimedia")
        assert classify_entry("PySide6/swscale-8.dll", "PySide6") == ("submodule", "Multimedia")
        assert classify_entry("PySide6/swresample-5.dll", "PySide6") == ("submodule", "Multimedia")
        # 闭包内有 Multimedia 时返回相同结果（保留与否由 _slim_extract 判断）
        assert classify_entry("PySide6/avcodec-61.dll", "PySide6", {"Multimedia"}) == ("submodule", "Multimedia")
        # 闭包内仅有 Widgets 时返回相同结果（_slim_extract 会剥离）
        assert classify_entry("PySide6/avcodec-61.dll", "PySide6", {"Widgets"}) == ("submodule", "Multimedia")

    def test_qml_abi_dll_classified_as_qml(self) -> None:
        """``pyside6qml.abi3.dll``/``pyside2qml.abi3.dll`` 按 Qml 子模块分类.

        回归测试：ref/RimSort 闭包内无 Qml（abi3.dll 隐式依赖仅让 Qt6Qml.dll 归
        shared 保留，未加入 keep_subs），pyside6qml.abi3.dll 是 QML 类型注册绑定层，
        非 QML 应用不需要。classify_entry 返回 ``("submodule", "Qml")``，当
        keep_subs 非空且不含 Qml 时由 :func:`_slim_extract` 剥离；keep_subs 为空时保留。
        """
        # classify_entry 总是返回 ("submodule", "Qml")，剥离由 _slim_extract 决定
        assert classify_entry("PySide6/pyside6qml.abi3.dll", "PySide6") == ("submodule", "Qml")
        assert classify_entry("PySide6/pyside6qml.abi3.dll", "PySide6", {"Widgets"}) == ("submodule", "Qml")
        assert classify_entry("PySide2/pyside2qml.abi3.dll", "PySide2") == ("submodule", "Qml")
        # 闭包内有 Qml 时返回相同结果（保留与否由 _slim_extract 判断）
        assert classify_entry("PySide6/pyside6qml.abi3.dll", "PySide6", {"Qml"}) == ("submodule", "Qml")

    def test_qml_abi_dll_with_qml_kept(self) -> None:
        """pyside6qml.abi3.dll 有 Qml 依赖时保留（与 test_qml_abi_dll_classified_as_qml 互补）."""
        result = classify_entry("PySide6/pyside6qml.abi3.dll", "PySide6", {"Qml"})
        assert result == ("submodule", "Qml")
        result = classify_entry("PySide6/pyside6qml.abi3.dll", "PySide6", {"Quick"})
        assert result == ("submodule", "Qml")

    def test_opengl32sw_no_opengl_dep_excluded(self) -> None:
        """opengl32sw.dll 无 OpenGL 相关模块依赖时剥离.

        回归测试：ref/RimSort 闭包 = {Widgets, Gui, Core, WebEngineWidgets,
        WebEngineCore, Network, Positioning, WebChannel}，无 OpenGL/Quick/Multimedia
        等模块，opengl32sw.dll（约 20MB）应被剥离。WebEngineCore 自带 Chromium
        GPU 加速，不依赖 opengl32sw.dll。
        """
        assert classify_entry("PySide6/opengl32sw.dll", "PySide6") == ("exclude", None)
        # 纯 Widgets 应用剥离
        assert classify_entry("PySide6/opengl32sw.dll", "PySide6", {"Widgets"}) == ("exclude", None)
        # RimSort 场景：WebEngine 闭包也剥离
        assert classify_entry(
            "PySide6/opengl32sw.dll",
            "PySide6",
            {"Widgets", "Gui", "Core", "WebEngineWidgets", "WebEngineCore", "Network", "Positioning"},
        ) == ("exclude", None)

    @pytest.mark.parametrize(
        "dep_module",
        ["OpenGL", "OpenGLWidgets", "Quick", "Quick3D", "QuickShapes", "QuickWidgets", "Multimedia", "Graphs"],
    )
    def test_opengl32sw_with_opengl_dep_kept(self, dep_module: str) -> None:
        """opengl32sw.dll 有任一 OpenGL 相关模块依赖时保留（软件 OpenGL 后备）."""
        result = classify_entry("PySide6/opengl32sw.dll", "PySide6", {dep_module})
        assert result == ("shared", None)

    def test_resources_debug_bin_excluded(self) -> None:
        """resources 目录中 .debug.bin 文件是 DevTools 调试资源，始终剥离.

        回归测试：原规则仅剥离 .debug.pak，遗漏 v8_context_snapshot.debug.bin
        （约 2.3MB）。扩展为 .debug.* 子串匹配，覆盖所有 DevTools 调试资源。
        """
        result = classify_entry(
            "PySide6/resources/v8_context_snapshot.debug.bin",
            "PySide6",
            {"WebEngineCore"},
        )
        assert result == ("exclude", None)
        # 原 .debug.pak 规则仍生效
        result = classify_entry(
            "PySide6/resources/qtwebengine_devtools_resources.debug.pak",
            "PySide6",
            {"WebEngineCore"},
        )
        assert result == ("exclude", None)
        # 非 .debug.* 资源仍保留
        result = classify_entry(
            "PySide6/resources/v8_context_snapshot.bin",
            "PySide6",
            {"WebEngineCore"},
        )
        assert result == ("shared", None)

    def test_non_qt_dll_still_shared(self) -> None:
        """非 Qt5/Qt6 前缀且非 FFmpeg/QML ABI/opengl32sw 的 DLL 仍归 shared 保留."""
        # VC++ 运行时
        assert classify_entry("PySide6/msvcp140.dll", "PySide6") == ("shared", None)
        assert classify_entry("PySide6/vcruntime140.dll", "PySide6") == ("shared", None)
        # pyside6.abi3.dll（绑定层，始终保留）
        assert classify_entry("PySide6/pyside6.abi3.dll", "PySide6") == ("shared", None)


class TestQtAuxiliaryDllIdentifiers:
    """Qt 辅助 DLL 识别函数（FFmpeg/QML ABI/opengl32sw）."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("avcodec-61.dll", True),
            ("avformat-61.dll", True),
            ("avutil-59.dll", True),
            ("swscale-8.dll", True),
            ("swresample-5.dll", True),
            # 大写也匹配
            ("AVCodec-61.dll", True),
            ("AvFormat-61.DLL", True),
            # 非 FFmpeg 前缀
            ("Qt6Core.dll", False),
            ("msvcp140.dll", False),
            ("pyside6.abi3.dll", False),
            ("opengl32sw.dll", False),
            # 非 .dll 后缀
            ("avcodec-61.lib", False),
            ("avcodec-61.pyd", False),
        ],
        ids=[
            "avcodec",
            "avformat",
            "avutil",
            "swscale",
            "swresample",
            "uppercase",
            "mixed_case",
            "qt6core",
            "msvcp",
            "pyside_abi",
            "opengl32sw",
            "lib_ext",
            "pyd_ext",
        ],
    )
    def test_is_ffmpeg_dll(self, filename: str, expected: bool) -> None:
        """FFmpeg 系列 DLL 识别：前缀 + 版本号 + .dll 后缀."""
        from fspack.slim.qt import _is_ffmpeg_dll

        assert _is_ffmpeg_dll(filename) is expected

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("pyside6qml.abi3.dll", True),
            ("pyside2qml.abi3.dll", True),
            ("PYSIDE6QML.ABI3.DLL", True),
            ("pyside6.abi3.dll", False),  # 非 qml 绑定层
            ("pyside2.abi3.dll", False),
            ("shiboken6.abi3.dll", False),
        ],
        ids=["pyside6qml", "pyside2qml", "uppercase", "pyside6_abi", "pyside2_abi", "shiboken6"],
    )
    def test_is_qml_abi_dll(self, filename: str, expected: bool) -> None:
        """QML 绑定层 ABI DLL 识别：pyside{2,6}qml.abi3.dll."""
        from fspack.slim.qt import _is_qml_abi_dll

        assert _is_qml_abi_dll(filename) is expected

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("opengl32sw.dll", True),
            ("OPENGL32SW.DLL", True),
            ("OpenGL32sw.Dll", True),
            ("opengl32.dll", False),  # 系统 OpenGL，非软件后备
            ("opengl32sw.lib", False),  # 非动态库
            ("mesa.dll", False),
        ],
        ids=["lowercase", "uppercase", "mixed_case", "system_opengl", "lib_ext", "mesa"],
    )
    def test_is_opengl_sw_dll(self, filename: str, expected: bool) -> None:
        """opengl32sw.dll 识别（Mesa 软件 OpenGL 后备）."""
        from fspack.slim.qt import _is_opengl_sw_dll

        assert _is_opengl_sw_dll(filename) is expected


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
        """QtQuick → QtQmlModels → QtQml → QtNetwork → QtCore + QtGui."""
        from fspack.slim.qt import _qt_module_closure

        result = _qt_module_closure({"Quick"})
        assert {"Quick", "QmlModels", "Qml", "Network", "Gui", "Core"}.issubset(result)

    def test_quick_controls2_includes_templates2(self) -> None:
        """QtQuickControls2 在 C 层链接依赖 QtQuickTemplates2，闭包须包含。

        回归测试：缺少此依赖会导致 dist 中 Qt5QuickTemplates2.dll 被剥离，
        运行时 ``import PySide2.QtQuickControls2`` 报 DLL load failed。
        """
        from fspack.slim.qt import _qt_module_closure

        result = _qt_module_closure({"QuickControls2"})
        assert "QuickTemplates2" in result
        assert {"QuickControls2", "QuickTemplates2", "Quick", "Qml", "Network", "Gui", "Core"}.issubset(result)

    def test_qml_includes_runtime_plugin_deps(self) -> None:
        """QtQml 运行时加载的 QML 插件依赖 QmlModels/QmlWorkerScript。

        回归测试：``qtquick2plugin.dll`` 在 C 层链接 ``Qt5QmlModels.dll`` 与
        ``Qt5QmlWorkerScript.dll``，二者须随 Qml 保留，否则运行 QML 时报
        "plugin cannot be loaded for module QtQuick"。
        """
        from fspack.slim.qt import _qt_module_closure

        result = _qt_module_closure({"Qml"})
        assert "QmlModels" in result
        assert "QmlWorkerScript" in result
        assert {"Qml", "QmlModels", "QmlWorkerScript", "Network", "Core"}.issubset(result)

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

    def test_split_wheel_essentials_shared_keep_subs(self, tmp_path: Path) -> None:
        """PySide6 拆分 wheel（pyside6_essentials）共享主包 keep_subs.

        PySide6 6.6+ 将包拆为 pyside6/pyside6_essentials/pyside6_addons 三个 wheel，
        后两者文件名归一化包名（pyside6-essentials/pyside6-addons）与顶层目录
        PySide6（归一化为 pyside6）不一致。``_detect_top_pkg`` 回退匹配使
        QtSlimSpec 识别这些拆分 wheel，共享 ``merged["pyside6"]`` 的 keep_subs，
        按子模块选择性保留而非全量解压。
        """
        whl = tmp_path / "wh" / "pyside6_essentials-6.11.1-cp310-abi3-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core_pyd",
                "PySide6/QtWidgets.pyd": b"widgets_pyd",
                "PySide6/Qt3DCore.pyd": b"3dcore_pyd",
                "PySide6/Qt6Core.dll": b"qt6core",
                "PySide6/Qt6Widgets.dll": b"qt6widgets",
                "PySide6/Qt6Charts.dll": b"qt6charts",
                "PySide6/translations/qt_ar.qm": b"qm",
                "PySide6/include/pyside.h": b"h",
                "PySide6/designer.exe": b"tool",
                "PySide6/plugins/platforms/qwindows.dll": b"plugin",
                "pyside6_essentials-6.11.1.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        # 用户 import QtCore/QtWidgets，闭包自动加入 Gui/Core
        count = slim_unpack([whl], dest, {"PySide6": frozenset({"QtCore", "QtWidgets"})})
        assert count == 1
        # 闭包内子模块保留（.pyd 归一化后匹配 keep_subs）
        assert (dest / "PySide6" / "QtCore.pyd").is_file()
        assert (dest / "PySide6" / "QtWidgets.pyd").is_file()
        assert (dest / "PySide6" / "Qt6Core.dll").is_file()
        assert (dest / "PySide6" / "Qt6Widgets.dll").is_file()
        # 闭包外子模块剥离
        assert not (dest / "PySide6" / "Qt3DCore.pyd").exists()
        assert not (dest / "PySide6" / "Qt6Charts.dll").exists()
        # _QT_EXCLUDE_SUBDIRS 生效（拆分 wheel 也应用 Qt 精简规则）
        assert not (dest / "PySide6" / "translations").exists()
        assert not (dest / "PySide6" / "include").exists()
        # STRIP_EXTS 生效
        assert not (dest / "PySide6" / "designer.exe").exists()
        # 基础插件保留
        assert (dest / "PySide6" / "plugins" / "platforms" / "qwindows.dll").is_file()
        # 元数据保留
        assert (dest / "pyside6_essentials-6.11.1.dist-info" / "METADATA").is_file()

    def test_split_wheel_addons_shared_keep_subs(self, tmp_path: Path) -> None:
        """PySide6 拆分 wheel（pyside6_addons）共享主包 keep_subs."""
        whl = tmp_path / "wh" / "pyside6_addons-6.11.1-cp310-abi3-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/QtMultimedia.pyd": b"mm_pyd",
                "PySide6/Qt6Multimedia.dll": b"qt6mm",
                "PySide6/avcodec-61.dll": b"ffmpeg",
                "PySide6/QtAsyncio/__init__.py": b"asyncio",
                "PySide6/metatypes/qt6core_metatypes.json": b"json",
                "pyside6_addons-6.11.1.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        # 用户仅 import QtCore/QtWidgets，闭包不含 Multimedia
        count = slim_unpack([whl], dest, {"PySide6": frozenset({"QtCore", "QtWidgets"})})
        assert count == 1
        # Multimedia 闭包外 → .pyd/.dll/FFmpeg 剥离
        assert not (dest / "PySide6" / "QtMultimedia.pyd").exists()
        assert not (dest / "PySide6" / "Qt6Multimedia.dll").exists()
        assert not (dest / "PySide6" / "avcodec-61.dll").exists()
        # _QT_EXCLUDE_SUBDIRS 生效
        assert not (dest / "PySide6" / "QtAsyncio").exists()
        assert not (dest / "PySide6" / "metatypes").exists()
        # 元数据保留
        assert (dest / "pyside6_addons-6.11.1.dist-info" / "METADATA").is_file()

    def test_split_wheel_no_keep_subs_still_excludes(self, tmp_path: Path) -> None:
        """拆分 wheel 在 keep_subs 为空时仍应用剥离规则（顶层 import 场景）."""
        whl = tmp_path / "wh" / "pyside6_essentials-6.11.1-cp310-abi3-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core_pyd",
                "PySide6/Qt6Core.dll": b"qt6core",
                "PySide6/translations/qt_ar.qm": b"qm",
                "PySide6/include/pyside.h": b"h",
                "PySide6/designer.exe": b"tool",
                "pyside6_essentials-6.11.1.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        # 无 submodule_usage：keep_subs 为空集，等价于全量解压 + 应用剥离规则
        count = slim_unpack([whl], dest)
        assert count == 1
        # 子模块文件全保留（keep_subs 为空）
        assert (dest / "PySide6" / "QtCore.pyd").is_file()
        assert (dest / "PySide6" / "Qt6Core.dll").is_file()
        # 但剥离规则仍生效
        assert not (dest / "PySide6" / "translations").exists()
        assert not (dest / "PySide6" / "include").exists()
        assert not (dest / "PySide6" / "designer.exe").exists()

    def test_split_wheel_multi_wheel_share_keep_subs(self, tmp_path: Path) -> None:
        """pyside6 + pyside6_essentials + pyside6_addons 三个 wheel 共享 keep_subs."""
        whl_dir = tmp_path / "wh"
        whl_dir.mkdir()
        # 主 wheel：仅 .pyd
        _make_wheel(
            whl_dir / "pyside6-6.11.1-cp310-abi3-win_amd64.whl",
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core_pyd",
                "PySide6/QtCharts.pyd": b"charts_pyd",
                "pyside6-6.11.1.dist-info/METADATA": b"meta",
            },
        )
        # essentials wheel：核心 DLL
        _make_wheel(
            whl_dir / "pyside6_essentials-6.11.1-cp310-abi3-win_amd64.whl",
            {
                "PySide6/Qt6Core.dll": b"qt6core",
                "PySide6/Qt6Charts.dll": b"qt6charts",
                "PySide6/translations/qt_ar.qm": b"qm",
                "pyside6_essentials-6.11.1.dist-info/METADATA": b"meta",
            },
        )
        # addons wheel：附加模块 DLL
        _make_wheel(
            whl_dir / "pyside6_addons-6.11.1-cp310-abi3-win_amd64.whl",
            {
                "PySide6/Qt6Multimedia.dll": b"qt6mm",
                "PySide6/avcodec-61.dll": b"ffmpeg",
                "PySide6/metatypes/qt6core_metatypes.json": b"json",
                "pyside6_addons-6.11.1.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        wheels = sorted(whl_dir.glob("*.whl"))
        count = slim_unpack(wheels, dest, {"PySide6": frozenset({"QtCore"})})
        assert count == 3
        # 主 wheel：QtCore.pyd 保留，QtCharts.pyd 剥离
        assert (dest / "PySide6" / "QtCore.pyd").is_file()
        assert not (dest / "PySide6" / "QtCharts.pyd").exists()
        # essentials：Qt6Core.dll 保留，Qt6Charts.dll 剥离，translations 剥离
        assert (dest / "PySide6" / "Qt6Core.dll").is_file()
        assert not (dest / "PySide6" / "Qt6Charts.dll").exists()
        assert not (dest / "PySide6" / "translations").exists()
        # addons：Multimedia 闭包外 → Qt6Multimedia.dll/avcodec 剥离，metatypes 剥离
        assert not (dest / "PySide6" / "Qt6Multimedia.dll").exists()
        assert not (dest / "PySide6" / "avcodec-61.dll").exists()
        assert not (dest / "PySide6" / "metatypes").exists()
        # 三个 wheel 的 dist-info 均保留
        assert (dest / "pyside6-6.11.1.dist-info" / "METADATA").is_file()
        assert (dest / "pyside6_essentials-6.11.1.dist-info" / "METADATA").is_file()
        assert (dest / "pyside6_addons-6.11.1.dist-info" / "METADATA").is_file()

    def test_split_wheel_numpy_libs_not_matched(self, tmp_path: Path) -> None:
        """numpy.libs 辅助目录不被回退匹配（DefaultSlimSpec 是兜底）.

        回归：``_detect_top_pkg`` 回退匹配仅识别非兜底 spec（如 QtSlimSpec），
        避免 numpy wheel 中的 ``numpy.libs/`` 辅助目录被误识别为 top_pkg。
        """
        whl = tmp_path / "wh" / "numpy-1.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                # numpy.libs 出现在 numpy 之前，但不应被回退匹配
                "numpy.libs/libopenblas.dll": b"blas",
                "numpy/__init__.py": b"",
                "numpy/core.pyd": b"core",
                "numpy-1.0.dist-info/METADATA": b"meta",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest, {"numpy": frozenset({"core"})})
        assert count == 1
        # numpy 顶层目录被正确识别（whl_pkg 严格匹配）
        assert (dest / "numpy" / "core.pyd").is_file()
        # numpy.libs 也被解压（属于跨包 shared，DefaultSlimSpec 不剥离）
        assert (dest / "numpy.libs" / "libopenblas.dll").is_file()

    # ---- 用户自定义 include/exclude 规则 ----

    def test_user_include_force_keep_excluded_file(self, tmp_path: Path) -> None:
        """slim include 强制保留被 spec 剥离的文件（覆盖 STRIP_EXTS）."""
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/designer.exe": b"tool",  # STRIP_EXTS 剥离
                "PySide6/Qt6Core.dll": b"c",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack(
            [whl],
            dest,
            {"PySide6": frozenset({"QtCore"})},
            slim_rules=SlimRules(include=("PySide6/designer.exe",)),
        )
        assert count == 1
        # 用户规则强制保留 designer.exe（覆盖 STRIP_EXTS 剥离）
        assert (dest / "PySide6" / "designer.exe").is_file()

    def test_user_exclude_force_strip_kept_file(self, tmp_path: Path) -> None:
        """slim exclude 强制剥离被 spec 保留的文件（覆盖 shared 保留）."""
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/Qt6Core.dll": b"c",  # 闭包内，spec 保留
                "PySide6/Qt6Charts.dll": b"charts",  # 闭包外，spec 剥离
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack(
            [whl],
            dest,
            {"PySide6": frozenset({"QtCore"})},
            slim_rules=SlimRules(exclude=("PySide6/Qt6Core.dll",)),
        )
        assert count == 1
        # 用户规则强制剥离 Qt6Core.dll（覆盖 spec 闭包保留）
        assert not (dest / "PySide6" / "Qt6Core.dll").exists()
        # 其他闭包内文件仍保留
        assert (dest / "PySide6" / "QtCore.pyd").is_file()

    def test_user_exclude_glob_pattern(self, tmp_path: Path) -> None:
        """slim exclude 支持 glob 模式（* 匹配任意字符含 /）."""
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/translations/qt_ar.qm": b"ar",
                "PySide6/translations/qt_de.qm": b"de",
                "PySide6/Qt6Core.dll": b"c",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack(
            [whl],
            dest,
            {"PySide6": frozenset({"QtCore"})},
            slim_rules=SlimRules(exclude=("PySide6/translations/*",)),
        )
        assert count == 1
        # glob 匹配 translations 目录所有文件
        assert not (dest / "PySide6" / "translations" / "qt_ar.qm").exists()
        assert not (dest / "PySide6" / "translations" / "qt_de.qm").exists()
        # 非 translations 文件不受影响
        assert (dest / "PySide6" / "Qt6Core.dll").is_file()

    def test_user_include_priority_over_exclude(self, tmp_path: Path) -> None:
        """slim include 优先级高于 exclude（同一文件冲突时保留）."""
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/designer.exe": b"tool",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack(
            [whl],
            dest,
            {"PySide6": frozenset({"QtCore"})},
            slim_rules=SlimRules(
                include=("PySide6/designer.exe",),
                exclude=("PySide6/designer.exe",),
            ),
        )
        assert count == 1
        # include 优先级高于 exclude → 保留
        assert (dest / "PySide6" / "designer.exe").is_file()

    def test_user_rules_no_match_fallback_spec(self, tmp_path: Path) -> None:
        """用户规则不匹配时走 spec 自动分类（向后兼容）."""
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/Qt3DCore.pyd": b"3d",  # 闭包外
                "PySide6/designer.exe": b"tool",  # STRIP_EXTS
            },
        )
        dest = tmp_path / "sp"
        # 用户规则不匹配任何文件 → 行为等同无用户规则
        count = slim_unpack(
            [whl],
            dest,
            {"PySide6": frozenset({"QtCore"})},
            slim_rules=SlimRules(
                include=("PySide6/nonexistent.dll",),
                exclude=("PySide6/nonexistent2.dll",),
            ),
        )
        assert count == 1
        # spec 自动分类生效
        assert (dest / "PySide6" / "QtCore.pyd").is_file()
        assert not (dest / "PySide6" / "Qt3DCore.pyd").exists()
        assert not (dest / "PySide6" / "designer.exe").exists()

    def test_user_exclude_case_sensitive(self, tmp_path: Path) -> None:
        """用户规则大小写敏感（fnmatchcase，Windows 文件名大小写不敏感但 wheel 路径保留原样）."""
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/Qt6Core.dll": b"c",
            },
        )
        dest = tmp_path / "sp"
        # 大写模式不匹配小写路径
        count = slim_unpack(
            [whl],
            dest,
            {"PySide6": frozenset({"QtCore"})},
            slim_rules=SlimRules(exclude=("PYSIDE6/QT6CORE.DLL",)),
        )
        assert count == 1
        # 大写模式不匹配 → 文件保留
        assert (dest / "PySide6" / "Qt6Core.dll").is_file()

    def test_slim_stats_stripped_files_and_bytes(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """精简统计日志输出剥离文件数与节省字节数."""
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        # 构造可剥离的文件：designer.exe（STRIP_EXTS）、Qt3DCore.pyd（闭包外）
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/Qt3DCore.pyd": b"3d",
                "PySide6/designer.exe": b"tool",
                "PySide6/examples/dummy.py": b"example",
            },
        )
        dest = tmp_path / "sp"
        with caplog.at_level("INFO", logger="fspack.slim.base"):
            slim_unpack([whl], dest, {"PySide6": frozenset({"QtCore"})})
        # 统计日志包含"剥离 N 个文件，节省 X.YMB"
        stats_logs = [r for r in caplog.records if "剥离" in r.getMessage() and "节省" in r.getMessage()]
        assert len(stats_logs) == 1
        msg = stats_logs[0].getMessage()
        assert "剥离 3 个文件" in msg  # designer.exe + Qt3DCore.pyd + examples/dummy.py
        assert "节省" in msg
        assert "MB" in msg

    def test_slim_stats_no_log_when_nothing_stripped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """无剥离时不输出统计日志."""
        whl = tmp_path / "wh" / "pkg-1.0-py3-none-any.whl"
        whl.parent.mkdir()
        _make_wheel(whl, {"pkg/__init__.py": b""})
        dest = tmp_path / "sp"
        with caplog.at_level("INFO", logger="fspack.slim.base"):
            slim_unpack([whl], dest)
        # 统计日志特征：同时含"剥离"和"节省"（区别于"解压...应用剥离规则"日志）
        stats_logs = [r for r in caplog.records if "剥离" in r.getMessage() and "节省" in r.getMessage()]
        assert not stats_logs

    def test_default_spec_strips_nested_tests(self, tmp_path: Path) -> None:
        """DefaultSlimSpec 自动剥离嵌套 tests 目录（pandas/scikit-learn 等无需专门 spec）."""
        whl = tmp_path / "wh" / "pandas-2.0.0-cp311-cp311-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "pandas/__init__.py": b"",
                "pandas/core/__init__.py": b"",
                "pandas/core/frame.py": b"# frame",
                "pandas/core/tests/__init__.py": b"",
                "pandas/core/tests/test_frame.py": b"# test",
                "pandas/io/tests/test_csv.py": b"# test",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest)
        assert count == 1
        # 嵌套 tests 被剥离
        assert not (dest / "pandas" / "core" / "tests" / "test_frame.py").exists()
        assert not (dest / "pandas" / "io" / "tests" / "test_csv.py").exists()
        # 运行时文件保留
        assert (dest / "pandas" / "core" / "frame.py").is_file()

    def test_default_spec_preserves_testing_subdir(self, tmp_path: Path) -> None:
        """DefaultSlimSpec 不剥离 testing 目录（numpy.testing 是公共 API）."""
        whl = tmp_path / "wh" / "numpy-1.26.0-cp311-cp311-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "numpy/__init__.py": b"",
                "numpy/testing/__init__.py": b"# testing api",
                "numpy/core/tests/test_x.py": b"# nested test",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack([whl], dest)
        assert count == 1
        # testing（单数，公共 API）保留
        assert (dest / "numpy" / "testing" / "__init__.py").is_file()
        # tests（复数，嵌套）剥离
        assert not (dest / "numpy" / "core" / "tests" / "test_x.py").exists()

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
        """import PySide2.QtWebEngineWidgets 时联动保留 resources、qtwebengine plugins 与 qml.

        WebEngineWidgets 闭包含 Quick（Qt6WebEngineWidgets.dll C 层依赖 Qt6Quick.dll），
        Quick 在 ``_QT_QML_DEPS`` 中，故 qml/ 目录联动保留。
        """
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
        # qml 依赖 Qml/Quick，WebEngineWidgets 闭包含 Quick → 联动保留
        assert (dest / "PySide2" / "qml" / "QtQuick.2" / "qmldir").is_file()
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

    def test_qt_auxiliary_dll_with_deps_kept(self, tmp_path: Path) -> None:
        """闭包含 Multimedia/Qml/Quick 时 FFmpeg/QML ABI/opengl32sw DLL 保留."""
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/QtQml.pyd": b"qml",
                "PySide6/QtQuick.pyd": b"quick",
                "PySide6/QtMultimedia.pyd": b"mm",
                "PySide6/Qt6Core.dll": b"c",
                "PySide6/Qt6Qml.dll": b"qml-dll",
                "PySide6/Qt6Multimedia.dll": b"mm-dll",
                # FFmpeg 系列（仅 Multimedia 闭包内保留）
                "PySide6/avcodec-61.dll": b"avcodec",
                "PySide6/avformat-61.dll": b"avformat",
                # QML 绑定层 ABI DLL（仅 Qml 闭包内保留）
                "PySide6/pyside6qml.abi3.dll": b"qml-abi",
                # opengl32sw.dll（仅 OpenGL 相关模块闭包内保留，Quick/Multimedia 都算）
                "PySide6/opengl32sw.dll": b"sw",
                # VC++ 运行时（始终保留）
                "PySide6/msvcp140.dll": b"msvcp",
                # 绑定层（始终保留）
                "PySide6/pyside6.abi3.dll": b"abi",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack(
            [whl],
            dest,
            {"PySide6": frozenset({"QtCore", "Qml", "Quick", "Multimedia"})},
        )
        assert count == 1
        # FFmpeg 系列：闭包含 Multimedia → 保留
        assert (dest / "PySide6" / "avcodec-61.dll").is_file()
        assert (dest / "PySide6" / "avformat-61.dll").is_file()
        # QML ABI DLL：闭包含 Qml → 保留
        assert (dest / "PySide6" / "pyside6qml.abi3.dll").is_file()
        # opengl32sw.dll：闭包含 Quick/Multimedia → 保留
        assert (dest / "PySide6" / "opengl32sw.dll").is_file()
        # VC++ 运行时始终保留
        assert (dest / "PySide6" / "msvcp140.dll").is_file()
        # 绑定层始终保留
        assert (dest / "PySide6" / "pyside6.abi3.dll").is_file()

    def test_qt_auxiliary_dll_without_deps_excluded(self, tmp_path: Path) -> None:
        """闭包不含 Multimedia/Qml/OpenGL 模块时 FFmpeg/QML ABI/opengl32sw DLL 剥离.

        用纯 Widgets 场景测试（不含 WebEngine，避免 WebEngineWidgets 闭包扩展
        引入 Quick/Qml 导致 opengl32sw.dll 与 pyside6qml.abi3.dll 联动保留）。
        WebEngine 场景的联动保留由 ``test_qt_webengine_dynamic_expansion`` 覆盖。
        """
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/QtGui.pyd": b"gui",
                "PySide6/QtWidgets.pyd": b"widgets",
                "PySide6/Qt6Core.dll": b"c",
                "PySide6/Qt6Gui.dll": b"g",
                "PySide6/Qt6Widgets.dll": b"w",
                # FFmpeg 系列（无 Multimedia → 剥离）
                "PySide6/avcodec-61.dll": b"avcodec",
                "PySide6/avformat-61.dll": b"avformat",
                "PySide6/avutil-59.dll": b"avutil",
                "PySide6/swscale-8.dll": b"swscale",
                "PySide6/swresample-5.dll": b"swresample",
                # QML 绑定层 ABI DLL（无 Qml → 剥离）
                "PySide6/pyside6qml.abi3.dll": b"qml-abi",
                # opengl32sw.dll（无 OpenGL 相关模块 → 剥离）
                "PySide6/opengl32sw.dll": b"sw",
                # VC++ 运行时（始终保留）
                "PySide6/msvcp140.dll": b"msvcp",
                # 绑定层（始终保留）
                "PySide6/pyside6.abi3.dll": b"abi",
                # metatypes 目录（始终剥离）
                "PySide6/metatypes/qt6core_metatypes.json": b"mt",
                # lib/cmake/ 三级子目录（剥离）
                "PySide6/lib/cmake/PySide6/PySide6Config.cmake": b"cmake",
                # QtAsyncio 目录（始终剥离）
                "PySide6/QtAsyncio/__init__.py": b"asyncio",
            },
        )
        dest = tmp_path / "sp"
        count = slim_unpack(
            [whl],
            dest,
            {"PySide6": frozenset({"QtCore", "QtGui", "QtWidgets"})},
        )
        assert count == 1
        # 基础子模块保留
        assert (dest / "PySide6" / "QtCore.pyd").is_file()
        assert (dest / "PySide6" / "QtWidgets.pyd").is_file()
        # FFmpeg 系列：闭包无 Multimedia → 剥离
        assert not (dest / "PySide6" / "avcodec-61.dll").exists()
        assert not (dest / "PySide6" / "avformat-61.dll").exists()
        assert not (dest / "PySide6" / "avutil-59.dll").exists()
        assert not (dest / "PySide6" / "swscale-8.dll").exists()
        assert not (dest / "PySide6" / "swresample-5.dll").exists()
        # QML ABI DLL：闭包无 Qml → 剥离
        assert not (dest / "PySide6" / "pyside6qml.abi3.dll").exists()
        # opengl32sw.dll：闭包无 OpenGL 相关模块 → 剥离
        assert not (dest / "PySide6" / "opengl32sw.dll").exists()
        # VC++ 运行时始终保留
        assert (dest / "PySide6" / "msvcp140.dll").is_file()
        # 绑定层始终保留
        assert (dest / "PySide6" / "pyside6.abi3.dll").is_file()
        # metatypes 目录剥离
        assert not (dest / "PySide6" / "metatypes").exists()
        # lib/cmake/ 剥离
        assert not (dest / "PySide6" / "lib" / "cmake").exists()
        # QtAsyncio 目录剥离
        assert not (dest / "PySide6" / "QtAsyncio").exists()


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
        """非 .pyd/.so 的顶层文件归 shared（.dll/.py/.py.typed 等）。."""
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
        """lxml 顶层 .pyd 按模块名归类（split(".")[0] 去除 ABI 标签）.

        C 扩展文件名格式为 ``<module>.<abi-tag>.pyd``（如
        ``etree.cpython-38-x86_64-linux-gnu.so``），用 ``split(".")[0]`` 取
        ``etree`` 作为模块名，与 AST 收集的子模块名匹配。
        """
        from fspack.slim.libs import LxmlSlimSpec

        result = LxmlSlimSpec.classify_entry("lxml/etree.cpython-38-x86_64-linux-gnu.so", "lxml", set())
        assert result == ("submodule", "etree")

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

    def test_classify_top_pyi_stripped(self) -> None:
        """matplotlib 顶层 .pyi 归 exclude（.pyi 在 STRIP_EXTS 中，运行时不需要）."""
        from fspack.slim.libs import MatplotlibSlimSpec

        assert MatplotlibSlimSpec.classify_entry("matplotlib/pyplot.pyi", "matplotlib", set()) == (
            "exclude",
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


class TestSklearnSlimSpec:
    """scikit-learn 精简规则：剥离 datasets/descr/ 与 datasets/images/，保留 data/."""

    def test_match_sklearn_only(self) -> None:
        from fspack.slim.libs import SklearnSlimSpec

        # wheel 文件名归一化为 scikit-learn，顶层目录归一化为 sklearn
        assert SklearnSlimSpec.match("scikit-learn") is True
        assert SklearnSlimSpec.match("sklearn") is True
        assert SklearnSlimSpec.match("scipy") is False
        assert SklearnSlimSpec.match("numpy") is False

    def test_classify_datasets_descr_excluded(self) -> None:
        """sklearn/datasets/descr/ 描述文件归 exclude（仅 DESCR 文本展示用）."""
        from fspack.slim.libs import SklearnSlimSpec

        assert SklearnSlimSpec.classify_entry("sklearn/datasets/descr/iris.rst", "sklearn", set()) == (
            "exclude",
            None,
        )
        assert SklearnSlimSpec.classify_entry("sklearn/datasets/descr/wine_data.rst", "sklearn", set()) == (
            "exclude",
            None,
        )

    def test_classify_datasets_images_excluded(self) -> None:
        """sklearn/datasets/images/ 示例图片归 exclude（仅 load_sample_image 用）."""
        from fspack.slim.libs import SklearnSlimSpec

        assert SklearnSlimSpec.classify_entry("sklearn/datasets/images/china.jpg", "sklearn", set()) == (
            "exclude",
            None,
        )
        assert SklearnSlimSpec.classify_entry("sklearn/datasets/images/face.jpg", "sklearn", set()) == (
            "exclude",
            None,
        )

    def test_classify_datasets_data_kept(self) -> None:
        """sklearn/datasets/data/ CSV 数据归 shared 保留（load_iris 运行时读取）."""
        from fspack.slim.libs import SklearnSlimSpec

        assert SklearnSlimSpec.classify_entry("sklearn/datasets/data/iris.csv", "sklearn", set()) == (
            "shared",
            None,
        )
        assert SklearnSlimSpec.classify_entry("sklearn/datasets/data/wine_data.csv", "sklearn", set()) == (
            "shared",
            None,
        )

    def test_classify_datasets_module_kept(self) -> None:
        """sklearn/datasets/__init__.py 与 _base.py 归 shared 保留（模块本身可用）."""
        from fspack.slim.libs import SklearnSlimSpec

        assert SklearnSlimSpec.classify_entry("sklearn/datasets/__init__.py", "sklearn", set()) == (
            "shared",
            None,
        )
        assert SklearnSlimSpec.classify_entry("sklearn/datasets/_base.py", "sklearn", set()) == ("shared", None)

    def test_classify_nested_tests_excluded(self) -> None:
        """sklearn/<sub>/tests/ 嵌套 tests 归 exclude（NESTED_TEST_DIRS 自动处理）."""
        from fspack.slim.libs import SklearnSlimSpec

        for sub in ("cluster", "decomposition", "ensemble", "svm", "linear_model"):
            assert SklearnSlimSpec.classify_entry(f"sklearn/{sub}/tests/test_dummy.py", "sklearn", set()) == (
                "exclude",
                None,
            ), f"{sub}/tests 应当剥离"

    def test_classify_runtime_subdir_kept(self) -> None:
        """sklearn 运行时子目录（cluster/decomposition/ensemble 等非 tests）归 shared."""
        from fspack.slim.libs import SklearnSlimSpec

        for subdir in ("cluster", "decomposition", "ensemble", "svm", "linear_model", "metrics"):
            assert SklearnSlimSpec.classify_entry(f"sklearn/{subdir}/_base.py", "sklearn", set()) == (
                "shared",
                None,
            ), f"{subdir} 应当保留"

    def test_classify_top_init_kept(self) -> None:
        """sklearn 顶层 __init__.py 归 shared."""
        from fspack.slim.libs import SklearnSlimSpec

        assert SklearnSlimSpec.classify_entry("sklearn/__init__.py", "sklearn", set()) == ("shared", None)

    def test_classify_dist_info(self) -> None:
        """sklearn dist-info 元数据归 metadata."""
        from fspack.slim.libs import SklearnSlimSpec

        assert SklearnSlimSpec.classify_entry("scikit_learn-1.3.0.dist-info/METADATA", "sklearn", set()) == (
            "metadata",
            None,
        )

    def test_classify_strip_exts_excluded(self) -> None:
        """sklearn .h/.pdb 文件归 exclude（STRIP_EXTS 统一处理）."""
        from fspack.slim.libs import SklearnSlimSpec

        assert SklearnSlimSpec.classify_entry("sklearn/_build_utils/header.h", "sklearn", set()) == (
            "exclude",
            None,
        )
        assert SklearnSlimSpec.classify_entry("sklearn/cluster/_k_means.pdb", "sklearn", set()) == (
            "exclude",
            None,
        )


class TestPyarrowSlimSpec:
    """pyarrow 精简规则：剥离 includes/ C++ 头文件与 Cython 定义目录."""

    def test_match_pyarrow_only(self) -> None:
        from fspack.slim.libs import PyarrowSlimSpec

        assert PyarrowSlimSpec.match("pyarrow") is True
        assert PyarrowSlimSpec.match("lxml") is False
        assert PyarrowSlimSpec.match("numpy") is False

    def test_classify_includes_excluded(self) -> None:
        """pyarrow/includes/ 二级目录归 exclude（C++ 头文件 + Cython 定义）."""
        from fspack.slim.libs import PyarrowSlimSpec

        # .pxd 文件（不在 STRIP_EXTS 中，需本 spec 剥离）
        assert PyarrowSlimSpec.classify_entry("pyarrow/includes/libarrow.pxd", "pyarrow", set()) == (
            "exclude",
            None,
        )
        # .h 文件（已在 STRIP_EXTS 中，本 spec 也剥离整个目录，双重保障）
        assert PyarrowSlimSpec.classify_entry("pyarrow/includes/arrow/api.h", "pyarrow", set()) == (
            "exclude",
            None,
        )

    def test_classify_runtime_module_kept(self) -> None:
        """pyarrow 运行时模块（__init__.py/lib.pyd 等）归 shared 保留."""
        from fspack.slim.libs import PyarrowSlimSpec

        assert PyarrowSlimSpec.classify_entry("pyarrow/__init__.py", "pyarrow", set()) == ("shared", None)
        assert PyarrowSlimSpec.classify_entry("pyarrow/lib.pyd", "pyarrow", set()) == ("shared", None)
        assert PyarrowSlimSpec.classify_entry("pyarrow/array.py", "pyarrow", set()) == ("shared", None)

    def test_classify_top_pyd_always_shared(self) -> None:
        """pyarrow 顶层 .pyd 归 shared 始终保留（top_ext_always_shared=True）.

        pyarrow 的顶层 C 扩展（lib.pyd/_compute.pyd 等）是 __init__ 硬依赖，
        剥离即 ImportError，故全部归 shared 不做子模块选择性剥离。
        """
        from fspack.slim.libs import PyarrowSlimSpec

        # lib.pyd（非 _ 开头）归 shared（top_ext_always_shared=True 覆盖 submodule）
        assert PyarrowSlimSpec.classify_entry("pyarrow/lib.pyd", "pyarrow", set()) == ("shared", None)
        # _compute.pyd（_ 开头）归 shared（私有模块 + top_ext_always_shared 双重保障）
        assert PyarrowSlimSpec.classify_entry("pyarrow/_compute.pyd", "pyarrow", set()) == ("shared", None)

    def test_classify_nested_tests_excluded(self) -> None:
        """pyarrow/<sub>/tests/ 嵌套 tests 归 exclude（NESTED_TEST_DIRS 自动处理）."""
        from fspack.slim.libs import PyarrowSlimSpec

        assert PyarrowSlimSpec.classify_entry("pyarrow/tests/test_array.py", "pyarrow", set()) == (
            "exclude",
            None,
        )
        assert PyarrowSlimSpec.classify_entry("pyarrow/parquet/tests/test_parquet.py", "pyarrow", set()) == (
            "exclude",
            None,
        )

    def test_classify_dist_info(self) -> None:
        """pyarrow dist-info 元数据归 metadata."""
        from fspack.slim.libs import PyarrowSlimSpec

        assert PyarrowSlimSpec.classify_entry("pyarrow-14.0.0.dist-info/METADATA", "pyarrow", set()) == (
            "metadata",
            None,
        )

    def test_classify_common_excluded(self) -> None:
        """pyarrow 通用剥离目录（examples/docs 等）归 exclude."""
        from fspack.slim.libs import PyarrowSlimSpec

        for subdir in ("examples", "docs", "doc"):
            assert PyarrowSlimSpec.classify_entry(f"pyarrow/{subdir}/dummy.py", "pyarrow", set()) == (
                "exclude",
                None,
            ), f"{subdir} 应当剥离"


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

    def test_nested_excludes_empty_still_strip_default_tests(self) -> None:
        """nested_excludes 为空时仍剥离嵌套 tests（基类 NESTED_TEST_DIRS 自动合并）.

        基类 :attr:`SlimSpec.NESTED_TEST_DIRS` 默认含 ``"tests"``，即使
        ``nested_excludes`` 参数为空也会剥离任意层级的 tests 目录。
        """
        from fspack.slim.base import SlimSpec

        # 基类 NESTED_TEST_DIRS 自动剥离 scipy/linalg/tests/
        assert SlimSpec._default_classify("scipy/linalg/tests/x.py", "scipy", set(), frozenset(), frozenset()) == (
            "exclude",
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
            "ft2font",  # split(".")[0] 去除 ABI 标签，与 AST 收集的子模块名匹配
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

    def test_get_spec_sklearn_pyarrow(self) -> None:
        """scikit-learn/pyarrow 走专门的 spec（非默认兜底）."""
        from fspack.slim import get_spec
        from fspack.slim.libs import PyarrowSlimSpec, SklearnSlimSpec

        assert get_spec("scikit-learn") is SklearnSlimSpec
        assert get_spec("pyarrow") is PyarrowSlimSpec

    def test_classify_entry_dispatches_to_sklearn(self) -> None:
        """classify_entry 按 top_pkg 归一化名分发到 SklearnSlimSpec。."""
        # sklearn/datasets/descr/ 剥离验证分发到 sklearn spec
        assert classify_entry("sklearn/datasets/descr/iris.rst", "sklearn") == ("exclude", None)

    def test_classify_entry_dispatches_to_pyarrow(self) -> None:
        """classify_entry 按 top_pkg 归一化名分发到 PyarrowSlimSpec。."""
        # pyarrow/includes/ 剥离验证分发到 pyarrow spec
        assert classify_entry("pyarrow/includes/libarrow.pxd", "pyarrow") == ("exclude", None)

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

    def test_stage_records_saved_bytes(self, tmp_path: Path) -> None:
        """slim_unpack 累加各 wheel 节省字节数到 stage.add_saved_bytes."""
        from fspack.progress import BuildTracker

        # PySide6 wheel：闭包内仅 QtCore，剥离 Qt3DCore.pyd/designer.exe/examples/dummy.py
        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/Qt3DCore.pyd": b"3d",
                "PySide6/designer.exe": b"tool",
                "PySide6/examples/dummy.py": b"example",
            },
        )
        dest = tmp_path / "sp"
        tracker = BuildTracker()
        with tracker.stage("解压 wheel(精简)") as stage:
            slim_unpack([whl], dest, {"PySide6": frozenset({"QtCore"})}, stage=stage)
        # 剥离 3 个文件（Qt3DCore.pyd + designer.exe + examples/dummy.py）
        # 各文件内容字节数：b"3d"(2) + b"tool"(4) + b"example"(7) = 13 字节
        assert tracker.records[0].bytes_saved == 13

    def test_stage_saved_bytes_zero_when_nothing_stripped(self, tmp_path: Path) -> None:
        """无可剥离文件时 stage.bytes_saved 为 0."""
        from fspack.progress import BuildTracker

        whl = tmp_path / "wh" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl.parent.mkdir()
        _make_wheel(
            whl,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
            },
        )
        dest = tmp_path / "sp"
        tracker = BuildTracker()
        with tracker.stage("解压 wheel(精简)") as stage:
            slim_unpack([whl], dest, {"PySide6": frozenset({"QtCore"})}, stage=stage)
        # 闭包含 QtCore，无剥离文件
        assert tracker.records[0].bytes_saved == 0

    def test_stage_saved_bytes_aggregates_multiple_wheels(self, tmp_path: Path) -> None:
        """多 wheel 解压时节省字节数累加."""
        from fspack.progress import BuildTracker

        whl1 = tmp_path / "wh1" / "PySide6-6.5.0-cp39-none-win_amd64.whl"
        whl1.parent.mkdir()
        _make_wheel(
            whl1,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCore.pyd": b"core",
                "PySide6/Qt3DCore.pyd": b"3d",  # 剥离 2 字节
            },
        )
        whl2 = tmp_path / "wh2" / "PySide6_Addons-6.5.0-cp39-none-win_amd64.whl"
        whl2.parent.mkdir()
        _make_wheel(
            whl2,
            {
                "PySide6/__init__.py": b"",
                "PySide6/QtCharts.pyd": b"charts",  # 剥离 6 字节
            },
        )
        dest = tmp_path / "sp"
        tracker = BuildTracker()
        with tracker.stage("解压 wheel(精简)") as stage:
            slim_unpack([whl1, whl2], dest, {"PySide6": frozenset({"QtCore"})}, stage=stage)
        # 累加：2 + 6 = 8 字节
        assert tracker.records[0].bytes_saved == 8

    def test_parallel_unpack_matches_serial(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """并行解压与串行解压产出文件集合一致.

        强制走并行路径（阈值调到 1），验证共享目录树 wheel（PySide6 拆分）
        在并行解压时无文件丢失、无 FileExistsError 崩溃。
        """
        from fspack import slim
        from fspack.slim.base import _unpack_wheel_dispatch

        # 构造 3 个共享 PySide6/ 顶层目录的拆分 wheel
        wheels_data = [
            ("PySide6-6.5.0-cp39-none-win_amd64.whl", {"PySide6/__init__.py": b"", "PySide6/QtCore.pyd": b"core"}),
            (
                "PySide6_Essentials-6.5.0-cp39-none-win_amd64.whl",
                {"PySide6/__init__.py": b"", "PySide6/QtGui.pyd": b"gui"},
            ),
            (
                "PySide6_Addons-6.5.0-cp39-none-win_amd64.whl",
                {"PySide6/__init__.py": b"", "PySide6/QtCharts.pyd": b"charts"},
            ),
        ]
        wheels: list[Path] = []
        for i, (name, entries) in enumerate(wheels_data):
            whl = tmp_path / f"wh{i}" / name
            whl.parent.mkdir()
            _make_wheel(whl, entries)
            wheels.append(whl)

        merged = {"pyside6": {"Core", "Gui", "Charts"}}

        # 串行解压
        serial_dest = tmp_path / "serial"
        serial_dest.mkdir()
        for whl in wheels:
            _unpack_wheel_dispatch(whl, serial_dest, merged, DEFAULT_SLIM_RULES)

        # 并行解压（强制走并行路径）
        monkeypatch.setattr(slim.base, "_PARALLEL_WHEEL_THRESHOLD", 1)
        parallel_dest = tmp_path / "parallel"
        parallel_dest.mkdir()
        slim_unpack(wheels, parallel_dest, {"PySide6": frozenset({"Core", "Gui", "Charts"})})

        # 比较两个目录的文件集合（相对路径）
        serial_files = {p.relative_to(serial_dest).as_posix() for p in serial_dest.rglob("*") if p.is_file()}
        parallel_files = {p.relative_to(parallel_dest).as_posix() for p in parallel_dest.rglob("*") if p.is_file()}
        assert serial_files == parallel_files


class TestParallelMapWithProgress:
    """parallel_map_with_progress 单元测试."""

    def test_empty_items_returns_empty(self) -> None:
        """空 items 列表返回空结果，不创建线程池."""
        from fspack.progress import parallel_map_with_progress

        result = parallel_map_with_progress([], lambda x: x, "空任务")
        assert result == []

    def test_single_item(self) -> None:
        """单 item 正常执行并返回结果."""
        from fspack.progress import parallel_map_with_progress

        result = parallel_map_with_progress([42], lambda x: x * 2, "单任务")
        assert result == [84]

    def test_multiple_items_all_collected(self) -> None:
        """多 item 结果全部收集（顺序可能不同，但内容一致）."""
        from fspack.progress import parallel_map_with_progress

        result = parallel_map_with_progress(list(range(10)), lambda x: x**2, "平方")
        assert sorted(result) == [i**2 for i in range(10)]

    def test_exception_propagates(self) -> None:
        """worker 抛异常时在 future.result() 重新抛出，与串行一致."""

        from fspack.progress import parallel_map_with_progress

        def fail(x: int) -> int:
            if x == 3:
                raise ValueError("故意失败")
            return x

        with pytest.raises(ValueError, match="故意失败"):
            parallel_map_with_progress([1, 2, 3, 4, 5], fail, "含失败")

    def test_stage_processed_incremented(self) -> None:
        """stage.processed() 按完成项数累加."""
        from fspack.progress import BuildTracker, parallel_map_with_progress

        tracker = BuildTracker()
        with tracker.stage("并行任务") as stage:
            parallel_map_with_progress([1, 2, 3, 4, 5], lambda x: x * 2, "并行", stage=stage)
        assert tracker.records[0].items == 5


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
            ".pyi",
            ".exe",
        )
        for ext in strip_exts:
            assert SlimSpec._is_strip_ext(f"foo{ext}") is True, f"{ext} 应当剥离"
        # 不在 STRIP_EXTS 中的扩展名
        for ext in (".py", ".pyd", ".dll", ".so", ".json", ".txt"):
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

    def test_default_spec_pyi_stripped(self) -> None:
        """.pyi 受 STRIP_EXTS 影响（运行时不需要，归 exclude）."""
        assert classify_entry("mypkg/core.pyi", "mypkg") == ("exclude", None)

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
