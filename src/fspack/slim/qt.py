"""Qt 库精简规则：白名单 + 子模块依赖闭包.

facade 模块：编排 :mod:`fspack.slim.qt_helpers`（文件名归一化与判断）与
:mod:`fspack.slim.qt_closure`（依赖映射与闭包计算）实现 Qt 库精简规则。
本模块保留 :class:`QtSlimSpec` 的 ``classify_entry`` 入口逻辑。

适用于 PySide2/PySide6/PyQt5/PyQt6。采用白名单+动态扩展机制：

- 基础依赖白名单：``__init__.py``、``_*.py``、``pyside2.abi3.dll``、VC++ 运行时、
  ``plugins/platforms``、``plugins/imageformats``、``plugins/styles`` 等基础插件
  （``imageformats/qsvg*.dll`` 例外：依赖 ``Svg`` 子模块，仅 ``Svg`` 在闭包内时保留）
- 子模块动态扩展：根据源码 import 的子模块（如 ``PySide2.QtMultimedia``），
  保留对应 ``.pyd`` 与 ``Qt5Xxx.dll``/``Qt6Xxx.dll``，并按依赖映射保留
  相关 plugins（如 ``plugins/mediaservice``）与 resources
  （``.pyi`` 已由 :attr:`SlimSpec.STRIP_EXTS` 统一剥离）
- 非必要目录剥离：``examples``/``translations``/``include``/``typesystems``/
  ``metatypes``/``lib``/``QtAsyncio`` 等始终跳过
- 按需加载的辅助 DLL 智能识别：
  - FFmpeg 系列（``avcodec-*``/``avformat-*`` 等）仅 Multimedia 闭包内保留
  - ``pyside6qml.abi3.dll``/``pyside2qml.abi3.dll`` 仅 Qml 闭包内保留
  - ``opengl32sw.dll`` 仅 OpenGL 相关模块（OpenGL/Quick/Multimedia 等）闭包内保留
- WebEngine DevTools 调试资源（``*.debug.pak``/``*.debug.bin``）始终剥离

依赖闭包：用户 ``import QtWidgets`` 时自动加入 ``Gui``/``Core``（C 层链接依赖，
AST 无法发现），无需用户显式声明或 ``--keep-module``。
"""

from __future__ import annotations

from pathlib import Path

from fspack._compat import override
from fspack.slim.base import SlimSpec, normalize_name
from fspack.slim.qt_closure import (
    _QT_ABI_DLL_DEPS,
    _QT_ABI_DLL_PACKAGES,
    _QT_OPENGL_DEPS,
    _QT_PLUGIN_DEPS,
    _QT_QML_DEPS,
    _QT_RESOURCE_DEPS,
    _QT_WEBENGINE_TOP_FILES,
    _qt_module_closure,
)
from fspack.slim.qt_helpers import (
    _QT_EXCLUDE_SUBDIRS,
    _QT_LIB_EXCLUDE_SUBDIRS,
    _is_ffmpeg_dll,
    _is_opengl_sw_dll,
    _is_qml_abi_dll,
    _normalize_qt_sub,
    _qt_dll_submodule,
)

__all__ = [
    "QT_PACKAGES",
    "QtSlimSpec",
    "_is_ffmpeg_dll",
    "_is_opengl_sw_dll",
    "_is_qml_abi_dll",
    "_normalize_qt_sub",
    "_qt_dll_submodule",
    "_qt_module_closure",
]

# Qt 库归一化包名集合
QT_PACKAGES = frozenset({"pyside2", "pyside6", "pyqt5", "pyqt6"})

# QtWebEngine 顶层文件名小写集合：zip 条目大小写不保证与发布名一致
# （部分 wheel 打包工具保留原始大小写变体），比较前统一 lower。
_WEBENGINE_TOP_FILES_LOWER = frozenset(f.lower() for f in _QT_WEBENGINE_TOP_FILES)


class QtSlimSpec(SlimSpec):
    """Qt 库精简规则：PySide2/PySide6/PyQt5/PyQt6 共享同一规则。

    白名单 + 子模块依赖闭包：用户 ``import QtWidgets`` 自动加入 ``Gui``/``Core``，
    闭包内的 ``.pyd`` 与 ``Qt5/6*.dll`` 保留；abi3.dll 隐式依赖的 Qml/Network DLL
    归 shared 始终保留（避免误保留 qml/ 资源目录）。
    """

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:
        """匹配 Qt 库归一化包名（pyside2/pyside6/pyqt5/pyqt6）."""
        return whl_pkg in QT_PACKAGES

    @classmethod
    @override
    def normalize_submodule(cls, sub: str) -> str:
        """Qt 子模块名归一化（``QtCore``/``Qt5Core`` → ``Core``）."""
        return _normalize_qt_sub(sub)

    @classmethod
    @override
    def expand_closure(cls, subs: set[str]) -> set[str]:
        """Qt 子模块依赖闭包扩展（构造新集合返回，不修改入参 ``subs``）.

        与基类"返回副本"约定一致：先复制入参再扩展，调用方入参保持原样。
        调用方（:func:`fspack.slim.unpack.slim_unpack`）自行 ``update`` 累积
        闭包结果。

        - **QtWidgets 始终保留**：QtWidgets 是 Qt GUI 基础依赖，QML 的
          Controls 1.x/Dialogs 插件（``qtquickcontrolsplugin.dll``/
          ``dialogplugin.dll``）C 层依赖 ``Qt5Widgets.dll``/``Qt6Widgets.dll``，
          qml 目录整体保留须保留 Widgets 配套。故任何 Qt 模块在闭包中时自动
          加入 ``Widgets``，避免插件保留但依赖 DLL 被剥离。
        - **QML 项目自动加入 ``Svg``**：QML 无 ``import QtSvg`` 语法（QtSvg 是
          C++ 模块），但 ``Image { source: "*.svg" }`` 通过 imageformats 插件
          加载 SVG，``plugins/imageformats/qsvg.dll`` 保留需 ``Qt5Svg.dll``/
          ``Qt6Svg.dll`` 配套。故 ``Qml`` 在闭包中时自动加入 ``Svg``。
        """
        merged = set(subs)
        if merged:
            merged.add("Widgets")
        if "Qml" in merged:
            merged.add("Svg")
        merged.update(_qt_module_closure(merged))
        return merged

    @classmethod
    @override
    def classify_entry(  # noqa: PLR0911, PLR0912
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """Qt 库条目分类。

        - ``.exe``/``.h``/``.cpp``/``.lib`` 等 STRIP_EXTS 扩展名 → exclude
          （由 :meth:`_classify_top_or_meta` 统一处理，含跨包）
        - 顶层 ``QtWebEngineProcess[.exe]``/``icudtl.dat`` → 仅 WebEngine 子模块
          保留时归 shared（须在 STRIP_EXTS 之前拦截，避免 .exe 误剥离）
        - 顶层 ``.pyd``/``.so`` → submodule（归一化子模块名；``.pyi`` 已由
          :meth:`_is_strip_ext` 在更早阶段统一剥离）；例外：``sip.<abi>.pyd``
          （pyqt5-sip/pyqt6-sip 提供的私有 sip 运行时，扩展模块 C 层导入）
          → shared 始终保留
        - 顶层 ``Qt5Xxx.dll``/``Qt6Xxx.dll`` → submodule（归一化子模块名）；
          PySide2/PySide6 的 abi3.dll 隐式依赖 Qml/Network DLL → 归 shared
        - 顶层 FFmpeg 系列 DLL（``avcodec-*``/``avformat-*`` 等）→ submodule，
          按 ``Multimedia`` 子模块选择性保留（仅 Multimedia 闭包内加载）
        - 顶层 ``pyside6qml.abi3.dll``/``pyside2qml.abi3.dll`` → submodule，
          按 ``Qml`` 子模块选择性保留（QML 绑定层，非 QML 应用不需要）
        - 顶层 ``opengl32sw.dll`` → 仅 ``_QT_OPENGL_DEPS`` 任一模块在闭包内时
          归 shared 保留；纯 Widgets/WebEngine 应用剥离（约 20MB 节省）
        - 其他非 Qt5/Qt6 前缀 DLL → shared（VC++ 运行时等）
        - 子目录 ``examples``/``translations``/``include``/``metatypes``/
          ``QtAsyncio`` 等 → exclude；``lib/cmake/`` 三级子目录剥离（cmake 配置），
          ``lib/`` 其他内容保留（PySide2 ``lib/fonts/`` 含 Qt 内嵌字体）
        - ``plugins/<subdir>/<files>`` → 按依赖映射保留/剥离，未知子目录剥离；
          ``imageformats/qsvg*.dll`` 例外：依赖 ``Svg`` 子模块，仅 ``Svg`` 在闭包
          内时保留（避免插件保留但 ``Qt5Svg.dll``/``Qt6Svg.dll`` 被剥离）
        - ``resources/`` → 仅 WebEngine 相关子模块时保留；内部含 ``.debug.`` 子串
          的文件（``*.debug.pak``/``*.debug.bin``）是 DevTools 调试资源，始终剥离
        - ``qml/`` → 仅 Qml/Quick 相关子模块时保留
        - 其他 → shared
        """
        parts = entry.split("/")

        # QtWebEngine 顶层文件特殊处理：须在 _classify_top_or_meta 之前拦截，
        # 否则 QtWebEngineProcess.exe 会被 STRIP_EXTS 中的 .exe 规则误剥离。
        # 仅当保留任一 WebEngine 子模块时保留，避免非 WebEngine 应用携带冗余 ICU 数据。
        # 大小写不敏感比较：zip 条目名大小写不保证与发布名一致。
        if len(parts) == 2 and parts[1].lower() in _WEBENGINE_TOP_FILES_LOWER:
            if _QT_RESOURCE_DEPS & keep_subs:
                return ("shared", None)
            return ("exclude", None)

        common = cls._classify_top_or_meta(entry, top_pkg, parts)
        if common is not None:
            return common

        is_abi_pkg = normalize_name(top_pkg) in _QT_ABI_DLL_PACKAGES

        # 顶层文件（parts == 2）
        if len(parts) == 2:
            filename = parts[1]
            if filename.startswith("__init__.") or filename.startswith("_"):
                return ("shared", None)
            suffix = Path(filename).suffix.lower()
            # C 扩展文件名格式为 ``<module>.<abi-tag>.pyd``，用 split(".")[0] 取模块名
            # （Path.stem 会保留 ABI 标签导致与 keep_subs 不匹配，详见 base.py 同处注释）
            stem = filename.split(".")[0]
            if suffix in cls.SUBMODULE_EXTS:
                # PyQt5/PyQt6 的私有 sip 运行时（pyqt5-sip/pyqt6-sip wheel 提供的
                # ``PyQt5/sip.<abi>.pyd``）由各扩展模块 C 层导入（``__init__.py``
                # 不 import，AST 无法发现，二进制内嵌 ``PyQt5.sip`` 字样），
                # 归 shared 始终保留——否则运行时 ``import PyQt5.QtWidgets``
                # 直接 ModuleNotFoundError: No module named 'PyQt5.sip'
                if stem == "sip":
                    return ("shared", None)
                # .pyd/.so 按归一化子模块名选择性保留（.pyi 已被 STRIP_EXTS 剥离）
                return ("submodule", _normalize_qt_sub(stem))
            if suffix == ".dll":
                # Qt5Xxx.dll/Qt6Xxx.dll 按子模块选择性保留
                qt_sub = _qt_dll_submodule(stem)
                if qt_sub is not None:
                    # PySide2/PySide6 的 abi3.dll 隐式依赖 Qml/Network 的 DLL → 归 shared
                    # 始终保留（AST 无法发现此 C 层依赖）；.pyd 仍按子模块选择性保留
                    if is_abi_pkg and qt_sub in _QT_ABI_DLL_DEPS:
                        return ("shared", None)
                    return ("submodule", qt_sub)
                # FFmpeg 系列 DLL（avcodec-61.dll 等）→ 按 Multimedia 子模块选择性保留
                # 仅 Qt6Multimedia.dll 运行时加载，Multimedia 不在闭包内时剥离
                if _is_ffmpeg_dll(filename):
                    return ("submodule", "Multimedia")
                # QML 绑定层 ABI DLL → 按 Qml 子模块选择性保留
                if _is_qml_abi_dll(filename):
                    return ("submodule", "Qml")
                # opengl32sw.dll → 按 OpenGL 相关模块闭包智能保留
                # 检查 keep_subs 与 _QT_OPENGL_DEPS 的交集（任一模块在闭包内即保留）
                if _is_opengl_sw_dll(filename):
                    if _QT_OPENGL_DEPS & keep_subs:
                        return ("shared", None)
                    return ("exclude", None)
                return ("shared", None)
            return ("shared", None)

        # 子目录（len(parts) >= 3）
        subdir = parts[1]
        if subdir in cls.COMMON_EXCLUDE_SUBDIRS or subdir in _QT_EXCLUDE_SUBDIRS:
            return ("exclude", None)
        # lib/cmake/ 三级子目录剥离（cmake 配置文件，构建系统用，运行时不需要）。
        # lib/ 本身不剥离（PySide2 的 lib/fonts/ 含 Qt 内嵌字体，运行时需要）。
        if subdir == "lib" and len(parts) >= 4 and parts[2] in _QT_LIB_EXCLUDE_SUBDIRS:
            return ("exclude", None)
        if subdir == "plugins" and len(parts) >= 4:
            plugin_type = parts[2]
            deps = _QT_PLUGIN_DEPS.get(plugin_type)
            if deps is None:
                # 未知 plugins 子目录，白名单制剥离
                return ("exclude", None)
            # imageformats 中的 qsvg*.dll 是 SVG 图片格式插件，C 层依赖
            # Qt5Svg.dll/Qt6Svg.dll，按 Svg 子模块选择性保留；其余 imageformats
            # 插件（qjpeg/qgif/qico 等）无外部 Qt DLL 依赖，始终保留。避免
            # "qsvg.dll 保留但 Qt5Svg.dll 被剥离"导致插件加载失败。
            if plugin_type == "imageformats":
                filename = parts[-1].lower()
                if filename.startswith("qsvg"):
                    return ("shared", None) if "Svg" in keep_subs else ("exclude", None)
            if not deps:
                # 空依赖集合 = 基础功能，始终保留
                return ("shared", None)
            if deps & keep_subs:
                return ("shared", None)
            return ("exclude", None)
        if subdir == "resources":
            if not (_QT_RESOURCE_DEPS & keep_subs):
                return ("exclude", None)
            # WebEngine 资源中含 ``.debug.`` 子串的文件是 Chromium DevTools 调试资源，
            # 运行时不需要（ref/RimSort qtwebengine_devtools_resources.debug.pak 浪费
            # 74MB；v8_context_snapshot.debug.bin 浪费 2.3MB），始终剥离。
            # 非 .debug.* 资源（如 icudtl.dat 副本、qtwebengine_resources.pak）保留。
            if ".debug." in parts[-1].lower():
                return ("exclude", None)
            return ("shared", None)
        if subdir == "qml":
            if _QT_QML_DEPS & keep_subs:
                return ("shared", None)
            return ("exclude", None)
        return ("shared", None)
