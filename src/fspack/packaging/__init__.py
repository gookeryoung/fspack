"""打包过程集中管理：运行时下载、C loader 编译、安装包生成。

子模块各自封装单一职责，调用方通过完整路径导入（如
``from fspack.packaging.runtime import download_embed``），本 ``__init__``
不做 re-export，避免触发所有子模块加载、保持惰性导入。

子包与子模块概览：

**子包（按职责拆分，facade 在各包 ``__init__.py``）：**

- :mod:`fspack.packaging.nuitka` —— :class:`NuitkaCompiler` 用户源码编译为本机
  ``.pyd``/``.so``（可选，``--nuitka`` 启用，参考 RimSort 打包方案）；
  子模块：``compiler``（facade 类）/ ``env`` / ``standalone`` / ``ccache`` /
  ``compile`` / ``strip`` / ``verify`` / ``protocol``
- :mod:`fspack.packaging.wheels` —— :func:`download_wheels` wheel 下载与依赖解析；
  子模块：``downloader`` / ``resolver`` / ``sdist`` / ``cache`` / ``markers``
- :mod:`fspack.packaging.installer` —— :class:`Installer` 基类，
  封装 ``build → 校验 → build_package`` 编排流程（NSIS / tar.gz + .deb）；
  子模块：``base`` / ``linux`` / ``macos`` / ``nsis`` / ``zip``；
  依赖 ``fspack.builder``，调用方直接导入，不在此 re-export
- :mod:`fspack.packaging.loader` —— :class:`LoaderCompiler` 基类，
  封装 ``generate → compile → cache`` 流程（Windows mingw / Linux gcc）；
  子模块：``compile`` / ``source``
- :mod:`fspack.packaging.pipeline` —— 构建流水线编排入口（``build`` 主入口 +
  阶段函数 re-export）；子模块：``stages``
- :mod:`fspack.packaging.runtime` —— :class:`RuntimeDownloader` 基类，
  封装 ``download → extract → ensure`` 三步流程（embed python / python-build-standalone）；
  子模块：``download`` / ``extract`` / ``urls`` / ``trim`` / ``pth``
- :mod:`fspack.packaging.pyc` —— pyc 预编译与 .py 源码剥离；
  子模块：``compile`` / ``stamp`` / ``source_strip``
- :mod:`fspack.packaging.win7` —— Win7 兼容性（PE 导入表检查 + 重编译版
  python3XX.dll 下载 + dist 全量扫描）；子模块：``check`` / ``dll`` / ``scan``

**顶层模块（跨子包基础设施或独立职责）：**

- :mod:`fspack.packaging.net` —— :class:`Downloader` HTTP 下载器（SSL + 进度条）
- :mod:`fspack.packaging.builtin` —— :class:`TkinterBundler` 内置库打包（为 embed python 补充 tkinter）
- :mod:`fspack.packaging.entry` —— :class:`EntryWrapper` 入口包装器源码生成
- :mod:`fspack.packaging.icon` —— :func:`find_favicon` 自动搜索 favicon 与
  :func:`ensure_ico` 图片格式转换（Pillow 可选）
- :mod:`fspack.packaging.dep_analyzer` —— 二进制依赖分析与未用二进制剥离
- :mod:`fspack.packaging.size_report` —— 构建产物体积统计
- :mod:`fspack.packaging.sbom` —— SBOM（软件物料清单）生成
- :mod:`fspack.packaging.profile` —— 性能剖析
- :mod:`fspack.packaging.sync` —— 源码同步（``copy_source``）
- :mod:`fspack.packaging.log_file` —— 构建日志文件管理
"""
