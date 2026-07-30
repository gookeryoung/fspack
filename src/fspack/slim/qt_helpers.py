"""Qt 文件名归一化与判断辅助.

提取自 :mod:`fspack.slim.qt`，按职责拆分。本模块专注于"从文件名提取/判断
Qt 子模块信息"——纯字符串处理，无依赖映射与闭包计算。

公开 API（含私有名，便于 facade 重新导出以保兼容）：

- :func:`_normalize_qt_sub`：Qt 子模块文件名归一化（``QtCore``/``Qt5Core`` → ``Core``）
- :func:`_qt_dll_submodule`：Qt 原生 DLL 文件名提取子模块名
- :func:`_is_ffmpeg_dll`/func:`_is_qml_abi_dll`/func:`_is_opengl_sw_dll`：特殊 DLL 判断
- :data:`_QT_EXCLUDE_SUBDIRS`/data:`_QT_LIB_EXCLUDE_SUBDIRS`：始终剥离的子目录
- :data:`_QT_FFMPEG_DLL_PREFIXES`/data:`_QT_QML_ABI_DLL_NAMES`/data:`_QT_OPENGL_SW_DLL_NAMES`：
  特殊 DLL 文件名前缀/名集合
"""

from __future__ import annotations

__all__ = [
    "_QT_EXCLUDE_SUBDIRS",
    "_QT_FFMPEG_DLL_PREFIXES",
    "_QT_LIB_EXCLUDE_SUBDIRS",
    "_QT_OPENGL_SW_DLL_NAMES",
    "_QT_QML_ABI_DLL_NAMES",
    "_is_ffmpeg_dll",
    "_is_opengl_sw_dll",
    "_is_qml_abi_dll",
    "_normalize_qt_sub",
    "_qt_dll_submodule",
]

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
        "metatypes",  # Qt 元类型 JSON（编译期用，运行时不需要，约 14MB）
        "QtAsyncio",  # QtAsyncio 模块（asyncio 集成，非 asyncio 应用不需要）
    }
)

# Qt 库 lib/ 子目录下始终剥离的三级子目录。
# 注意：lib/ 本身不剥离（PySide2 的 lib/fonts/ 含 Qt 内嵌字体，运行时需要），
# 仅剥离 lib/cmake/（cmake 配置文件，构建系统用，运行时不需要）。
_QT_LIB_EXCLUDE_SUBDIRS = frozenset({"cmake"})

# FFmpeg 系列 DLL 文件名前缀 → 仅 QtMultimedia 闭包内保留。
# PySide6 wheel 携带 avcodec-61.dll/avformat-61.dll/avutil-59.dll/swscale-8.dll/
# swresample-5.dll 等 FFmpeg 库（合计约 18MB），仅 Qt6Multimedia.dll 运行时加载。
# 文件名格式 ``<prefix>-<version>.dll``，用 startswith 识别前缀。
# 当 Multimedia 不在 keep_subs 时剥离（Qt6Multimedia.dll 已被剥离却仍保留 FFmpeg 是浪费）。
_QT_FFMPEG_DLL_PREFIXES = frozenset({"avcodec", "avformat", "avutil", "swscale", "swresample"})

# QML 绑定层 ABI DLL：仅 Qml 闭包内保留。
# pyside6qml.abi3.dll/pyside2qml.abi3.dll 是 QML 类型注册的绑定层，
# 仅当用户 import QtQml 时被加载，非 QML 应用无需保留。
_QT_QML_ABI_DLL_NAMES = frozenset({"pyside6qml.abi3.dll", "pyside2qml.abi3.dll"})

# opengl32sw.dll：Mesa 软件 OpenGL 渲染后备，仅 OpenGL 相关模块闭包内保留。
# 系统无 GPU 驱动或驱动不兼容时 Qt 加载此 DLL 作为 OpenGL 后备（约 20MB）。
# 仅当用户使用 OpenGL/Quick/Quick3D/Multimedia/Graphs/DataVisualization 等模块时需要；
# 纯 Widgets/WebEngine 应用不直接使用 OpenGL，可剥离。
# WebEngineCore 自带 Chromium GPU 加速，不依赖 opengl32sw.dll。
_QT_OPENGL_SW_DLL_NAMES = frozenset({"opengl32sw.dll"})


def _normalize_qt_sub(stem: str) -> str:
    """Qt 子模块文件名归一化.

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
    """Qt 原生 DLL 文件名提取子模块名.

    ``Qt5Core`` → ``Core``，``Qt6Gui`` → ``Gui``，``Qt53DRender`` → ``3DRender``。
    非 Qt5/Qt6 前缀返回 None（如 ``pyside2.abi3``、``msvcp140``）。
    """
    for prefix in ("Qt5", "Qt6"):
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    return None


def _is_ffmpeg_dll(filename: str) -> bool:
    """判断文件名是否为 FFmpeg 系列 DLL（仅 QtMultimedia 闭包内保留）.

    匹配 ``avcodec-<ver>.dll``/``avformat-<ver>.dll``/``avutil-<ver>.dll``/
    ``swscale-<ver>.dll``/``swresample-<ver>.dll`` 等格式。文件名转小写后
    按 ``-`` 分隔，首段在前缀集合中且后缀为 ``.dll`` 即匹配。
    """
    if not filename.lower().endswith(".dll"):
        return False
    stem = filename[: -len(".dll")]
    # 文件名格式 ``<prefix>-<version>``，取首个 ``-`` 之前部分
    prefix = stem.split("-", 1)[0].lower()
    return prefix in _QT_FFMPEG_DLL_PREFIXES


def _is_qml_abi_dll(filename: str) -> bool:
    """判断文件名是否为 QML 绑定层 ABI DLL（仅 Qml 闭包内保留）."""
    return filename.lower() in _QT_QML_ABI_DLL_NAMES


def _is_opengl_sw_dll(filename: str) -> bool:
    """判断文件名是否为 opengl32sw.dll（Mesa 软件 OpenGL 后备）."""
    return filename.lower() in _QT_OPENGL_SW_DLL_NAMES
