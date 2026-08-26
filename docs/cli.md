# CLI 参考

全局选项：`-V/--version` 显示版本，`-v/--verbose` 开启 DEBUG 日志。

命令速查：

| 命令 | 别名 | 说明 |
|------|------|------|
| `fsp build` | `fsp b` | 打包项目，生成可执行文件与运行时 |
| `fsp run` | `fsp r` | 运行已打包项目（Linux 原生，`.exe` 自动用 wine） |
| `fsp clean` | `fsp c` | 清理 dist/ 目录 |
| `fsp package` | `fsp p` | 生成安装包（Windows NSIS / Linux .deb + tar.gz） |
| `fsp init` | `fsp i` | 从模板创建新项目（18 个模板可选） |
| `fsp doctor` | — | 环境诊断：检查打包工具可用性与配置 |
| `fsp cache` | — | 缓存健康检查与清理（损坏/过期/孤儿文件） |

## fsp build

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

## fsp run

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

剖析选项支持多次运行统计（`-PR N` 取 wall_ms 中位数实际样本做汇总与对比基准）与 GUI 界面就绪自终止（Qt/tkinter 主循环首帧上屏即打点）。详见 [性能文档](performance.md)。

## fsp package

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

## fsp init

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

18 个模板按分类：CLI(4) / GUI(4) / 游戏(1) / 科学(3) / Web(3) / 配置(3)。详见 `fsp init --list`。

## fsp doctor

```text
fsp doctor                # 环境诊断：检查打包工具与配置
```

输出三色诊断报告（绿=OK / 黄=WARN / 红=ERROR）：

- **环境信息**：Python 版本、平台、fspack 版本、镜像源、缓存目录大小
- **工具检查**：mingw-w64/gcc/clang/NSIS/wine/pip/uv/Pillow（按平台过滤）
- **修复建议**：缺失工具给出安装命令（如 `choco install mingw` / `sudo apt install gcc`）

打包失败时先跑 `fsp doctor` 前置发现环境问题。

`fsp doctor --test` 用内置典型项目模板验证环境可打包性。`src/fspack/assets/templates/` 下共 8 个模板，每个代表一类打包能力维度（依赖形态/入口数/前端/运行时段）：

| 模板 | 类型 | 亮点 |
|------|------|------|
| `cli_complex` | CLI | 多文件结构 + lxml/ordered-set 二进制依赖，Python 3.14 |
| `tk_app` | GUI 应用 | tkinter 标准库，零第三方依赖 |
| `pyside2_qml_dashboard` | QML 应用 | PySide2+QML 仪表盘，uv.lock 真实项目形态 |
| `pygame_snake` | 游戏 | pygame 贪吃蛇，>=3.13 新运行时段 |
| `pygame_tetris` | 游戏 | pygame 俄罗斯方块，多模块结构，3.8-3.12 运行时段 |
| `sci_stack` | 科学计算 | NumPy+SciPy+numexpr+Matplotlib 流水线，free-threaded 3.14t |
| `webview_app` | 前后端分离 | Vue + Vite + pywebview |
| `multi_entry` | 多入口 | cli+gui+web 三入口 |

完整模板列表见 [src/fspack/assets/templates/](../src/fspack/assets/templates/) 目录。

`--test` 可选基准剖析选项（与 `fsp b -P`/`fsp r -P` 体系对齐）：`-P` 输出各模板构建阶段耗时报告并聚合落盘单个剖析日志（`.benchmarks/fsp-d-<时间戳>.json`），`-PO <路径>` 指定输出路径，`-PC [REF]` 与历史基准日志对比（不带值趋势表 / `last` / 近 N 次 / 基准文件路径）。

## fsp cache

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
