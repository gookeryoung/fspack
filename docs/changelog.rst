更新日志
=========

v0.2.7（未发布）
----------------

- feat: 新增 ``--nuitka`` 编译模式，用户源码编译为 .pyd 本机执行（速度提升 30-50%）；按 Python 版本锁定 Nuitka 版本（3.8/3.9→2.5.1，3.10+→4.1.3），自动装到本地缓存 ``~/.fspack/cache/nuitka/`` 不污染 dist/runtime；Windows 用缓存的 standalone python 运行编译，避免 embed python 触发 reExecute fork bomb；入口文件保留 .py 兼容 ``runpy.run_path()``；stamp 缓存命中跳过整个阶段；缺 pip 时 ensurepip/uv 两轮自救
- feat: 新增 ``--pyc-optimize`` 字节码优化级别与 ``--no-site`` 禁用 site.py 加载选项
- feat: QtWebEngine 资源按需保留（.debug.pak 无条件剥离，icudtl.dat/QtWebEngineProcess 按 WebEngine 使用情况保留）
- feat: 打包阶段（生成 NSIS 脚本/编译安装包）纳入 BuildTracker 汇总表统计
- feat(pyside2-qml-dashboard): 新增 WSL 管理仪表盘 QML 示例项目
- fix(slim): 补全 Qt QML/Quick 模块依赖映射，修复 QML 项目运行时 DLL 缺失
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
