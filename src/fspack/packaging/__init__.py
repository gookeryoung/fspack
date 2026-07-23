"""打包过程集中管理：运行时下载、C loader 编译、安装包生成。

子模块各自封装单一职责，调用方通过完整路径导入（如
``from fspack.packaging.runtime import download_embed``），本 ``__init__``
不做 re-export，避免触发所有子模块加载、保持惰性导入。

子模块概览：

- :mod:`fspack.packaging.runtime` —— :class:`RuntimeDownloader` 基类，
  封装 ``download → extract → ensure`` 三步流程（embed python / python-build-standalone）
- :mod:`fspack.packaging.loader` —— :class:`LoaderCompiler` 基类，
  封装 ``generate → compile → cache`` 流程（Windows mingw / Linux gcc）
- :mod:`fspack.packaging.installer` —— :class:`Installer` 基类，
  封装 ``build → 校验 → build_package`` 编排流程（NSIS / tar.gz + .deb）；
  依赖 ``fspack.builder``，调用方直接导入，不在此 re-export
- :mod:`fspack.packaging.wheels` —— :func:`download_wheels` wheel 下载与依赖解析
- :mod:`fspack.packaging.net` —— :class:`Downloader` HTTP 下载器（SSL + 进度条）
- :mod:`fspack.packaging.builtin` —— :class:`TkinterBundler` 内置库打包（为 embed python 补充 tkinter）
- :mod:`fspack.packaging.entry` —— :class:`EntryWrapper` 入口包装器源码生成
- :mod:`fspack.packaging.icon` —— :func:`find_favicon` 自动搜索 favicon 与
  :func:`ensure_ico` 图片格式转换（Pillow 可选）
"""
