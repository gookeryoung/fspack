"""Qt 库精简规则：白名单 + 子模块依赖闭包。

适用于 PySide2/PySide6/PyQt5/PyQt6。采用白名单+动态扩展机制：

- 基础依赖白名单：``__init__.py``、``_*.py``、``pyside2.abi3.dll``、VC++ 运行时、
  ``plugins/platforms``、``plugins/imageformats``、``plugins/styles`` 等基础插件
- 子模块动态扩展：根据源码 import 的子模块（如 ``PySide2.QtMultimedia``），
  保留对应 ``.pyd``/``.pyi`` 与 ``Qt5Xxx.dll``/``Qt6Xxx.dll``，并按依赖映射保留
  相关 plugins（如 ``plugins/mediaservice``）与 resources
- 非必要目录剥离：``examples``/``translations``/``include``/``typesystems`` 等始终跳过

依赖闭包：用户 ``import QtWidgets`` 时自动加入 ``Gui``/``Core``（C 层链接依赖，
AST 无法发现），无需用户显式声明或 ``--keep-module``。
"""

from __future__ import annotations

from pathlib import Path

from fspack.slim.base import SlimSpec, override, register_spec
from fspack.wheel_cache import normalize_name

__all__ = [
    "QT_PACKAGES",
    "QtSlimSpec",
    "_normalize_qt_sub",
    "_qt_dll_submodule",
    "_qt_module_closure",
]

# Qt 库归一化包名集合
QT_PACKAGES = frozenset({"pyside2", "pyside6", "pyqt5", "pyqt6"})

# 含 ABI 绑定 DLL（pyside2.abi3.dll/pyside6.abi3.dll）的 Qt 包。
# 这些绑定层归 shared 始终保留，但其 C 层隐式依赖 Qt5Qml.dll/Qt6Qml.dll（AST 无法发现），
# 而 Qml.dll 又传递依赖 Network.dll。这些 DLL 在 classify_entry 中归 shared 始终保留，
# 不通过子模块保留集合处理——避免误保留 qml/ 资源目录（仅运行 QML 应用时才需要）。
# PyQt5/PyQt6 的绑定层（sip）不依赖 Qml/Network，无需处理。
_QT_ABI_DLL_PACKAGES = frozenset({"pyside2", "pyside6"})

# abi3.dll 隐式依赖的 Qt 子模块 DLL（归一化名）。
# Qml.dll 是 abi3.dll 的直接 C 层依赖；Network.dll 是 Qml.dll 的传递依赖。
# 这些 DLL 归 shared 始终保留，对应 .pyd 仍按子模块选择性保留（仅用户 import 时）。
_QT_ABI_DLL_DEPS = frozenset({"Qml", "Network"})

# Qt 库始终剥离的二级子目录（非必要文件）
_QT_EXCLUDE_SUBDIRS = frozenset(
    {
        "examples",  # 示例代码
        "translations",  # 翻译文件（约 29MB）
        "include",  # C 头文件
        "typesystems",  # PySide 类型系统描述
        "glue",  # 内部胶水代码
        "support",  # 内部支持文件
        "scripts",  # 脚本
        "doc",  # 文档
    }
)

# Qt plugins 子目录 → 依赖的子模块（归一化名）
# 空集合表示始终保留（基础功能必需），非空集合表示需任一依赖子模块在保留集合中
_QT_PLUGIN_DEPS: dict[str, frozenset[str]] = {
    # 基础功能，始终保留
    "platforms": frozenset(),  # 窗口系统集成（必需）
    "imageformats": frozenset(),  # 图片格式支持（常用）
    "styles": frozenset(),  # 控件样式
    "platforminputcontexts": frozenset(),  # 输入法
    "platformthemes": frozenset(),  # 平台主题
    "egldeviceintegrations": frozenset(),  # OpenGL EGL 集成
    # 按子模块依赖保留
    "iconengines": frozenset({"Svg"}),
    "mediaservice": frozenset({"Multimedia"}),
    "playlistformats": frozenset({"Multimedia"}),
    "audio": frozenset({"Multimedia"}),
    "video": frozenset({"Multimedia"}),
    "sqldrivers": frozenset({"Sql"}),
    "printsupport": frozenset({"PrintSupport"}),
    "bearer": frozenset({"Network"}),
    "position": frozenset({"Positioning"}),
    "sensors": frozenset({"Sensors"}),
    "scenegraph": frozenset({"Quick"}),
    "graphicaleffects": frozenset({"Quick"}),
    "qmltooling": frozenset({"Qml"}),
    "qml1tooling": frozenset({"Qml"}),
    "canbus": frozenset({"SerialBus"}),
    "scxml": frozenset({"Scxml"}),
    "geometryloaders": frozenset({"3DRender"}),
    "sceneparsers": frozenset({"3DRender"}),
    "renderers": frozenset({"3DRender"}),
    "webview": frozenset({"WebView"}),
    "qtwebengine": frozenset({"WebEngineCore", "WebEngineWidgets", "WebEngine"}),
}

# resources 目录依赖（WebEngine 运行时资源，约 15MB）
_QT_RESOURCE_DEPS = frozenset({"WebEngineCore", "WebEngineWidgets", "WebEngine"})

# qml 目录依赖（QtQml/QtQuick 运行时，约 21MB）
_QT_QML_DEPS = frozenset(
    {"Qml", "Quick", "QuickWidgets", "QuickControls2", "Quick3D", "QuickShapes", "QuickTemplates2"}
)

# Qt 子模块依赖映射（归一化名）：key 为 Qt 子模块名（如 Core/Widgets），value 为该模块
# 直接依赖的其他 Qt 子模块（不含自身）。用于白名单闭包计算——用户 import QtWidgets 时
# 自动加入 Gui/Core（C 层链接依赖，AST 无法发现），无需用户显式声明或 --keep-module。
# 未知模块名（不在映射中）原样保留在闭包结果中，不触发额外依赖推导。
_QT_MODULE_DEPS: dict[str, frozenset[str]] = {
    # 核心三件套
    "Core": frozenset(),
    "Gui": frozenset({"Core"}),
    "Widgets": frozenset({"Gui", "Core"}),
    # 网络/通信
    "Network": frozenset({"Core"}),
    "WebSockets": frozenset({"Core"}),
    "WebChannel": frozenset({"Core"}),
    "RemoteObjects": frozenset({"Core"}),
    # 数据/格式
    "Sql": frozenset({"Core"}),
    "Xml": frozenset({"Core"}),
    "XmlPatterns": frozenset({"Core", "Network"}),
    "Svg": frozenset({"Gui", "Core"}),
    "SvgWidgets": frozenset({"Svg", "Widgets", "Gui", "Core"}),
    "PrintSupport": frozenset({"Widgets", "Gui", "Core"}),
    # 多媒体
    "Multimedia": frozenset({"Gui", "Core", "Network"}),
    "MultimediaWidgets": frozenset({"Multimedia", "Widgets", "Gui", "Core"}),
    # 并发/测试
    "Concurrent": frozenset({"Core"}),
    "Test": frozenset({"Core"}),
    # OpenGL
    "OpenGL": frozenset({"Gui", "Core"}),
    "OpenGLWidgets": frozenset({"OpenGL", "Widgets", "Gui", "Core"}),
    # QML/Quick
    "Qml": frozenset({"Network", "Core"}),
    "QmlModels": frozenset({"Qml", "Core"}),
    "QmlWorkerScript": frozenset({"Qml", "Core"}),
    "Quick": frozenset({"Qml", "Gui", "Core"}),
    "QuickWidgets": frozenset({"Quick", "Qml", "Widgets", "Gui", "Core"}),
    "Quick3D": frozenset({"Quick", "Gui", "Core"}),
    "QuickShapes": frozenset({"Quick", "Gui", "Core"}),
    "QuickControls2": frozenset({"Quick", "Qml", "Gui", "Core"}),
    "QuickTemplates2": frozenset({"Quick", "Gui", "Core"}),
    "LabsQmlModels": frozenset({"Qml", "Core"}),
    "LabsSettings": frozenset({"Core"}),
    "LabsSharedImage": frozenset({"Gui", "Core"}),
    "LabsWavefrontMesh": frozenset({"Gui", "Core"}),
    "LabsFolderListModel": frozenset({"Qml", "Core"}),
    # 3D
    "3DCore": frozenset({"Core", "Gui", "Network"}),
    "3DRender": frozenset({"3DCore", "Gui", "Core", "Network"}),
    "3DInput": frozenset({"3DCore", "Core"}),
    "3DLogic": frozenset({"3DCore", "Core"}),
    "3DExtras": frozenset({"3DRender", "3DInput", "3DLogic", "3DCore", "Gui", "Core"}),
    "3DAnimation": frozenset({"3DRender", "3DCore", "Core"}),
    # 可视化
    "Charts": frozenset({"Widgets", "Gui", "Core"}),
    "DataVisualization": frozenset({"Gui", "Core"}),
    "DataVisualizationQml": frozenset({"DataVisualization", "Quick", "Qml", "Gui", "Core"}),
    # UI 工具
    "UiTools": frozenset({"Widgets", "Gui", "Core"}),
    "Help": frozenset({"Widgets", "Gui", "Core"}),
    "Designer": frozenset({"Xml", "Widgets", "Gui", "Core"}),
    # Web
    "WebEngine": frozenset({"Network", "Gui", "Core"}),
    "WebEngineCore": frozenset({"Network", "Positioning", "Gui", "Core"}),
    "WebEngineWidgets": frozenset({"WebEngineCore", "Widgets", "Gui", "Core"}),
    "WebEngineQuick": frozenset({"WebEngineCore", "Quick", "Qml", "Gui", "Core"}),
    # 设备/位置
    "Bluetooth": frozenset({"Core"}),
    "Positioning": frozenset({"Core"}),
    "Location": frozenset({"Positioning", "Core"}),
    "Sensors": frozenset({"Core"}),
    "SerialPort": frozenset({"Core"}),
    "SerialBus": frozenset({"Core"}),
    "Nfc": frozenset({"Core"}),
    "Scxml": frozenset({"Core"}),
    "StateMachine": frozenset({"Core"}),
    # 脚本
    "Script": frozenset({"Core"}),
    "ScriptTools": frozenset({"Script", "Core"}),
    # 其他
    "ShaderTools": frozenset({"Gui", "Core"}),
    "Pdf": frozenset({"Core"}),
    "PdfWidgets": frozenset({"Pdf", "Widgets", "Gui", "Core"}),
    "AxContainer": frozenset({"Widgets", "Gui", "Core"}),
    "TextToSpeech": frozenset({"Core"}),
    "VirtualKeyboard": frozenset({"Qml", "Gui", "Core"}),
}


def _normalize_qt_sub(stem: str) -> str:
    """Qt 子模块文件名归一化。

    统一 ``QtCore``/``Qt5Core``/``Qt6Core`` 为 ``Core``，
    ``Qt3DCore``/``Qt53DCore`` 为 ``3DCore``。非 Qt 模块名原样返回。
    """
    for prefix in ("Qt5", "Qt6"):
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    if stem.startswith("Qt") and len(stem) > 2:
        return stem[2:]
    return stem


def _qt_dll_submodule(stem: str) -> str | None:
    """Qt 原生 DLL 文件名提取子模块名。

    ``Qt5Core`` → ``Core``，``Qt6Gui`` → ``Gui``，``Qt53DRender`` → ``3DRender``。
    非 Qt5/Qt6 前缀返回 None（如 ``pyside2.abi3``、``msvcp140``）。
    """
    for prefix in ("Qt5", "Qt6"):
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    return None


def _qt_module_closure(submodules: set[str]) -> set[str]:
    """计算 Qt 子模块集合的传递依赖闭包（归一化名）。

    输入 Qt 绑定包的子模块名集合（如 ``{Widgets}``），返回包含所有传递依赖的
    闭包集合（如 ``{Widgets, Gui, Core}``）。未知模块名（不在 ``_QT_MODULE_DEPS``
    映射中）原样保留在结果中，但不触发额外依赖推导——这保证未来 Qt 新增模块或
    映射未覆盖场景下，至少保留用户显式 import 的子模块，避免误剥离。
    """
    closure = set(submodules)
    changed = True
    while changed:
        changed = False
        for mod in list(closure):
            deps = _QT_MODULE_DEPS.get(mod)
            if not deps:
                continue
            new_deps = deps - closure
            if new_deps:
                closure.update(new_deps)
                changed = True
    return closure


@register_spec
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
        """Qt 子模块依赖闭包扩展（就地修改 ``subs`` 并返回）。

        与基类约定不同：此处返回 ``subs`` 自身（已在 :func:`_qt_module_closure`
        中就地扩展），调用方据此直接 ``subs.update(...)`` 累积闭包结果。
        """
        closure = _qt_module_closure(subs)
        subs.update(closure)
        return subs

    @classmethod
    @override
    def classify_entry(  # noqa: PLR0911, PLR0912
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """Qt 库条目分类。

        - 顶层 ``.exe`` → exclude（Qt 自带开发工具）
        - 顶层 ``.pyd``/``.pyi``/``.so`` → submodule（归一化子模块名）
        - 顶层 ``Qt5Xxx.dll``/``Qt6Xxx.dll`` → submodule（归一化子模块名）；
          PySide2/PySide6 的 abi3.dll 隐式依赖 Qml/Network DLL → 归 shared
        - 非 Qt5/Qt6 前缀 DLL → shared（VC++ 运行时等）
        - 子目录 ``examples``/``translations``/``include`` 等 → exclude
        - ``plugins/<subdir>/<files>`` → 按依赖映射保留/剥离，未知子目录剥离
        - ``resources/`` → 仅 WebEngine 相关子模块时保留
        - ``qml/`` → 仅 Qml/Quick 相关子模块时保留
        - 其他 → shared
        """
        common = cls._classify_top_or_meta(entry, top_pkg)
        if common is not None:
            return common

        is_abi_pkg = normalize_name(top_pkg) in _QT_ABI_DLL_PACKAGES

        parts = entry.split("/")

        # 顶层文件（parts == 2）
        if len(parts) == 2:
            filename = parts[1]
            if filename.startswith("__init__.") or filename.startswith("_"):
                return ("shared", None)
            suffix = Path(filename).suffix.lower()
            stem = Path(filename).stem
            if suffix == ".exe":
                # Qt 自带开发工具（designer.exe 等），运行时不需要
                return ("exclude", None)
            if suffix in cls.SUBMODULE_EXTS:
                # .pyd/.pyi/.so 按归一化子模块名选择性保留
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
                return ("shared", None)
            return ("shared", None)

        # 子目录（len(parts) >= 3）
        subdir = parts[1]
        if subdir in _QT_EXCLUDE_SUBDIRS:
            return ("exclude", None)
        if subdir == "plugins" and len(parts) >= 4:
            plugin_type = parts[2]
            deps = _QT_PLUGIN_DEPS.get(plugin_type)
            if deps is None:
                # 未知 plugins 子目录，白名单制剥离
                return ("exclude", None)
            if not deps:
                # 空依赖集合 = 基础功能，始终保留
                return ("shared", None)
            if deps & keep_subs:
                return ("shared", None)
            return ("exclude", None)
        if subdir == "resources":
            if _QT_RESOURCE_DEPS & keep_subs:
                return ("shared", None)
            return ("exclude", None)
        if subdir == "qml":
            if _QT_QML_DEPS & keep_subs:
                return ("shared", None)
            return ("exclude", None)
        return ("shared", None)
