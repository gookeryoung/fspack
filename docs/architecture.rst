架构与工作原理
==============

本文档介绍 fspack 的构建流水线、模块结构与技术实现细节。面向开发者与贡献者，
普通用户请参考 :doc:`cli` 命令参考与 :doc:`configuration` 配置说明。

构建流水线
----------

``fsp b`` 构建流水线按下列顺序执行（下表为 Windows 热构建 profile 的实测
阶段名与顺序，带「可选」的仅在对应条件成立时出现）：

1. **解析项目**：解析 ``pyproject.toml``，识别项目名、版本、入口模块、
   CLI/GUI 类型，解析 Python 版本（``--py-version`` > ``.python-version``
   > ``requires-python`` > 平台默认），合并 CLI 私有包源
2. **构建前端**（可选）：识别 web 结构（``web-static-dirs`` 配置或
   ``package.json`` 结构扫描），产物缺失时在复制源码前就地构建——否则打出的
   应用会在终端用户机器上尝试安装前端依赖（无 node 环境，必然失败）
3. **下载运行时**：Windows 下载 embed python zip；Linux/macOS 下载
   python-build-standalone tar.gz。macOS 目标仅支持 macOS 构建机、Linux 目标
   仅支持 Linux 构建机（clang 无法可靠交叉产出 Mach-O，macOS 的 gcc 是 clang
   垫片、Windows 的 gcc 是 mingw，均无法产出 ELF；Windows 目标支持任意构建机）。
   版本受平台资产表约束：Windows embed 覆盖 3.8–3.14（自由线程 3.13t/3.14t
   改走 standalone），Linux/macOS standalone 覆盖 3.10–3.14（上游已不发布
   3.8/3.9）；架构由构建机决定，Windows/Linux 仅 x86_64，macOS 按
   ``platform.machine()`` 取 x86_64 或 arm64。每次构建只产出一个目标，
   产物统一写入 ``dist/``
4. **解压运行时**：安全解压（拒绝越界路径条目）到 ``dist/runtime/``
5. **精简标准库**：Windows 按版本 + 黑名单指纹精简 embed zip（action 缓存
   命中跳过）；Linux/macOS 剥离 standalone 的 test/ensurepip/idlelib 等，
   ``--no-stdlib-trim`` 可关闭
6. **分析依赖**：AST 扫描源码 import，分类标准库/本地/第三方，与
   ``pyproject.toml`` 声明依赖比对；结果按源码指纹缓存，未改动跳过。
   ``[project.optional-dependencies]`` 分组经 ``--extra`` / ``[tool.fspack] extras``
   启用后合并到声明依赖集合：自引用 ``"my-pkg[extra]"`` 递归展开，第三方
   ``"pkg[extra]"`` 原样透传 pip；扩展后依赖参与缓存键，extras 变化触发缓存失效
7. **下载依赖**：先用 ``uv pip compile --no-index`` 在本地 wheel 缓存
   （+ 用户 ``--find-links``）上快速解析精确版本（热缓存约 40ms，替代原先
   ``pip download --no-index`` 预检的 1.5–2.7s；uv 不可用、含 sdist、direct
   URL 等无法确定的情形一律回退原 pip 预检）；本地不足时用 ``uv`` 解析精确
   版本与平台 wheel，再 ``pip download --no-deps`` 并行下载，失败包自动 sdist
   回退构建，解包到 ``dist/runtime/Lib/site-packages/``（Windows）或
   ``dist/runtime/python/lib/python3.X/site-packages/``（Linux/macOS）。
   解包时按 AST 收集的子模块使用信息选择性保留（wheel 精简）：Qt 库按依赖闭包
   （如 ``QtWidgets`` → ``Gui``/``Core``）保留对应 ``.pyd``/``.dll``，剥离未用子模块、
   translations/include/metatypes 开发资源、``.exe`` 工具、``.pyi`` 类型 stub；
   ``slim-include``/``slim-exclude`` 用户规则覆盖自动分类。AST 检出 ``tkinter``
   使用时，从 python-build-standalone Windows 构建提取 tkinter 组件（纯 Python
   包 + ``_tkinter.pyd`` + Tcl/Tk 运行时脚本）补充到 runtime，按版本缓存 zip
   避免重复下载。Windows 目标随后覆盖 ``python3X._pth``，注册 site-packages
   与 ``..\src`` 路径；``--no-site`` 时省略 ``import site`` 行并剥离
   site-packages 下 ``.pth`` 文件（site.py 不加载时它们只是垃圾文件）
8. **复制源码**：项目源码增量同步到 ``dist/src/``，排除 dist/build/.venv 等构建产物
   与测试/文档/凭证；已有产物时只删除源中已不存在的文件、覆盖新增与改动文件，
   保留 ``__pycache__`` 以复用 ``.pyc``
9. **预编译字节码**（默认）：``compileall`` 预编译 src+site-packages 为 ``.pyc``
   加速首次启动；stamp 缓存命中跳过；``--pyc-optimize`` 控制 -O/-OO 级别；
   ``--pyc-strip`` 进一步剥离 ``.py`` 仅留 ``.pyc``
10. **标准库 zip 化**（Linux/macOS 生效）：把 standalone 标准库打成 zip 缩减
    文件数；Windows 目标该阶段为空操作
11. **精简 runtime**（Linux/macOS 生效）：strip libpython 调试符号、删 python
    二进制/include/share，非 tkinter 项目剥离 Tcl/Tk；``--no-slim-runtime`` 关闭
12. **解析图标**：CLI ``--icon`` > ``[tool.fspack] icon`` > ``favicon.*`` > 默认
    ``assets/icons/app.ico``；非 ``.ico`` 经 Pillow 转换（同一源图按内容摘要
    缓存，命中跳过转换）。仅 Windows 目标生效
13. **生成 C loader**：按平台模板生成 C 源码（烧入入口脚本相对路径），
    mingw（Windows）、gcc（Linux）或 clang（macOS）编译为可执行文件；编译产物
    按源码哈希缓存
14. **Win7 loader 校验**（Windows）：loader exe 导入表若引入 Win8+ API 属
    fspack 回归，违规阻断构建（硬门禁）
15. **二进制依赖分析**（可选 ``--analyze-deps``）：解析 ``.dll``/``.so``/
    ``.dylib`` 依赖树，剥离无引用文件
16. **Win7 兼容扫描**（Windows，默认启用，``--no-win7-scan`` 关闭）：先对 dist
    下全部 PE 执行导入表原地改名（kernel32 Win8+ API → Win7 原生等价函数），
    再全量扫描复核、注入内置 Win7 shim（api-ms-win-core-path +
    bcryptprimitives.ProcessPrng），剩余违规写报告提示（软门禁，不阻断）
17. **生成 SBOM**（默认启用，``--no-sbom`` 关闭）：扫描 ``*.dist-info`` 生成
    SPDX 2.3 JSON 到 ``dist/release/``；失败降级告警
18. **生成产物清单**（默认启用，``--no-manifest`` 关闭）：扫描 dist 文件记录
    大小/SHA256（按 (size, mtime) 备忘录复用哈希），生成 JSON 供
    ``fsp manifest diff`` 对比版本差异

可选编译阶段（``--nuitka``）在「预编译字节码」处替代用户源码的 ``.pyc``：
用户源码编译为 ``.pyd`` 本机执行；按 Python 版本锁定 Nuitka 版本
（3.8/3.9→2.5.1，3.10+→4.1.3），自动装到 ``~/.fspack/cache/nuitka/``；Windows
优先复用构建机自身 python（major.minor 与目标一致时免下载），否则用缓存于
``~/.fspack/cache/python/`` 的 standalone python 运行编译（embed python 不完整
会触发 reExecute fork bomb），tarball 与 tkinter 打包共享 ``standalone-windows/``
缓存；Nuitka 编译中间缓存（clcache/scons-config 等）经 ``NUITKA_CACHE_DIR``
全量重定向到 ``nuitka-work/``，与系统默认位置隔离（历史污染的坏 clcache 条目
被反复命中会导致 .pyd 大量损坏）；Windows 编译器由 Nuitka 下载缓存提供（注入
``NUITKA_CACHE_DIR_DOWNLOADS`` 重定向到 ``nuitka-winlibs-mingw/``：检测到 MSVC
（vswhere 探测 Visual Studio C++ 工具链，优先级最高）时跳过 winlibs 预填充且
不加 force flag，否则预填充 winlibs gcc——py<3.13 scons 默认即 winlibs，
py>=3.13 且无 MSVC 时经 ``--experimental=force-mingw64`` 强制——zig 编译的
.pyd 可能损坏不再使用；预填充时识别缓存目录下用户手动放置的 winlibs zip 解压
替代下载，损坏归档删除后回退下载；系统 mingw 会被 scons 拒绝不使用）；编译
产物验证异常数量 ≥3 且过半时自动清 ``nuitka-work/`` 编译缓存重试一轮（自愈，
winlibs 工具链不受影响）；入口文件保留 ``.py`` 不编译（``runpy.run_path()``
兼容，多入口时每个入口一个不上编译的包装脚本）；
stamp 缓存键 = ``nuitka_version|py_version|src_fingerprint|entry_rels``，
命中跳过整个阶段；交叉构建自动跳过。Python 3.9+ 的 Win7 兼容 DLL 注入
（api-ms-win-core-path 替代 DLL）在 Nuitka 模式下与重编译版 runtime 互斥，
显式 ``--nuitka`` 时自动关闭 Win7 组件替换（产物收窄为 Win8+，日志告警）

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
       ├── <name>-<ver>-py<ver>-windows-x86_64-slim-setup.exe   # Windows NSIS
       ├── <name>_<ver>-py<ver>-slim_amd64.deb                  # Linux .deb
       └── <name>-<ver>-py<ver>-linux-x86_64-slim.tar.gz  # Linux 便携包

多入口机制
----------

单个项目可声明多个入口，每个入口生成独立 exe，共享 runtime/依赖/源码。每个入口
按自身脚本 import 推断 CLI/GUI 类型，支持 cli/gui/web 混合。

入口声明来源与优先级：

1. ``[project.scripts]``（PEP 621 标准）：``name = "module:function"``，
   fspack 自动识别 flat/src layout 将 dotted module 解析为脚本文件路径。
2. 无任何入口声明时按文件名兜底扫描（``detect_entry``）。

``[tool.fspack.entries]`` 已移除支持：声明该表时报错，提示迁移到
``[project.scripts]``。

多入口模式下每个入口写入 ``<name>.entry`` 文件，C loader 运行时按
``<exe_basename>.entry`` 查找入口脚本。单入口项目仍写 ``.entry`` 文件，向后兼容。

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

fspack 支持通过环境变量或 CLI 标志启用的离线模式，适用于无网络环境（内网 CI、
离线打包机）或需精确控制缓存来源的场景。

启用方式
~~~~~~~~

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - 变量/标志
     - 作用
   * - ``FSPACK_OFFLINE``
     - 设为 ``1``/``true``/``yes``/``on``（不区分大小写）启用离线模式
   * - ``fsp b -O`` / ``--offline``
     - 单次构建启用离线模式（分发时设 ``FSPACK_OFFLINE=1``，仅当前命令进程生效）
   * - ``FSPACK_CACHE_DIR``
     - 覆盖缓存根目录（默认 ``~/.fspack/cache``），所有子模块缓存目录派生自此

缓存目录结构
~~~~~~~~~~~~

缓存根目录下按子模块划分，互不干扰：

.. code-block:: text

   <cache_root>/
   ├── embed/                # Windows embed python zip
   ├── standalone/           # Linux python-build-standalone tar.gz
   ├── standalone-windows/   # Windows standalone tarball（nuitka 编译与 tkinter 提取共享）
   ├── wheels/               # 第三方 wheel + 依赖解析缓存（.deps_cache.json）
   ├── python/               # Nuitka 编译用 standalone python（按 standalone 版本分目录）
   ├── nuitka/               # Nuitka 包（按 py_version 分目录）
   ├── nuitka-winlibs-mingw/ # Nuitka winlibs gcc 工具链（Windows 编译 .pyd；内部按
   │                         #   Nuitka downloads 约定 gcc/x86_64/<specificity>/，根目录
   │                         #   可放 winlibs zip 归档供识别解压；检测到 MSVC 时跳过预填充）
   ├── nuitka-work/          # Nuitka 编译中间缓存（NUITKA_CACHE_DIR 重定向，含 clcache/
   │                         #   scons-config 等；与系统默认位置隔离防历史污染，损坏率
   │                         #   过高时自动清空重试）
   ├── loaders/              # C loader 编译缓存（按源码 hash 命名）
   ├── ccache/               # ccache 二进制与编译缓存
   ├── tkinter/              # tkinter 补充包缓存（按 standalone 版本命名的 zip）
   └── win7-dll/             # Win7 重编译版 python3XX.dll 的 embed zip 缓存

增量 action 标记（图标转换、embed stdlib 精简、Win7 dist 扫描、loader 校验、
manifest 哈希备忘录）不放在缓存根目录，而是写到 ``<dist>/build/action-*.json``：
``build/`` 不进入安装包、也不参与 dist 扫描，标记随构建产物一起被
``fsp c`` 清理，避免"缓存认旧产物"的跨项目串味。

子模块缓存目录通过 :mod:`fspack.config.cache` 的 ``embed_cache_dir()``/
``standalone_cache_dir()``/``wheel_cache_dir()``/``nuitka_cache_dir()``/
``nuitka_winlibs_cache_dir()``/``nuitka_work_cache_dir()``/``loader_cache_dir()``/
``ccache_cache_dir()``/
``tkinter_cache_dir()`` 派生，统一从 ``cache_root()`` 计算，确保
``FSPACK_CACHE_DIR`` 环境变量对所有子模块生效。

工作原理
~~~~~~~~

离线模式在所有下载入口（``runtime/download.py``/``wheels/downloader.py``/``nuitka/env.py``/
``nuitka/winlibs.py``/``builtin.py``）检查 ``is_offline()``，缓存命中时正常返回，缓存未命中时立即抛出
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
   * - ``runtime/download.py``
     - ``EmbedError``
     - embed python / standalone tarball 缓存未命中
   * - ``wheels/downloader.py``
     - ``DependencyError``
     - ``--no-index`` 解析失败（缓存 + find-links 均未命中）
   * - ``nuitka/env.py``
     - ``NuitkaError``
     - standalone python / Nuitka 包缓存未命中
   * - ``nuitka/winlibs.py``
     - ``NuitkaError``
     - Windows 时 winlibs gcc 工具链缓存未命中（无 gcc.exe 且无本地归档）
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
（详见各 ``__init__.py`` docstring）。层间依赖方向、"叶子模块"清单与
facade 例外见 :doc:`adr/0001-layering`，由 ``tests/test_layout.py`` 机械守卫。

顶层模块
~~~~~~~~

.. list-table::
   :widths: 25 75
   :header-rows: 1

   * - 模块
     - 职责
   * - ``cli.py`` / ``cli_parser.py`` / ``cli_init.py`` / ``cli_help.py``
     - CLI 入口与命令分发（cargo 风格短命令 ``fsp b/c/r/p``）+ argparse 参数声明 + ``init`` 子命令 + 帮助/版本快速路径；延迟导入重模块使 ``--help`` 提速
   * - ``builder.py``
     - 高层构建 facade，re-export ``pipeline.build``/``runtime``/``loader``/``sync`` 等
   * - ``analyzer/``（子包：``analysis`` / ``ast_scan`` / ``fingerprint``）
     - AST 扫描源码 import，分类标准库/本地/第三方依赖；``analysis`` 负责编排与
       串行/并行调度（按采样预估的串行成本决定是否启用进程池），``ast_scan`` 负责
       单文件 AST 与 QML 解析，``fingerprint`` 负责源码指纹
   * - ``runner.py``
     - 运行已打包项目（原生可执行文件直跑；Linux 下 ``.exe`` 用 wine）
   * - ``console.py``
     - rich 驱动的彩色输出与日志配置
   * - ``platform.py``
     - 平台检测（Windows/Linux）与 ``Platform`` 枚举
   * - ``progress.py``
     - ``BuildTracker``（阶段进度与汇总）与 ``spinner`` 进度显示
   * - ``metrics.py``
     - ``StageRecord``/``StageRecorder`` 阶段指标数据结构（从 ``progress`` 拆出的叶子模块）
   * - ``astutil.py``
     - ``collect_imports``：单棵 AST 的导入收集。``config``（推断 app 类型）与
       ``analyzer`` 都向下依赖本叶子模块，消除 config ↔ analyzer 跨层往返
   * - ``archive.py``
     - zip 条目安全校验原语（拒绝绝对路径/路径穿越/盘符/符号链接），
       ``runtime.extract`` 与 ``slim.unpack`` 共用，避免精简层反向依赖运行时层
   * - ``jsoncache.py``
     - JSON 缓存读取骨架（解析 → 根 dict 校验 → 损坏删除/忽略），供依赖解析缓存等复用
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
   * - ``pipeline/``（``__init__.py`` / ``executor.py`` / ``*_stage.py`` / ``stages.py`` / ``context.py``）
     - 构建流水线编排（``build()`` 入口，阶段调度）
   * - ``runtime/``（``__init__.py`` / ``download.py`` / ``extract.py`` / ``urls.py`` / ``hashes.py`` / ``trim.py`` / ``stdlib_zip.py`` / ``pth.py``）
     - ``RuntimeDownloader``：embed python / python-build-standalone 下载解压 + sha256 清单校验 + 安全解压 + stdlib 精简/zip 化 + ``._pth`` 生成
   * - ``loader/``（``__init__.py`` / ``source.py`` / ``compile.py`` / ``toolchain.py`` / ``cache_keys.py`` / ``resource.py``）
     - C loader facade：源码资源读取（``assets/loader/*.c``）+ 编译器基类/平台子类 + 工具链发现 + 缓存键 + icon/版本信息资源
   * - ``installer/``（``__init__.py`` / ``base.py`` / ``facade.py`` / ``dist_prep.py`` / ``request.py`` / ``linux.py`` / ``macos.py`` / ``nsis.py`` / ``zip.py`` / ``sevenzip.py``）
     - 安装包 facade：NSIS / .deb + tar.gz / .pkg + .dmg / 跨平台 zip 与 7z（``ReleaseRequest``/``SignOptions`` 参数收敛）
   * - ``wheels/``（``__init__.py`` / ``downloader.py`` / ``pip_args.py`` / ``process.py`` / ``output.py`` / ``resolver.py`` / ``local_cache.py`` / ``uv_bridge.py`` / ``parallel.py`` / ``sdist.py`` / ``cache.py`` / ``markers.py``）
     - wheel 下载 facade：入口编排（``downloader``）+ 参数构造（``pip_args``）+ 进程执行（``process``）+ 输出解析（``output``）+ pip/uv 调用 + 本地缓存 uv 离线解析（``local_cache``）+ sdist 回退 + 并行下载 + 依赖解析缓存 + python_version 标记预过滤
   * - ``nuitka/``（``__init__.py`` / ``compiler.py`` / ``env.py`` / ``standalone.py`` / ``winlibs.py`` / ``indexes.py`` / ``ccache.py`` / ``compile.py`` / ``strip.py`` / ``verify.py`` / ``protocol.py`` / ``progress.py``）
     - Nuitka 编译 facade：环境就绪 + standalone python + winlibs gcc 工具链 + 国内索引回退 + ccache + 并行编译调度 + 编译流程 + 产物剥离 + 验证
   * - ``pyc/``（``__init__.py`` / ``compile.py`` / ``stamp.py`` / ``source_strip.py``）
     - 字节码预编译（``compileall`` + stamp 缓存 + .py 源码剥离）
   * - ``win7/``（``__init__.py`` / ``check.py`` / ``dll.py`` / ``patch.py`` / ``scan.py`` / ``shim_build.py``）
     - Win7 兼容门禁：PE 导入表静态检查 + 重编译版 python3XX.dll 下载校验 + dist 全量扫描报告 + 导入表原地改名（``patch``）+ shim 就地编译（``shim_build``）
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
   * - ``action_cache.py``
     - 通用增量 action 缓存（输入指纹 → ``<dist>/build/action-<name>.json``）：
       图标转换、embed stdlib 精简、Win7 dist 扫描、loader 校验、manifest 哈希
       备忘录共用一套键/版本/损坏容忍语义
   * - ``manifest.py`` / ``sbom.py``
     - 产物清单（大小 + SHA256，哈希按 (size, mtime_ns) 备忘录复用）与 SPDX 2.3 SBOM 生成
   * - ``site_packages.py``
     - site-packages 目录定位与包名归一化（``normalize_pkg_name``）
   * - ``lockfile.py``
     - 依赖锁文件（``requirements.lock``）：``--lock`` 构建成功后写出实际
       wheel 的 ``name==version --hash=sha256:...``，``--frozen`` 按锁文件下载
       并校验哈希，缺失/不符直接报错
   * - ``profile.py`` / ``profile_log.py`` / ``log_file.py``
     - 构建耗时/内存 profile 采集与渲染、性能日志落盘与历史对比、构建日志文件
       （TEXT/JSON 格式，``try/finally`` 清理）
   * - ``size_report.py``
     - 构建产物按目录/类型汇总体积与 Top-N 明细
   * - ``dep_analyzer/``（``common`` / ``graph`` / ``pe`` / ``elf`` / ``macho``）
     - 二进制依赖分析（``--analyze-deps``）：按平台解析 PE/ELF/Mach-O 导入表
       构图并剥离无引用文件

slim/ 子包
~~~~~~~~~~

wheel 精简 facade：

- ``base`` — 抽象基类
- ``spec`` — 注册表
- ``qt``/``libs``/``default`` — 具体 spec
- ``unpack`` — 按需解压

性能优化
--------

fspack 内置多层性能优化，量化数据、基线快照与更新流程统一见 :doc:`performance`：

- **增量缓存**：源码指纹缓存（依赖分析结果）、``.pyc`` stamp、Nuitka stamp、
  C loader 源码哈希、图标转换摘要、embed stdlib 精简指纹、Win7 扫描/loader
  校验签名、manifest 哈希备忘录（按 (size, mtime_ns) 复用 SHA256）。缓存命中
  只跳过对应的"昂贵动作"，热构建仍需枚举源码、解包/校验 dist 与扫描全量 PE——
  不是"只重做变更文件"：Win7 兼容扫描在热构建里通常是最大单项，因为它必须
  复核整棵 dist 的导入表
- **通用 action 缓存**：上述标记统一由 :mod:`fspack.packaging.action_cache`
  管理（键 = 输入指纹，值 = ``<dist>/build/action-<name>.json``，版本号/损坏
  即视为未命中），避免四套各自实现漂移
- **CLI 懒加载**：``fsp`` 入口延迟导入重模块（``config``/``console``/``platform``），
  ``fsp --help`` 冷启动远低于全量导入
- **wheel 本地解析**：依赖缓存未命中时先用 ``uv pip compile --no-index`` 在本地
  缓存上解析精确版本，替代 ``pip download --no-index`` 全量预检；无法确定时
  回退原预检
- **wheel 并行下载**：``uv`` 解析精确版本后用 ``ThreadPoolExecutor`` 并行
  ``pip download --no-deps``，失败包自动 sdist 回退构建
- **AST 并行门槛**：按等距采样的 AST 节点密度预估串行解析成本，只有预估能省
  下明显时间才启用 4 进程池——注释密集型源码（字节多、节点少）保持串行
- **Win7 兼容**：Python 3.9+ 注入 api-ms-win-core-path 替代 DLL，支持在 Win7/Win2008R2 运行
- **国内镜像**：默认阿里云源 PyPI 与 embed python 镜像，``--mirror`` 切换（aliyun/huawei/tsinghua）
- **彩色进度显示**：rich 驱动的步骤进度（> 准备运行时 / √ 构建完成），
  错误/警告/一般消息颜色区分，``-v`` 开启 DEBUG 日志

完整 API 参考见 :doc:`api`。
