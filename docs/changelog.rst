更新日志
=========

v0.2.7（未发布）
----------------

- ci: benchmark gate 对比策略从「与上一次基线对比」改为「与历史最佳基准对比」。新增 ``scripts/compare_benchmark.py`` 扫描 ``.benchmarks/`` 下所有历史 JSON，按测试名找最小 median 作为最佳基准，当前运行与最佳对比，median 超过最佳 25% 视为退化（exit 1）。与上一次基线对比相比，最佳基准过滤了 GitHub Actions 共享机器性能波动导致的慢运行，减少误报
- feat: 新增 ``[project.optional-dependencies]`` 可选依赖分组支持（PEP 621）。``fsp b``/``fsp p`` 新增 ``--extra <name>`` CLI 参数（可多次指定）启用分组，等价 ``pip install pkg[extra]`` 语义；``[tool.fspack] extras`` 配置默认启用分组；CLI ``--extra`` 完全覆盖配置默认（集合语义，非合并）；自引用 ``my-pkg[extra]`` 递归展开（含循环保护），第三方 ``pkg[extra]`` 原样透传 pip；扩展后依赖纳入依赖分析缓存键，extras 变化触发缓存失效；未知分组名报错并列出可选分组
- feat: 新增 ``--recursive``/``-R`` 递归打包模式，``fsp b -R [project]``/``fsp p -R [project]`` 递归扫描 project 目录下所有含 ``pyproject.toml`` 的子项目依次构建/打包；跳过 ``.venv``/``dist``/``build``/``.git`` 等开发期目录；单项目失败不中断后续项目，最后汇总成功/失败列表并通过退出码（0=全部成功，1=有失败）传播结果，便于 CI 检测
- perf: ``analyzer.source_fingerprint`` 哈希算法从 SHA-256 改为 BLAKE2b（digest_size=32，输出 64 hex 字符与原一致），CPython 实现略快 10-20%
- perf: ``analyzer._local_packages`` 用 ``os.scandir`` 替代 ``Path.iterdir``，``DirEntry.is_file``/``is_dir`` 复用枚举时的 stat 缓存减少系统调用
- perf: ``analyzer._parse_serial``/``_parse_file_worker`` 用 ``Path.read_bytes()`` + ``ast.parse(bytes)`` 替代 ``read_text(encoding="utf-8")`` + ``ast.parse(str)``，``ast.parse`` 内部 C 解码快于 Python 层 ``.decode``，50 文件场景 ``analyze_dependencies`` 提速约 14%
- perf: ``.pyi`` 类型存根文件纳入 ``STRIP_EXTS`` 统一剥离（mypy/pyrefly 等类型检查工具用，应用运行时不需要），所有 spec 共享，无需专门处理；从 ``SUBMODULE_EXTS`` 移除避免按子模块选择性保留
- feat: 新增 scikit-learn 精简规则，剥离 datasets/descr/ 描述文件与 datasets/images/ 示例图片（保留 data/ 运行时必需），fit/predict/transform 等算法 API 不受影响
- feat: 新增 pyarrow 精简规则，剥离 includes/ C++ 头文件与 Cython 定义目录（.pxd 文件需本 spec 覆盖，.h 已由 STRIP_EXTS 剥离），顶层 C 扩展（lib.pyd 等）始终保留
- feat: 构建汇总表新增"节省"列，wheel 精简与标准库精简阶段累计剥离字节数直观显示（如 "45.2MB"），无需翻阅逐 wheel 日志；无剥离时显示 "-" 避免误导
- feat: 新增 ``[tool.fspack] slim-include``/``slim-exclude`` wheel 精简用户自定义规则，支持 fnmatch glob 模式强制保留/剥离特定文件；优先级 ``slim-include`` > ``slim-exclude`` > spec 自动分类；用于覆盖 AST 闭包误判、强制剥离 ``opengl32sw.dll``/``translations`` 等不需要的文件
- feat: wheel 精简统计日志，解压完成后输出"剥离 N 个文件，节省 X.YMB / Y.YMB (Z%)"，便于评估精简效果
- refactor: 嵌套 tests 目录剥离提升到 ``SlimSpec.NESTED_TEST_DIRS`` 基类属性，所有走兜底的库（pandas/scikit-learn 等）无需专门 spec 即可自动剥离 ``pkg/sub/tests/`` 三级嵌套测试目录；``testing``（单数，numpy 公共 API）不受影响
- feat: 新增 ``--extra-index-url``/``--find-links`` 私有包源支持，可多次指定；与 ``[tool.fspack] extra-index-urls``/``find-links`` 配置合并（CLI 追加在后、去重保留首次出现），透传给 pip/uv 的 ``--extra-index-url``/``--find-links``；私有包源纳入依赖解析缓存键，切换源后强制重新解析；sdist 回退路径（``pip wheel``）同步透传私有包源
- feat: 新增 ``--nuitka`` 编译模式，用户源码编译为 .pyd 本机执行（速度提升 30-50%）；按 Python 版本锁定 Nuitka 版本（3.8/3.9→2.5.1，3.10+→4.1.3），自动装到本地缓存 ``~/.fspack/cache/nuitka/`` 不污染 dist/runtime；Windows 用缓存的 standalone python 运行编译，避免 embed python 触发 reExecute fork bomb；入口文件保留 .py 兼容 ``runpy.run_path()``；stamp 缓存命中跳过整个阶段；缺 pip 时 ensurepip/uv 两轮自救
- feat: 新增 ``--pyc-optimize`` 字节码优化级别与 ``--no-site`` 禁用 site.py 加载选项
- feat: QtWebEngine 资源按需保留（.debug.pak 无条件剥离，icudtl.dat/QtWebEngineProcess 按 WebEngine 使用情况保留）
- feat: 打包阶段（生成 NSIS 脚本/编译安装包）纳入 BuildTracker 汇总表统计
- feat: NSIS 安装包支持升级安装，``.onInit`` 检测注册表已安装版本；同版本直接覆盖不打扰，不同版本弹出对话框询问"是否先卸载再安装"，确认后静默调用旧版 ``uninstall.exe /S _?=$INSTDIR`` 等待真正卸载完成再继续（``_?=`` 参数阻止卸载器自我复制到 ``%TEMP%``，否则 ``ExecWait`` 不等待）；``InstallDirRegKey`` 读取上次安装路径作为默认目录，避免重复选择
- fix: ``fsp init`` 模板 ``requires-python`` 增加 Python 上限版本约束 ``<3.12``（PySide2 模板保持已有的 ``<3.11`` 不变），避免生成的项目在 3.12+ 环境因依赖兼容性问题无法安装
- feat(pyside2-qml-dashboard): 新增 WSL 管理仪表盘 QML 示例项目
- fix(slim): 补全 Qt QML/Quick 模块依赖映射，修复 QML 项目运行时 DLL 缺失
- fix(slim): 修复 PySide6 6.6+ 拆分 wheel（pyside6_essentials/addons）全量解压问题；``_detect_top_pkg`` 回退匹配使 QtSlimSpec 识别拆分 wheel 的 ``PySide6`` 顶层目录，共享主包 keep_subs；补全 WebEngineCore/WebEngineWidgets 的 Quick/QuickWidgets/PrintSupport 依赖与 Quick 的 OpenGL/QmlMeta 依赖（dumpbin 验证 C 层 DLL 导入表）
- fix: Nuitka 编译用心跳线程与流式输出显示进度，避免长时间无输出被误认为卡死；``--jobs=1`` 限制 C 编译并行度
- fix: ``tarfile.extractall`` 加 PEP 706 ``filter="data"`` 过滤器（Python 3.12+），消除 DeprecationWarning 并阻止路径穿越（runtime.py 与 nuitka.py 两处）
- fix(test): Linux e2e 测试增加平台跳过条件，Windows 上的 mingw gcc 缺 ``dlfcn.h`` 无法交叉编译 Linux loader
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
