# fspack

> 极速 Python 项目打包器（cargo 风格短命令）。

[![PyPI](https://img.shields.io/pypi/v/fspack)](https://pypi.org/project/fspack/)
[![CI](https://github.com/gookeryoung/fspack/actions/workflows/ci.yml/badge.svg)](https://github.com/gookeryoung/fspack/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Coverage](https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen.svg)

fspack 将 Python 项目打包为可执行文件与跨平台安装包：用 embed python（Windows）或 python-build-standalone（Linux）提供运行时，C loader 配置环境并调用用户脚本，NSIS 生成 Windows 安装包、dpkg-deb 生成 Linux .deb 与 tar.gz 便携包。命令风格参考 cargo，常用操作均可用两字母短命令完成。

## 特性

- **cargo 风格短命令**：`fsp b` 打包、`fsp r` 运行、`fsp c` 清理、`fsp p` 生成安装包
- **零依赖入侵**：不需修改用户源码，自动分析 import 推断第三方依赖
- **embed python 运行时**：Windows 用官方 embed python zip，Linux 用 indygreg python-build-standalone
- **C loader 启动器**：动态加载 libpython，烧入入口路径，mingw/gcc 编译为原生可执行文件
- **跨平台安装包**：`fsp p` 按目标平台生成 Windows NSIS 安装包（含开始菜单/桌面快捷方式、卸载器、中英文双语）或 Linux .deb + tar.gz 便携包，`--format` 可指定 zip 跨平台便携包
- **双平台支持**：Windows（embed + mingw 交叉编译）、Linux（python-build-standalone + gcc）
- **多入口打包**：`[tool.fspack.entries]` 声明多个入口，单个项目生成多个 exe 共享 runtime/依赖/源码，支持 cli/gui/web 混合类型
- **字节码预编译**：默认将 src 与 site-packages 预编译为 .pyc 加速首次启动；`--pyc-strip` 剥离 .py 仅留 .pyc，`--pyc-optimize` 控制 -O/-OO 优化级别
- **Nuitka 本机编译**：`--nuitka` 将用户源码编译为 .pyd 本机执行（速度提升 30-50%），自动按 Python 版本锁定 Nuitka 版本并装到本地缓存 `~/.fspack/cache/nuitka/`，stamp 缓存命中跳过整个阶段
- **标准库精简**：默认剥离 Linux standalone 的 test/ensurepip/idlelib 等无用模块；`--no-stdlib-trim` 可关闭
- **增量构建缓存**：源码指纹 + 预编译 stamp + Nuitka stamp 三层缓存，未改动文件跳过复制与重编
- **Win7 兼容**：Python 3.9+ 注入 api-ms-win-core-path 替代 DLL，支持在 Win7/Win2008R2 运行
- **国内镜像**：默认清华源 PyPI 与 embed python 镜像，`--mirror` 切换（aliyun/huawei/tsinghua）
- **彩色进度显示**：rich 驱动的步骤进度（> 准备运行时 / √ 构建完成），错误/警告/一般消息颜色区分，`-v` 开启 DEBUG 日志

## 安装

```bash
pip install fspack
```

或使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv add fspack
```

## 快速上手

在 Python 项目根目录（含 `pyproject.toml`）执行：

```bash
# 打包当前项目（生成 dist/<name>.exe 与 dist/runtime/）
fsp b

# 运行已打包项目
fsp r

# 生成安装包到 dist/release/（Windows: <name>-setup.exe / Linux: <name>_<ver>_amd64.deb + <name>-<ver>-linux.tar.gz）
fsp p

# 清理 dist/
fsp c
```

也可指定项目目录与选项：

```bash
fsp b /path/to/project --mirror aliyun --py-version 3.11.9 --target windows
```

## 命令参考

全局选项：`-V/--version` 显示版本，`-v/--verbose` 开启 DEBUG 级别日志。

| 命令 | 别名 | 说明 |
|------|------|------|
| `fsp build` | `fsp b` | 打包项目，生成 dist/ 下可执行文件与运行时 |
| `fsp run` | `fsp r` | 运行已打包项目（Linux 原生直跑，`.exe` 自动用 wine） |
| `fsp clean` | `fsp c` | 清理 dist/ 目录 |
| `fsp package` | `fsp p` | 生成安装包（Windows NSIS / Linux .deb + tar.gz） |

### fsp build

```text
fsp b [project] [--mirror <name>] [--py-version <ver>] [--target <platform>]
              [--keep-module <mod>] [--icon <path>] [--no-stdlib-trim]
              [--no-pyc] [--pyc-strip] [--pyc-optimize <0|1|2>] [--no-site] [--nuitka]
```

- `project`：项目目录，默认当前目录
- `--mirror`：镜像源（aliyun/huawei/tsinghua），默认 tsinghua
- `--py-version`：embed python 版本，默认 3.11.9（Windows）/ 3.11.10（Linux，匹配 python-build-standalone release）
- `--target`：目标平台（windows/linux），默认当前平台
- `--keep-module`：显式保留子模块（如 `PySide2.QtGui`），可重复指定
- `--icon`：exe 图标文件路径（.ico/.png/.jpg 等），覆盖 `[tool.fspack] icon`；未指定时按 `[tool.fspack] icon` > 自动搜索 favicon.* > 默认 app.ico 解析
- `--no-stdlib-trim`：关闭标准库精简（默认剥离 Linux standalone 的 test/ensurepip/idlelib 等无用模块）
- `--no-pyc`：关闭字节码预编译（默认预编译 src+site-packages 为 .pyc 加速首次启动）
- `--pyc-strip`：剥离非 `__init__.py` 的 .py 源码（仅保留 .pyc，需配合预编译；保留包标识避免命名空间包问题）
- `--pyc-optimize`：字节码优化级别：0=保留 docstring/assert，1=剥离 assert，2=剥离 assert+docstring（-OO，体积减 5-15%，启动提速 5-10%，默认 2）
- `--no-site`：禁用 site.py 加载（`_pth` 省略 `import site` 行，节省 ~20-30ms 启动时间）
- `--nuitka`：启用 Nuitka 编译模式，用户源码编译为 .pyd 本机执行（速度提升 30-50%）。Nuitka 自动装到本地缓存 `~/.fspack/cache/nuitka/`，不污染 dist/runtime；交叉构建自动跳过；默认关闭

### fsp run

```text
fsp r [project] [--entry <name>] [--debug] [-- <args>...]
```

- `project`：项目目录，默认当前目录
- `--entry <name>`：多入口项目指定要运行的入口名（与 `[tool.fspack.entries]` 键匹配），单入口项目可省略
- `--debug`：用 embed python 直跑入口脚本（绕过 GUI loader，输出可见）
- `-- <args>`：透传给目标程序的参数（`--` 分隔）

### fsp clean

```text
fsp c [project]
```

### fsp package

```text
fsp p [project] [--mirror <name>] [--py-version <ver>] [--target <plat>] [--no-build] [--format <fmt>]
```

- `--target`：目标平台（windows/linux），默认当前平台
- `--no-build`：跳过重建，直接打包已有 dist（需先 `fsp b`）
- `--format`：发行包格式（auto/zip/nsis/tar.gz/deb/all，默认 auto）
  - `auto`：平台默认（Windows=nsis，Linux=tar.gz+deb）
  - `zip`：跨平台便携包
  - `nsis`：Windows 安装包
  - `tar.gz`/`deb`：Linux 便携包/安装包
  - `all`：当前平台全部格式

按目标平台分发：Windows 走 NSIS 生成 `dist/release/<name>-setup.exe`；Linux 走 dpkg-deb 生成 `dist/release/<name>_<ver>_amd64.deb` 与 `dist/release/<name>-<ver>-linux.tar.gz` 便携包；`zip` 格式生成 `dist/release/<name>-<ver>-<plat>.zip` 跨平台便携包。

## 工作原理

`fsp b` 构建流水线：

1. **解析** `pyproject.toml`，识别项目名、版本、入口模块、CLI/GUI 类型
2. **下载运行时**：Windows 下载 embed python zip 并解压到 `dist/runtime/`；Linux 下载 python-build-standalone tar.gz 并解压到 `dist/runtime/python/`
3. **分析依赖**：AST 扫描源码 import，分类标准库/本地/第三方，与 `pyproject.toml` 声明依赖比对；结果按源码指纹缓存，未改动跳过
4. **补充内置库**（仅 Windows）：AST 检出 `tkinter` 使用时，从 python-build-standalone Windows 构建提取 tkinter 组件（纯 Python 包 + `_tkinter.pyd` + Tcl/Tk 运行时脚本）补充到 runtime，按版本缓存 zip 避免重复下载
5. **下载 wheel**：用 dev python 的 `pip download` 拉取目标平台 wheel，解包到 `dist/runtime/Lib/site-packages/`（Windows）或 `dist/runtime/python/lib/python3.X/site-packages/`（Linux）
6. **写 _pth**（仅 Windows）：覆盖 `runtime/python3X._pth`，注册 site-packages 与 `..\src` 路径；`--no-site` 时省略 `import site` 行节省启动时间
7. **复制源码**：项目源码复制到 `dist/src/`，排除 dist/build/.venv 等构建产物；按 mtime 跳过未改动文件
8. **标准库精简**（默认，仅 Linux）：剥离 standalone 的 test/ensurepip/idlelib 等无用模块；`--no-stdlib-trim` 可关闭
9. **字节码预编译**（默认）：`compileall` 预编译 src+site-packages 为 .pyc 加速首次启动；stamp 缓存命中跳过；`--pyc-optimize` 控制 -O/-OO 级别；`--pyc-strip` 进一步剥离 .py 仅留 .pyc
10. **Nuitka 编译**（可选，`--nuitka`）：用户源码编译为 .pyd 本机执行；按 Python 版本锁定 Nuitka 版本（3.8/3.9→2.5.1，3.10+→4.1.3），自动装到 `~/.fspack/cache/nuitka/`；stamp 缓存键 = `nuitka_version|py_version|src_fingerprint`，命中跳过整个阶段；交叉构建自动跳过
11. **Win7 兼容 DLL 注入**（Windows，Python 3.9+）：注入 api-ms-win-core-path 替代 DLL，支持在 Win7/Win2008R2 运行
12. **生成 C loader**：按平台模板生成 C 源码（烧入入口脚本相对路径），mingw（Windows）或 gcc（Linux）编译为可执行文件

dist 布局：

```text
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
```

## 多入口打包

单个项目可通过 `[tool.fspack.entries]` 声明多个入口，每个入口生成独立 exe，
共享 runtime/依赖/源码。每个入口按自身脚本 import 推断 CLI/GUI 类型，支持
cli/gui/web 混合。

```toml
[project]
name = "my_app"
version = "0.1.0"
dependencies = ["PySide2>=5.15.2", "flask"]

[tool.fspack.entries]
cli = "cli.py"        # 生成 cli.exe（CLI 类型）
gui = "gui.py"        # 生成 gui.exe（GUI 类型，加 -mwindows）
web = "web.py"        # 生成 web.exe（CLI 类型）
```

```bash
fsp b                 # 构建：生成 cli.exe/gui.exe/web.exe 三个入口
fsp r --entry cli     # 运行 cli 入口
fsp r --entry gui     # 运行 gui 入口
fsp r --entry web     # 运行 web 入口
```

多入口模式下每个入口写入 `<name>.entry` 文件，C loader 运行时按
`<exe_basename>.entry` 查找入口脚本。单入口项目（无 `[tool.fspack.entries]`）
仍写 `.entry` 文件，向后兼容。

## 示例

`examples/` 下提供多类典型项目验证打包效果（下划线命名者由 slow 端到端测试覆盖）：

| 示例 | 类型 | 说明 |
|------|------|------|
| cli_helloworld | 无库 CLI | 最小示例，验证基础流水线 |
| cli_tool | 有库 CLI | requests 依赖，验证 wheel 下载与解包 |
| cli_complex | 无库 CLI | 展示型，多文件结构 |
| cli_office | 有库 CLI | pypdf 依赖，uv workspace 成员 |
| gui_calc | 有库 GUI | PySide6 依赖，验证 GUI 快捷方式与 DLL 搜索 |
| pyside2_app | 有库 GUI | PySide2 依赖，验证 requires-python 版本自动解析 |
| pyqt5_cli | 有库 GUI | PyQt5 依赖，验证 Python 3.12 兼容 |
| tk_app | 有库 GUI | tkinter 内置库打包，验证 TkinterBundler 从 standalone 提取补充到 embed python |
| pygame_cli | 有库 pygame | pygame 依赖，验证多媒体库打包 |
| pygame_conway | 有库 pygame | pygame 生命游戏，验证多文件结构与 slow 端到端测试 |
| pygame_gktetris | 有库 pygame | pygame 俄罗斯方块，验证 entities 包结构打包 |
| pygame_snake | 有库 pygame | pygame 贪吃蛇，验证 dummy 驱动运行 |
| pyside2_qml_dashboard | 有库 GUI | PySide2+QML 仪表盘示例，验证 QML 资源与多视图打包 |
| sci_numpy | 科学计算 | numpy 依赖，验证数值计算库打包 |
| sci_scipy | 科学计算 | scipy 依赖，验证大型科学计算库精简规则 |
| sci_matplotlib | 科学计算 | matplotlib 依赖，验证绘图库 C 扩展打包 |
| web_app | 有库 web | flask 依赖，验证 web 框架打包 |
| multi_entry | 多入口混合 | cli+gui+web 三入口共享 runtime/依赖，验证 `[tool.fspack.entries]` 多入口打包 |

## 平台支持

| 平台 | 运行时 | 编译器 | 安装包 |
|------|--------|--------|--------|
| Windows | embed python（python.org） | mingw-w64 交叉编译 | NSIS（.exe） |
| Linux | python-build-standalone（indygreg） | gcc | .deb + tar.gz |

Linux dev 机可交叉编译 Windows 包（`fsp b --target windows`），反之亦然。

## 已知限制

### `missing` 依赖误报导入名≠包名

`fsp b` 日志中 `AST 发现未声明依赖` 提示可能误报：当导入名与 PyPI 包名不一致时
（如 `import yaml` 对应包名 `PyYAML`、`import PIL` 对应 `Pillow`），即使 pyproject.toml
已正确声明依赖，`missing` 比较归一化包名仍会提示未声明。不影响打包功能（`declared`
优先下载），仅日志有误导性。

## CI/CD 集成

fspack 可集成到其他 Python 项目的 CI/CD 工作流，实现自动打包与打包成功验证。提供两个可复用的 GitHub Actions workflow 模板：

- [`templates/pack-check.yml`](templates/pack-check.yml) — PR 验证打包（push/PR 触发，验证打包不破坏）
- [`templates/release-pack.yml`](templates/release-pack.yml) — Release 发布安装包（tag 触发，矩阵打包 Windows + Linux 安装包附到 GitHub Release）

### fspack 自身的发布流程

fspack 自身通过 [.github/workflows/release.yml](.github/workflows/release.yml) 在 `git push v*.*.*` 时用**各平台原生 runner 打包自身**发布到 GitHub Release：

| job | runner | 产物 |
|-----|--------|------|
| `pypi` | ubuntu-latest | sdist + wheel（uv build → uv publish）|
| `pack-windows` | windows-latest | NSIS 安装包 `.exe` + 跨平台便携包 `.zip` |
| `pack-linux` | ubuntu-latest | tar.gz 便携包 + `.deb` 安装包 + 跨平台便携包 `.zip` |
| `release` | ubuntu-latest | 收集以上产物统一上传到 GitHub Release |

原生平台打包使 pyc 预编译生效、loader 用本机 gcc/mingw 原生编译，避免交叉编译的 wine 不稳定。任一 job 失败时 `release` 仍尝试发布已成功产物，但最终标记 workflow 失败。

### 快速上手

1. 复制模板到你的项目：

   ```bash
   cp templates/pack-check.yml your-project/.github/workflows/
   cp templates/release-pack.yml your-project/.github/workflows/
   ```

2. 在仓库 **Settings → Secrets and variables → Actions → Variables** 配置：

   | 变量名 | 必填 | 说明 | 示例值 |
   |--------|------|------|--------|
   | `PROJECT_NAME` | 是 | 项目名（与 `pyproject.toml` 的 `name` 一致） | `my_app` |
   | `EXPECTED_OUTPUT` | 是 | 运行打包后 exe 应输出的预期字符串 | `hello from my_app` |
   | `ENTRY_NAMES` | 否 | 多入口项目入口名列表（逗号分隔），未设置时按单入口处理 | `cli,gui,web` |

3. 触发：push 到 main 验证打包，打 tag（`git tag v0.1.0`）发布安装包。

### 测试打包成功的三层反馈

| 阶段 | 成功反馈 | 失败反馈 |
|------|---------|---------|
| 构建 | `fsp b` 退出码 0 + 产物断言通过 | 退出码非零，上传 dist/ 供调试 |
| 运行 | grep 命中预期字符串 | grep 失败，输出实际内容到日志 |
| 安装包 | 文件存在 + 魔数校验（MZ/`!<arch>`/gzip） | 文件缺失或魔数错误 |

完整集成方案见 [CI/CD 集成指南](docs/integration.md)。

## 开发

```bash
# 安装开发依赖
uv sync --extra dev

# 运行测试（含覆盖率，阈值 95%）
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95

# 类型检查
uv run pyrefly check

# 代码风格
uv run ruff check src tests
uv run ruff format --check src tests
```

### Make 快捷命令

项目提供 Makefile 封装常用操作，运行 `make help` 查看全部命令：

```bash
make sync     # 安装开发依赖
make check    # 全套门禁 (lint + typecheck + cov)
make build    # 构建分发包
make clean    # 清理构建产物
make bump PART=patch  # 版本号 bump
```

## 文档

文档由 Sphinx 构建，托管在 ReadTheDocs：

```bash
make doc
```

## 多版本测试

使用 tox 在多个 Python 版本（py38, py39, py310, py311, py312, py313, py314）下运行测试：

```bash
make tox
```

## 许可证

MIT
