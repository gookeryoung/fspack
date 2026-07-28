# iter-86：fsp doctor 环境诊断命令

## 需求清单

- [x] req-47：功能性能完善（阶段 1 第 1 轮）

## 迭代目标

实现 `fsp doctor` 子命令，检查打包工具可用性（mingw-w64/gcc/NSIS/wine/
pip/uv/Pillow）与配置（Python 版本、平台、镜像源、缓存目录大小），输出
三色诊断报告（绿=OK / 黄=WARN / 红=ERROR）与修复建议，帮助用户前置发现
环境问题，避免打包中途失败。

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/fspack/cli_doctor.py` | 新增：环境诊断模块（CheckResult/CheckStatus/DoctorReport 数据结构 + run_doctor + print_doctor_report + 各工具检查函数） |
| `src/fspack/cli.py` | 新增：_add_doctor_subparser + _run_doctor 分发 |
| `tests/test_cli_doctor.py` | 新增：47 个测试覆盖数据结构/工具检查/缓存扫描/报告渲染/CLI 集成 |
| `README.md` | 命令速查表添加 doctor + 新增 "fsp doctor" 详细说明章节 |

## 关键决策与依据

### 数据结构设计：frozen dataclass + Enum

- `CheckStatus(str, Enum)`：三态枚举（OK/WARN/ERROR），继承 `str` 便于
  序列化与测试断言（`status == "ok"`）
- `CheckResult`：frozen dataclass 含 name/status/detail/suggestion 四字段，
  `suggestion` 默认空字符串避免 None 检查
- `DoctorReport`：frozen dataclass 含 env_info + tool_checks 两个 tuple，
  `has_error`/`has_warn` 属性便于上层判断

### 工具检查通用函数 _check_tool_version

抽取公共逻辑为 `_check_tool_version(name, cmd, *, parse_version, error_suggestion, warn_only)`：
- `shutil.which(cmd[0])` 检查 PATH 可用性（避免 subprocess 启动开销）
- `subprocess.run` 超时 5s（`_VERSION_TIMEOUT`），捕获 OSError/TimeoutExpired
- `parse_version=False` 用于 wine 等版本输出多行的工具，仅返回 "可用"
- `warn_only=True` 让可选工具（wine/uv）缺失降级为 WARN，不阻塞打包

### 平台相关工具过滤

按 `detect_platform()` 过滤工具检查项：
- **Windows**：mingw-w64（必备）+ NSIS（必备）
- **Linux**：gcc（必备）+ wine（可选，运行 .exe 验证）+ NSIS（可选，交叉打 Windows 包）

避免在 Linux 上误报 mingw 缺失（Linux 不需要 mingw，gcc 即可）。

### pip 检查的双重回退

pip 可能以两种形式可用：
1. `pip` 命令在 PATH
2. `python -m pip` 模块形式（pip 命令不在 PATH 但模块已安装）

`_check_pip` 先查 `shutil.which("pip")` + `shutil.which("pip3")`，均未找到
时回退 `subprocess.run([sys.executable, "-m", "pip", "--version"])`，仍失败
才返回 ERROR。复用 `sys.executable` 确保诊断当前解释器环境，非 PATH 中的
其他 python。

### Pillow 版本守卫

Pillow < 9.4.0 不支持 `bitmap_format="png"` 参数（req-43 升级原因），doctor
检查 Pillow 版本：< 9.4.0 返回 WARN（不阻塞打包但 alpha 退化）；版本号无法
解析时跳过版本检查仅报告已安装。

### 缓存目录大小扫描

用 `os.walk(path, followlinks=False)` 递归累加文件大小，避免引入额外依赖
（如 psutil）。目录不存在视为 OK（首次使用尚未下载缓存），扫描失败降级为
WARN（不影响打包）。`_format_size` 按 B/KiB/MiB/GiB/TiB/PiB 阶梯格式化。

### 报告渲染用 rich Table

复用 `console.step()`/`console.rich.print()` 输出，与现有日志配置一致。
表格列：名称 / 状态 / 详情 / 修复建议。状态列用 rich markup 着色
（`[green]√ OK[/]`/`[yellow]! WARN[/]`/`[red]× ERROR[/]`）。

## 代码实现情况

- `cli_doctor.py` 481 行，含完整类型注解与中文 docstring
- 13 个 check 函数（5 环境信息 + 8 工具检查）
- 通用 `_check_tool_version` 函数覆盖成功/未找到/超时/OSError/退出码非零/
  空stdout 6 种场景
- `print_doctor_report` 渲染环境信息表 + 工具检查表 + 汇总结论
- CLI 集成：`_run_doctor` 调用 `run_doctor` + `print_doctor_report`，
  无项目依赖，可在任何目录执行

## 整合优化情况

- 复用 `fspack.console.console` 单例，不新建 rich Console 实例
- 复用 `fspack.config.MIRRORS`/`DEFAULT_MIRROR` 与 `fspack.config.cache.cache_root`
- 延迟导入：`run_doctor` 内部 `from fspack.config import ...`，使
  `import fspack.cli_doctor` 不触发 config 加载
- `Platform` 类型仅在 TYPE_CHECKING 块导入，避免运行时循环依赖

## 测试验证结果

`tests/test_cli_doctor.py` 共 47 个测试，按场景分组：

- **数据结构**（6 项）：CheckStatus 枚举值、CheckResult frozen、DoctorReport
  has_error/has_warn/all_ok
- **_format_size**（7 项参数化）：0 B / 1023 B / 1.0 KiB / 1.5 KiB / 1.0 MiB
  / 1.0 GiB / 1.0 TiB
- **_format_status**（3 项参数化）：OK/WARN/ERROR 中文标签 + rich 样式
- **_check_tool_version**（8 项）：成功/未找到/未找到 warn_only/超时/OSError/
  退出码非零/no_parse_version/空 stdout
- **_check_pillow**（4 项）：OK/版本过低/未安装/版本不可解析
- **_check_pip**（4 项）：pip 命令/python -m pip 回退/未找到/模块执行 OSError
- **_check_cache_dir**（3 项）：不存在/有文件/扫描失败
- **_dir_size**（3 项）：空目录/有文件/跳过不可读
- **run_doctor**（3 项）：Windows 平台/Linux 平台/必备工具缺失 has_error
- **print_doctor_report**（4 项）：全 OK/有 ERROR/有 WARN/表格渲染
- **CLI 集成**（2 项）：`fsp doctor` 分发 / `fsp --help` 含 doctor

`cli_doctor.py` 覆盖率 96.31%（缺失行是各平台专属工具检查的实际 subprocess
调用，已在 run_doctor mock 测试中验证聚合逻辑）。

## 遗留事项

无。iter-86 完整交付 `fsp doctor` 功能。

## 下一轮计划

**iter-87：`--dry-run` 预览模式**：`fsp b --dry-run` 解析 pyproject.toml +
自动解析 Python 版本 + AST 扫描依赖 + 显示打包计划（目标平台、Python 版本、
依赖列表、预估 wheel 数、runtime 来源、loader 编译器）不执行下载与编译。
