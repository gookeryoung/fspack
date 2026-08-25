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

没有项目？一行命令从模板创建（22 个模板覆盖 CLI/GUI/游戏/科学/Web 常见场景）：

```bash
fsp init my-app --template pyside2   # 创建 PySide2 GUI 项目
fsp init --list                      # 查看所有可用模板
```

每个模板生成可直接打包的项目骨架（`pyproject.toml` + 入口脚本），`cd` 进去立即 `fsp b`。

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

**自动依赖推断与按需精简**：fspack 扫描源码 `import` 推断依赖，并按使用情况精简
wheel。例如 `from PySide6.QtWidgets import QApplication` 只打包 QtWidgets 及其依赖
闭包，剥离未用的 QtCharts/QtWebEngine 等大体积模块。典型 PySide6 应用可从 300MB
精简到 80MB。

**多入口项目**：一个项目里有 CLI 工具 + GUI 界面 + Web 服务，可用 PEP 621 标准的
`[project.scripts]` 声明多个入口，一次打包生成多个 exe，共享运行时与依赖。详见
[配置参考](docs/configuration.md)。

**离线打包**：`FSPACK_OFFLINE=1`（或 `fsp b -O` 单次约定）启用离线模式，所有下载
阶段仅从本地缓存读取，适用于内网 CI 与离线打包机。完整用法见
[离线打包指南](docs/offline.md)。

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

```text
fsp b [project] [--mirror] [--py-version] [--target] [--nuitka] [-R] [--dry-run] [--profile] ...
fsp r [project] [--entry <name>] [--debug] [--profile] [-- <args>...]
fsp p [project] [--no-build] [--format <auto|zip|nsis|tar.gz|deb|all>] [-R]
fsp init [name] [--template <id>] [--list]
fsp doctor                # 环境诊断（--test 用内置模板验证可打包性）
fsp cache status|clean    # 缓存健康检查与清理
```

全部选项明细见 [CLI 参考](docs/cli.md)。

## 配置

`pyproject.toml` 的 `[tool.fspack]` 段支持图标、排除规则、wheel 精简、私有源、
构建默认值、extras 分组等配置，多入口用 PEP 621 标准 `[project.scripts]` 声明：

```toml
[tool.fspack]
icon = "assets/app.ico"
slim-exclude = ["PySide6/translations/*"]   # wheel 精简：强制剥离

[project.scripts]
gui = "myapp.gui:main"                       # 生成 gui.exe（无控制台窗口）
```

完整配置项说明见 [配置参考](docs/configuration.md)。

## 平台支持

| 平台 | 运行时 | 安装包 |
|------|--------|--------|
| Windows | 便携 Python（官方 embed） | NSIS `.exe` |
| Linux | python-build-standalone | `.deb` + `.tar.gz` |
| macOS | python-build-standalone | `.pkg` + `.dmg` |

Windows 目标支持任意构建机交叉编译（Linux/macOS 装 mingw-w64 即可 `fsp b --target windows`）；
macOS/Linux 目标需在同平台构建机上构建（跨机请求会明确报错，`--dry-run` 预览不受限）。

Windows 产物可在 **Win7 SP1** 上运行（Python 3.9–3.14）：fspack 自动注入 API shim
或替换 Win7 重编译版 `python3XX.dll`，并输出全量兼容扫描报告。详见
[分发指南](docs/distribution.md)。

## CI/CD 集成

提供 [`pack-check.yml`](templates/pack-check.yml)（PR 验证打包）与
[`release-pack.yml`](templates/release-pack.yml)（Release 发布安装包）两个 GitHub
Actions 模板：复制到 `.github/workflows/`，配置 `PROJECT_NAME`/`EXPECTED_OUTPUT`
变量即可。完整方案见 [CI/CD 集成指南](docs/integration.md)。

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

Windows exe 自动嵌入 PE 资源段（VS_VERSIONINFO/manifest/图标）降低杀软误报；
生产分发建议代码签名。Win7 兼容、误报申诉等详见 [分发指南](docs/distribution.md)。

## 开发

```bash
uv sync --extra dev                          # 安装开发依赖
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95  # 测试
uv run pyrefly check                         # 类型检查
uv run ruff check src tests                  # lint
```

`make help` 查看全部快捷命令。详细架构与模块索引见 [架构文档](docs/architecture.rst)。

## 文档

- [CLI 参考](docs/cli.md) — 全部命令与选项明细
- [配置参考](docs/configuration.md) — `[tool.fspack]` 配置项与多入口声明
- [离线打包指南](docs/offline.md) — 内网/离线机打包方案
- [分发指南](docs/distribution.md) — 安全分发与 Win7 兼容
- [架构与工作原理](docs/architecture.rst) — 构建流水线、模块索引、技术实现细节
- [CI/CD 集成指南](docs/integration.md) — GitHub Actions 集成方案
- [API 参考](docs/api.rst) — 自动生成的 API 文档
- [更新日志](docs/changelog.rst) — 版本变更记录

## 许可证

MIT
