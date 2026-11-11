"""C loader 源码资源：从 ``assets/loader/*.c`` 读取并注入平台常量.

从 :mod:`fspack.packaging.loader` 拆分而来，C 源码本体存放在
``src/fspack/assets/loader/`` 下（``loader_windows.c``/``loader_linux.c``/
``loader_macos.c``/``splash_windows.c``），构建期按需读取，避免在 Python 模块
中内嵌大段 C 文本。平台特定常量以 ``{python_dll}``/``{libpython}`` 占位符
保留在 C 源码中，由 :mod:`fspack.packaging.loader.compile` 的
``WindowsLoader.generate_source``/``LinuxLoader.generate_source``/
``MacLoader.generate_source`` 用字符串替换填充。

入口脚本路径在运行时从 ``<exe_dir>/<exe_basename>.entry`` 文件读取（多入口模式），
回退到 ``<exe_dir>/.entry``（单入口模式，向后兼容）。构建时为每个入口写对应
``<name>.entry`` 文件，使 loader 源码仅依赖 ``py_xy`` 与平台，可按
``(py_xy, app_type, platform)`` 缓存跨项目复用。

Windows 模板含三个 ``FSPACK:SPLASH*`` 标记，由 :func:`apply_splash` 注入/
剥离 splash 启动画面代码（``--splash`` 构建选项，默认关闭）。
"""

from __future__ import annotations

import functools
from pathlib import Path

from fspack.exceptions import LoaderError

__all__ = [
    "apply_splash",
    "loader_linux_source",
    "loader_macos_source",
    "loader_windows_source",
    "splash_windows_source",
]

# source.py 位于 src/fspack/packaging/loader/，assets 在其三级父目录 fspack/ 下
_LOADER_ASSET_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "loader"


@functools.lru_cache(maxsize=None)
def _loader_asset(name: str) -> str:
    """读取 ``assets/loader/<name>`` 的 C 源码（构建期按需加载并缓存）.

    Raises:
        LoaderError: 资源文件缺失（wheel 打包清单未包含 assets/loader 时）。
    """
    path = _LOADER_ASSET_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise LoaderError(f"loader C 源码资源缺失: {path}（请检查打包清单是否包含 assets/loader）") from e


def loader_windows_source() -> str:
    """Windows loader C 源码（``{python_dll}`` 占位符由调用方替换）."""
    return _loader_asset("loader_windows.c")


def loader_linux_source() -> str:
    """Linux loader C 源码（``{libpython}`` 占位符由调用方替换）."""
    return _loader_asset("loader_linux.c")


def loader_macos_source() -> str:
    """macOS loader C 源码（``{libpython}`` 占位符由调用方替换）."""
    return _loader_asset("loader_macos.c")


def splash_windows_source() -> str:
    """Windows splash 启动画面 C 片段（``__FSPACK_SPLASH_TITLE__`` 占位符）."""
    return _loader_asset("splash_windows.c")


def _c_escape(text: str) -> str:
    """转义文本为 C 字符串字面量内容（反斜杠与双引号，宽/窄字面量通用）."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def apply_splash(source: str, title: str | None) -> str:
    """向 Windows loader 源码注入或剥离 splash 启动画面代码.

    ``title`` 为 None 时剥离全部 ``FSPACK:SPLASH*`` 标记（默认构建，不含
    splash）；非 None 时注入 :func:`splash_windows_source` 代码块（标题经
    :func:`_c_escape` 转义为 C 宽字符串字面量）。

    用标记替换而非 ``str.format`` 拼接：splash 块内 C 花括号无需转义，
    且平台常量替换先行完成，互不干扰。
    """
    if title is None:
        return (
            source.replace("/* FSPACK:SPLASH */\n", "")
            .replace("    /* FSPACK:SPLASH_CLOSE */\n", "")
            .replace("    /* FSPACK:SPLASH_SHOW */\n", "")
        )
    block = splash_windows_source().replace("__FSPACK_SPLASH_TITLE__", _c_escape(title))
    return (
        source.replace("/* FSPACK:SPLASH */", block)
        .replace("    /* FSPACK:SPLASH_CLOSE */", "    splash_close();")
        .replace("    /* FSPACK:SPLASH_SHOW */", "    splash_show();")
    )
