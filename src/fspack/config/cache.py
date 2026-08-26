"""缓存目录与离线模式配置.

统一管理 fspack 的缓存根目录与离线模式开关，通过环境变量支持自定义部署：

- ``FSPACK_CACHE_DIR``：覆盖缓存根目录（默认 ``~/.fspack/cache``）
- ``FSPACK_OFFLINE``：设为 ``1``/``true``/``yes`` 启用离线模式（默认关闭）

离线模式下所有下载阶段跳过网络请求，仅使用本地缓存；缓存未命中时报清晰错误
而非卡死或重试。适用于无网络环境（内网 CI、离线打包机）或需精确控制
缓存来源的场景。

公共 API：

- :func:`cache_root` — 缓存根目录（环境变量 > 默认值）
- :func:`is_offline` — 是否启用离线模式
- :func:`embed_cache_dir` / :func:`standalone_cache_dir` / :func:`wheel_cache_dir` /
  :func:`nuitka_cache_dir` / :func:`loader_cache_dir` / :func:`ccache_cache_dir` /
  :func:`tkinter_cache_dir` / :func:`win7_dll_cache_dir` — 各子模块缓存目录
- :func:`nuitka_winlibs_cache_dir` — Nuitka winlibs-mingw 工具链缓存目录
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "cache_root",
    "ccache_cache_dir",
    "embed_cache_dir",
    "is_offline",
    "loader_cache_dir",
    "nsis_cache_dir",
    "nuitka_cache_dir",
    "nuitka_winlibs_cache_dir",
    "standalone_cache_dir",
    "tkinter_cache_dir",
    "wheel_cache_dir",
    "win7_dll_cache_dir",
]

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def cache_root() -> Path:
    """返回 fspack 缓存根目录.

    优先读 ``FSPACK_CACHE_DIR`` 环境变量，未设置时回退到 ``~/.fspack/cache``。
    返回的目录不一定存在（调用方按需 ``mkdir(parents=True, exist_ok=True)``）。

    :return: 缓存根目录路径
    """
    env = os.environ.get("FSPACK_CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".fspack" / "cache"


def is_offline() -> bool:
    """返回是否启用离线模式.

    读 ``FSPACK_OFFLINE`` 环境变量，值为 ``1``/``true``/``yes``/``on``
    （不区分大小写）时返回 ``True``。离线模式下下载阶段跳过网络请求，
    仅使用本地缓存。

    :return: 离线模式启用返回 ``True``，否则 ``False``
    """
    return os.environ.get("FSPACK_OFFLINE", "").lower() in _TRUE_VALUES


def embed_cache_dir() -> Path:
    """Windows embed python 缓存目录（``<cache_root>/embed``）."""
    return cache_root() / "embed"


def standalone_cache_dir() -> Path:
    """Linux python-build-standalone 缓存目录（``<cache_root>/standalone``）."""
    return cache_root() / "standalone"


def wheel_cache_dir() -> Path:
    """第三方 wheel 与依赖解析缓存目录（``<cache_root>/wheels``）."""
    return cache_root() / "wheels"


def nuitka_cache_dir() -> Path:
    """Nuitka 包与编译用 standalone python 缓存根目录（``<cache_root>/nuitka``）."""
    return cache_root() / "nuitka"


def loader_cache_dir() -> Path:
    """C loader 编译缓存目录（``<cache_root>/loaders``）."""
    return cache_root() / "loaders"


def ccache_cache_dir() -> Path:
    """ccache 二进制与编译缓存目录（``<cache_root>/ccache``）."""
    return cache_root() / "ccache"


def tkinter_cache_dir() -> Path:
    """tkinter 补充包缓存目录（``<cache_root>/tkinter``）."""
    return cache_root() / "tkinter"


def win7_dll_cache_dir() -> Path:
    """Win7 重编译版 python3XX.dll 的 embed zip 缓存目录（``<cache_root>/win7-dll``）."""
    return cache_root() / "win7-dll"


def nuitka_winlibs_cache_dir() -> Path:
    """Nuitka winlibs-mingw 工具链缓存目录（``<cache_root>/nuitka-winlibs-mingw``）.

    作为 Nuitka 的 downloads 缓存根（注入 ``NUITKA_CACHE_DIR_DOWNLOADS``），
    内部按 Nuitka ``getCachedDownload`` 的目录约定存放 winlibs gcc：
    ``gcc/x86_64/<specificity>/mingw64/bin/gcc.exe``。
    """
    return cache_root() / "nuitka-winlibs-mingw"


def nuitka_work_cache_dir() -> Path:
    """Nuitka 编译工作缓存目录（``<cache_root>/nuitka-work``）.

    作为 ``NUITKA_CACHE_DIR`` 全量重定向目标（注入 Nuitka 子进程环境变量），
    收纳 clcache/scons-config 等编译中间缓存，与系统默认位置
    ``%LOCALAPPDATA%\\Nuitka\\Nuitka\\Cache``（Windows）/ ``~/.cache/Nuitka``
    （Linux）隔离——历史污染条目（坏 clcache 缓存被反复命中导致 .pyd 大量
    损坏）不再影响 fspack 构建。downloads 类缓存经专属变量
    ``NUITKA_CACHE_DIR_DOWNLOADS`` 单独指向
    :func:`nuitka_winlibs_cache_dir`，清本目录不影响 winlibs 工具链。
    """
    return cache_root() / "nuitka-work"


def nsis_cache_dir() -> Path:
    """NSIS 工具链缓存目录（``<cache_root>/nsis``）.

    存放 NSIS 归档（官方 ``nsis-<version>.zip`` 或社区 portable 变体
    ``.zip``/``.7z``）与解压后的 makensis 工具链目录，见
    :mod:`fspack.packaging.installer.nsis_tool`。
    """
    return cache_root() / "nsis"
