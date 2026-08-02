架构与工作原理
==============

本文档介绍 fspack 的构建流水线、模块结构与技术实现细节。面向开发者与贡献者，
普通用户请参考 `README <https://github.com/gookeryoung/fspack#readme>`_ 的使用指南。

构建流水线
----------

``fsp b`` 构建流水线共 12 个阶段：

1. **解析** ``pyproject.toml``，识别项目名、版本、入口模块、CLI/GUI 类型
2. **下载运行时**：Windows 下载 embed python zip 并解压到 ``dist/runtime/``；
   Linux 下载 python-build-standalone tar.gz 并解压到 ``dist/runtime/python/``
3. **分析依赖**：AST 扫描源码 import，分类标准库/本地/第三方，与
   ``pyproject.toml`` 声明依赖比对；结果按源码指纹缓存，未改动跳过。
   ``[project.optional-dependencies]`` 分组经 ``--extra`` / ``[tool.fspack] extras``
   启用后合并到声明依赖集合：自引用 ``"my-pkg[extra]"`` 递归展开，第三方
   ``"pkg[extra]"`` 原样透传 pip；扩展后依赖参与缓存键，extras 变化触发缓存失效
4. **补充内置库**（仅 Windows）：AST 检出 ``tkinter`` 使用时，从
   python-build-standalone Windows 构建提取 tkinter 组件（纯 Python 包 +
   ``_tkinter.pyd`` + Tcl/Tk 运行时脚本）补充到 runtime，按版本缓存 zip 避免重复下载
5. **下载 wheel**：用 ``uv`` 解析精确版本与平台 wheel，再 ``pip download --no-deps``
   并行下载（ThreadPoolExecutor，I/O 密集网络下载提速 ~17%），解包到
   ``dist/runtime/Lib/site-packages/``（Windows）或
   ``dist/runtime/python/lib/python3.X/site-packages/``（Linux）。
   解包时按 AST 收集的子模块使用信息选择性保留（wheel 精简）：Qt 库按依赖闭包
   （如 ``QtWidgets`` → ``Gui``/``Core``）保留对应 ``.pyd``/``.dll``，剥离未用子模块、
   translations/include/metatypes 开发资源、``.exe`` 工具、``.pyi`` 类型 stub；
   ``slim-include``/``slim-exclude`` 用户规则覆盖自动分类
6. **写 _pth**（仅 Windows）：覆盖 ``runtime/python3X._pth``，注册 site-packages 与
   ``..\src`` 路径；``--no-site`` 时省略 ``import site`` 行节省启动时间
7. **复制源码**：项目源码复制到 ``dist/src/``，排除 dist/build/.venv 等构建产物；
   按 mtime 跳过未改动文件
8. **标准库精简**（默认，仅 Linux）：剥离 standalone 的 test/ensurepip/idlelib 等
   无用模块；``--no-stdlib-trim`` 可关闭
9. **字节码预编译**（默认）：``compileall`` 预编译 src+site-packages 为 ``.pyc``
   加速首次启动；stamp 缓存命中跳过；``--pyc-optimize`` 控制 -O/-OO 级别；
   ``--pyc-strip`` 进一步剥离 ``.py`` 仅留 ``.pyc``
10. **Nuitka 编译**（可选，``--nuitka``）：用户源码编译为 ``.pyd`` 本机执行；
    按 Python 版本锁定 Nuitka 版本（3.8/3.9→2.5.1，3.10+→4.1.3），自动装到
    ``~/.fspack/cache/nuitka/``；Windows 用缓存于 ``~/.fspack/cache/python/`` 的
    standalone python 运行编译（embed python 不完整会触发 reExecute fork bomb）；
    入口文件保留 ``.py`` 不编译（``runpy.run_path()`` 兼容）；
    stamp 缓存键 = ``nuitka_version|py_version|src_fingerprint|entry_rels``，
    命中跳过整个阶段；交叉构建自动跳过
11. **Win7 兼容 DLL 注入**（Windows，Python 3.9+）：注入 api-ms-win-core-path
    替代 DLL，支持在 Win7/Win2008R2 运行
12. **生成 C loader**：按平台模板生成 C 源码（烧入入口脚本相对路径），
    mingw（Windows）或 gcc（Linux）编译为可执行文件

dist 布局
---------

.. code-block:: text

   dist/
   ├── <name>.exe          # C loader 启动器
   ├── runtime/            # Python 运行时
   │   ├── python311.dll   # Windows embed
   │   ├── python311._pth
   │   └── Lib/site-packages/   # 第三方依赖（.pyc 预编译）
   ├── src/                # 用户源码（--nuitka 时编译为 .pyd）
   └── release/            # 安装包（fsp p 产出）
       ├── <name>-setup.exe           # Windows NSIS
       ├── <name>_<ver>_amd64.deb     # Linux .deb
       └── <name>-<ver>-linux.tar.gz  # Linux 便携包

多入口机制
----------

单个项目可通过 ``[tool.fspack.entries]`` 声明多个入口，每个入口生成独立 exe，
共享 runtime/依赖/源码。每个入口按自身脚本 import 推断 CLI/GUI 类型，支持
cli/gui/web 混合。

多入口模式下每个入口写入 ``<name>.entry`` 文件，C loader 运行时按
``<exe_basename>.entry`` 查找入口脚本。单入口项目（无 ``[tool.fspack.entries]``）
仍写 ``.entry`` 文件，向后兼容。

递归打包
--------

``--recursive``/``-R`` 模式递归扫描给定目录下所有含 ``pyproject.toml`` 的子项目
（含目录自身），依次执行构建/打包。特性：

- 跳过 ``.venv``/``dist``/``build``/``.git``/``__pycache__`` 等开发期目录
- 单项目失败不中断后续项目，最后汇总成功/失败列表
- 退出码：0=全部成功，1=有失败（便于 CI 检测）
- 子项目按路径字母序排序，保证可重复构建

离线模式
--------

fspack 支持通过环境变量启用的离线模式，适用于无网络环境（内网 CI、离线打包机）
或需精确控制缓存来源的场景。

环境变量
~~~~~~~~

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - 变量
     - 作用
   * - ``FSPACK_OFFLINE``
     - 设为 ``1``/``true``/``yes``/``on``（不区分大小写）启用离线模式
   * - ``FSPACK_CACHE_DIR``
     - 覆盖缓存根目录（默认 ``~/.fspack/cache``），所有子模块缓存目录派生自此

缓存目录结构
~~~~~~~~~~~~

缓存根目录下按子模块划分，互不干扰：

.. code-block:: text

   <cache_root>/
   ├── embed/          # Windows embed python zip
   ├── standalone/     # Linux python-build-standalone tar.gz
   ├── wheels/         # 第三方 wheel + 依赖解析缓存（.deps_cache.json）
   ├── nuitka/         # Nuitka 包 + 编译用 standalone python（按 py_version 分目录）
   ├── loaders/        # C loader 编译缓存（按 source hash 命名）
   ├── ccache/         # ccache 二进制与编译缓存
   └── tkinter/        # tkinter 补充包缓存（按 standalone 版本命名的 zip）

子模块缓存目录通过 :mod:`fspack.config.cache` 的 ``embed_cache_dir()``/
``standalone_cache_dir()``/``wheel_cache_dir()``/``nuitka_cache_dir()``/
``loader_cache_dir()``/``ccache_cache_dir()``/``tkinter_cache_dir()`` 派生，
统一从 ``cache_root()`` 计算，确保 ``FSPACK_CACHE_DIR`` 环境变量对所有子模块生效。

工作原理
~~~~~~~~

离线模式在所有下载入口（``runtime.py``/``wheels/downloader.py``/``nuitka/env.py``/
``builtin.py``）检查 ``is_offline()``，缓存命中时正常返回，缓存未命中时立即抛出
包含"离线模式"关键字的明确异常，不尝试网络请求。错误信息包含：

- 缺失的文件名或依赖名
- 已搜索的所有路径（缓存目录 + 用户 ``--find-links`` 路径）
- 解决方案提示（预下载到缓存、新增 ``--find-links``、取消 ``FSPACK_OFFLINE``）

.. code-block:: python

   # runtime.py：embed python 下载
   def download(...):
       if archive_path.is_file():
           return archive_path          # 缓存命中
       if is_offline():
           raise EmbedError(
               f"离线模式下 {cls.runtime_label} 缓存未命中: {archive_path.name}，"
               f"请预先下载放入 {cache_dir} 或取消 FSPACK_OFFLINE 环境变量"
           )
       # 在线下载逻辑...

离线 wheel 本地搜索
~~~~~~~~~~~~~~~~~~~

``wheels/downloader.py`` 的 ``_run_pip_download`` 在离线模式下用 ``--no-index`` 参数
调用 ``pip download``，仅从本地目录解析依赖。除默认的 ``wheel_cache_dir()``
外，**用户通过 ``--find-links`` 提供的本地 wheel 目录也参与 ``--no-index`` 解析**，
扩大本地搜索范围：

.. code-block:: python

   # 构造用户提供的 find-links 参数
   user_find_links_args = []
   for link in find_links:
       user_find_links_args.extend(["--find-links", link])

   # --no-index 调用同时搜索 cache_dir 和用户 find_links
   result = _run_pip(
       [*base_args, *user_find_links_args, "--no-index", *filtered],
       f"检查缓存 {len(filtered)} 个依赖",
       suppress_error=True,
   )
   if result is None:
       if is_offline():
           searched = [str(cache_dir), *find_links]
           raise DependencyError(
               f"离线模式下依赖缓存未命中: {', '.join(filtered)}，"
               f"已搜索路径: {'; '.join(searched)}。..."
           )

错误处理流程
~~~~~~~~~~~

各下载层的离线异常类型：

.. list-table::
   :widths: 30 30 40
   :header-rows: 1

   * - 下载层
     - 异常类型
     - 触发条件
   * - ``runtime.py``
     - ``EmbedError``
     - embed python / standalone tarball 缓存未命中
   * - ``wheels/downloader.py``
     - ``DependencyError``
     - ``--no-index`` 解析失败（缓存 + find-links 均未命中）
   * - ``nuitka/env.py``
     - ``NuitkaError``
     - standalone python / Nuitka 包缓存未命中
   * - ``builtin.py``
     - ``BuiltinError``
     - tkinter 补充包的 standalone Windows tarball 缓存未命中

非离线模式下，缓存未命中的处理：

- runtime/standalone：回退到 ``Downloader.download`` 网络下载
- wheel：从 ``--no-index`` 回退到 ``_download_online``，用 ``uv pip compile``
  或 ``pip download`` 在线解析下载
- ccache：无系统级且无缓存时跳过（不强制下载，Nuitka 编译时若 ccache 不可用自动降级）

模块结构
--------

源码位于 ``src/fspack/``，按职责分包，每个子包通过 facade 模式暴露公开 API
（详见各 ``__init__.py`` docstring）。

顶层模块
~~~~~~~~

.. list-table::
   :widths: 25 75
   :header-rows: 1

   * - 模块
     - 职责
   * - ``cli.py``
     - CLI 入口（cargo 风格短命令 ``fsp b/c/r/p``），延迟导入重模块使 ``--help`` 提速
   * - ``builder.py``
     - 高层构建 facade，re-export ``pipeline.build``/``runtime``/``loader``/``sync`` 等
   * - ``analyzer.py``
     - AST 扫描源码 import，分类标准库/本地/第三方依赖
   * - ``runner.py``
     - 运行已打包项目（Linux 原生，Windows 自动用 wine）
   * - ``console.py``
     - rich 驱动的彩色输出与日志配置
   * - ``platform.py``
     - 平台检测（Windows/Linux）与 ``Platform`` 枚举
   * - ``progress.py``
     - ``BuildTracker``/``StageRecorder``/``spinner`` 进度显示
   * - ``exceptions.py``
     - 自定义异常层次（``FspackError``/``DependencyError``/``NuitkaError`` 等）
   * - ``_compat.py``
     - 版本兼容层（``override`` 装饰器等）

config/ 子包
~~~~~~~~~~~~

配置 facade，拆分为：

- ``models`` — 数据结构（dataclass）
- ``parsing`` — pyproject.toml 解析
- ``versions`` — Python/Nuitka 版本映射
- ``cache`` — 缓存目录与离线模式配置（``cache_root``/``is_offline`` 及各子模块缓存目录）

packaging/ 子包
~~~~~~~~~~~~~~~

打包流程 facade，子模块按职责拆分：

.. list-table::
   :widths: 35 65
   :header-rows: 1

   * - 子模块
     - 职责
   * - ``pipeline/``（``__init__.py`` / ``stages.py``）
     - 构建流水线编排（``build()`` 入口，10+ 阶段调度）
   * - ``runtime.py``
     - ``RuntimeDownloader``：embed python / python-build-standalone 下载解压
   * - ``loader/``（``__init__.py`` / ``source.py`` / ``compile.py``）
     - C loader facade：源码模板 + 编译流程 + icon 资源 + MinGW 运行时 DLL 注入
   * - ``installer/``（``__init__.py`` / ``base.py`` / ``linux.py`` / ``macos.py`` / ``nsis.py`` / ``zip.py``）
     - 安装包 facade：NSIS / .deb + tar.gz / .pkg + .dmg / 跨平台 zip
   * - ``wheels/``（``__init__.py`` / ``downloader.py`` / ``resolver.py`` / ``sdist.py`` / ``cache.py`` / ``markers.py``）
     - wheel 下载 facade：pip/uv 调用 + sdist 回退 + 并行下载 + 依赖解析缓存 + python_version 标记预过滤
   * - ``nuitka/``（``__init__.py`` / ``compiler.py`` / ``env.py`` / ``standalone.py`` / ``ccache.py`` / ``compile.py`` / ``strip.py`` / ``verify.py`` / ``protocol.py``）
     - Nuitka 编译 facade：环境就绪 + standalone python + ccache + 编译流程 + 产物剥离 + 验证
   * - ``pyc.py``
     - 字节码预编译（``compileall`` + stamp 缓存）
   * - ``sync.py``
     - 源码同步（``copy_source`` + 增量同步 + site-packages 指纹）
   * - ``builtin.py``
     - ``TkinterBundler``：从 standalone 提取 tkinter 补充到 embed python
   * - ``entry.py``
     - ``EntryWrapper``：入口包装器源码生成
   * - ``icon.py``
     - favicon 自动搜索与图片格式转换（Pillow 可选，支持透明通道）
   * - ``net.py``
     - ``Downloader``：HTTP 下载器（SSL + 进度条）

slim/ 子包
~~~~~~~~~~

wheel 精简 facade：

- ``base`` — 抽象基类
- ``spec`` — 注册表
- ``qt``/``libs``/``default`` — 具体 spec
- ``unpack`` — 按需解压

性能优化
--------

fspack 内置多层性能优化：

- **增量构建缓存**：源码指纹 + 预编译 stamp + Nuitka stamp 三层缓存，未改动文件跳过复制与重编
- **CLI 懒加载**：``fsp`` 入口延迟导入重模块（``config``/``console``/``platform``），
  ``fsp --help`` 冷启动从 ~100ms 降到 ~61ms
- **wheel 并行下载**：``uv`` 解析精确版本后用 ``ThreadPoolExecutor`` 并行
  ``pip download --no-deps``，失败包自动 sdist 回退构建
- **Win7 兼容**：Python 3.9+ 注入 api-ms-win-core-path 替代 DLL，支持在 Win7/Win2008R2 运行
- **国内镜像**：默认清华源 PyPI 与 embed python 镜像，``--mirror`` 切换（aliyun/huawei/tsinghua）
- **彩色进度显示**：rich 驱动的步骤进度（> 准备运行时 / √ 构建完成），
  错误/警告/一般消息颜色区分，``-v`` 开启 DEBUG 日志

完整 API 参考见 :doc:`api`。
