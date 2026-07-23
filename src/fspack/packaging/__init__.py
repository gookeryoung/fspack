"""打包过程集中管理：运行时下载、C loader 编译、安装包生成。

子模块各自提取共性基类或封装类：

- :mod:`fspack.packaging.runtime` —— :class:`RuntimeDownloader` 基类，
  封装 ``download → extract → ensure`` 三步流程（embed python / python-build-standalone）
- :mod:`fspack.packaging.loader` —— :class:`LoaderCompiler` 基类，
  封装 ``generate → compile → cache`` 流程（Windows mingw / Linux gcc）
- :mod:`fspack.packaging.installer` —— :class:`Installer` 基类，
  封装 ``build → 校验 → build_package`` 编排流程（NSIS / tar.gz + .deb）
- :mod:`fspack.packaging.wheels` —— :func:`download_wheels` wheel 下载与依赖解析
- :mod:`fspack.packaging.net` —— :class:`Downloader` HTTP 下载器（SSL + 进度条）
- :mod:`fspack.packaging.builtin` —— :class:`TkinterBundler` 内置库打包（为 embed python 补充 tkinter）
- :mod:`fspack.packaging.entry` —— :class:`EntryWrapper` 入口包装器源码生成
- :mod:`fspack.packaging.icon` —— :func:`find_favicon` 自动搜索 favicon 与
  :func:`ensure_ico` 图片格式转换（Pillow 可选）

注意：``installer`` 模块依赖 ``fspack.builder``，为避免循环导入（builder →
packaging → installer → builder），本 ``__init__`` 不导出 ``installer`` 的 API。
``installer`` 相关 API 请直接 ``from fspack.packaging.installer import ...``。
"""

from __future__ import annotations

from fspack.packaging.builtin import TkinterBundler
from fspack.packaging.entry import EntryWrapper
from fspack.packaging.icon import SUPPORTED_IMAGE_EXTS, ensure_ico, find_favicon
from fspack.packaging.loader import (
    LINUX_GCC,
    MINGW_GCC,
    LinuxLoader,
    LoaderCompiler,
    WindowsLoader,
    compile_loader,
    gcc_available,
    generate_loader_source,
    loader_cache_dir,
    mingw_available,
)
from fspack.packaging.net import Downloader
from fspack.packaging.runtime import (
    STANDALONE_BASE_URL,
    STANDALONE_RELEASE_TAG,
    EmbedRuntime,
    RuntimeDownloader,
    StandaloneRuntime,
    download_embed,
    download_standalone,
    embed_dirname,
    embed_zip_name,
    ensure_embed,
    ensure_standalone,
    extract_embed,
    extract_standalone,
    standalone_tarball_name,
    standalone_url,
    write_pth,
)
from fspack.packaging.wheels import download_wheels

__all__ = [
    "LINUX_GCC",
    "MINGW_GCC",
    "STANDALONE_BASE_URL",
    "STANDALONE_RELEASE_TAG",
    "SUPPORTED_IMAGE_EXTS",
    "Downloader",
    "EmbedRuntime",
    "EntryWrapper",
    "LinuxLoader",
    "LoaderCompiler",
    "RuntimeDownloader",
    "StandaloneRuntime",
    "TkinterBundler",
    "WindowsLoader",
    "compile_loader",
    "download_embed",
    "download_standalone",
    "download_wheels",
    "embed_dirname",
    "embed_zip_name",
    "ensure_embed",
    "ensure_ico",
    "ensure_standalone",
    "extract_embed",
    "extract_standalone",
    "find_favicon",
    "gcc_available",
    "generate_loader_source",
    "loader_cache_dir",
    "mingw_available",
    "standalone_tarball_name",
    "standalone_url",
    "write_pth",
]
