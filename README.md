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

## 为什么选 fspack

| 你想要的 | fspack 给你的 |
|---------|--------------|
| 一行命令打包 | `fsp b` 生成可执行文件，`fsp p` 生成安装包，cargo 风格两字母短命令 |
| 不改源码 | 自动 AST 扫描 import 推断依赖，无需手动声明打包配置 |
| 小体积安装包 | 自动精简 wheel（剥离未用子模块/翻译/头文件）、预编译 `.pyc`、可选剥离 `.py` |
| 跨平台分发 | Windows 出 `.exe` + NSIS 安装包，Linux 出 `.deb` + `.tar.gz`，支持交叉编译 |
| 双击就能跑 | 内置便携运行时，用户机无需装 Python；Windows 安装包含快捷方式与卸载器 |
| 首次启动快 | 默认预编译字节码，`--nuitka` 可本机编译提速 30-50% |
| 多入口项目 | 一个项目生成多个 exe（cli/gui/web），共享运行时与依赖 |
| 国内网络友好 | 默认清华镜像，`--mirror` 一键切换阿里/华为源 |

## 核心特性

### 一行命令，零配置打包

无需写 spec 文件、无需改源码。fspack 自动识别 `pyproject.toml` 的入口与依赖，
AST 扫描源码推断实际使用的第三方库，开箱即用。

```bash
fsp b                    # 打包
fsp r                    # 运行验证
fsp p                    # 生成安装包
fsp c                    # 清理
```

### 自动依赖推断，按需精简

fspack 扫描源码的 `import` 语句，自动识别你用了哪些第三方库。更智能的是：
只打包你真正用到的部分。例如 `from PySide6.QtWidgets import QApplication` 只会
打包 QtWidgets 及其依赖闭包（QtGui/QtCore），剥离未用的 QtCharts/QtWebEngine
等大体积模块。典型 PySide6 应用可从 300MB 精简到 80MB。

### 生成可分发安装包

| 平台 | 产出 | 特性 |
|------|------|------|
| Windows | `<name>-setup.exe` | NSIS 安装包，开始菜单/桌面快捷方式、卸载器、中英文双语 |
| Linux | `<name>_<ver>_amd64.deb` | dpkg 安装包，`apt install` 即用 |
| Linux | `<name>-<ver>-linux.tar.gz` | 便携包，解压即用 |
| 跨平台 | `<name>-<ver>-<plat>.zip` | `--format zip` 生成跨平台便携包 |

### 多入口项目一次打包

一个项目里有 CLI 工具 + GUI 界面 + Web 服务？fspack 支持声明多个入口，一次打包
生成多个 exe，共享运行时与依赖，不重复打包。

```toml
[tool.fspack.entries]
cli = "cli.py"        # 生成 cli.exe
gui = "gui.py"        # 生成 gui.exe（GUI 类型，无控制台窗口）
web = "web.py"        # 生成 web.exe
```

### Nuitka 本机编译加速（可选）

`--nuitka` 将用户源码编译为 `.pyd` 本机执行，速度提升 30-50%。Nuitka 自动装到
本地缓存，不污染项目环境；stamp 缓存命中跳过整个编译阶段。

### 递归打包多项目（monorepo 友好）

`-R/--recursive` 递归扫描目录下所有含 `pyproject.toml` 的子项目，依次构建/打包，
便于一次性处理 monorepo 或 `examples/` 目录。单项目失败不中断，最后汇总结果。

### 离线打包（内网/无网络环境）

fspack 内置离线模式，适用于内网 CI、离线打包机或需精确控制缓存来源的场景。
启用离线模式后，所有下载阶段（运行时、wheel、Nuitka、ccache、tkinter 补充包）
**只从本地缓存读取**，缓存未命中时立即报清晰错误，不卡死、不重试网络。

#### 环境变量

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `FSPACK_OFFLINE=1` | 启用离线模式（值为 `1`/`true`/`yes`/`on`，不区分大小写） | 关闭 |
| `FSPACK_CACHE_DIR` | 自定义缓存根目录 | `~/.fspack/cache` |

缓存目录结构：

```text
<cache_root>/
├── embed/          # Windows embed python zip
├── standalone/     # Linux python-build-standalone tar.gz
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

缓存未命中时，fspack 会抛出包含"离线模式"关键字的明确异常，并列出**已搜索路径**，
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
fsp b -R ./examples        # 递归构建 examples/ 下所有示例
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

### fsp build

```text
fsp b [project] [--mirror <name>] [--py-version <ver>] [--target <platform>]
              [--keep-module <mod>] [--icon <path>] [--no-stdlib-trim]
              [--no-pyc] [--pyc-strip] [--pyc-optimize <0|1|2>] [--no-site] [--nuitka]
              [-R|--recursive] [--dry-run] [--no-size-report]
```

| 选项 | 说明 |
|------|------|
| `project` | 项目目录，默认当前目录 |
| `--mirror` | 镜像源（aliyun/huawei/tsinghua），默认 tsinghua |
| `--py-version` | Python 版本，默认 3.11.9（Windows）/ 3.11.10（Linux） |
| `--target` | 目标平台（windows/linux），默认当前平台 |
| `--keep-module` | 显式保留子模块（如 `PySide2.QtGui`），可重复 |
| `--icon` | exe 图标（.ico/.png/.jpg），覆盖配置与自动搜索 |
| `--no-stdlib-trim` | 关闭标准库精简 |
| `--no-pyc` | 关闭字节码预编译 |
| `--pyc-strip` | 剥离 `.py` 仅留 `.pyc`（减小体积） |
| `--pyc-optimize` | 字节码优化级别：0/1/2（默认 2，体积减 5-15%） |
| `--no-site` | 禁用 site.py（节省 ~20-30ms 启动） |
| `--nuitka` | 启用 Nuitka 本机编译（提速 30-50%） |
| `-R`/`--recursive` | 递归扫描子项目依次构建 |
| `--dry-run` | 仅预览打包计划，不执行实际构建（不下载/不编译/不复制） |
| `--no-size-report` | 关闭构建结束后的体积报告 |

`--dry-run` 输出打包计划表格（项目信息/依赖分析/构建选项），便于打包前确认配置正确，避免无效构建。

构建完成后默认输出体积报告：runtime/src/site-packages/其他 四类占比 + site-packages Top 10 包体积排序，帮助定位体积热点。`--no-size-report` 可关闭。

### fsp run

```text
fsp r [project] [--entry <name>] [--debug] [-- <args>...]
```

| 选项 | 说明 |
|------|------|
| `--entry <name>` | 多入口项目指定入口名 |
| `--debug` | 用 embed python 直跑（绕过 loader，输出可见） |
| `-- <args>` | 透传给目标程序的参数 |

### fsp package

```text
fsp p [project] [--mirror <name>] [--py-version <ver>] [--target <plat>] [--no-build] [--format <fmt>]
              [-R|--recursive]
```

| 选项 | 说明 |
|------|------|
| `--no-build` | 跳过重建，直接打包已有 dist |
| `--format` | 发行包格式（auto/zip/nsis/tar.gz/deb/all，默认 auto） |
| `-R`/`--recursive` | 递归扫描子项目依次打包 |

`--format` 选项：

| 格式 | 说明 |
|------|------|
| `auto` | 平台默认（Windows=nsis，Linux=tar.gz+deb） |
| `zip` | 跨平台便携包 |
| `nsis` | Windows 安装包 |
| `tar.gz`/`deb` | Linux 便携包/安装包 |
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
- **工具检查**：mingw-w64/gcc/NSIS/wine/pip/uv/Pillow（按平台过滤）
- **修复建议**：缺失工具给出安装命令（如 `choco install mingw` / `sudo apt install gcc`）

打包失败时先跑 `fsp doctor` 前置发现环境问题。

## 示例

`examples/` 下提供 18 个典型项目，覆盖各类打包场景：

| 示例 | 类型 | 亮点 |
|------|------|------|
| `cli_helloworld_pyall` | 无库 CLI | 最小示例，验证基础流水线 |
| `cli_complex_py314` | 无库 CLI | 多文件结构，Python 3.14 |
| `cli_office_py38` | 有库 CLI | pypdf 依赖，uv workspace |
| `pyside2_app_py310` | GUI 应用 | PySide2 依赖 |
| `pyside2_qml_dashboard_py38` | QML 应用 | PySide2+QML 仪表盘 |
| `pyqt5_cli_pyall` | GUI 应用 | PyQt5，Python 3.12 兼容 |
| `tk_app_pyall` | tkinter | 内置库打包验证 |
| `pygame_conway_py38` | 游戏 | pygame 生命游戏 |
| `pygame_gktetris_py38` | 游戏 | pygame 俄罗斯方块 |
| `sci_numpy_py38` | 科学计算 | numpy 数值计算 |
| `sci_matplotlib_py38` | 科学计算 | matplotlib 绘图 |
| `web_app_pyall` | Web 服务 | flask web 框架 |
| `multi_entry_py310` | 多入口 | cli+gui+web 三入口 |

完整示例列表见 [examples/](examples/) 目录。

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

[tool.fspack.entries]                      # 多入口声明
cli = "cli.py"
gui = "gui.py"
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

## 平台支持

| 平台 | 运行时 | 安装包 |
|------|--------|--------|
| Windows | 便携 Python（官方 embed） | NSIS `.exe` |
| Linux | python-build-standalone | `.deb` + `.tar.gz` |

Linux 可交叉编译 Windows 包（`fsp b --target windows`），反之亦然。

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

## 已知限制

### `missing` 依赖误报

当导入名与 PyPI 包名不一致时（如 `import yaml` 对应 `PyYAML`），日志可能提示
`AST 发现未声明依赖`。不影响打包功能（`declared` 优先下载），仅日志有误导性。

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
