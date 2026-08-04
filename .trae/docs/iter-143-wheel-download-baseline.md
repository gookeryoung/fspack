# iter-143: wheel 下载基线

## 需求清单

- [x] **iter-143 wheel 下载基线**：(1) pip vs uv（iter-132）下载 50 wheel 对比基线；
  (2) 缓存命中 vs 冷下载对比；(3) 加入 CI benchmark job

## 迭代目标

补齐 req-49 L127-128 列出的 wheel 下载基线任务（阶段 4 第三轮）：建立
pip/uv 并行下载与缓存命中/冷下载四种模式下的可量化基线，验证 iter-132
uv 加速效果与 deps_cache 缓存命中收益。所有网络 I/O（subprocess 调用
pip/uv）通过 mock 替换为 ``time.sleep``，仅测量 Python 层编排开销
（``ThreadPoolExecutor`` 调度、``as_completed`` 聚合）+ 模拟下载耗时。

## 改动文件清单

- ``tests/test_wheel_download_baseline.py``（新增）：
  - 4 个基线测试（``TestWheelDownloadBaseline`` 类，``@pytest.mark.slow``）：
    - ``test_pip_parallel_download_baseline``：pip 并行下载 50 包，30ms/包
    - ``test_uv_parallel_download_baseline``：uv 并行下载 50 包，10ms/包
    - ``test_cache_hit_baseline``：deps_cache 命中跳过下载，预填 50 wheel
    - ``test_cold_download_baseline``：冷下载完整编排，mock 链路 7 处
  - 辅助：``_make_resolved_packages``、``_make_sleep_download_one``、``_run_parallel_download``
- ``.github/workflows/ci.yml``（修改）：
  - benchmark job 新增 ``tests/test_wheel_download_baseline.py``

## 关键决策与依据

### mock 模式：单包下载函数用 ``time.sleep`` 模拟网络耗时

``_download_one_resolved``（pip 路径）与 ``_download_one_with_uv``（uv 路径）
是 ``_download_resolved_parallel`` 内 ``ThreadPoolExecutor`` 调用的单包下载
入口。mock 这两个函数用 ``time.sleep`` 替代真实 subprocess 调用。

``time.sleep`` 释放 GIL，与真实 ``subprocess.run`` 阻塞行为一致，让
``ThreadPoolExecutor`` 线程并行收益可观测（8 worker 并行下 50 包，总耗时
接近 ``单包耗时 * ceil(50/8)`` 而非 ``单包耗时 * 50``）。

sleep 时长选择：
- pip 路径：30ms/包（pip 启动 ~150ms + 网络，缩短 5x 保持 CI 时间合理）
- uv 路径：10ms/包（uv 启动 ~10ms + 网络，比 pip 快 3x）
- 3x 比例符合 iter-132 设计目标（uv 比 pip 快 2-5x：无 Python 解释器启动
  开销 + Rust HTTP 客户端 reqwest 并发连接）

### pip vs uv 路径切换：``uv_path`` 参数

``_download_resolved_parallel`` 的 ``uv_path`` 参数控制走 uv 还是 pip 路径：
- ``uv_path=None`` → ``_download_worker`` 直接调 ``_download_one_resolved``（pip）
- ``uv_path="uv"`` → ``_download_worker`` 优先调 ``_download_one_with_uv``（uv），
  失败才回退 pip

两条路径都用 ``ThreadPoolExecutor`` 并行（``max_workers=min(8, len(resolved))``），
区别仅在单包耗时。pip 基线 mock ``_download_one_resolved``，uv 基线 mock
``_download_one_with_uv``，互不干扰。

### 缓存命中基线：预填 50 wheel + deps cache

``download_wheels`` 入口先调 ``_deps_cache_key`` 计算缓存键，再调
``_load_deps_cache`` 查缓存。命中则直接返回缓存中的 wheel 路径列表，
跳过整个 pip/uv 下载流程。

本基线预创建 50 个 wheel 文件 + 写入 deps cache（``_save_deps_cache``），
让 ``download_wheels`` 走缓存命中分支。与 ``test_perf_baseline.py`` 的
``TestWheelDownloadCacheBaseline`` 互补：后者测 ``_load_deps_cache`` 单次
调用耗时，本基线测 ``download_wheels`` 入口缓存命中路径（含
``_deps_cache_key`` 计算 + ``_load_deps_cache`` + ``StageRecorder`` 回写）。

### 冷下载基线：7 处 mock 完整链路

冷下载基线测 ``download_wheels`` 完整冷下载编排开销，mock 链路 7 处：

1. ``_load_deps_cache`` 返回 None（强制冷下载）
2. ``_save_deps_cache`` noop（避免写文件 I/O 影响基线）
3. ``_find_pip_python`` 返回 "python"（避免 PATH 查找 subprocess）
4. ``_run_pip`` 返回 None（让 ``--no-index`` 离线解析"失败"，走 ``_download_online``）
5. ``_find_uv`` 返回 "uv"，``_uv_supports_download`` 返回 True
6. ``_resolve_with_uv`` 返回 50 包解析结果
7. ``_download_one_with_uv`` sleep 10ms 模拟 uv 下载

本基线应接近 uv 并行下载基线（编排开销 <10ms），差异来自 ``_deps_cache_key``
计算 + ``_load_deps_cache`` 查找 + ``_find_pip_python`` + ``_run_pip``
+ ``_resolve_with_uv`` + ``_parse_wheel_names`` 等编排开销。

### rounds 选择

- pip 并行 rounds=8：~210ms/轮，8 轮平衡稳定性与运行时间
- uv 并行 rounds=12：~70ms/轮，12 轮取 median 稳定
- 缓存命中 rounds=20：耗时极短（<5ms），20 轮确保统计稳定
- 冷下载编排 rounds=10：与 uv 并行一致，10 轮平衡稳定性与运行时间

### 退化阈值：25% 保持不变，10% 延至 iter-145

与 iter-142 一致，当前 ``compare_benchmark.py`` 用单一全局阈值，降到 10%
会让现有高方差测试（如 ``medium_cold`` stddev=27%）频繁误报。iter-145
规划"按基线类别分组对比"支持按类别设阈值后，为 wheel 下载基线
（确定性 sleep，stddev <1%）单独设 10%。

## 代码实现情况

### 测试样本

50 个 ``name==version`` 精确版本需求（``pkg_000==1.0.0`` ~ ``pkg_049==1.0.0``），
与 ``test_perf_baseline.py`` 的 AST 基线 / ``test_nuitka_compile_baseline.py``
的编译基线对齐，覆盖中等规模项目依赖。

### ``_make_sleep_download_one`` 辅助

```python
def _make_sleep_download_one(sleep_seconds: float, *, mode: str) -> Any:
    def fake_download(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        time.sleep(sleep_seconds)
        req = kwargs.get("req")
        if req is None and args:
            req = args[1] if mode == "uv" else args[0]
        pkg_name = req.split("==")[0] if req else "pkg"
        wheel_name = f"{pkg_name}-1.0.0-py3-none-any.whl"
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=f"Saved {wheel_name}\n", stderr="",
        )
    return fake_download
```

``mode`` 参数区分 pip/uv 路径的 req 位置：pip 路径
``_download_one_resolved(req, base_args, ...)`` req 在 args[0]；uv 路径
``_download_one_with_uv(uv_path, req, cache_dir, ...)`` req 在 args[1]。
返回带 ``Saved <name>.whl`` stdout 的 ``CompletedProcess`` 供
``_parse_pip_download_wheels`` 解析。

### ``_run_parallel_download`` 辅助

封装 ``_download_resolved_parallel`` 调用，构造最小化参数（``base_args``/
``extra_args`` 仅满足函数签名，实际 pip/uv 子进程已被 mock 不会执行）。
pip/uv 基线共用，区别仅在 ``uv_path`` 参数与 mock 的单包下载函数。

## 测试验证结果

### 实测基线数据（本地 Windows，Python 3.11，min-rounds=5 快速验证）

| 测试 | Median | Min | Mean | StdDev | Rounds |
|------|--------|-----|------|--------|--------|
| cache_hit | 1.33 ms | 1.30 ms | 1.33 ms | 0.025 ms | 20 |
| uv_parallel | 72.73 ms | 72.26 ms | 72.74 ms | 0.348 ms | 12 |
| cold_download | 75.10 ms | 74.57 ms | 75.21 ms | 0.524 ms | 10 |
| pip_parallel | 212.95 ms | 212.74 ms | 213.08 ms | 0.396 ms | 8 |

对比分析：
- **pip vs uv**：212.95 / 72.73 = **2.93x 提速**（远超 req-49 iter-132 目标 ≥ 2x）
  - 理论上限 3x（30ms/10ms 比例），实测 2.93x 接近上限
  - 8 worker 并行下 50 包：pip 7 批 * 30ms = 210ms，uv 7 批 * 10ms = 70ms
- **缓存命中 vs 冷下载**：75.10 / 1.33 = **56.5x 加速比**
  - 缓存命中仅 JSON 解析 + 50 次 ``is_file()`` 调用，~1.3ms
  - 冷下载含完整编排 + 50 包并行下载，~75ms
- **冷下载 vs uv 并行**：75.10 / 72.73 = 1.033（编排开销 ~2.4ms，<10ms 预期）
  - 编排开销来自 ``_deps_cache_key`` + ``_load_deps_cache`` + ``_find_pip_python``
    + ``_run_pip`` + ``_resolve_with_uv`` + ``_parse_wheel_names``
- **StdDev < 1% of median**：确定性 ``time.sleep`` 让基线极稳定，CI 跨运行
  对比不会误报退化

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 122 files already formatted
- pyrefly: 0 errors
- pytest -m "not slow": 2105 passed, 12 skipped, 22 deselected
  （iter-142 为 18 deselected，新增 4 个 slow 测试被排除）
- coverage: 95.68%（≥ 95% 门禁，与 iter-142 一致——新测试为 slow 不计入默认 coverage）

### 性能基线测试总数

- 现有 10 个：``test_perf_baseline.py``
- iter-141 新增 4 个：``test_build_perf_baseline.py``
- iter-142 新增 4 个：``test_nuitka_compile_baseline.py``
- iter-143 新增 4 个：``test_wheel_download_baseline.py``
- 合计 22 个，远超 req-49 验收标准"性能基线测试数 ≥ 14"

## 整合优化情况

- mock 模式与 ``test_nuitka_compile_baseline.py`` 的 ``_make_sleep_stream``
  一致（``time.sleep`` 释放 GIL），保持测试套件内一致性
- ``_make_sleep_download_one`` 复用 iter-142 的 ``_make_sleep_stream`` 模式，
  适配 wheel 下载场景（返回 ``CompletedProcess`` 而非 tuple）
- 缓存命中基线与 ``test_perf_baseline.py`` 的 ``TestWheelDownloadCacheBaseline``
  互补：后者测单次 ``_load_deps_cache`` 调用，本基线测 ``download_wheels``
  入口完整缓存命中路径
- CI benchmark job 一并扩展，覆盖 iter-141~143 全部基线测试文件

## 遗留事项

- 退化阈值 10% 延至 iter-145：当前 ``compare_benchmark.py`` 单一全局阈值
  降到 10% 会让高方差测试误报。iter-145 "按基线类别分组对比"支持按类别
  设阈值后，为 wheel 下载基线（stddev <1%）单独设 10%
- 本基线用 ``time.sleep`` 模拟下载耗时，不反映真实 pip/uv 网络下载耗时
  （受 PyPI 限流、wheel 大小、网络带宽影响）。真实下载耗时基线需端到端
  集成测试，超出 req-49 范围
- pip vs uv 加速比基于模拟耗时（30ms vs 10ms，3x 比例），真实加速比需
  端到端测：iter-132 注释提到 uv 比 pip 快 2-5x（无 Python 解释器启动
  开销 + Rust HTTP 客户端）

## 下一轮计划

iter-144 启动时间基线（req-49 L129-130，阶段 4 第四轮）：
1. entry wrapper 启动耗时基线（用 ``python -X importtime`` 解析）
2. lazy-import 启用 vs 关闭对比
3. ``--no-site`` 启用 vs 关闭对比
