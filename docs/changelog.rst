更新日志
=========

v0.4.16（未发布）
------------------

- chore: 移除覆盖度低的简单示例模板（``cli_helloworld``/``cli_office``/``pygame_cli``），测试改用 ``tk_app``（无依赖）与 ``web_app``（flask 依赖）等现存模板，同步更新 README 示例清单与集成文档中的模板路径
- fix: 前端构建命令（``pnpm install``/``run build``）改为流式透传输出并增加 600s 超时保护。原实现静默捕获输出且无超时，vite/vue-tsc 构建数分钟无任何显示被误认为卡死，真卡死时无限阻塞；超时后 Windows 用 ``taskkill /T /F`` 递归终止 ``pnpm.CMD → node → vite`` 整棵进程树（仅杀直接子进程时孙进程持有管道写端，drain 线程等不到 EOF 永久阻塞），失败与超时均抛含输出尾部的明确错误

v0.4.15
-------

- feat: ``fsp b`` 自动识别 web 项目结构并在打包前构建前端，产物仅含前端构建结果、源码不再进入 dist
- feat: 新增 Win7 产物兼容保障——loader exe PE 导入表硬校验与 dist 全量兼容扫描报告；新增 win7 重编译版 ``python3XX.dll`` 清单驱动下载并集成 ``runtime_stage``，Windows 3.12+ 目标自动替换官方 DLL
- feat: ``fsp doctor`` 新增 Win7 兼容自检项，NSIS 安装包增加 UCRT 缺失检测提示
- feat: 新增 ``webview_app`` 前后端分离 Web 模板（Vue + Vite + pywebview），并精简模板依赖（移除未使用的 psutil/pyperclip/win10toast 与 hatch 前端构建钩子）
- perf: Nuitka 构建优先复用构建机 python 并共享 standalone tarball 缓存
- fix: 落实全量审查修复约 60 项（PE 导入表错位、缓存误删、stamp 键缺失、网络重试盲区等），包 ``__init__`` 职责迁移至子模块并保持 facade 兼容
- fix: 字节码预编译用解释器 ``-O``/``-OO`` 标志替代 ``compileall -o``，修复 Python 3.8 报 unrecognized arguments 导致预编译整体跳过
- fix: Nuitka 产物验证区分二进制损坏与依赖缺失，依赖缺失不再误删 ``.pyd`` 并保留 ``.py``
- fix: Nuitka 编译参数改用 ``--mode=module`` 并显式 ``--nofollow-imports``，消除 4.x 下两类警告
- fix: ``fsp c`` 清理 dist 时长路径删除失败导致残留
- refactor: 大模块按职责拆分为子模块——packaging 顶层 runtime/pyc/win7 三家族归组为子包，loader/compile、installer/base、config/parsing、wheels/resolver、doctor/envs 与 doctor/templates 各自拆分，facade 保持导入路径兼容
- docs: 同步架构文档与迭代记录，清理已完成的 Win7 兼容开发计划文档
- chore: 调整示例项目配置，修复 CI 无 mingw 环境下 loader 测试失败与 wheel 下载基线测试 mock 签名，统一代码格式化

v0.4.14
-------

- feat: ``fsp cache`` 扩展为多 cache 类型健康检查与清理，覆盖 ``~/.fspack/cache/`` 下全部 7 个子目录（wheels/embed/standalone/nuitka/loaders/ccache/tkinter）。新增 ``--target <name>`` 限定单 cache 类型，``--stale`` 启用过期文件清理。各扫描器识别三类问题：损坏文件（zip/tar 结构非法、PE 头缺失、空文件，扫描期自动删除）、过期文件（版本不在 ``KNOWN_*_VERSIONS`` 的旧 zip/tar/子目录，需 ``--stale`` 显式清理）、孤儿文件（wheels 专用：未被 deps 引用的 wheel）。默认扫描全部 cache 类型，逐个渲染详细报告
- fix: loader 缓存健康检查在非 Windows 平台漏检损坏 exe

v0.4.13
-------

- fix: 修复应用清单版本号格式错误

v0.4.12
-------

- fix: loader 资源编译修复——补全 Windows 资源脚本中 StringFileInfo 的 END 配对，资源编译失败时跳过缓存写入

v0.4.11
-------

- docs: 新增安全与分发章节

v0.4.10
-------

- feat: Windows loader exe 自动嵌入 PE 资源段（VS_VERSIONINFO + application manifest），降低 Defender 等杀软启发式误报。版本信息从 ``pyproject.toml`` 的 ``[project].description`` 与 ``[project].authors[0].name`` 提取填充 CompanyName/FileDescription/ProductName 等字段；manifest 声明 asInvoker、PerMonitorV2 DPI 感知、Win7-11 supportedOS；资源段变化纳入 loader 缓存键触发重编。README 新增「安全与分发」章节，补充代码签名（signtool）使用与误报申诉指引
- refactor: 重构资源编译逻辑，统一资源编译入口与测试用例，调整导入顺序并替换内置 xml 转义实现

v0.4.9
------

- refactor: 调整 Qt DLL 加载的环境变量设置逻辑
- chore: 移除未使用的 pytest-asyncio 依赖

v0.4.8
------

- feat: wheel 下载实时速度显示——单包下载流式输出 pip 进度条，``_DownloadMonitor`` 监控 ``cache_dir`` 文件大小变化显示实时下载速度
- fix: 单包下载加事件日志，避免静默下载被误判为卡住

v0.4.7
------

- perf: 并行化 pyc 编译与 SBOM/manifest 生成，修复 tracemalloc 内存峰值采集 bug
- fix: 清理 data-dirs 下 compileall 生成的 ``__pycache__``

v0.4.6
------

- fix: 消除命令因未捕获异常抛出原始 traceback 的严重缺陷
- fix: 完善配置和源码文件读取的错误处理
- refactor: 模板加载增加缓存文件与目录的跳过过滤

v0.4.5
------

- fix: 模板加载遇非 UTF-8 文件跳过而非崩溃

v0.4.4
------

- feat: 新增 ``manifest`` 子命令，init/doctor 子命令拆分与配置缓存优化
- perf: 优化依赖分析与缓存写入的性能与内存占用
- refactor: 整合项目代码与包结构，doctor/analyzer 子包化并抽取公用工具
- refactor: 数据驱动整合 6 个 CLI 声明文件为 2 个
- build: sdist 隐藏文件改为 ``.*`` 全局排除 + 三白名单 force-include

v0.4.3
------

- feat: ``data-dirs`` 配置支持排除数据资源目录的依赖扫描和指纹计算
- fix: PySide2 模板限制 Python<3.10，避免 3.10+ 无 wheel 导致下载失败
- fix: tkinter 打包检测缓存损坏并自动重建
- refactor: 模板系统重构为文件模板加载机制，消除 registry.py 硬编码，统一模板数据结构与加载逻辑

v0.4.2
------

- feat: 新增前后端分离 Web 打包功能（``AppType.WEB`` + ``web-static-dirs`` 配置 + 项目模板）
- fix: 修正前后端分离模板的静态资源目录配置
- refactor: 删除 ``RuntimeDownloader.ensure`` 死代码方法

v0.4.1
------

- feat: 新增 ``data-dirs`` 配置以保留数据资源目录

v0.4.0
------

- feat: 多入口默认入口选择改为 GUI 优先、同类型按字母排序
- fix: 修复 ``_validate_tar_member`` 误拒 terminfo 合法别名链接导致 Linux 打包失败
- fix: 修复 ``.gitignore`` 的 ``wheels/`` 规则误匹配 ``src/fspack/packaging/wheels/`` 子包导致 PyPI 发布包缺失 wheels 模块
- refactor: site-packages 平铺到 ``dist/site-packages``，与 runtime 平级

v0.3.26
-------

- fix: 豁免 ``numpy/_core/tests/_natype.py`` 与 ``_locales.py`` 不被嵌套 tests 规则剥离，修复 scipy 项目打包后 ``import scipy`` 触发 numpy.testing 导入失败
- fix: 修复 python-build-standalone tarball 相对路径符号链接被误拒
- refactor: 预编译拆分为两次 ``compileall`` 调用适配不同优化级别，拆分 ``_precompile_pyc`` 并新增 stamp 键逻辑测试

v0.3.25
-------

- refactor: 修复 QML 项目 SVG 与控件加载失败问题

v0.3.24
-------

- fix: 修复 QML 项目 SVG 加载失败，自动绑定 QtSvg 依赖并补全 Widgets 强制保留逻辑

v0.3.23
-------

- fix(slim): QML 项目自动绑定 QtSvg 支持。QML 无 ``import QtSvg`` 语法但 ``Image { source: "*.svg" }`` 通过 imageformats 插件加载 SVG，fspack 检测到 ``Qml`` 在闭包中时自动加入 ``Svg`` 子模块，使 ``Qt5Svg.dll``/``Qt6Svg.dll`` 与 ``plugins/imageformats/qsvg.dll`` 都保留，无需用户显式声明；非 QML 项目不用 SVG 时剥离 ``qsvg.dll`` 与 ``Qt5Svg.dll``，消除"插件保留但依赖 DLL 被剥离"的矛盾
- chore: 默认镜像源切换至阿里云

v0.3.22
-------

- feat: 新增 ``fsp cache`` 子命令与缓存健康检查工具
- feat: wheel 下载 uv 加速，uv 可用时优先用 ``uv pip download`` 替代 pip download
- feat: 构建中断恢复——``--auto-clean`` 自动清理 dist 半成品，``.build_failed`` 记录失败信息
- feat: Nuitka 编译并行化与健壮性基础修复
- feat: 多入口 loader 并行编译，``ThreadPoolExecutor`` 并行编译 entry loader，共享 ``TemporaryDirectory`` 每入口独立子目录
- feat: AST 调优、冷启动惰性化、安全 extract、编译验证增强、依赖分析容错
- feat: ``compare_benchmark`` 支持按基线类别分组对比，不同类别设不同退化阈值
- test: 新增打包速度、entry wrapper 启动、wheel 下载、Nuitka 编译等性能基线测试

v0.3.21
-------

- feat: ``fsp init`` 新增 ``--python-version`` 参数与 Win7 fastapi 守卫
- perf: 延迟 console/profile 导入，``import fspack.builder`` 84ms→56ms
- ci: 放宽 benchmark gate systemic 检测阈值，修复机器抖动误阻断

v0.3.20
-------

- perf: 延迟导入重模块与进度模块，优化 builder 热路径加载性能

v0.3.19
-------

- perf: CLI 启动懒加载与结构解耦优化，``import fspack.cli`` 提速 67%
- docs: 更新文档特性说明与排版，批量移除代码注释中的迭代版本标记

v0.3.18
-------

- feat: 安全加固——依赖哈希校验、SBOM 生成、Windows/Linux 代码签名
- feat: 新增启动时间优化，entry wrapper 注入 lazy-import 钩子与 ``path_importer_cache`` 预填充
- fix: 修复 ``_EXCLUDED_DIRS`` 缺少 ``.uv-cache``/``node_modules``/``.pyrefly_cache``/``htmlcov`` 导致第三方包源码被误扫描
- refactor: packaging 模块按职责拆分为 nuitka/wheels/installer/loader/pipeline 五个子包，``cli_doctor.py``/``analyzer.py``/``slim/qt.py`` 拆分为 facade + 职责子模块
- refactor: 抽取 site-packages 路径查找与包名规范化共性到 ``site_packages.py``，修复 RECORD 路径含逗号时解析错位的潜在 BUG
- test: 补全性能基线矩阵，新增 Nuitka ensure_env 与 wheel 缓存命中基线测试
- chore: ``fsp doctor`` 新增别名 ``d``，更新 benchmark 基线快照

v0.3.17
-------

- perf: 优化打包性能与内存使用，增强信息提示
- fix: extras 信息在构建与打包汇总表中体现

v0.3.16
-------

- ci: benchmark gate 新增系统性退化检测，机器负载波动不阻断 CI

v0.3.15
-------

- feat: 新增 ``[project.optional-dependencies]`` 可选依赖分组支持（PEP 621）。``fsp b``/``fsp p`` 新增 ``--extra <name>`` CLI 参数（可多次指定）启用分组，等价 ``pip install pkg[extra]`` 语义；``[tool.fspack] extras`` 配置默认启用分组；CLI ``--extra`` 完全覆盖配置默认（集合语义，非合并）；自引用 ``my-pkg[extra]`` 递归展开（含循环保护），第三方 ``pkg[extra]`` 原样透传 pip；扩展后依赖纳入依赖分析缓存键，extras 变化触发缓存失效；未知分组名报错并列出可选分组
- ci: benchmark gate 对比策略从「与上一次基线对比」改为「与历史最佳基准对比」。新增 ``scripts/compare_benchmark.py`` 扫描 ``.benchmarks/`` 下所有历史 JSON，按测试名找最小 median 作为最佳基准，当前运行与最佳对比，median 超过最佳 25% 视为退化（exit 1）。与上一次基线对比相比，最佳基准过滤了 GitHub Actions 共享机器性能波动导致的慢运行，减少误报
- fix: matplotlib 模板显式 import tkinter 与 TkAgg 后端，修复打包后 ``plt.show`` 报 FigureCanvasAgg 非交互错误

v0.3.14
-------

- fix: ``fsp init`` 模板 ``requires-python`` 增加 Python 上限版本约束 ``<3.12``（PySide2 模板保持已有的 ``<3.11`` 不变），避免生成的项目在 3.12+ 环境因依赖兼容性问题无法安装
- chore: ``pyproject.toml`` 移除 templates 文件排除规则

v0.3.13
-------

- fix: NSIS 卸载旧版不生效，用 ``_?=`` 参数让 ExecWait 等待卸载真正完成

v0.3.12
-------

- feat: NSIS 安装包支持升级安装，``.onInit`` 检测注册表已安装版本；同版本直接覆盖不打扰，不同版本弹出对话框询问"是否先卸载再安装"，确认后静默调用旧版 ``uninstall.exe /S _?=$INSTDIR`` 等待真正卸载完成再继续（``_?=`` 参数阻止卸载器自我复制到 ``%TEMP%``，否则 ``ExecWait`` 不等待）；``InstallDirRegKey`` 读取上次安装路径作为默认目录，避免重复选择

v0.3.11
-------

- feat: 支持 ``[project.scripts]`` 入口识别与 flat/src layout 自动识别
- feat: ``doctor --bench`` 基准增强——历史基准持久化与横向对比、历史扁平化存储、应用调用响应速度统计、基准机器信息匿名化
- test: 修复 Windows 无符号链接权限与 Rich Table 窄终端换行导致的测试失败，补充 ``[project.scripts]`` 入口识别典型场景测试

v0.3.10
-------

- feat: 新增 ``fsp doctor`` 环境诊断子命令，整合 examples 为 assets/templates 并支持 ``--test`` 模板可调用性验证与 ``--bench`` 基准测试
- feat: 新增 ``fsp init`` 命令与 22 个项目模板
- feat: 新增 macOS 平台 runtime 与 loader 支持，及 ``.pkg``/``.dmg``/codesign 安装包生成
- feat: ``fsp b`` 新增 ``--dry-run`` 预览模式、体积报告、``--log-file`` 日志持久化（text/json 双格式）与 ``--profile`` 耗时分析报告
- feat: 离线环境支持——``FSPACK_CACHE_DIR``/``FSPACK_OFFLINE`` 环境变量、下载层 fail-fast 与 wheel 本地搜索增强
- feat: 新增二进制依赖分析与 QML 依赖扫描，补全 Qt 运行时依赖
- feat: 新增 standalone runtime 精简阶段，Linux 打包体积减少约 100MB
- perf: ``ProjectInfo.from_dir`` 增加 lru_cache 缓存，避免构建流程内重复解析
- fix: 修复 tk app 打包后 ``_tkinter`` ImportError
- fix: 修复顶层模式 ``runpy.run_path`` 下本地绝对导入失败
- fix: 修复 PySide2 QML 打包后运行错误并约束 Python 版本
- fix: 并行下载多包 sdist 回退合并所有失败包 stderr
- fix: 兼容旧版 Python 的 tomllib 导入，修复多入口模板 doctor 运行验证跳过问题
- refactor: 拆分 ``wheel_pip.py``/``pipeline.py``/``nuitka_compile.py`` 等大模块为职责单一的模块，重构模板加载与 cli_doctor 运行验证逻辑
- refactor: 抽离 CI 兼容逻辑到 ``_compat`` 模块，移除始终失效的 ``inject_mingw_runtime_dlls`` 函数
- ci: CI 三 job 增强（Windows 矩阵 + slow-e2e cron + benchmark 门禁），actions 升级 v5/v6 消除 Node.js 20 警告，benchmark 退化门禁改为监控模式
- test: 补充 dep_analyzer 单元测试至 74 个、Linux 平台离线集成测试与 py313 pygame conway CI 测试
- docs: README 重构为使用导向并添加 doctor 命令说明，技术细节移到 ``docs/architecture.rst``
- chore: 删除废弃的 pyqt5_cli_pyall 模板与 GitHub Actions 工作流模板，简化 PyQt/PySide 模板配置

v0.3.9
------

- fix: icon 转换使用 ``bitmap_format=png`` 保留完整 8-bit alpha 通道
- build: sdist 排除 ``.benchmarks``/``.github``/``.trae`` 等 11 项开发内部目录，新增项目 favicon.ico 资源

v0.3.8
------

- feat: 新增 ``--recursive``/``-R`` 递归打包模式，``fsp b -R [project]``/``fsp p -R [project]`` 递归扫描 project 目录下所有含 ``pyproject.toml`` 的子项目依次构建/打包；跳过 ``.venv``/``dist``/``build``/``.git`` 等开发期目录；单项目失败不中断后续项目，最后汇总成功/失败列表并通过退出码（0=全部成功，1=有失败）传播结果，便于 CI 检测
- perf: ``analyzer.source_fingerprint`` 哈希算法从 SHA-256 改为 BLAKE2b（digest_size=32，输出 64 hex 字符与原一致），CPython 实现略快 10-20%
- perf: ``analyzer._local_packages`` 用 ``os.scandir`` 替代 ``Path.iterdir``，``DirEntry.is_file``/``is_dir`` 复用枚举时的 stat 缓存减少系统调用
- perf: ``analyzer._parse_serial``/``_parse_file_worker`` 用 ``Path.read_bytes()`` + ``ast.parse(bytes)`` 替代 ``read_text(encoding="utf-8")`` + ``ast.parse(str)``，``ast.parse`` 内部 C 解码快于 Python 层 ``.decode``，50 文件场景 ``analyze_dependencies`` 提速约 14%
- chore: 新增 py313 康威生命游戏示例，清理旧示例文件

v0.3.7
------

- feat: 新增 scikit-learn 精简规则，剥离 datasets/descr/ 描述文件与 datasets/images/ 示例图片（保留 data/ 运行时必需），fit/predict/transform 等算法 API 不受影响
- feat: 新增 pyarrow 精简规则，剥离 includes/ C++ 头文件与 Cython 定义目录（.pxd 文件需本 spec 覆盖，.h 已由 STRIP_EXTS 剥离），顶层 C 扩展（lib.pyd 等）始终保留
- perf: ``.pyi`` 类型存根文件纳入 ``STRIP_EXTS`` 统一剥离（mypy/pyrefly 等类型检查工具用，应用运行时不需要），所有 spec 共享，无需专门处理；从 ``SUBMODULE_EXTS`` 移除避免按子模块选择性保留
- refactor: 嵌套 tests 目录剥离提升到 ``SlimSpec.NESTED_TEST_DIRS`` 基类属性，所有走兜底的库（pandas/scikit-learn 等）无需专门 spec 即可自动剥离 ``pkg/sub/tests/`` 三级嵌套测试目录；``testing``（单数，numpy 公共 API）不受影响
- refactor: 拆分 ``wheels.py`` 与 ``slim/base.py`` 为职责单一的模块，完成剩余模块拆分与代码质量优化
- test: 新增 4 个端到端 slow 测试覆盖 PySide2 QML、Nuitka 编译与 slim-include 规则
- docs: 同步架构文档与模块索引

v0.3.6
------

- feat: 新增 ``[tool.fspack] slim-include``/``slim-exclude`` wheel 精简用户自定义规则，支持 fnmatch glob 模式强制保留/剥离特定文件；优先级 ``slim-include`` > ``slim-exclude`` > spec 自动分类；用于覆盖 AST 闭包误判、强制剥离 ``opengl32sw.dll``/``translations`` 等不需要的文件
- feat: 构建汇总表新增"节省"列，wheel 精简与标准库精简阶段累计剥离字节数直观显示（如 "45.2MB"），无需翻阅逐 wheel 日志；无剥离时显示 "-" 避免误导
- feat: wheel 精简统计日志，解压完成后输出"剥离 N 个文件，节省 X.YMB / Y.YMB (Z%)"，便于评估精简效果
- fix(slim): 修复 PySide6 6.6+ 拆分 wheel（pyside6_essentials/addons）全量解压问题；``_detect_top_pkg`` 回退匹配使 QtSlimSpec 识别拆分 wheel 的 ``PySide6`` 顶层目录，共享主包 keep_subs；补全 WebEngineCore/WebEngineWidgets 的 Quick/QuickWidgets/PrintSupport 依赖与 Quick 的 OpenGL/QmlMeta 依赖（dumpbin 验证 C 层 DLL 导入表）
- fix: 补全 Quick 闭包的 Qt6OpenGL/Qt6QmlMeta 依赖与 WebEngine 闭包的 C 层 DLL 依赖
- refactor: 抽离 SlimRules 共用类，合并三个相似解析函数
- docs: 完善 wheel 精简用户规则文档与代码注释

v0.3.5
------

- chore: 切换到 Python 3.11，支持 Win7

v0.3.4
------

- fix: Nuitka 编译后 Win7 无法运行，注入 MinGW 运行时 DLL 并限制 ``_WIN32_WINNT=0x0601``
- build: 更新 fspack 依赖到 0.3.3 并移除 nuitka/ccache 配置

v0.3.3
------

- feat: Qt 库精简智能识别 FFmpeg/QML ABI/opengl32sw/lib/cmake/metatypes/QtAsyncio 等可剥离资源
- fix: 修复 standalone python 缺 Win7 兼容 DLL 导致 fspack 自身打包后在 Win7 上无法运行 nuitka 编译

v0.3.2
------

- feat: 新增 ``--extra-index-url``/``--find-links`` 私有包源支持，可多次指定；与 ``[tool.fspack] extra-index-urls``/``find-links`` 配置合并（CLI 追加在后、去重保留首次出现），透传给 pip/uv 的 ``--extra-index-url``/``--find-links``；私有包源纳入依赖解析缓存键，切换源后强制重新解析；sdist 回退路径（``pip wheel``）同步透传私有包源
- feat: 新增 ``nuitka_packages`` 配置项与 ``--nuitka-pkg`` CLI 标志，支持手动指定第三方依赖用 Nuitka 编译
- fix: Nuitka 编译稳定性修复——启用 ``.pyd`` 可加载性验证防止 zig 编译损坏产物、避免 4.x ziglang 交互式下载阻塞构建、修复编译后访问违例与 ``.build`` 残留
- fix: 修复 ccache 反复下载，Windows zip 解压后迁移子目录可执行文件到根
- fix: ``pyc_strip`` 删除 ``.py`` 前迁移 ``.pyc`` 到 legacy 布局，修复 ModuleNotFoundError
- fix: 打包时排除 ``.dep_cache.json``/``.nuitka_compile_stamp``/``.pyc_stamp``/``*.build`` 等构建中间文件
- fix: 入口包装器在 GUI 子系统下用 ``os.devnull`` 替代 None 标准流
- fix: slim 模块名提取用 ``split('.')[0]`` 替代 ``Path.stem``，避免 ABI 标签导致 C 扩展误剥离
- fix: subprocess 调用统一用 UTF-8 解码，避免中文 Windows GBK 解码失败
- fix: ``_satisfies`` 支持 PEP 440 通配符前缀匹配 ``==3.12.*``
- ci: 修复 release 工作流三处失败——``gh release create`` 幂等处理重打 tag、Install NSIS 步骤写入 ``GITHUB_PATH``、release job 显式指定 ``GH_REPO``
- build: ``pyproject.toml`` 增加 Nuitka 打包依赖白名单
- chore: 新增 Python 3.14 complex CLI 示例项目，示例项目增加 Python 版本标识覆盖多版本场景

v0.3.1
------

- feat: 新增 ``[tool.fspack]`` 配置支持，可声明 ``exclude`` 排除目录与 ``nuitka``/``pyc_strip``/``no_site``/``pyc_optimize`` 等构建默认值
- feat: 新增 ``ccache`` 配置项与 ``--ccache`` CLI 标志，加速 Nuitka 重复编译
- feat: Nuitka 环境就绪失败时回退到 ``.pyc`` 模式，避免中断构建
- perf: ``fsp p`` 默认复用已就绪 dist，避免 ``fsp b`` 后重复构建
- perf: Nuitka 编译跳过 ``__init__.py``，避免无收益的 subprocess 开销
- fix: ``fsp p`` 透传 ``[tool.fspack]`` 构建默认值到 ``build()``，修复 nuitka 等配置不生效
- fix: ``pyc_strip`` 保留入口 ``.py`` 文件，修复 Nuitka 打包后 ``runpy.run_module`` ImportError
- fix: 修复 Windows CI 上 RichHandler 输出中文日志时 UnicodeEncodeError
- refactor: 引入 BuildContext 聚合阶段函数上下文，按职责拆分大函数（最大函数从 260 行降到 91 行）
- chore: 测试 mock makensis 调用修复 Linux CI 无 NSIS 时 4 个测试失败，自身打包排除 templates 目录

v0.3.0
------

- feat: 新增 ``--nuitka`` 编译模式，用户源码编译为 .pyd 本机执行（速度提升 30-50%）；按 Python 版本锁定 Nuitka 版本（3.8/3.9→2.5.1，3.10+→4.1.3），自动装到本地缓存 ``~/.fspack/cache/nuitka/`` 不污染 dist/runtime；Windows 用缓存的 standalone python 运行编译，避免 embed python 触发 reExecute fork bomb；入口文件保留 .py 兼容 ``runpy.run_path()``；stamp 缓存命中跳过整个阶段；缺 pip 时 ensurepip/uv 两轮自救
- feat: 新增 ``--pyc-optimize`` 字节码优化级别与 ``--no-site`` 禁用 site.py 加载选项
- feat: QtWebEngine 资源按需保留（.debug.pak 无条件剥离，icudtl.dat/QtWebEngineProcess 按 WebEngine 使用情况保留）
- feat: 打包阶段（生成 NSIS 脚本/编译安装包）纳入 BuildTracker 汇总表统计
- feat(pyside2-qml-dashboard): 新增 WSL 管理仪表盘 QML 示例项目
- fix: Nuitka 编译用心跳线程与流式输出显示进度，避免长时间无输出被误认为卡死；``--jobs=1`` 限制 C 编译并行度
- fix: ``tarfile.extractall`` 加 PEP 706 ``filter="data"`` 过滤器（Python 3.12+），消除 DeprecationWarning 并阻止路径穿越（runtime.py 与 nuitka.py 两处）
- fix(test): Linux e2e 测试增加平台跳过条件，Windows 上的 mingw gcc 缺 ``dlfcn.h`` 无法交叉编译 Linux loader
- fix: 转义 CLI help 字符串中的 ``%`` 避免 argparse 格式化崩溃
- refactor: 封装 BuildOptions 聚合 build 开关参数，移除 commands/ 目录薄包装层
- refactor: 重构依赖检测与版本选择逻辑，修复镜像与命名问题

v0.2.6
------

- fix(linux): wrapper 显式添加 site-packages 到 sys.path
- fix(ci): CI 环境下禁用 rich legacy_windows 渲染

v0.2.5
------

- fix(ci): 修复 CI 自打包三处失败
- chore: 更新 fspack 依赖版本到 0.2.4

v0.2.4
------

- fix(ci): release.yml 在 uv sync 后显式安装 pip，修复 fspack download_wheels 失败
- chore: 更新 fspack 依赖包版本到 0.2.3

v0.2.3
------

- ci: release.yml 增加 Windows/Linux 原生平台自打包 job，发布构建版到 GitHub Release
- docs: README 与 integration.md 补充 fspack 自身原生平台打包发布流程说明
- refactor: 删除 slim/__init__.py 中无用的 import zipfile 及相关注释
- chore: 同步 .python-version 至 3.13 并更新 uv.lock

v0.2.2
------

- feat: 发行包文件名 Python 版本标签使用完整版本号（如 py3.13.14）而非 major.minor
- perf: 优化 find_favicon 性能并补充解析图标阶段追踪
- fix: 修复 --py-version 传短版本号（如 3.13）时未映射到完整版本导致下载 404
- fix: 修复 .python-version 文件为 UTF-16 编码时解码失败的问题
- fix: 安装包文件名使用与构建一致的 Python 版本
- chore: 更新 3.13 embed 版本映射为 3.13.14，升级 Python 版本到 3.13

v0.2.1
------

- perf: 重复构建增量优化——source_fingerprint os.walk 剪枝、预编译 stamp 缓存、copy_source 跳过未改动文件、保留 __pycache__、分析依赖缓存
- fix: 修复 CI Linux 上交叉构建守卫导致预编译测试失败
- build(python): 调整 Python 版本要求为 3.11

v0.2.0
------

- feat: 新增标准库精简与字节码预编译阶段，构建汇总体现精简解压
- feat: 新增 fsp p --format 选项，支持 zip 跨平台便携包与多格式发行包调度
- feat: 注入 Win7 兼容 DLL 支持 Python 3.9+ 在 Win7 上运行
- feat: 新增 favicon 自动搜索与多格式图片 icon 支持
- feat: KNOWN_EMBED_VERSIONS 新增 3.14 映射，发行包文件名体现 Python 版本与 slim 精简标识
- fix: C loader 改用 SetDllDirectoryW/LoadLibraryExW 修复 Win7 上 DLL 搜索路径问题
- fix: 修复 .python-version=3.13 解析为短版本号致 embed 下载 404
- refactor: 清理 packaging/__init__ 死代码，find_favicon 改用 os.walk 浅层目录优先
- chore: 默认镜像源切换至清华源，更新 Python 版本要求与开发依赖分组

v0.1.9
------

- refactor(analyzer): 新增开发期目录排除规则，添加对应测试

v0.1.8
------

- feat: 新增 tkinter 内置库打包，从 python-build-standalone Windows 构建提取 tkinter 组件，并新增 tk_app 示例验证
- feat: 新增 numpy/lxml、matplotlib/scipy 等大型库精简规则与可复用分类辅助
- feat: 默认精简规则剥离 examples/docs/tests 等非必要子目录，精简 dist-info 元数据与 .h/.cpp/.lib/.pdb/.pyc/.exe 等扩展名
- feat: 精简 dist/src 复制内容，剥离开发期元数据/工具配置/凭证/文档/测试代码；installer 文件名加入版本号
- perf: 惰性策略优化——CLI 按需导入 command、合并 AST 遍历与 zip 打开
- refactor: 打包过程模块抽象为 packaging 包，提取 RuntimeDownloader/LoaderCompiler/Installer 基类；整合 mirror/wheel_cache/project 模块消除循环依赖；优化 PySide2/6 依赖自动闭包逻辑
- fix: 修复 embed python 3.8 typing_extensions 崩溃、matplotlib ft2font C 扩展误剥离、sci_scipy ModuleNotFoundError、PySide2 qml 目录误保留、slim spec 注册顺序依赖等多个问题
- fix: NSIS 安装包排除 uv build 产物、loader 缓存命中不再创建空目录、修复示例代码 bug
- test: 为 cli_complex/cli_office/pygame_conway/pygame_gktetris 补充 slow 端到端测试
- chore: 修复 ruff/pyrefly 配置漂移，批量更新项目配置与文档

v0.1.7
------

- feat: NSIS 安装包默认为所有应用类型生成桌面快捷方式与开始菜单程序快捷方式
- feat: NSIS 安装包新增添加/删除程序注册表条目与开始菜单卸载快捷方式
- feat: clean 清理 dist 时保留 installer.nsi 便于改代码后重新打包分发
- feat: slim 改进 Qt 库白名单精简策略，按子模块动态扩展并自动计算 C 层依赖闭包
- chore: 增加自身打包配置
- docs: 完成 req-19 简化策略收尾，新增 iter-19 迭代记录并归档需求

v0.1.6
------

- feat: 用 uv pip compile 在线解析依赖图，避免 pip resolution-too-deep
- feat: 添加 pip 下载过程实时流式输出可视化
- fix: 修复 src-layout 项目入口包装器无法识别包模式的问题
- fix: uv 路径加 sdist 回退，处理无 wheel 的包（如 win-unicode-console），重试用 -i index 而非 --no-index
- fix: 修复 _stream_subprocess 在 bufsize=0 时 stderr 无 read1 方法导致崩溃

v0.1.5
------

- fix: 修复依赖下载时 python_version 环境标记未过滤和无 wheel 包无法下载的问题

v0.1.4
------

- fix(cli_office): 改进 PDF 文件生成的错误处理
- chore(vscode): 新增 Python 格式化器与保存动作配置
- chore: 将 fspack 依赖版本更新至 0.1.3

v0.1.3
------

- feat: 实现 exe 图标自定义功能，支持 CLI 指定与配置文件配置
- feat: 新增入口包装器，支持 Qt 插件路径与相对导入，添加默认应用图标
- test: 新增 multi_entry slow 端到端测试，完善 release-pack.yml 多入口支持
- docs: 修复 README 中仓库链接的下划线错误
- chore: 调整 lint 配置与示例代码，整理代码格式与清理冗余文件，更新 copier 模板版本到 v0.8.5

v0.1.2
------

- chore: 维护性版本发布，仅更新 uv.lock 依赖锁文件

v0.1.1
------

- feat: 新增单项目多入口打包功能，支持 cli/gui/web 混合类型
- feat: 依赖解析缓存跳过 pip 调用，wheel 下载优先 --no-index 本地缓存解析，拆分下载与解压 stage
- feat: 添加构建进度跟踪与可视化功能，添加复杂 cli 示例项目
- refactor: dataclass 工厂方法下沉为 from_* 类方法，wheel 缓存移至 ~/.fspack/cache/wheels 支持跨项目复用
- build(pyproject): 新增 fsp 作为 fspack 的别名命令
- docs: 新增 CI/CD 集成指南与可复用 workflow 模板

v0.1.0
------

- feat: 实现 fspack P1 CLI 与 C loader 垂直切片，新增 fsp p 子命令生成 NSIS 安装包（P2）
- feat: 新增 P3 Linux 平台支持（python-build-standalone + gcc loader）
- feat: 新增 5 类典型示例端到端验证并修复 _pth 位置与镜像源（P5）
- feat: 新增 Linux 安装包支持 .deb + tar.gz 便携包（P7）
- feat: wheel 缓存复用，按 uv→pip→fspack 缓存→下载优先级获取依赖
- feat: 精简打包按子模块 import 选择性解压 wheel，Qt5 原生 DLL 归 shared 避免运行时加载失败
- feat: Python 版本自动解析（.python-version + requires-python），引入 rich 彩色进度显示
- fix: 修复 Linux loader 运行崩溃、pygame SysFont Windows 崩溃、wheel 缓存重复下载等多个问题
- docs: 重写 README 突出 fspack 打包 CLI 核心功能，文档化 embed python 不含 tkinter 的 Windows 打包限制
- build: 调整 ruff、pyrefly 和 bumpversion 排除路径，更新 tox 命令和新增 pypi 发布任务
