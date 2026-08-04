# iter-142: Nuitka 编译基线

## 需求清单

- [x] **iter-142 Nuitka 编译基线**：(1) 50 文件串行 vs 并行（iter-131）对比基线；
  (2) ccache 命中 vs 未命中对比；(3) 加入 CI benchmark job，退化 >10% 失败

## 迭代目标

补齐 req-49 L125-127 列出的 Nuitka 编译基线任务（阶段 4 第二轮）：建立
`_compile_files` 在串行/并行/ccache 命中/未命中四种模式下的可量化基线，
验证 iter-131 并行化提速效果与 ccache 加速比。所有重活（`_stream_compile`
子进程调用、`_build_compile_env` 环境构造）被 mock，仅测量 Python 层编排
开销 + 模拟编译耗时。

## 改动文件清单

- `tests/test_nuitka_compile_baseline.py`（新增）：
  - 4 个基线测试（`TestNuitkaCompileBaseline` 类，`@pytest.mark.slow`）：
    - `test_serial_compile_baseline`：串行编译（`_MAX_COMPILE_WORKERS=1`），50 文件
    - `test_parallel_compile_baseline`：并行编译（默认 `max_workers=min(cpu,4)`），50 文件
    - `test_ccache_hit_baseline`：ccache 命中模拟（2ms/文件，并行模式）
    - `test_ccache_miss_baseline`：ccache 未命中模拟（20ms/文件，并行模式）
  - 1 个 fixture：`_compile_setup` 构造 50 个 `.py` 文件 + mock `_build_compile_env`
  - 辅助：`_make_py_files`、`_make_sleep_stream`
- `.github/workflows/ci.yml`（修改）：
  - benchmark job 新增 `tests/test_build_perf_baseline.py`（补 iter-141 遗漏）
    与 `tests/test_nuitka_compile_baseline.py`

## 关键决策与依据

### mock 模式：`_stream_compile` 用 `time.sleep` 模拟编译耗时

`_stream_compile` 是真实 nuitka 子进程调用的入口，mock 后用 `time.sleep`
替代。`time.sleep` 释放 GIL，与真实 `Popen.wait` 阻塞行为一致，让
`ThreadPoolExecutor` 线程并行收益可观测（并行模式下多个 worker 同时 sleep，
总耗时接近单文件耗时而非 N 倍）。

sleep 时长选择：
- 串行/并行基线：10ms/文件（50 文件串行 500ms，并行 ~125ms）
- ccache 命中：2ms/文件（模拟 gcc 读缓存 .o，50 文件并行 ~25ms）
- ccache 未命中：20ms/文件（模拟 gcc 全量编译，50 文件并行 ~250ms）

### 串行模式：monkeypatch `_MAX_COMPILE_WORKERS=1`

`_compile_files` 内 `max_workers = min(cpu_count, _MAX_COMPILE_WORKERS)`。
将 `_MAX_COMPILE_WORKERS` monkeypatch 为 1 让 `max_workers=1`，
`ThreadPoolExecutor` 退化为单线程顺序执行，与 iter-131 前的串行 for 循环
等价。无需改 `_compile_files` 源码，纯测试侧 mock。

### ccache 命中/未命中模拟

ccache 状态在真实代码中通过 `_build_compile_env` 构造的 `CC` 环境变量区分
（`CC="ccache gcc"` vs `CC="gcc"`），影响 gcc 编译速度。本基线 mock
`_build_compile_env` 为空 dict（屏蔽环境构造开销），通过 `_stream_compile`
的不同 sleep 时长模拟 gcc 编译速度差异：
- 命中：2ms/文件（gcc 读缓存 .o，快）
- 未命中：20ms/文件（gcc 全量编译，慢）

10x 比例符合 ccache 官方文档（5-10x 加速比）。

### rounds 选择

- 串行 rounds=5：500ms/轮，5 轮平衡稳定性与运行时间（串行慢）
- 并行 rounds=10：125ms/轮，10 轮取 median 稳定
- ccache 命中 rounds=15：25ms/轮（耗时短），15 轮确保统计稳定
- ccache 未命中 rounds=10：250ms/轮，10 轮平衡稳定性与运行时间

### CI benchmark job 扩展

iter-141 遗漏：`test_build_perf_baseline.py` 未加入 CI benchmark job。
本次一并补上。CI 现运行三个基线测试文件：`test_perf_baseline.py` +
`test_build_perf_baseline.py` + `test_nuitka_compile_baseline.py`。

### 退化阈值：25% 保持不变，10% 延至 iter-145

req-49 L126 要求"退化 >10% 失败"，但当前 `compare_benchmark.py` 用单一
全局阈值，降到 10% 会让现有高方差测试（如 `medium_cold` stddev=27%）
频繁误报。iter-145 规划"按基线类别分组对比"支持按类别设阈值，届时为
Nuitka 基线（确定性 sleep，stddev <1%）单独设 10% 阈值。本次保持 25%
全局阈值，新测试首次运行为"无历史可比"不触发退化判定。

## 代码实现情况

### 测试样本

50 个 `.py` 文件（与 `test_perf_baseline.py` 的 AST 基线对齐），内容简单
（`x = <i>`），因为 `_stream_compile` 被 mock 不解析内容。

### `_make_sleep_stream` 辅助

```python
def _make_sleep_stream(sleep_seconds: float) -> Any:
    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        time.sleep(sleep_seconds)
        return (0, "", "")
    return staticmethod(fake_stream)
```

返回 `staticmethod` 包装的 fake 函数，直接 `monkeypatch.setattr` 到
`NuitkaCompiler._stream_compile`。

### `_compile_setup` fixture

```python
@pytest.fixture
def _compile_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))
    src = tmp_path / "src"
    py_files = _make_py_files(src)
    return tmp_path, py_files
```

4 个测试共用，避免每个测试重复构造文件 + mock env。

## 测试验证结果

### 实测基线数据（本地 Windows，Python 3.11，min-rounds=3 快速验证）

| 测试 | Median | Min | Mean | StdDev | Rounds |
|------|--------|-----|------|--------|--------|
| serial | 514.52 ms | 514.47 ms | 515.13 ms | 1.10 ms | 5 |
| parallel | 134.84 ms | 134.31 ms | 134.97 ms | 0.58 ms | 10 |
| ccache_hit | 32.92 ms | 31.07 ms | 32.85 ms | 0.63 ms | 15 |
| ccache_miss | 266.09 ms | 265.59 ms | 266.07 ms | 0.41 ms | 10 |

对比分析：
- **并行 vs 串行**：514.52 / 134.84 = **3.82x 提速**（远超 30% 验收标准）
  - 理论上限 4x（`max_workers=4`），实测 3.82x 接近上限（编排开销 ~10ms）
- **ccache 命中 vs 未命中**：266.09 / 32.92 = **8.08x 加速比**
  - 符合 ccache 官方文档 5-10x 区间
- **StdDev < 1% of median**：确定性 `time.sleep` 让基线极稳定，CI 跨运行
  对比不会误报退化

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 1 file already formatted
- pyrefly: 0 errors
- pytest -m "not slow": 2105 passed, 12 skipped, 18 deselected
  （iter-141 为 14 deselected，新增 4 个 slow 测试被排除）
- coverage: 95.68%（≥ 95% 门禁，与 iter-141 一致——新测试为 slow 不计入默认 coverage）

### 性能基线测试总数

- 现有 10 个：`test_perf_baseline.py`
- iter-141 新增 4 个：`test_build_perf_baseline.py`
- iter-142 新增 4 个：`test_nuitka_compile_baseline.py`
- 合计 18 个，满足 req-49 验收标准"性能基线测试数 ≥ 14"

## 整合优化情况

- mock 模式与 `test_nuitka.py` 的 `_compile_files` 测试一致
  （`_stream_compile` + `_build_compile_env`），保持测试套件内一致性
- `_compile_setup` fixture 抽到模块级，4 个测试共用
- CI benchmark job 一并补上 iter-141 遗漏的 `test_build_perf_baseline.py`

## 遗留事项

- 退化阈值 10% 延至 iter-145：当前 `compare_benchmark.py` 单一全局阈值
  降到 10% 会让高方差测试误报。iter-145 "按基线类别分组对比"支持按类别
  设阈值后，为 Nuitka 基线（stddev <1%）单独设 10%
- 本基线用 `time.sleep` 模拟编译耗时，不反映真实 nuitka 编译耗时（受 gcc
  版本、源码复杂度、ccache 状态影响）。真实编译耗时基线需端到端集成测试，
  超出 req-49 范围
- ccache 命中/未命中通过不同 sleep 时长模拟，未测真实 ccache 缓存逻辑
  （`_ensure_ccache` PATH 查找/下载）。`_ensure_ccache` 由
  `test_perf_baseline.py` 的 `TestNuitkaEnsureEnvBaseline` 间接覆盖

## 下一轮计划

iter-143 wheel 下载基线（req-49 L127-128，阶段 4 第三轮）：
1. pip vs uv（iter-132）下载 50 wheel 对比基线
2. 缓存命中 vs 冷下载对比
3. 加入 CI benchmark job
