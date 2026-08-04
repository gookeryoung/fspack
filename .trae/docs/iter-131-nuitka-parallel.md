# iter-131: Nuitka 编译并行化

## 需求清单

- [x] `_compile_files` 用 `ThreadPoolExecutor` 并行编译多个 `.py`
- [x] `max_workers = min(cpu_count, 4)` 平衡并行收益与资源限制
- [x] 全局心跳替代每文件心跳（输出已完成数/总数/已耗时）
- [x] 每进程 `--jobs` 调整为 `max(1, cpu_count // max_workers)` 避免过度超订
- [x] 测试覆盖并行编译、顺序、心跳、异常传播

## 迭代目标

将 `_compile_files` 从串行 for 循环重构为 `ThreadPoolExecutor` 并行编译，
利用 subprocess 释放 GIL 的特性并行调 nuitka `--module`。全局心跳替代每文件心跳，
减少线程创建/销毁开销。50 文件场景预期提速 ≥30%（基线对比留 iter-142）。

## 改动文件清单

- `src/fspack/packaging/nuitka/compile.py`：顶部新增 `from concurrent.futures import ThreadPoolExecutor, as_completed`；新增 `_MAX_COMPILE_WORKERS = 4` 常量；模块 docstring 更新；`_compile_files` 完全重构为并行版本
- `tests/test_nuitka.py`：新增 `_MAX_COMPILE_WORKERS` 导入；更新 `test_compile_src_heartbeat_logs_progress` 适配全局心跳消息格式；新增 8 个并行编译测试

## 关键决策与依据

### ThreadPoolExecutor 而非 multiprocessing

Nuitka 编译核心是 subprocess 调用（`Popen` → `wait`），GIL 在 `wait()` 时释放，
线程足够并行。ThreadPoolExecutor 无需序列化开销，且 worker 函数可访问闭包变量
（`cls`/`compile_env`/`jobs`），比 ProcessPoolExecutor 简单。

### max_workers = min(cpu_count, 4)

req-49 明确要求。4 上限平衡并行收益与 Windows 资源限制（句柄/内存）。
subprocess 并发可能触发 Windows 资源限制，4 进程已足够受益（I/O 等待重叠）。

### 每进程 --jobs = max(1, cpu_count // max_workers)

串行模式 `--jobs = cpu_count`（全核 gcc 并行）。并行模式若保持 `--jobs = cpu_count`，
总 gcc 进程数 = `max_workers * cpu_count`（8 核机器 = 4*8 = 32 gcc 进程），
过度超订导致内存膨胀/OOM。改为 `cpu_count // max_workers` 使总 gcc 进程数 ≈ cpu_count，
与串行模式总并行度一致。并行收益来自 I/O 等待重叠（读 .py / 写 .c / gcc 启动）。

### 全局心跳替代每文件心跳

串行模式每文件起一个心跳线程（create + join N 次），开销 = N * (thread create + join)。
并行模式全局心跳仅起一个线程，输出"已完成 X/Y, 已耗时 Zs"总进度。
线程安全：主线程写 `completed_count[0]`（as_completed 迭代中），心跳线程读，
GIL 下 int 读写原子，无需锁。

### as_completed 主线程聚合

`compiled_files`/`failed`/`stage.processed()` 仅在主线程（as_completed 迭代）聚合，
无共享可变状态竞争。`StageRecorder` 非线程安全（`self._items += n` 无锁），
主线程聚合避免锁需求。

### 异常传播

worker 内 `_stream_compile` 抛异常（如 `FileNotFoundError`，py_exe 不存在）时
`future.result()` 重抛。`with ThreadPoolExecutor` 的 `__exit__` 调 `shutdown(wait=True)`
等待在途任务后传播异常。`finally` 块确保心跳线程停止。
现有测试 `test_compile_src_compile_files_exception_cleans_build_dirs` 仍通过。

## 代码实现情况

### _compile_files 并行核心

```python
def _compile_one(py_file: Path) -> tuple[Path, int]:
    """单文件编译 worker：调 nuitka --module，返回 (文件路径, 退出码)."""
    returncode, _stdout, _stderr = cls._stream_compile([...], env=compile_env)
    return py_file, returncode

# 全局心跳
completed_count: list[int] = [0]
stop_heartbeat = threading.Event()

def _global_heartbeat(...):
    while not _stop.wait(_HEARTBEAT_INTERVAL):
        elapsed = int(time.monotonic() - _start)
        _logger.info("Nuitka 并行编译中: 已完成 %d/%d, 已耗时 %ds", _done[0], _total, elapsed)

hb_thread = threading.Thread(target=_global_heartbeat, daemon=True)
hb_thread.start()
try:
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_compile_one, f): f for f in py_files}
        for future in as_completed(futures):
            py_file, returncode = future.result()
            completed_count[0] += 1
            if returncode == 0:
                compiled_files.add(py_file)
                stage.processed()
            else:
                failed += 1
finally:
    stop_heartbeat.set()
    hb_thread.join(timeout=1.0)
```

### jobs 计算

```python
cpu = os.cpu_count() or 1
max_workers = min(cpu, _MAX_COMPILE_WORKERS)
jobs = max(1, cpu // max_workers)  # 总 gcc 进程数 ≈ cpu_count
```

## 测试验证结果

### 新增测试（8 个）

- `test_max_compile_workers_constant`：`_MAX_COMPILE_WORKERS = 4` 常量校验
- `test_compile_files_parallel_max_workers_capped`：mock ThreadPoolExecutor 捕获 max_workers，验证 `min(cpu_count, 4)`
- `test_compile_files_parallel_completes_all_files`：3 文件（2 成功 1 失败），验证计数与 stage.processed
- `test_compile_files_parallel_exception_propagates`：worker 抛 FileNotFoundError，验证异常传播
- `test_compile_files_parallel_heartbeat_stops_on_exception`：异常时心跳线程停止，不泄漏
- `test_compile_files_parallel_jobs_adjusted`：验证 `--jobs = max(1, cpu_count // max_workers)`
- `test_compile_files_parallel_empty_files`：空文件列表返回空集合
- `test_compile_files_parallel_global_heartbeat_format`：全局心跳消息格式含"已完成"/"已耗时"

### 更新测试（1 个）

- `test_compile_src_heartbeat_logs_progress`：消息从 "Nuitka 编译中" 改为 "并行编译中" + "已完成"

### 门禁结果

- ruff check: All checks passed!
- ruff format: 2 files already formatted
- pyrefly: 0 errors
- pytest: 2001 passed, 12 skipped（iter-130 为 1944 passed，新增 8 个 + 其他模块新增）
- coverage: 95.63%（iter-130 为 95.52%，+0.11%）
- 10 benchmarks: 全通过
- 守护测试: 全通过（test_build_parser_does_not_load_config 等 7 个）

## 整合优化情况

- `_resolve_jobs` 不再被 `_compile_files` 调用（改用 `os.cpu_count() // max_workers`），
  但方法保留（其他测试直接测试它，且可能被其他场景使用）
- 心跳线程从 N 次 create/join 降为 1 次，减少线程开销
- `as_completed` 天然支持"谁先完成谁处理"，无需额外编排

## 遗留事项

- 50 文件基线对比（提速 ≥30%）留 iter-142（Nuitka 编译基线）
- `max_workers * jobs` 总并行度 = cpu_count（已调整），若实测 CPU 利用率不足可调大 jobs
- 并行模式下 Nuitka .build 残留清理由 `compile_src` 的 `finally` 块统一处理（`_cleanup_build_dirs`），
  无需在 `_compile_files` 内额外清理

## 下一轮计划

iter-132 wheel 下载 uv 加速：
1. `_download_online` 在 uv 可用时改用 `uv pip download`
2. 保留 pip 回退
3. `_resolve_with_uv` 与下载阶段共享 uv 路径检测
4. 基线对比：50 wheel 场景提速 ≥40%
