"""Qt 子模块依赖映射与闭包计算.

提取自 :mod:`fspack.slim.qt`，按职责拆分。本模块专注于"Qt 子模块依赖
关系建模"——C 层 DLL 链接依赖（AST 无法发现）、plugins/resources/qml
目录依赖映射、传递依赖闭包计算。

公开 API（含私有名，便于 facade 重新导出以保兼容）：

- :func:`_qt_module_closure`：Qt 子模块集合的传递依赖闭包
- :data:`_QT_MODULE_DEPS`：Qt 子模块 → 直接依赖的子模块（归一化名）
- :data:`_QT_PLUGIN_DEPS`：plugins 子目录 → 依赖的子模块
- :data:`_QT_RESOURCE_DEPS`：resources 目录依赖的子模块（WebEngine 资源）
- :data:`_QT_QML_DEPS`：qml 目录依赖的子模块
- :data:`_QT_ABI_DLL_PACKAGES`/data:`_QT_ABI_DLL_DEPS`：abi3.dll 隐式依赖
- :data:`_QT_OPENGL_DEPS`：使用 OpenGL 的子模块（保留 opengl32sw.dll 用）
- :data:`_QT_WEBENGINE_TOP_FILES`：QtWebEngine 运行时必需的顶层文件
"""

from __future__ import annotations

__all__ = [
    "_QT_ABI_DLL_DEPS",
    "_QT_ABI_DLL_PACKAGES",
    "_QT_MODULE_DEPS",
    "_QT_OPENGL_DEPS",
    "_QT_PLUGIN_DEPS",
    "_QT_QML_DEPS",
    "_QT_RESOURCE_DEPS",
    "_QT_WEBENGINE_TOP_FILES",
    "_qt_module_closure",
]

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

# QtWebEngine 运行时必需的顶层文件：仅当保留任一 WebEngine 子模块时保留。
# - QtWebEngineProcess.exe：Chromium 子进程宿主，加载页面时由 QtWebEngineCore 派生
#   （若剥离会致 QtWebEngine 渲染失败）。注意 .exe 在 STRIP_EXTS 中会被默认剥离，
#   故需在 _classify_top_or_meta 之前拦截此特殊文件名。
# - icudtl.dat：ICU 国际化数据（约 10MB），Chromium 内嵌 ICU 必需，非 WebEngine
#   应用无需保留。
# - QtWebEngineProcess.exe 对应 Linux 版本无后缀（ELF），同样按此规则判断。
_QT_WEBENGINE_TOP_FILES = frozenset({"QtWebEngineProcess.exe", "QtWebEngineProcess", "icudtl.dat"})

# qml 目录依赖（QtQml/QtQuick 运行时，约 21MB）
_QT_QML_DEPS = frozenset(
    {"Qml", "Quick", "QuickWidgets", "QuickControls2", "Quick3D", "QuickShapes", "QuickTemplates2"}
)

# 会使用 OpenGL 的 Qt 子模块（归一化名）。
# 这些模块在 C 层链接 OpenGL，运行时可能加载 opengl32sw.dll 作为软件后备。
_QT_OPENGL_DEPS = frozenset(
    {
        "OpenGL",
        "OpenGLWidgets",
        "Quick",
        "Quick3D",
        "QuickShapes",
        "QuickWidgets",
        "Multimedia",
        "Graphs",
        "DataVisualization",
        "DataVisualizationQml",
    }
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
    # Qml 在运行时会加载 QML 插件（qml/QtQml/qmlplugin.dll、qml/QtQuick.2/qtquick2plugin.dll
    # 等），这些插件在 C 层链接 QmlModels 与 QmlWorkerScript（AST 无法发现），故 Qml 直接依赖二者。
    "Qml": frozenset({"QmlModels", "QmlWorkerScript", "Network", "Core"}),
    "QmlModels": frozenset({"Qml", "Core"}),
    "QmlWorkerScript": frozenset({"Qml", "Core"}),
    # QmlMeta：QML 元对象/注册系统，Qt6Quick.dll 隐式依赖（dumpbin 验证）
    "QmlMeta": frozenset({"Qml", "QmlModels", "QmlWorkerScript", "Core"}),
    # Quick 的 C 层 DLL 依赖（dumpbin 验证 Qt6Quick.dll 导入表）：
    # - Qt6OpenGL.dll：Quick 默认用 OpenGL 渲染场景图
    # - Qt6QmlMeta.dll：Quick 元类型注册
    "Quick": frozenset({"QmlModels", "Qml", "QmlMeta", "OpenGL", "Gui", "Core"}),
    "QuickWidgets": frozenset({"Quick", "Qml", "Widgets", "Gui", "Core"}),
    "Quick3D": frozenset({"Quick", "Gui", "Core"}),
    "QuickShapes": frozenset({"Quick", "Gui", "Core"}),
    "QuickControls2": frozenset({"QuickTemplates2", "Quick", "Qml", "Gui", "Core"}),
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
    # WebEngineCore/WebEngineWidgets 的 C 层 DLL 依赖（dumpbin 验证）：
    # - Qt6WebEngineCore.dll 直接导入 Qt6Quick.dll（Chromium 用 QML 渲染）
    # - Qt6WebEngineWidgets.dll 直接导入 Qt6Quick.dll/Qt6QuickWidgets.dll/Qt6PrintSupport.dll
    # 故闭包须含 Quick/QuickWidgets/PrintSupport，否则 .pyd 加载时 DLL load failed。
    "WebEngine": frozenset({"Network", "Gui", "Core"}),
    "WebEngineCore": frozenset({"Network", "Positioning", "Quick", "Gui", "Core"}),
    "WebEngineWidgets": frozenset({"WebEngineCore", "Quick", "QuickWidgets", "PrintSupport", "Widgets", "Gui", "Core"}),
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


def _qt_module_closure(submodules: set[str]) -> set[str]:
    """计算 Qt 子模块集合的传递依赖闭包（归一化名）.

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
