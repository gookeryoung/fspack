# iter-90: `--profile` 耗时分析报告

## 需求清单

- [x] 新增 `packaging/profile.py` 模块实现耗时分析采集与渲染
- [x] 复用 `BuildTracker.stage()` 已收集的各阶段 wall time 数据
- [x] 扩展内存峰值采集（用 `tracemalloc` 标准库替代 psutil，避免新依赖）
- [x] 采集 CPU 时间（`time.process_time()`）
- [x] CLI `fsp b` 添加 `--profile` 标志
- [x] `pipeline.build()` 集成 `ProfileContext` 生命周期管理
- [x] 报告含「耗时分析报告」表格（各阶段）+「资源总览」表格（wall/cpu/内存）
- [x] 提供 `profile_report_to_json` 函数支持 JSON 序列化
- [x] 编写测试覆盖 profile 模块、CLI 透传与 build 集成
- [x] 全套门禁通过 + 文档更新 + git 提交

## 迭代目标

为 `fsp b` 添加 `--profile` 选项，构建结束后输出耗时分析报告，识别瓶颈阶段。
报告含两部分：「耗时分析报告」表格（各阶段 wall time/占比/缓存命中/下载/节省/
项数/备注）与「资源总览」表格（墙钟时间/CPU 时间/CPU 占比/内存峰值）。

## 改动文件清单

- `src/fspack/packaging/profile.py`：新增模块，定义 `ProfileReport` 数据类、
  `ProfileContext` 上下文管理器、`print_profile_report`/`profile_report_to_json` 函数
- `src/fspack/packaging/pipeline.py`：`build()` 添加 `profile` 参数；
  用 `ProfileContext` 包装 `_execute_build`，构建结束后调用 `print_profile_report`
- `src/fspack/cli.py`：`_add_build_subparser` 注册 `--profile` 标志；
  `_run_build` 透传 `profile=ns.profile`
- `tests/test_profile.py`：新增 20 个测试覆盖 profile 模块、CLI 透传与 build 集成
- `tests/test_cli.py`/`test_build_dry_run.py`/`test_size_report.py`/
  `test_log_file.py`/`test_cli_recursive.py`：fake_build 签名同步添加 `profile` 参数
- `README.md`：`fsp build` 命令速查表与章节添加 `--profile` 选项
- `.trae/req/req-47-feature-perf-polish.md`：iter-90 标记完成

## 关键决策与依据

### 1. 用 tracemalloc 标准库替代 psutil（待用户复核）

req-47 原计划用 `psutil`（新增依赖）采集内存峰值。本迭代改用 Python 标准库
`tracemalloc`，理由：

- `tracemalloc` 是 Python 3.4+ 标准库，无新依赖，符合「优先标准库」原则
- 对于打包工具，主要内存消耗是 Python 对象（AST/文件列表/配置等），
  `tracemalloc` 测量 Python 分配峰值足够精确
- `psutil` 测量 RSS 包含 C 扩展内存，但 fspack 不大量使用 C 扩展
- 避免新依赖降低项目复杂度与打包体积

如后续迭代需要测量 RSS（含 C 扩展内存），可引入 `psutil` 作为可选依赖。

### 2. 独立模块而非内联到 pipeline.py

按用户偏好「按责任拆分模块」，耗时分析涉及 tracemalloc 生命周期、CPU 时间
采样、表格渲染、JSON 序列化等多个关注点，独立到 `packaging/profile.py`
便于测试与复用。pipeline.py 仅在 `build()` 入口做一次 `ProfileContext` 包装
与 `print_profile_report` 调用。

### 3. ProfileContext 用上下文管理器管理生命周期

`ProfileContext` 用 `__enter__`/`__exit__` 管理 `tracemalloc` 启动/停止，
确保异常路径也能正确清理。`collect()` 方法在 `with` 块外仍可调用
（`memory_peak` 为 0），便于在 `finally` 后采集数据。

### 4. 不添加 BuildOptions 字段

`--profile` 是一次性诊断选项，不需要持久化到 `[tool.fspack]` 配置层
（与 `--dry-run` 类似）。仅在 CLI 层处理，透传到 `build()` 的 `profile` 参数。

### 5. 复用 StageRecord 而非新定义数据类

`BuildTracker` 已通过 `stage()` 收集各阶段 `StageRecord`（含 elapsed/
bytes_downloaded/bytes_saved/cache_hit/items/skipped/detail），`ProfileReport`
直接复用 `tuple[StageRecord, ...]` 作为 stages 字段，避免重复定义。

## 代码实现情况

### profile.py 核心类与函数

```python
@dataclass(frozen=True)
class ProfileReport:
    """构建耗时分析报告."""
    wall_time: float       # 总墙钟时间
    cpu_time: float        # 总 CPU 时间
    memory_peak: int       # 内存峰值（字节，tracemalloc）
    stages: tuple[StageRecord, ...]  # 各阶段记录

    @property
    def cpu_ratio(self) -> float:
        """CPU 占比 = cpu_time / wall_time."""
        return self.cpu_time / self.wall_time if self.wall_time > 0 else 0.0

class ProfileContext:
    """耗时分析上下文，管理 tracemalloc 与 CPU 时间采样."""

    def __enter__(self) -> ProfileContext:
        """进入：启动 tracemalloc，记录 CPU 与墙钟起点."""
        self._cpu_start = time.process_time()
        self._wall_start = time.perf_counter()
        tracemalloc.start()
        self._started = True
        return self

    def __exit__(self, *exc: object) -> None:
        """退出：停止 tracemalloc."""
        if self._started:
            tracemalloc.stop()
            self._started = False

    def collect(self, tracker: BuildTracker) -> ProfileReport:
        """采集 profile 数据，返回 ProfileReport."""
        ...

def print_profile_report(report: ProfileReport) -> None:
    """渲染耗时分析报告表格 + 资源总览表格到控制台."""
    ...

def profile_report_to_json(report: ProfileReport) -> str:
    """序列化为 JSON 字符串，便于 CI 上传到 ELK/Loki."""
    ...
```

### pipeline.py 集成

```python
def build(..., profile: bool = False) -> ProjectInfo:
    """执行完整构建流水线."""
    ...
    profile_ctx = ProfileContext() if profile else None
    try:
        if profile_ctx is not None:
            with profile_ctx:
                info = _execute_build(...)
        else:
            info = _execute_build(...)
    finally:
        teardown_log_file(log_wrapper)
    if profile_ctx is not None:
        report = profile_ctx.collect(tracker)
        print_profile_report(report)
    return info
```

### CLI 透传

```python
p.add_argument(
    "--profile",
    action="store_true",
    help="启用耗时分析报告：wall/CPU/内存峰值 + 各阶段占比...",
)

# _run_build
build(..., profile=ns.profile)
```

## 测试验证结果

`tests/test_profile.py` 20 个测试：

**ProfileReport 数据类（4 个）**：
- 字段正确赋值/`cpu_ratio` 计算/`wall_time=0` 避免除零/frozen 不可变

**ProfileContext 上下文管理器（5 个）**：
- 进入启动 tracemalloc/退出停止/`collect` 返回报告/`collect` 在 `__exit__` 后调用
  /异常时清理 tracemalloc/追踪内存分配

**print_profile_report（3 个）**：
- 渲染表格不抛异常/空 stages 列表/显示缓存命中与下载字节

**profile_report_to_json（3 个）**：
- 输出合法 JSON/空 stages/保留中文（`ensure_ascii=False`）

**CLI 透传（2 个）**：
- `--profile` 透传 True/未指定默认 False

**build 集成（3 个）**：
- `profile=True` 输出报告/`profile=False` 不输出/异常时清理 tracemalloc

**门禁结果**：
- ruff check：All checks passed
- ruff format：90 files already formatted
- pyrefly check：0 errors（5 suppressed, 7 warnings not shown）
- pytest：1418 passed, 1 skipped, 覆盖率 97.81%（>= 95%）
- profile.py 模块覆盖率 95%

## 整合优化情况

- `ProfileReport` 用 `frozen=True` dataclass，符合「配置/描述类用 frozen」约定
- `ProfileContext` 用 `__slots__` 减少内存占用
- `cpu_ratio` 用 property 计算，避免存储冗余字段
- `profile_report_to_json` 用 `round(x, 4)` 控制浮点精度，便于 JSON 对比
- 复用 `BuildTracker.records` 与 `StageRecord`，不重复定义数据结构

## 遗留事项

- **待用户复核**：用 `tracemalloc` 标准库替代 `psutil` 的决策。如需测量 RSS
  （含 C 扩展内存），可在后续迭代引入 `psutil` 作为可选依赖
- JSON 报告当前仅通过 `profile_report_to_json` 函数提供，未添加 `--profile-file`
  选项写入文件。如需文件输出，可在后续迭代添加

## 下一轮计划

iter-91：`nuitka_compile.py` + `nuitka_env.py` 拆分。`nuitka_compile.py` 809 行
→ `nuitka_compile.py`（compile_src/compile_packages + stamp 缓存）+
`nuitka_strip.py`（产物剥离与 .pyd 验证）；`nuitka_env.py` 666 行 →
`nuitka_env.py` + `nuitka_standalone.py` + `nuitka_ccache.py`。
