# iter-144: 启动时间基线

## 需求清单

- [x] **iter-144 启动时间基线**：(1) entry wrapper 启动耗时基线（用 `python -X importtime`
  解析）；(2) lazy-import 启用 vs 关闭对比；(3) `--no-site` 启用 vs 关闭对比

## 迭代目标

补齐 req-49 L129-130 列出的启动时间基线任务（阶段 4 第四轮）：建立 entry
wrapper 在四种启动模式（默认/lazy/no-site/组合）下的端到端启动耗时基线，
验证 lazy-import 与 `--no-site` 优化效果。用真实 `subprocess.run` 启动
Python 解释器执行 wrapper，所有耗时都是真实的（无 mock），但通过最小化
dist 结构（仅 1 个模拟 `numpy` 包 + 1 个用户入口）控制绝对耗时，让优化
收益可观测。

## 改动文件清单

- `tests/test_entry_startup_baseline.py`（新增）：
  - 4 个基线测试（`TestEntryStartupBaseline` 类，`@pytest.mark.slow`）：
    - `test_default_startup_baseline`：默认启动（`python _entry_app.py`），
      numpy `__init__.py` 全量执行
    - `test_lazy_import_startup_baseline`：lazy-import 启用，numpy `__init__.py`
      延迟执行
    - `test_no_site_startup_baseline`：`python -S` 模拟 `--no-site`，跳过
      site.py 加载
    - `test_no_site_lazy_combined_baseline`：`python -S` + lazy 双重优化
  - 辅助：`_make_minimal_dist`、`_measure_wall_ms`、`_verify_importtime_lazy`
- `.github/workflows/ci.yml`（修改）：
  - benchmark job 新增 `tests/test_entry_startup_baseline.py`

## 关键决策与依据

### 真实 subprocess 而非 mock：测量端到端启动耗时

与前 3 个基线（iter-141~143）用 `time.sleep` mock 重活不同，本基线用真实
`subprocess.run` 启动 Python 解释器执行 wrapper。理由：

1. **启动耗时本质是端到端**：包含解释器启动 + site.py 加载 + wrapper 执行
   + numpy import + 退出，无法用 mock 拆解
2. **lazy-import 优化点在 import 链路**：`LazyLoader` 是否真延迟了
   `numpy/__init__.py` 执行，必须实际跑 import 才能验证
3. **`--no-site` 优化点在 site.py**：`python -S` 跳过 site.py 是 CPython
   内置行为，无法 mock

代价是 subprocess 启动抖动较大（StdDev 3-5ms，相对 median 5-8%），但 10
轮取 median 后中位数稳定，对比收益（lazy 省 ~50ms）远大于噪声。

### 最小化 dist 结构：1 个模拟 numpy + 1 个用户入口

不构造完整 dist（含 runtime/python.exe、nuitka 编译产物等），仅创建
wrapper 运行所需的最小目录：

```
dist/
├── _entry_app.py          # EntryWrapper 生成的包装器
├── src/
│   └── app.py             # 用户入口（import numpy; print）
└── runtime/
    └── Lib/
        └── site-packages/
            └── numpy/
                └── __init__.py  # time.sleep(0.05) 模拟重量级 init
```

用 `sys.executable` 作为解释器（开发机的 Python），不依赖 dist 中的
runtime/python.exe。wrapper 的 `_DIST_DIR`/`_RUNTIME_DIR`/`_SITE_PACKAGES`
计算仍走真实路径，验证 wrapper 在最小 dist 下的正确性。

### 模拟 numpy `__init__.py` 用 `time.sleep(0.05)`

真实 numpy `__init__.py` 启动耗时 ~80-150ms（C 扩展初始化 + 子模块导入），
用 50ms 模拟：

- 保持 CI 时间合理（4 基线 * 10 轮 * ~70ms = ~2.8s 总时间）
- 50ms 让 lazy 收益（省 `__init__.py` 执行）显著大于测量噪声（subprocess
  启动抖动 ~5-10ms），lazy vs 默认 median 差应 ~50ms
- `importlib.util.LazyLoader` 对纯 Python 模块（含 `time.sleep` 的
  `__init__.py`）有效，能真实延迟 `__init__.py` 执行

### `python -S` 模拟 `--no-site`

`python -S` 是 CPython 内置选项，跳过 `site.py` 加载（~10-20ms）。
`--no-site` 是 fspack 的运行时选项，语义与 `python -S` 一致（不加载
site.py），但 fspack 实现可能用 `PYTHONNOUSERSITE` 等环境变量。本基线
用 `python -S` 直接模拟，避免依赖 fspack 运行时实现细节。

注意：`python -S` 也跳过 `site-packages` 路径设置，但 wrapper 自身会
显式 `sys.path.insert(0, _SITE_PACKAGES)`，所以 numpy 仍可 import。

### `_verify_importtime_lazy`：用 `python -X importtime` 功能验证

基线测试主流程测 wall time（`_measure_wall_ms`），功能验证由
`_verify_importtime_lazy` 单独负责。`python -X importtime` 在 stderr
输出每个 import 的 cumulative/self 耗时（微秒），解析 `numpy` 行的
cumulative：

- **lazy 关闭**：cumulative 应 > 40000us（含 `time.sleep(50ms)` = 50000us）
- **lazy 启用**：cumulative 应 < 10000us（仅 LazyLoader 创建，不执行
  `__init__.py`）

正则 `_IMPORTTIME_RE = re.compile(r"^import time:\s*\d+\s*\|\s*(\d+)\s*\|.*\bnumpy\b")`
匹配 cumulative 列。本验证确保 lazy 优化真实生效，而非 wall time 偶然
降低。

### `app.py` 只 `import numpy` 不访问属性

```python
import numpy
print("hello")
```

- **默认模式**：`import numpy` 触发 `__init__.py` 执行（sleep 50ms）
- **lazy 模式**：`import numpy` 仅创建 LazyLoader 模块对象，
  `__init__.py` 不执行（app 不访问 numpy 属性，lazy 永不触发）

这样 wall time 差异就是 50ms（numpy `__init__.py` 的 sleep 耗时），
干净反映 lazy 收益。若 app 访问 numpy 属性（如 `numpy.array`），lazy
会触发实际加载，wall time 与默认一致，无法测出 lazy 收益。

### rounds 选择

- 4 基线均用 `rounds=10`：subprocess 启动抖动较大（OS 调度、文件系统
  缓存），需要足够轮数取 median
- ~50-90ms/轮，10 轮平衡稳定性与 CI 运行时间（4 基线 * 10 轮 * ~70ms
  = ~2.8s 总时间）
- 与 iter-141 的 `medium_cold rounds=15` 相比略低，因每轮耗时更短

### 退化阈值：25% 保持不变，10% 延至 iter-145

与 iter-142/143 一致，当前 `compare_benchmark.py` 用单一全局阈值，降到
10% 会让现有高方差测试（如 `medium_cold` stddev=27%）频繁误报。iter-145
规划"按基线类别分组对比"支持按类别设阈值后，为启动时间基线（subprocess
抖动 5-8%）单独设阈值。

## 代码实现情况

### `_make_minimal_dist` 辅助

构造最小 dist 目录：

```python
def _make_minimal_dist(dist_dir, *, lazy_imports=()):
    # 模拟 numpy 包：__init__.py sleep 50ms 模拟重量级 init
    numpy_init = dist_dir / "runtime" / "Lib" / "site-packages" / "numpy" / "__init__.py"
    numpy_init.write_text(f"import time\ntime.sleep({_NUMPY_INIT_SLEEP})\n")
    # 用户入口：import numpy 但不访问其属性
    (src_dir / "app.py").write_text('import numpy\nprint("hello")\n')
    # 生成 wrapper：顶层模式（module_dotted=None）
    wrapper_source = EntryWrapper.generate_wrapper_source(
        entry_name="app", module_dotted=None, entry_rel="app.py",
        pkg_root_rel=".", has_tkinter=False, lazy_imports=lazy_imports,
    )
    wrapper_path = dist_dir / "_entry_app.py"
    wrapper_path.write_text(wrapper_source)
    return wrapper_path
```

`lazy_imports` 参数透传给 `EntryWrapper.generate_wrapper_source`：空元组
时 wrapper 不注入 `_LazyImportFinder`，非空时注入 finder。

### `_measure_wall_ms` 辅助

用 `time.perf_counter` 包住 `subprocess.run`，返回端到端 wall time
（含解释器启动 + wrapper 执行 + numpy import + 退出）。功能验证由
`_verify_importtime_lazy` 单独负责。

### `_verify_importtime_lazy` 辅助

用 `python -X importtime` 解析 numpy cumulative，验证 lazy 是否真延迟
了 `__init__.py` 执行：

```python
_IMPORTTIME_RE = re.compile(r"^import time:\s*\d+\s*\|\s*(\d+)\s*\|.*\bnumpy\b")

def _verify_importtime_lazy(dist_dir, wrapper_path, *, lazy_enabled):
    result = subprocess.run([sys.executable, "-X", "importtime", str(wrapper_path)], ...)
    numpy_cumulative_us = None
    for line in result.stderr.splitlines():
        m = _IMPORTTIME_RE.match(line)
        if m:
            numpy_cumulative_us = int(m.group(1))
            break
    if lazy_enabled:
        assert numpy_cumulative_us < 10000  # __init__.py 不应执行
    else:
        assert numpy_cumulative_us > 40000  # __init__.py 应执行 sleep(0.05)
```

## 测试验证结果

### 实测基线数据（本地 Windows，Python 3.11.15，rounds=10）

| 测试 | Median | Min | Mean | StdDev | Rounds |
|------|--------|-----|------|--------|--------|
| default | 94.47 ms | 94.09 ms | 96.29 ms | 5.49 ms | 10 |
| no_site | 91.71 ms | 91.04 ms | 92.39 ms | 1.71 ms | 10 |
| lazy | 43.35 ms | 42.23 ms | 44.50 ms | 3.54 ms | 10 |
| no_site_lazy_combined | 41.45 ms | 40.12 ms | 41.73 ms | 1.24 ms | 10 |

对比分析：

- **默认 vs lazy**：94.47 - 43.35 = **51.12ms 收益**，接近 numpy
  `__init__.py` 的 `time.sleep(0.05)` 耗时（50ms），证明 lazy 真实
  延迟了 `__init__.py` 执行
- **默认 vs no-site**：94.47 - 91.71 = **2.76ms 收益**，site.py 在
  Windows 上加载耗时较小（~3ms，Linux 上可能 ~10-20ms）
- **默认 vs 组合**：94.47 - 41.45 = **53.02ms 收益**，双重优化收益
  ≈ lazy 收益 + no-site 收益（51.12 + 2.76 = 53.88ms，实测 53.02ms
  接近）
- **lazy vs 组合**：43.35 - 41.45 = 1.90ms，组合中 no-site 增量收益
  与单独 no-site 收益（2.76ms）一致
- **StdDev 1.24-5.49ms**：subprocess 启动抖动比 mock 测试大（iter-143
  wheel baseline 是 0.025-0.524ms StdDev），但 median 稳定，对比收益
  （50ms）远大于噪声（5ms）

### `python -X importtime` 功能验证

4 个基线均通过 `_verify_importtime_lazy` 验证：

- `default`/`no_site`：numpy cumulative > 40000us（`__init__.py` 执行
  `sleep(0.05)`）
- `lazy`/`no_site_lazy_combined`：numpy cumulative < 10000us（`__init__.py`
  不执行，仅 LazyLoader 创建）

证明 lazy-import 优化真实生效，而非 wall time 偶然降低。

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 123 files already formatted
- pyrefly check: 0 errors（17 suppressed, 6 warnings not shown）
- pytest -m "not slow": 2105 passed, 12 skipped, 26 deselected
  （iter-143 为 22 deselected，新增 4 个 slow 测试被排除）
- coverage: 95.68%（≥ 95% 门禁，与 iter-143 一致——新测试为 slow 不计入
  默认 coverage）

### 性能基线测试总数

- 现有 10 个：`test_perf_baseline.py`
- iter-141 新增 4 个：`test_build_perf_baseline.py`
- iter-142 新增 4 个：`test_nuitka_compile_baseline.py`
- iter-143 新增 4 个：`test_wheel_download_baseline.py`
- iter-144 新增 4 个：`test_entry_startup_baseline.py`
- 合计 26 个，远超 req-49 验收标准"性能基线测试数 ≥ 14"

## 整合优化情况

- 4 基线结构与 iter-141~143 的 4 基线对齐（`@pytest.mark.slow` +
  `class TestXxxBaseline` + rounds 注释）
- `EntryWrapper.generate_wrapper_source` 调用方式与 `test_entry.py` 的
  单元测试一致，验证 wrapper 在最小 dist 下的端到端行为
- `python -X importtime` 解析模式可复用于后续 wrapper 优化验证（如
  `path_importer_cache` 预填充效果）
- CI benchmark job 一并扩展，覆盖 iter-141~144 全部基线测试文件

## 遗留事项

- 退化阈值 10% 延至 iter-145：当前 `compare_benchmark.py` 单一全局阈值
  降到 10% 会让高方差测试误报。iter-145 "按基线类别分组对比"支持按类别
  设阈值后，为启动时间基线（subprocess 抖动 5-8%）单独设阈值
- 本基线用 `time.sleep(0.05)` 模拟 numpy `__init__.py`，不反映真实
  numpy 启动耗时（~80-150ms）。真实启动耗时基线需端到端集成测试
  （打包真实 numpy + 真实 runtime/python.exe），超出 req-49 范围
- `python -S` 模拟 `--no-site`，不验证 fspack 运行时 `--no-site` 选项
  的实现（可能用 `PYTHONNOUSERSITE` 等环境变量）。运行时选项验证需
  端到端打包测试
- no-site 收益在 Windows 上仅 ~3ms，Linux 上可能 ~10-20ms（site.py
  加载更多路径）。CI 在 Ubuntu 上运行，预期 no-site 收益比本地 Windows
  更大

## 下一轮计划

iter-145 CI 性能门禁固化（req-49 L131-134，阶段 4 第五轮，收尾）：
1. `.github/workflows/ci.yml` benchmark job 扩展，覆盖 iter-141~144 新基线
   （已逐步扩展，iter-145 检查完整性）
2. `scripts/compare_benchmark.py` 支持按基线类别分组对比（不同类别设
   不同阈值，解决 subprocess 抖动基线误报）
3. 文档 `docs/performance.md` 性能基线对比与更新指南
4. iter-126~145 全量回归与基线快照 `0126_iter145-final.json`
