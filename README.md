# fspack

> 极速 Python 项目打包器（cargo 风格短命令）。

[![PyPI](https://img.shields.io/pypi/v/fspack)](https://pypi.org/project/fspack/)
[![CI](https://github.com/gooker_young/fspack/actions/workflows/ci.yml/badge.svg)](https://github.com/gooker_young/fspack/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Coverage](https://img.shields.io/badge/coverage-%E2%89%A595%25-brightgreen.svg)

fspack 将 Python 项目打包为可执行文件与跨平台安装包：用 embed python（Windows）或 python-build-standalone（Linux）提供运行时，C loader 配置环境并调用用户脚本，NSIS 生成 Windows 安装包、dpkg-deb 生成 Linux .deb 与 tar.gz 便携包。命令风格参考 cargo，常用操作均可用两字母短命令完成。

## 特性

- **cargo 风格短命令**：`fsp b` 打包、`fsp r` 运行、`fsp c` 清理、`fsp p` 生成安装包
- **零依赖入侵**：不需修改用户源码，自动分析 import 推断第三方依赖
- **embed python 运行时**：Windows 用官方 embed python zip，Linux 用 indygreg python-build-standalone
- **C loader 启动器**：动态加载 libpython，烧入入口路径，mingw/gcc 编译为原生可执行文件
- **跨平台安装包**：`fsp p` 按目标平台生成 Windows NSIS 安装包（含开始菜单/桌面快捷方式、卸载器、中英文双语）或 Linux .deb + tar.gz 便携包
- **双平台支持**：Windows（embed + mingw 交叉编译）、Linux（python-build-standalone + gcc）
- **国内镜像**：默认阿里云 PyPI 与 embed python 镜像，`--mirror` 切换
- **彩色进度显示**：rich 驱动的步骤进度（▶ 准备运行时 / ✓ 构建完成），错误/警告/一般消息颜色区分，`-v` 开启 DEBUG 日志

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
```

- `project`：项目目录，默认当前目录
- `--mirror`：镜像源（aliyun/huawei/tsinghua），默认 aliyun
- `--py-version`：embed python 版本，默认 3.11.9（Windows）/ 3.11.10（Linux，匹配 python-build-standalone release）
- `--target`：目标平台（windows/linux），默认当前平台

### fsp run

```text
fsp r [project] [-- <args>...]
```

- `project`：项目目录，默认当前目录
- `-- <args>`：透传给目标程序的参数（`--` 分隔）

### fsp clean

```text
fsp c [project]
```

### fsp package

```text
fsp p [project] [--mirror <name>] [--py-version <ver>] [--target <plat>] [--no-build]
```

- `--target`：目标平台（windows/linux），默认当前平台
- `--no-build`：跳过重建，直接打包已有 dist（需先 `fsp b`）

按目标平台分发：Windows 走 NSIS 生成 `dist/release/<name>-setup.exe`；Linux 走 dpkg-deb 生成 `dist/release/<name>_<ver>_amd64.deb` 与 `dist/release/<name>-<ver>-linux.tar.gz` 便携包。

## 工作原理

`fsp b` 构建流水线：

1. **解析** `pyproject.toml`，识别项目名、版本、入口模块、CLI/GUI 类型
2. **下载运行时**：Windows 下载 embed python zip 并解压到 `dist/runtime/`；Linux 下载 python-build-standalone tar.gz 并解压到 `dist/runtime/python/`
3. **分析依赖**：AST 扫描源码 import，分类标准库/本地/第三方，与 `pyproject.toml` 声明依赖比对
4. **下载 wheel**：用 dev python 的 `pip download` 拉取目标平台 wheel，解包到 `dist/runtime/Lib/site-packages/`（Windows）或 `dist/runtime/python/lib/python3.X/site-packages/`（Linux）
5. **写 _pth**（仅 Windows）：覆盖 `runtime/python3X._pth`，注册 site-packages 与 `..\src` 路径
6. **复制源码**：项目源码复制到 `dist/src/`，排除 dist/build/.venv 等构建产物
7. **生成 C loader**：按平台模板生成 C 源码（烧入入口脚本相对路径），mingw（Windows）或 gcc（Linux）编译为可执行文件

dist 布局：

```text
dist/
├── <name>.exe          # C loader 启动器
├── runtime/            # Python 运行时
│   ├── python311.dll   # Windows embed
│   ├── python311._pth
│   └── Lib/site-packages/   # 第三方依赖
├── src/                # 用户源码
└── release/            # 安装包（fsp p 产出）
    ├── <name>-setup.exe           # Windows NSIS
    ├── <name>_<ver>_amd64.deb     # Linux .deb
    └── <name>-<ver>-linux.tar.gz  # Linux 便携包
```

## 示例

`tests/examples/` 下提供 5 类典型项目验证打包效果：

| 示例 | 类型 | 说明 |
|------|------|------|
| helloworld | 无库 CLI | 最小示例，验证基础流水线 |
| clitool | 有库 CLI | requests 依赖，验证 wheel 下载与解包 |
| guicalc | 有库 GUI | PySide6 依赖，验证 GUI 快捷方式与 DLL 搜索 |
| pygamedemo | 有库 pygame | pygame 依赖，验证多媒体库打包 |
| webapp | 有库 web | flask 依赖，验证 web 框架打包 |

## 平台支持

| 平台 | 运行时 | 编译器 | 安装包 |
|------|--------|--------|--------|
| Windows | embed python（python.org） | mingw-w64 交叉编译 | NSIS（.exe） |
| Linux | python-build-standalone（indygreg） | gcc | .deb + tar.gz |

Linux dev 机可交叉编译 Windows 包（`fsp b --target windows`），反之亦然。

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
