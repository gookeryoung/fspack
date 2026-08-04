# iter-141: 打包速度端到端基线

## 需求清单

- [x] **iter-141 打包速度端到端基线**：新增 `tests/test_build_perf_baseline.py`：
  (1) 小项目（1 入口、3 依赖）冷/热缓存构建耗时基线；(2) 中项目（10 入口、20 依赖）
  基线；(3) 用 `pytest-benchmark` 的 `pedantic` 模式确保可复现

## 迭代目标

补齐 req-49 L122-124 列出的端到端基线任务（阶段 4 第一轮）：建立 `build()`
全流程编排耗时的可量化基线，作为 iter-142~144（Nuitka 编译/wheel 下载/启动时间）
优化的对照参考。所有重活（runtime 下载、wheel 下载、源码编译、loader 编译）通过
`monkeypatch` 替换为 noop，仅测量阶段编排 + `BuildTracker` + `ProjectInfo` 解析 +
`console` 渲染等开销。

## 改动文件清单

- `tests/test_build_perf_baseline.py`（新增）：
  - 4 个基线测试（`TestBuildPerfBaseline` 类，`@pytest.mark.slow`）：
    - `test_small_project_cold_cache_baseline`：小项目冷缓存（每轮 `clear_project_cache`）
    - `test_small_project_warm_cache_baseline`：小项目热缓存（预热一次命中 `lru_cache`）
    - `test_medium_project_cold_cache_baseline`：中项目冷缓存（10 入口 AST 扫描）
    - `test_medium_project_warm_cache_baseline`：中项目热缓存
  - 3 个 fixture：
    - `small_project`：1 入口 `smallapp.py` + 3 依赖（numpy/requests/PySide2）
    - `medium_project`：10 入口 `app0~app9.py` + 20 依赖（scientific/gui/web/io/db）
    - `_mock_pipeline`：patch 7 个阶段函数为 noop（与 `test_profile.py` 一致）
  - 辅助：`_empty_report`、`_medium_pyproject`、`_medium_entry_source`、
    `_IMPORTABLE_DEPS`（剔除 PyPI 名含 `-` 的依赖，避免 import 语法错误）

## 关键决策与依据

### mock 范围：7 个阶段函数为 noop

与 `test_profile.py`/`test_log_file.py` 一致，patch `fspack.packaging.pipeline`
下 7 个阶段函数：`_prepare_runtime`/`_analyze_dependencies`/`_download_dependencies`/
`write_pth`/`copy_source`/`_compile_user_sources`/`_build_entry_loaders`。

未 mock 的阶段：
- `_slim_runtime`：Windows 目标下 `_trim_standalone_runtime` 自动跳过（函数内检测
  Windows 直接 return），无需 mock
- `_resolve_project_icon`：项目无 `favicon.*` 时直接返回默认 `_DEFAULT_ICON`，
  轻量扫描，无副作用
- `_analyze_binary_dependencies`：`opts.analyze_deps` 默认 False 跳过

### `BuildOptions(no_sbom=True, no_size_report=True)` 跳过两个阶段

`generate_sbom` 会写 `dist/release/<name>-<version>-sbom.json`，跨轮次累积文件
让第二轮起 `_has_dist_artifacts` 检测到 `release/` 子目录触发半成品告警。
`print_size_report` 扫描 dist 目录引入 I/O 噪声。两者均与"编排开销"基线目标
无关，用 `no_sbom=True, no_size_report=True` 跳过。

### pedantic 模式 + rounds 选择

用 `benchmark.pedantic(build, setup=_setup, rounds=N, iterations=1)`：
- `iterations=1`：每轮单次调用，避免 inner loop 命中缓存（冷缓存场景关键）
- `setup` 每轮重建 kwargs 元组，冷缓存场景调 `clear_project_cache()`
- 小项目 `rounds=20`：与 `test_perf_baseline.py` 的 `ProjectInfo` 冷解析基线一致，
  含文件 I/O 抖动大，20 轮取 median 稳定
- 中项目 `rounds=15`：10 入口 AST 扫描开销大，15 轮平衡稳定性与运行时间

### `console.rich.capture()` 抑制输出

`build()` 末尾 `console.rich.print(tracker.summary())` 会输出表格污染 pytest
输出。用 `with console.rich.capture():` 包裹 `benchmark.pedantic` 调用，不影响
耗时测量（capture 仅重定向 stdout，不增加可观开销）。

### 中项目依赖列表设计

20 个依赖覆盖 scientific/gui/web/io/db 五个领域，便于 iter-142/143 按领域分组
测 Nuitka 编译与 wheel 下载基线。入口源码仅引用其中合法 Python 标识符的子集
（`_IMPORTABLE_DEPS`，剔除 `websocket-client`/`psycopg2-binary`/`Pillow`/`pyyaml`
等 PyPI 名含 `-` 或与模块名不一致的包），避免 `import websocket-client` 语法错误。

## 代码实现情况

### 小项目样本

```python
_SMALL_PYPROJECT = """
[project]
name = "smallapp"
version = "1.0.0"
dependencies = ["numpy", "requests", "PySide2"]

[tool.fspack]
pyc_strip = true
no_site = true
"""
```

入口 `smallapp.py` 含 `def main()` + `if __name__ == "__main__"` 守卫，触发
`detect_entry` 识别 + PySide2 import 触发 `app_type=gui` 推断。

### 中项目样本

```python
_MEDIUM_DEPS = ["numpy", "pandas", ..., "pyyaml"]  # 20 个

def _medium_pyproject():
    deps = ", ".join(f'"{d}"' for d in _MEDIUM_DEPS)
    entries = "\n".join(f'app{i} = "app{i}.py"' for i in range(10))
    return f"""[project]
name = "mediumapp"
dependencies = [{deps}]
[tool.fspack.entries]
{entries}
"""
```

每入口引用 2 个 third-party 依赖（按 `idx % len(_IMPORTABLE_DEPS)` 与
`(idx + 7) % len(_IMPORTABLE_DEPS)` 取模），让 AST 扫描有实际内容。

### pedantic 调用

```python
def _setup():
    clear_project_cache()  # 冷缓存场景调用，热缓存场景不调
    return (project,), kwargs

with console.rich.capture():
    result = benchmark.pedantic(
        build,
        setup=_setup,
        rounds=_ROUNDS_SMALL,
        iterations=1,
    )
assert result.name == "smallapp"  # 功能正确性验证
```

## 测试验证结果

### 实测基线数据（本地 Windows，Python 3.11）

| 测试 | Median | Min | Mean | StdDev | Rounds |
|------|--------|-----|------|--------|--------|
| small_warm | 3.15 ms | 2.97 ms | 3.20 ms | 0.15 ms | 20 |
| medium_warm | 3.12 ms | 3.05 ms | 3.36 ms | 0.56 ms | 15 |
| small_cold | 3.68 ms | 3.52 ms | 4.86 ms | 5.17 ms | 20 |
| medium_cold | 5.31 ms | 5.10 ms | 5.65 ms | 1.45 ms | 15 |

观察：
- **热缓存下中小项目接近**（3.15 vs 3.12 ms）：缓存命中 O(1)，编排开销与入口数无关
- **冷缓存下中项目显著慢**（5.31 vs 3.68 ms，+44%）：10 入口 AST 扫描开销显著
- **缓存收益**：小项目 +14%，中项目 +70%（入口数越多，AST 扫描开销越大，缓存收益越高）

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 1 file already formatted
- pyrefly: 0 errors
- pytest -m "not slow": 2105 passed, 12 skipped, 14 deselected
  （iter-140 为 10 deselected，新增 4 个 slow 测试被排除）
- coverage: 95.68%（>= 95% 门禁，与 iter-140 一致——新测试为 slow 不计入默认 coverage）

### 性能基线测试总数

req-49 验收标准要求"性能基线测试数 ≥ 14（现有 10 + 新增 4）"：
- 现有 10 个：`test_perf_baseline.py` 的 `TestAstBaseline`(2) + `TestSlimBaseline`(2) +
  `TestFingerprintBaseline`(1) + `TestProjectInfoBaseline`(2) + `TestEntryWrapperBaseline`(1) +
  `TestNuitkaEnsureEnvBaseline`(1) + `TestWheelDownloadCacheBaseline`(1)
- 新增 4 个：`test_build_perf_baseline.py` 的 `TestBuildPerfBaseline`(4)
- 合计 14 个，满足验收标准

## 整合优化情况

- mock 范围与 `test_profile.py`/`test_log_file.py` 一致（7 个阶段函数），保持
  测试套件内 mock 模式一致性，便于后续 iter-142~144 复用 fixture
- `_mock_pipeline` fixture 抽到模块级，4 个测试共用，避免每个测试重复 7 行
  `monkeypatch.setattr`
- `BuildOptions(no_sbom=True, no_size_report=True)` 是新引入的"基线测试用 opts"，
  后续 iter-142~144 的端到端基线可复用此模式跳过无关阶段

## 遗留事项

- 本基线测量的是"编排开销"（mock 掉重活），不反映真实打包耗时。真实打包耗时
  受 Nuitka 编译/wheel 下载/runtime 解压等主导，由 iter-142~144 各专项基线覆盖
- 中项目冷缓存 stddev=1.45ms（约 27% of median），噪声较大。原因：10 入口 AST
  扫描涉及 10 次文件 I/O + ast.parse，I/O 抖动大。`rounds=15` 已是稳定性与
  运行时间的折中，CI 跨运行对比时建议用 `compare_benchmark.py` 的 systemic
  检测（5/9 退化 + 中位 30%）避免误报
- `_IMPORTABLE_DEPS` 剔除了 4 个 PyPI 名与模块名不一致的依赖（Pillow→PIL、
  pyyaml→yaml、websocket-client→websocket、psycopg2-binary→psycopg2），
  实际入口 import 仅引用 16 个依赖。如需让 AST 扫描识别全部 20 个依赖，
  可在入口源码中用 `from PIL import Image` 等形式引用（iter-142 按需扩展）

## 下一轮计划

iter-142 Nuitka 编译基线（req-49 L125-127，阶段 4 第二轮）：
1. 50 文件串行 vs 并行（iter-131）对比基线
2. ccache 命中 vs 未命中对比
3. 加入 CI benchmark job，退化 >10% 失败
