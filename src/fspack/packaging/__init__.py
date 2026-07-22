"""打包过程集中管理：运行时下载、C loader 编译、安装包生成。

三个子模块各自提取共性基类：

- :mod:`fspack.packaging.runtime` —— :class:`RuntimeDownloader` 基类，
  封装 ``download → extract → ensure`` 三步流程（embed python / python-build-standalone）
- :mod:`fspack.packaging.loader` —— :class:`LoaderCompiler` 基类，
  封装 ``generate → compile → cache`` 流程（Windows mingw / Linux gcc）
- :mod:`fspack.packaging.installer` —— :class:`Installer` 基类，
  封装 ``build → 校验 → build_package`` 编排流程（NSIS / tar.gz + .deb）

注意：``installer`` 模块依赖 ``fspack.builder``，为避免循环导入（builder →
packaging → installer → builder），本 ``__init__`` 仅导出 ``runtime`` 与
``loader`` 的 API。``installer`` 相关 API 请直接 ``from fspack.packaging.installer import ...``。
"""

from __future__ import annotations

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

__all__ = [
    "LINUX_GCC",
    "MINGW_GCC",
    "STANDALONE_BASE_URL",
    "STANDALONE_RELEASE_TAG",
    "EmbedRuntime",
    "LinuxLoader",
    "LoaderCompiler",
    "RuntimeDownloader",
    "StandaloneRuntime",
    "WindowsLoader",
    "compile_loader",
    "download_embed",
    "download_standalone",
    "embed_dirname",
    "embed_zip_name",
    "ensure_embed",
    "ensure_standalone",
    "extract_embed",
    "extract_standalone",
    "gcc_available",
    "generate_loader_source",
    "loader_cache_dir",
    "mingw_available",
    "standalone_tarball_name",
    "standalone_url",
    "write_pth",
]
