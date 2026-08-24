# fspack

> 把 Python 项目变成可执行文件与安装包 —— 一行命令搞定。

[![PyPI](https://img.shields.io/pypi/v/fspack)](https://pypi.org/project/fspack/)
[![CI](https://github.com/gookeryoung/fspack/actions/workflows/ci.yml/badge.svg)](https://github.com/gookeryoung/fspack/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Coverage](https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen.svg)

fspack 让你的 Python 项目秒变可分发的桌面应用。无需改一行代码，`fsp b` 一行命令
产出 `.exe`，`fsp p` 再一行产出 Windows 安装包或 Linux `.deb`。自动分析依赖、
精简体积、预编译加速，开箱即用。

## 30 秒上手

```bash
pip install fspack
cd your-project          # 含 pyproject.toml 的 Python 项目
fsp b                    # 产出 dist/your-app.exe
fsp p                    # 产出 dist/release/your-app-setup.exe
```

就这样。你的 Python 项目已经变成可以分发给别人双击运行的桌面应用了。

## 从模板开始：fsp init

没有项目？一行命令从模板创建：

```bash
fsp init my-app                          # 交互式选择模板（22 个可选）
fsp init my-app --template pyside2       # 直接指定模板
fsp init --list                          # 查看所有可用模板
```

22 个模板覆盖常见场景：

| 分类 | 模板 |
|------|------|
| CLI | helloworld / args / rich / requests / click / typer |
| GUI | pyside2 / pyside6 / pyside2-qml / pyside6-qml / pyqt5 / tkinter |
| 游戏 | pygame / snake |
| 科学 | matplotlib / numpy / scipy |
| Web | flask / fastapi |
| 配置 | pyinstaller / multi-entry / full-config |

每个模板生成可直接打包的项目骨架（`pyproject.toml` + 入口脚本），`cd` 进去立即 `fsp b`。

```bash
fsp init my-gui --template pyside2       # 创建 PySide2 GUI 项目
cd my-gui
fsp b                                    # 打包为 my-gui.exe
```

## 核心特性

| 你想要的 | fspack 给你的 |
|---------|--------------|
| 一行命令打包 | `fsp b` 生成可执行文件，`fsp p` 生成安装包，cargo 风格两字母短命令 |
| 不改源码 | 自动 AST 扫描 import 推断依赖，无需手动声明打包配置 |
| 小体积安装包 | 自动精简 wheel（剥离未用子模块/翻译/头文件）、预编译 `.pyc`、可选剥离 `.py` |
| 跨平台分发 | Windows 出 `.exe` + NSIS 安装包，Linux 出 `.deb` + `.tar.gz`，macOS 出 `.pkg` + `.dmg` |
| 双击就能跑 | 内置便携运行时，用户机无需装 Python；Windows 安装包含快捷方式与卸载器 |
| 首次启动快 | 默认预编译字节码，`--nuitka` 可本机编译提速 30-50% |
| 多入口项目 | 一个项目生成多个 exe（cli/gui/web），共享运行时与依赖 |
| 国内网络友好 | 默认清华镜像，`--mirror` 一键切换阿里/华为源 |
| 递归打包 | `-R` 递归扫描子项目（monorepo 友好），单项目失败不中断，最后汇总结果 |
| 离线打包 | `FSPACK_OFFLINE=1` 仅从本地缓存读取，缓存未命中即报错，不卡死不重试 |

### 自动依赖推断与按需精简

fspack 扫描源码 `import` 推断依赖，并按使用情况精简 wheel。例如
`from PySide6.QtWidgets import QApplication` 只打包 QtWidgets 及其依赖闭包
（QtGui/QtCore），剥离未用的 QtCharts/QtWebEngine 等大体积模块。典型 PySide6
应用可从 300MB 精简到 80MB。

### 多入口项目

一个项目里有 CLI 工具 + GUI 界面 + Web 服务，可声明多个入口，一次打包生成多个
exe，共享运行时与依赖。默认推荐使用 PEP 621 标准的 `[project.scripts]`（fspack
自动识别 flat/src layout，将 dotted module 解析为脚本路径）：

```toml
[project.scripts]
cli = "myapp.cli:main"   # 生成 cli.exe
gui = "myapp.gui:main"   # 生成 gui.exe（GUI 类型，无控制台窗口）
web = "myapp.web:main"   # 生成 web.exe
```

已声明 `[project.scripts]` 时无需再定义 `[tool.fspack.entries]`。后者仅在需要
覆盖同名入口或补充打包专属入口（如调试脚本）时使用：

```toml
[tool.fspack.entries]
cli = "scripts/cli.py"   # 覆盖同名入口，指定脚本相对路径
debug = "debug.py"       # 补充额外入口
```

### 离线打包

`FSPACK_OFFLINE=1` 启用离线模式，所有下载阶段（运行时、wheel、Nuitka、ccache、
tkinter 补充包）只从本地缓存读取，缓存未命中时立即报清晰错误，不卡死、不重试网络。
适用于内网 CI、离线打包机或需精确控制缓存来源的场景。

#### 环境变量

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `FSPACK_OFFLINE=1` | 启用离线模式（值为 `1`/`true`/`yes`/`on`，不区分大小写） | 关闭 |
| `FSPACK_CACHE_DIR` | 自定义缓存根目录 | `~/.fspack/cache` |

缓存目录结构：

```text
<cache_root>/
├── embed/          # Windows embed python zip
├── standalone/     # Linux/macOS python-build-standalone tar.gz
├── wheels/         # 第三方 wheel + 依赖解析缓存
├── nuitka/         # Nuitka 包 + 编译用 standalone python
├── loaders/        # C loader 编译缓存
├── ccache/         # ccache 二进制与编译缓存
└── tkinter/        # tkinter 补充包缓存
```

#### 典型用法

**1. 预下载缓存（联网机器）**

在能联网的机器上跑一次正常构建，缓存会自动填充到 `~/.fspack/cache/`：

```bash
fsp b                    # 正常构建，自动下载并缓存
```

将整个 `~/.fspack/cache/` 目录拷贝到离线机器（或用 `FSPACK_CACHE_DIR` 指定路径）。

**2. 离线机器构建**

```bash
# 设置环境变量启用离线模式 + 指定缓存路径
export FSPACK_OFFLINE=1
export FSPACK_CACHE_DIR=/path/to/cache

fsp b                    # 仅从本地缓存读取，不联网
```

**3. 用 --find-links 指定额外的本地 wheel 目录**

若 wheel 不在默认缓存目录，可通过 `--find-links`（或 `pyproject.toml` 的
`find-links`）指定额外的本地 wheel 仓库，离线模式下也会搜索这些路径：

```bash
export FSPACK_OFFLINE=1
fsp b --find-links /data/wheels --find-links /shared/wheels
```

```toml
# pyproject.toml
[tool.fspack]
find-links = ["./wheels", "/shared/wheels"]
```

#### 离线模式错误排查

缓存未命中时，fspack 会抛出包含"离线模式"关键字的明确异常，并列出已搜索路径，
便于快速定位：

```text
fspack.exceptions.DependencyError: 离线模式下依赖缓存未命中: pypdf，
已搜索路径: /home/user/.fspack/cache/wheels; /data/wheels。
请预先下载 wheel 放入上述路径之一，或通过 --find-links 指定本地 wheel 目录，
或取消 FSPACK_OFFLINE 环境变量
```

排查步骤：

1. 检查错误信息中"已搜索路径"是否包含你预下载的目录
2. 用 `pip download -d <cache_path> <package>` 预下载缺失的 wheel
3. 运行时缓存（embed python、standalone）放入对应子目录（`embed/`、`standalone/`）
4. 若需联网，删除 `FSPACK_OFFLINE` 环境变量即可恢复在线模式

## 安装

```bash
pip install fspack
```

或用 [uv](https://docs.astral.sh/uv/)：

```bash
uv add fspack
```

## 快速上手

### 单项目打包

在 Python 项目根目录（含 `pyproject.toml`）执行：

```bash
# 1. 打包：生成 dist/<name>.exe 与 dist/runtime/
fsp b

# 2. 运行验证：直接跑打包产物
fsp r

# 3. 生成安装包：产出 dist/release/<name>-setup.exe
fsp p

# 4. 清理：删除 dist/
fsp c
```

也可指定项目目录与选项：

```bash
fsp b /path/to/project --mirror aliyun --py-version 3.11.9 --target windows
```

### 递归打包多项目

```bash
fsp b -R ./workspace       # 递归构建 workspace/ 下所有子项目
fsp p -R ./monorepo        # 递归打包 monorepo/ 下所有子项目
```

### 多入口项目

```bash
fsp b                     # 构建：生成所有声明的入口 exe
fsp r --entry cli         # 运行 cli 入口
fsp r --entry gui         # 运行 gui 入口
```

## 命令速查

全局选项：`-V/--version` 显示版本，`-v/--verbose` 开启 DEBUG 日志。

| 命令 | 别名 | 说明 |
|------|------|------|
| `fsp build` | `fsp b` | 打包项目，生成可执行文件与运行时 |
| `fsp run` | `fsp r` | 运行已打包项目（Linux 原生，`.exe` 自动用 wine） |
| `fsp clean` | `fsp c` | 清理 dist/ 目录 |
| `fsp package` | `fsp p` | 生成安装包（Windows NSIS / Linux .deb + tar.gz） |
| `fsp init` | `fsp i` | 从模板创建新项目（22 个模板可选） |
| `fsp doctor` | — | 环境诊断：检查打包工具可用性与配置 |
| `fsp cache` | — | 缓存健康检查与清理（损坏/过期/孤儿文件） |

### fsp build

```text
fsp b [project] [--mirror <name>] [--py-version <ver>] [--target <platform>]
              [--keep-module <mod>] [--icon <path>] [--no-stdlib-trim]
              [--no-pyc] [--pyc-strip] [--pyc-optimize <0|1|2>] [--no-site] [--nuitka]
              [--extra <name>] [-R|--recursive] [--dry-run] [--no-size-report]
              [--log-file <path>] [--log-format <text|json>]
              [--profile]
```

| 选项 | 说明 |
|------|------|
| `project` | 项目目录，默认当前目录 |
| `--mirror` | 镜像源（aliyun/huawei/tsinghua），默认 aliyun |
| `--py-version` | Python 版本，默认 3.11.9（Windows）/ 3.11.10（Linux/macOS） |
| `--target` | 目标平台（windows/linux/macos），默认当前平台；macOS 目标仅支持 macOS 构建机 |
| `--keep-module` | 显式保留子模块（如 `PySide2.QtGui`），可重复 |
| `--icon` | exe 图标（.ico/.png/.jpg），覆盖配置与自动搜索 |
| `--no-stdlib-trim` | 关闭标准库精简 |
| `--no-pyc` | 关闭字节码预编译 |
| `--pyc-strip` | 剥离 `.py` 仅留 `.pyc`（减小体积） |
| `--pyc-optimize` | 字节码优化级别：0/1/2（默认 2，体积减 5-15%） |
| `--no-site` | 禁用 site.py（节省 ~20-30ms 启动） |
| `--nuitka` | 启用 Nuitka 本机编译（提速 30-50%） |
| `--extra` | 启用 `[project.optional-dependencies]` 分组（可重复，覆盖 `[tool.fspack] extras`） |
| `-R`/`--recursive` | 递归扫描子项目依次构建 |
| `--dry-run` | 仅预览打包计划，不执行实际构建（不下载/不编译/不复制） |
| `--no-size-report` | 关闭构建结束后的体积报告 |
| `--log-file` | 将构建日志写入文件（UTF-8 追加，含时间戳/级别/异常栈） |
| `--log-format` | 日志文件格式：`text`（默认）/`json`（结构化，便于采集） |
| `--profile` | 启用耗时分析报告（wall/CPU/内存峰值 + 各阶段占比） |

`--dry-run` 输出打包计划表格（项目信息/依赖分析/构建选项），便于打包前确认配置正确，避免无效构建。

构建完成后默认输出体积报告：runtime/src/site-packages/其他 四类占比 + site-packages Top 10 包体积排序，帮助定位体积热点。`--no-size-report` 可关闭。

`--log-file` 将构建过程日志写入文件，便于 CI 上传与问题排查。`--log-format json` 输出结构化 JSON（每行一条记录，含 timestamp/level/logger/message/module/function/line 字段，支持 `extra=` 业务上下文），便于 ELK/Loki 采集。

`--profile` 启用耗时分析报告：构建结束后输出「耗时分析报告」表格（各阶段 wall time/占比/缓存命中/下载/节省）与「资源总览」表格（墙钟时间/CPU 时间/CPU 占比/内存峰值），识别瓶颈阶段。用标准库 `tracemalloc` 采集内存峰值，无新依赖。

### fsp run

```text
fsp r [project] [--entry <name>] [--debug] [--profile] [-- <args>...]
```

| 选项 | 说明 |
|------|------|
| `--entry <name>` | 多入口项目指定入口名 |
| `--debug` | 用 embed python 直跑（绕过 loader，输出可见） |
| `--profile` | 输出启动耗时剖析汇总（loader/环境准备/import 各阶段耗时） |
| `-- <args>` | 透传给目标程序的参数 |

`--profile` 启动耗时剖析：注入三级打点（C loader 阶段 / 入口包装器各阶段 / CPython 原生 `importtime` 逐模块导入），运行结束后输出对齐的耗时汇总表（各阶段与逐模块耗时、占总时长占比、未归因时间的"未细分"提示），定位启动性能优化点。可与 `--debug` 组合（无 loader 段）。旧 dist 构建的 wrapper 无打点时汇总缺 wrapper 段并提示重新构建，重新 `fsp b` 后完整。

### fsp package

```text
fsp p [project] [--mirror <name>] [--py-version <ver>] [--target <plat>] [--no-build] [--format <fmt>]
              [--extra <name>] [-R|--recursive]
```

| 选项 | 说明 |
|------|------|
| `--no-build` | 跳过重建，直接打包已有 dist |
| `--format` | 发行包格式（auto/zip/nsis/tar.gz/deb/all，默认 auto） |
| `--extra` | 启用 `[project.optional-dependencies]` 分组（可重复，仅在重建时生效） |
| `-R`/`--recursive` | 递归扫描子项目依次打包 |

`--format` 选项：

| 格式 | 说明 |
|------|------|
| `auto` | 平台默认（Windows=nsis，Linux=tar.gz+deb，macOS=pkg+dmg） |
| `zip` | 跨平台便携包 |
| `nsis` | Windows 安装包 |
| `tar.gz`/`deb` | Linux 便携包/安装包 |
| `pkg`/`dmg` | macOS 安装包/磁盘镜像 |
| `all` | 当前平台全部格式 |

### fsp init

```text
fsp init [project_name] [--template <id>] [--list] [--description <desc>] [--directory <path>]
```

| 选项 | 说明 |
|------|------|
| `project_name` | 项目名（默认当前目录名） |
| `--template <id>` | 模板 id（未指定且 stdin 是 TTY 时交互式选择；非 TTY 用 helloworld） |
| `--list` | 列出所有可用模板后退出 |
| `--description <desc>` | 项目描述（写入 pyproject.toml） |
| `--directory <path>` | 父目录（默认当前目录） |

22 个模板按分类：CLI(6) / GUI(6) / 游戏(2) / 科学(3) / Web(2) / 配置(3)。详见 `fsp init --list`。

### fsp doctor

```text
fsp doctor                # 环境诊断：检查打包工具与配置
```

输出三色诊断报告（绿=OK / 黄=WARN / 红=ERROR）：

- **环境信息**：Python 版本、平台、fspack 版本、镜像源、缓存目录大小
- **工具检查**：mingw-w64/gcc/clang/NSIS/wine/pip/uv/Pillow（按平台过滤）
- **修复建议**：缺失工具给出安装命令（如 `choco install mingw` / `sudo apt install gcc`）

打包失败时先跑 `fsp doctor` 前置发现环境问题。

### fsp cache

```text
fsp cache status [--target <name>]      # 扫描缓存目录健康状态
fsp cache clean  [--dry-run] [--stale] [--target <name>]  # 清理损坏与过期文件
```

扫描 `~/.fspack/cache/` 下全部 7 个子目录（wheels/embed/standalone/nuitka/loaders/ccache/tkinter），
识别三类问题：

- **损坏文件**（zip/tar 结构非法、PE 头缺失、空文件）：扫描期自动删除
- **过期文件**（版本不在 `KNOWN_*_VERSIONS` 中的旧 zip/tar/子目录）：需 `--stale` 显式清理
- **孤儿文件**（wheels 专用：未被 deps 引用的 wheel / 引用缺失 wheel 的 deps）

| 选项 | 说明 |
|------|------|
| `--target <name>` | 限定单 cache 类型（wheels/embed/standalone/nuitka/loaders/ccache/tkinter） |
| `--dry-run` | 仅预览将删除的文件，不实际删除（clean 专用） |
| `--stale` | 额外清理过期文件（默认仅清理损坏文件与 wheels 的 stale/orphan） |

缓存损坏（磁盘写满/下载中断）会导致构建失败，`fsp cache status` 可前置发现并自动删除损坏文件。

## 示例

`src/fspack/assets/templates/` 下内置 13 个典型项目模板，覆盖各类打包场景（`fsp doctor --test` 用其验证环境可打包性）：

| 模板 | 类型 | 亮点 |
|------|------|------|
| `cli_complex` | CLI | 多文件结构，Python 3.14 |
| `tk_app` | tkinter | 内置库打包验证 |
| `pyside2_app` | GUI 应用 | PySide2 依赖 |
| `pyside2_qml_dashboard` | QML 应用 | PySide2+QML 仪表盘 |
| `pygame_conway` | 游戏 | pygame 生命游戏 |
| `pygame_snake` | 游戏 | pygame 贪吃蛇 |
| `pygame_tetris` | 游戏 | pygame 俄罗斯方块 |
| `sci_numpy` | 科学计算 | numpy 数值计算 |
| `sci_scipy` | 科学计算 | scipy 科学计算 |
| `sci_matplotlib` | 科学计算 | matplotlib 绘图 |
| `web_app` | Web 服务 | flask web 框架 |
| `webview_app` | 前后端分离 | Vue + Vite + pywebview |
| `multi_entry` | 多入口 | cli+gui+web 三入口 |

完整模板列表见 [src/fspack/assets/templates/](src/fspack/assets/templates/) 目录。

## 配置参考

`pyproject.toml` 的 `[tool.fspack]` 段支持以下配置项（均可选）：

```toml
[tool.fspack]
icon = "assets/app.ico"                    # exe 图标
exclude = ["examples", "docs"]             # 源码复制时额外排除的 glob 模式
slim-include = ["PySide6/Qt6Charts.dll"]   # wheel 精简：强制保留
slim-exclude = [                           # wheel 精简：强制剥离
    "PySide6/opengl32sw.dll",
    "PySide6/translations/*",
]
extra-index-urls = ["https://pypi.company.com/simple/"]  # 私有 PyPI 源
find-links = ["./wheels"]                  # 本地 wheel 目录

[project.scripts]                          # 多入口声明（推荐，PEP 621 标准）
cli = "myapp.cli:main"
gui = "myapp.gui:main"

[tool.fspack.entries]                      # 可选：覆盖同名入口/补充打包专属入口
debug = "debug.py"
```

### wheel 精简用户规则

`slim-include`/`slim-exclude` 支持 fnmatch glob 模式，匹配 wheel 内 POSIX 相对路径。

**优先级**：`slim-include` > `slim-exclude` > 自动分类

典型场景：

```toml
# 强制保留被自动闭包排除的 Qt 模块
slim-include = ["PySide6/Qt6Charts.dll"]

# 剥离不需要的大体积文件
slim-exclude = [
    "PySide6/opengl32sw.dll",      # 软件 OpenGL 后备（20MB）
    "PySide6/translations/*",      # 翻译资源（29MB）
    "PySide6/include/*",           # C 头文件（14MB）
]
```

### 构建默认值

以下配置项作为 CLI 标志未显式指定时的回退默认值：

```toml
[tool.fspack]
nuitka = false           # 启用 Nuitka 编译模式
pyc_strip = false        # 剥离 .py 仅留 .pyc
pyc_optimize = 2         # 字节码优化级别 0/1/2
no_site = false          # 禁用 site.py
no_pyc = false           # 关闭字节码预编译
no_stdlib_trim = false   # 关闭标准库精简
ccache = false           # Nuitka 编译启用 ccache
nuitka_packages = []     # Nuitka 编译包含的额外包
```

### 可选依赖分组（extras）

fspack 支持 [PEP 621](https://peps.python.org/pep-0621/) 的 `[project.optional-dependencies]`，
按需启用分组依赖。等价 `pip install pkg[extra]` 语义：分组内依赖合并到下载集合，
自引用 `my-pkg[extra]` 递归展开，第三方 `pkg[extra]` 原样透传 pip。

```toml
[project.optional-dependencies]
gui = ["PySide2"]
web = ["flask", "uvicorn"]
full = ["myapp[gui]", "myapp[web]", "numpy"]  # 自引用递归展开

[tool.fspack]
extras = ["gui"]   # 配置默认启用分组（可省略，用 CLI --extra 覆盖）
```

```bash
fsp b --extra gui --extra web    # CLI 启用多个分组（覆盖配置默认）
fsp p --extra full               # package 子命令同样支持
```

**优先级**：CLI `--extra` 完全覆盖 `[tool.fspack] extras` 配置默认（集合语义，非合并）。
未指定 CLI `--extra` 时用配置默认；两者均未指定时仅打包 `[project] dependencies`。
未知分组名报错并列出可选分组。extras 变化触发依赖分析缓存失效（重新分析）。

## 平台支持

| 平台 | 运行时 | 安装包 |
|------|--------|--------|
| Windows | 便携 Python（官方 embed） | NSIS `.exe` |
| Linux | python-build-standalone | `.deb` + `.tar.gz` |
| macOS | python-build-standalone | `.pkg` + `.dmg` |

Windows 目标支持任意构建机交叉编译（Linux/macOS 装 mingw-w64 即可 `fsp b --target windows`）；
macOS/Linux 目标需在同平台构建机上构建（macOS 的 `gcc` 为 clang 垫片、Windows 的 `gcc`
为 mingw，均无法产出 ELF；非 macOS 的 `clang` 无法产出 Mach-O。跨机请求会明确报错，
`--dry-run` 预览不受限）。

### Windows 7 支持

Windows 产物可在 **Win7 SP1** 上运行（Python 3.9–3.14），fspack 自动处理两代兼容问题：

| Python 版本 | 兼容手段 |
|------------|---------|
| 3.9–3.11 | 构建时注入内置 `api-ms-win-core-path-l1-1-0.dll` shim（官方 dll 仅缺此 API Set） |
| 3.12–3.14 | 按 sha256 清单从 [PythonVista](https://github.com/adang1345/PythonVista) 下载 Win7 重编译版 `python3XX.dll` 替换官方件（官方 dll 静态导入 Win8+ API，shim 无法覆盖） |

构建期自动执行三道门禁（无需配置）：loader exe 导入表校验（违规即构建失败）、python3XX.dll 双重校验（sha256 + 导入表）、dist 全量 `.dll`/`.pyd` 扫描并输出报告 `dist/release/win7-compat-report.txt`（第三方依赖违规仅报告不阻断，可据此更换依赖版本；`--no-win7-scan` 可关闭）。

**运行前提**：目标机需已安装 UCRT（Win7 装 [KB2999226](https://www.microsoft.com/en-us/download/details.aspx?id=49077)，Win10/11 自带）。NSIS 安装包会在启动时检测 `ucrtbase.dll`，缺失时提示 KB 编号并询问是否继续；zip 便携包请自行确认该前提。

**已知限制**：第三方 pyd 若自身链接了 Win8+ API，在 Win7 上加载会失败——兼容报告会列出此类文件，但 fspack 无法自动修复（只能更换依赖版本）；Nuitka 编译模式（`--nuitka`）的用户代码产物同样受扫描报告监督。

## CI/CD 集成

fspack 可集成到 CI/CD 工作流，实现自动打包与发布。提供两个 GitHub Actions 模板：

- [`templates/pack-check.yml`](templates/pack-check.yml) — PR 验证打包
- [`templates/release-pack.yml`](templates/release-pack.yml) — Release 发布安装包

快速集成：

1. 复制模板到 `.github/workflows/`
2. 配置 `PROJECT_NAME`、`EXPECTED_OUTPUT` 变量
3. push 验证打包，打 tag 发布安装包

完整集成方案见 [CI/CD 集成指南](docs/integration.md)。

## 产物布局

`fsp b` 产出的 `dist/` 布局：

```text
dist/
├── <name>.exe          # 可执行文件（双击运行）
├── runtime/            # Python 运行时（便携，无需用户装 Python）
│   └── Lib/site-packages/   # 第三方依赖（已精简 + 预编译）
├── src/                # 你的源码
└── release/            # fsp p 产出的安装包
    ├── <name>-setup.exe           # Windows 安装包
    ├── <name>_<ver>_amd64.deb     # Linux .deb
    └── <name>-<ver>-linux.tar.gz  # Linux 便携包
```

## 安全与分发

### 资源段嵌入（自动）

fspack 打包 Windows exe 时自动嵌入 PE 资源段，降低 Windows Defender 等杀软启发式误报：

- **VS_VERSIONINFO**：从 `pyproject.toml` 的 `[project].description` 与 `[project].authors[0].name` 提取，填充 CompanyName / FileDescription / ProductName / OriginalFilename 等字段
- **application manifest**：声明 asInvoker（loader 不提权）、PerMonitorV2 DPI 感知、Win7-11 supportedOS
- **图标**：按 CLI `--icon` > 配置 `icon` > favicon 自动搜索 > 默认图标 优先级解析

在 `pyproject.toml` 声明 `description` 与 `authors` 即可丰富资源段（未声明时回退到项目名，不留空值字段）：

```toml
[project]
name = "my-app"
version = "1.0.0"
description = "我的桌面应用"
authors = [{ name = "张三" }]
```

### 代码签名（推荐）

生产分发建议用 Authenticode 证书签名 exe 与安装包，进一步降低误报。fspack 打包后用 Windows SDK `signtool` 签名：

```bash
fsp b                                              # 产出 dist/my-app.exe
signtool sign /fd SHA256 /f cert.pfx /p <密码> dist/my-app.exe
signtool sign /fd SHA256 /f cert.pfx /p <密码> dist/release/my-app-setup.exe
```

无证书时可生成自签名证书用于内部分发，或向 DigiCert / Sectigo 等机构购买代码签名证书。

### 误报申诉

mingw 编译的小型 exe 即使嵌入资源段仍可能被部分杀软误报。确认安全但被误报时：

1. **Microsoft Defender**：访问 [微软安全智能提交页](https://www.microsoft.com/wdsi/filesubmission)，选择"我认为此文件是安全的"
2. **VirusTotal**：上传至 [virustotal.com](https://www.virustotal.com) 查看各引擎检测结果，对命中引擎单独申诉
3. **代码签名**：签名后的 exe 误报率显著降低，多数杀软对已签名文件放宽启发式阈值
4. **重复检测**：杀软定义更新后可能自动解除误报，签名 + 等待更新通常即可解决

## 开发

```bash
uv sync --extra dev                          # 安装开发依赖
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95  # 测试
uv run pyrefly check                         # 类型检查
uv run ruff check src tests                  # lint
```

`make help` 查看全部快捷命令。详细架构与模块索引见 [架构文档](docs/architecture.rst)。

## 文档

- [架构与工作原理](docs/architecture.rst) — 构建流水线、模块索引、技术实现细节
- [CI/CD 集成指南](docs/integration.md) — GitHub Actions 集成方案
- [API 参考](docs/api.rst) — 自动生成的 API 文档
- [更新日志](docs/changelog.rst) — 版本变更记录

## 许可证

MIT
