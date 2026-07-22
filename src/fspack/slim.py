"""精简打包：按子模块 import 分析选择性解压 wheel。

Qt 库（PySide2/PySide6/PyQt5/PyQt6）采用白名单+动态扩展机制：

- 基础依赖白名单：``__init__.py``、``_*.py``、``pyside2.abi3.dll``、VC++ 运行时、
  ``plugins/platforms``、``plugins/imageformats``、``plugins/styles`` 等基础插件
- 子模块动态扩展：根据源码 import 的子模块（如 ``PySide2.QtMultimedia``），
  保留对应 ``.pyd``/``.pyi`` 与 ``Qt5Xxx.dll``/``Qt6Xxx.dll``，并按依赖映射保留
  相关 plugins（如 ``plugins/mediaservice``）与 resources
- 非必要目录剥离：``examples``/``translations``/``include``/``typesystems`` 等始终跳过
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Sequence

from fspack.exceptions import DependencyError
from fspack.progress import StageRecorder, iter_with_progress
from fspack.wheel_cache import WheelInfo, normalize_name

__all__ = ["classify_entry", "slim_unpack"]

_logger = logging.getLogger(__name__)

# 子模块扩展名：仅这些文件按子模块名选择性保留
_SUBMODULE_EXTS = frozenset({".pyd", ".pyi", ".so"})

# Qt 库归一化包名集合
_QT_PACKAGES = frozenset({"pyside2", "pyside6", "pyqt5", "pyqt6"})

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


def classify_entry(  # noqa: PLR0911, PLR0912
    entry: str,
    top_pkg: str,
    keep_subs: set[str] | None = None,
) -> tuple[str, str | None]:
    """分类 wheel 条目归属。

    返回 ``(类别, 子模块名|None)``，类别为 ``"metadata"``/``"exclude"``/``"shared"``/``"submodule"``：

    - ``metadata``: ``*.dist-info/**`` 元数据，始终保留
    - ``exclude``: 可安全剥离的非必要文件（Qt 库的 examples/translations/include 等、
      开发工具 exe），始终跳过
    - ``shared``: 包级共享文件（``__init__.py``、``_*.py``、VC++ 运行时、
      ``pyside2.abi3.dll`` 等、基础 plugins），始终保留
    - ``submodule``: 子模块专属文件（``.pyd``/``.pyi``/``.so``、Qt5/Qt6 原生 DLL），
      仅当子模块被 import 时保留

    Qt 库（PySide2/PySide6/PyQt5/PyQt6）启用白名单精简：

    - ``Qt5Xxx.dll``/``Qt6Xxx.dll`` 按子模块选择性保留（``Qt5Core.dll`` ↔ ``Core``）
    - ``plugins/`` 子目录按依赖映射保留（``platforms``/``imageformats`` 始终保留，
      ``mediaservice`` 需 ``Multimedia`` 等）
    - ``resources/`` 仅 WebEngine 相关子模块时保留
    - ``qml/`` 仅 Qml/Quick 相关子模块时保留
    - ``examples``/``translations``/``include``/``typesystems`` 等始终剥离

    ``keep_subs`` 为归一化后的子模块名集合（Qt 库为 ``Core``/``Gui`` 等）。
    """
    parts = entry.split("/")
    if parts[0].endswith(".dist-info"):
        return ("metadata", None)
    if parts[0] != top_pkg:
        return ("shared", None)

    is_qt = normalize_name(top_pkg) in _QT_PACKAGES
    is_abi_pkg = normalize_name(top_pkg) in _QT_ABI_DLL_PACKAGES
    subs = keep_subs or set()

    # 顶层文件（parts == 2）
    if len(parts) == 2:
        filename = parts[1]
        if filename.startswith("__init__.") or filename.startswith("_"):
            return ("shared", None)
        suffix = Path(filename).suffix.lower()
        stem = Path(filename).stem
        if is_qt and suffix == ".exe":
            # Qt 自带开发工具（designer.exe 等），运行时不需要
            return ("exclude", None)
        if suffix in _SUBMODULE_EXTS:
            # .pyd/.pyi/.so 按子模块名选择性保留
            sub = _normalize_qt_sub(stem) if is_qt else stem
            return ("submodule", sub)
        if is_qt and suffix == ".dll":
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
    if is_qt:
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
            if deps & subs:
                return ("shared", None)
            return ("exclude", None)
        if subdir == "resources":
            if _QT_RESOURCE_DEPS & subs:
                return ("shared", None)
            return ("exclude", None)
        if subdir == "qml":
            if _QT_QML_DEPS & subs:
                return ("shared", None)
            return ("exclude", None)
    return ("shared", None)


def _detect_top_pkg(whl: Path, whl_pkg: str) -> str | None:
    """从 wheel 条目中找出与 whl_pkg 归一化名匹配的顶层目录名。

    遍历 wheel 条目，返回第一个 ``normalize_name`` 后等于 ``whl_pkg`` 的目录名。
    无匹配时返回 None（调用方走全量解压）。
    """
    try:
        with zipfile.ZipFile(whl) as zf:
            for name in zf.namelist():
                top = name.split("/")[0]
                if not top.endswith(".dist-info") and normalize_name(top) == whl_pkg:
                    return top
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e
    return None


def _full_unpack(whl: Path, dest: Path) -> None:
    """全量解压单个 wheel 到目标目录。."""
    try:
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(dest)
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e


def _slim_extract(whl: Path, dest: Path, top_pkg: str, keep_subs: set[str]) -> None:
    """按需解压 wheel，跳过未保留子模块文件与非必要文件。."""
    skipped = 0
    try:
        with zipfile.ZipFile(whl) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    zf.extract(info, dest)
                    continue
                category, sub = classify_entry(info.filename, top_pkg, keep_subs)
                if category == "exclude":
                    skipped += 1
                    continue
                if category == "submodule" and sub not in keep_subs:
                    skipped += 1
                    continue
                zf.extract(info, dest)
    except zipfile.BadZipFile as e:
        raise DependencyError(f"wheel 损坏: {whl}") from e
    if skipped:
        _logger.info("精简 %s: 跳过 %d 个未用子模块文件", whl.name, skipped)


def slim_unpack(  # noqa: PLR0912
    wheels: Sequence[Path],
    site_packages_dir: Path,
    submodule_usage: dict[str, frozenset[str]] | None = None,
    keep_modules: set[str] | None = None,
    *,
    stage: StageRecorder | None = None,
) -> int:
    """按子模块 import 分析选择性解压给定 wheel 列表（白名单制）。

    - 合并 ``submodule_usage``（AST 收集）与 ``keep_modules``（用户显式指定）
      构建每个包的保留集合；Qt 库子模块名归一化（``QtCore`` → ``Core``）
    - **Qt 库**（pyside2/pyside6/pyqt5/pyqt6）自动按 Qt 模块依赖映射计算传递依赖
      闭包（如 ``import QtWidgets`` 自动加入 ``Gui``/``Core``），闭包内的子模块
      对应的 ``.pyd`` 与 ``Qt5/Qt6*.dll`` 均保留，无需用户显式声明 C 层依赖
    - 有保留集合的 wheel 按需解压（跳过未保留子模块的 ``.pyd``/``.pyi``/``.so``、
      ``Qt5/Qt6*.dll``，剥离 examples/translations 等非必要目录，
      绑定层 ``pyside2.abi3.dll``、基础 ``plugins/platforms`` 等始终保留）
    - 无保留集合的 wheel 全量解压（向后兼容：纯顶层 import 或无子模块分析时）
    - 返回解包 wheel 数量

    ``stage`` 用于通过 ``iter_with_progress`` 显示解压进度并回写处理项数到 BuildTracker。
    """
    site_packages_dir.mkdir(parents=True, exist_ok=True)

    merged: dict[str, set[str]] = {}
    if submodule_usage:
        for pkg, subs in submodule_usage.items():
            pkg_norm = normalize_name(pkg)
            if pkg_norm in _QT_PACKAGES:
                merged[pkg_norm] = {_normalize_qt_sub(s) for s in subs}
            else:
                merged[pkg_norm] = set(subs)
    if keep_modules:
        for spec in keep_modules:
            if "." not in spec:
                continue
            pkg, sub = spec.split(".", 1)
            pkg_norm = normalize_name(pkg)
            norm_sub = _normalize_qt_sub(sub) if pkg_norm in _QT_PACKAGES else sub
            merged.setdefault(pkg_norm, set()).add(norm_sub)

    # Qt 库：按 Qt 模块依赖映射计算传递依赖闭包，自动加入 C 层依赖子模块
    # 例如用户 import QtWidgets → 闭包自动加入 Gui/Core，保留对应 .pyd 与 Qt5/6*.dll，
    # 用户无需在代码中显式 import PySide2.QtGui/QtCore 或 --keep-module 声明 C 层依赖。
    # PySide2/PySide6 的 abi3.dll 隐式依赖 Qml/Network 的 DLL 在 classify_entry 中归 shared
    # 始终保留，此处不处理——避免误保留 qml/ 资源目录。
    for pkg, subs in merged.items():
        if pkg in _QT_PACKAGES:
            subs.update(_qt_module_closure(subs))

    sorted_wheels = sorted(wheels)
    count = 0
    for whl in iter_with_progress(sorted_wheels, "解压 wheel", stage=stage):
        info = WheelInfo.from_filename(whl.name)
        if info is None:
            _full_unpack(whl, site_packages_dir)
            count += 1
            continue
        whl_pkg = normalize_name(info.name)
        keep_subs = merged.get(whl_pkg)
        if not keep_subs:
            _full_unpack(whl, site_packages_dir)
            count += 1
            continue
        top_pkg = _detect_top_pkg(whl, whl_pkg)
        if top_pkg is None:
            _full_unpack(whl, site_packages_dir)
            count += 1
            continue
        _logger.info("精简解压 %s: 保留子模块 %s", whl.name, ", ".join(sorted(keep_subs)))
        _slim_extract(whl, site_packages_dir, top_pkg, keep_subs)
        count += 1
    if stage is not None and count:
        stage.set_detail(f"{count} wheels 解压")
    return count
