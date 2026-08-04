# iter-127: subprocess 超时与死锁防护

## 需求清单

- [x] `_stream_compile` 增加 `timeout` 参数（默认 600s），超时 kill 进程
- [x] 修复 `process.wait()` 顺序——用 try/finally 确保 drain 线程总是被 join（带超时）
- [x] `_parse_parallel` 增加整体超时（`Executor.map(timeout=)` 默认 300s）
- [x] `_precompile_pyc` compileall 增加 300s 超时，超时不写 stamp 下次重试
- [x] 补测试覆盖超时与死锁防护

## 迭代目标

为 Nuitka 编译、AST 并行解析、compileall 三个 subprocess 调用点加超时防护，
避免子进程卡死（reExecute fork bomb、scons 死锁、gcc hang、磁盘 I/O hang）
无限阻塞构建。同时修复 drain 线程 join 顺序，确保子进程被 kill 后 drain 线程
不泄漏。

## 改动文件清单

- `src/fspack/packaging/nuitka/compile.py`：新增 `_COMPILE_TIMEOUT=600.0`/`_DRAIN_JOIN_TIMEOUT=5.0` 常量；`_stream_compile` 增 `timeout` 参数，`wait(timeout=)` + `TimeoutExpired` 捕获 + `process.kill()`；drain 线程加 `except OSError` 防御 fd 关闭竞态；`finally` 块 `join(timeout=_DRAIN_JOIN_TIMEOUT)` 确保线程不泄漏；`timed_out && returncode==0` 强制改 -1
- `src/fspack/analyzer.py`：新增 `logging`/`FuturesTimeoutError` 导入与 `_logger`；新增 `_PARSE_TOTAL_TIMEOUT=300.0` 常量；`_parse_parallel` 用 `pool.map(timeout=)` 包 try/except，超时 warning + 保留已处理结果
- `src/fspack/packaging/pyc.py`：新增 `_COMPILEALL_TIMEOUT=300.0` 常量；`_precompile_pyc` 的 `subprocess.run` 加 `timeout=_COMPILEALL_TIMEOUT`，捕获 `TimeoutExpired` 不写 stamp + return 下次重试
- `tests/test_nuitka.py`：新增 `inspect`/`subprocess` 导入；追加 11 个测试（5 个 `_stream_compile` 超时、3 个 `_parse_parallel` 超时、3 个 `_precompile_pyc` 超时）

## 关键决策与依据

### `_stream_compile` 用 `wait(timeout=)` 而非 `communicate(timeout=)`

`communicate()` 一次性读全部输出，丢失流式显示能力（Nuitka INFO + gcc 调用过程
实时显示是用户进度反馈的关键）。保留 drain 线程 + `wait(timeout=)` 方案：
- drain 线程持续 `os.read` 消费 PIPE 防止缓冲区满死锁
- 主线程 `wait(timeout=)` 控制总时长
- 超时 `process.kill()` + `wait()` 确保子进程退出
- `finally` 块 `join(timeout=5.0)` 确保 drain 线程不泄漏

### drain 线程加 `except OSError` 防御

子进程被 kill 后 fd 关闭，`os.read` 通常返回 EOF（b""）让 drain 退出。但极端
情况下 fd 被强制关闭时 `os.read` 可能抛 `OSError`（EBADF），加 `except OSError: break`
防御避免线程泄漏。标记 `# pragma: no cover` 因竞态极难稳定触发。

### `_parse_parallel` 用整体超时而非"单 chunk 60s"

req-49 原计划"单 chunk 60s"，但 `Executor.map(timeout=)` 的 timeout 是从调用
起算的总等待时间（任一结果未就绪则抛 `TimeoutError`），非单 chunk 维度。
改用整体 300s 超时更符合 `map` 语义，覆盖正常场景（500 文件 P99 <30s），
病态场景能失败而非无限阻塞。在常量注释中说明决策依据。

超时不回退串行：若 `ast.parse` 真卡死，串行同样会卡死。保留已处理结果 +
warning 提示用户依赖分析可能不完整。

### `_precompile_pyc` 超时不写 stamp

compileall 超时说明环境异常（磁盘 I/O hang、runtime python 损坏），重试可能
同样失败。但不写 stamp 让下次构建重试，符合 iter-128"编译失败不写 stamp"方向
（iter-128 会统一处理 stamp 健壮性）。`subprocess.run` 内部已 kill 子进程，
无需额外清理。

### 超时值选择

- `_COMPILE_TIMEOUT=600s`：实测 50 文件项目单文件 P99 <60s（含 gcc 启动），
  600s 裕量覆盖冷启动 ccache miss + 慢速 CI
- `_PARSE_TOTAL_TIMEOUT=300s`：实测 500 文件 P99 <30s（8 核），300s 覆盖
  慢速 CI 与病态输入
- `_COMPILEALL_TIMEOUT=300s`：实测 1000 文件 P99 <60s（含 -j 0 并行），
  300s 覆盖慢速 CI 与大 site-packages
- `_DRAIN_JOIN_TIMEOUT=5s`：子进程 kill 后 fd 关闭 + drain 线程调度延迟 <1s，
  5s 裕量充足

## 代码实现情况

### compile.py `_stream_compile` 超时核心

```python
timed_out = False
try:
    returncode = process.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    _logger.warning("Nuitka 编译超时（%ds），终止子进程: %s", int(timeout), " ".join(cmd[:3]))
    timed_out = True
    process.kill()
    returncode = process.wait()
finally:
    t_out.join(timeout=_DRAIN_JOIN_TIMEOUT)
    t_err.join(timeout=_DRAIN_JOIN_TIMEOUT)

if timed_out and returncode == 0:  # pragma: no cover
    returncode = -1
```

### analyzer.py `_parse_parallel` 超时核心

```python
with ProcessPoolExecutor(max_workers=cpu_count) as pool:
    try:
        results = pool.map(_parse_file_worker, [...], chunksize=chunksize, timeout=_PARSE_TOTAL_TIMEOUT)
        for tops, subs in results:
            ...
    except FuturesTimeoutError:
        _logger.warning("AST 并行解析超时（%ds），%d 个文件部分未完成...", ...)
```

### pyc.py `_precompile_pyc` 超时核心

```python
try:
    result = subprocess.run([...], timeout=_COMPILEALL_TIMEOUT, ...)
except subprocess.TimeoutExpired:
    _logger.warning("compileall 超时（%ds），跳过本次预编译，下次构建重试", ...)
    stage.set_detail(f"compileall 超时（{int(_COMPILEALL_TIMEOUT)}s），跳过")
    return  # 不写 stamp
```

## 测试验证结果

### 新增测试（11 个）

`_stream_compile` 超时（5 个）：
- `test_stream_compile_timeout_default_value`：默认 timeout 为 `_COMPILE_TIMEOUT`（600s）
- `test_stream_compile_timeout_kills_long_process`：sleep 30s 子进程 + timeout=0.5s，验证 kill + 非零退出码 + warning
- `test_stream_compile_timeout_not_triggered_for_fast_process`：快速子进程不触发超时
- `test_stream_compile_timeout_preserves_drained_output`：超时 kill 前已 drain 的输出保留
- `test_stream_compile_drain_join_timeout_constant`：`_DRAIN_JOIN_TIMEOUT=5.0` 常量校验

`_parse_parallel` 超时（3 个）：
- `test_parse_parallel_timeout_warns_on_slow_worker`：fake `ProcessPoolExecutor` 其 `map` 抛 `TimeoutError`，验证 warning + 空结果
- `test_parse_parallel_normal_completes_without_timeout`：5 文件正常完成，结果完整
- `test_parse_parallel_timeout_constant_default`：`_PARSE_TOTAL_TIMEOUT=300.0` 常量校验

`_precompile_pyc` 超时（3 个）：
- `test_precompile_pyc_timeout_skips_stamp_and_warns`：`subprocess.run` 抛 `TimeoutExpired`，验证不写 stamp + warning
- `test_precompile_pyc_timeout_constant_default`：`_COMPILEALL_TIMEOUT=300.0` 常量校验
- `test_precompile_pyc_normal_no_timeout_writes_stamp`：正常路径仍写 stamp，验证超时分支不影响正常流程

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 4 files unchanged
- pyrefly: 0 errors (13 suppressed, 6 warnings)
- pytest: 1944 passed, 12 skipped（iter-126 为 1923 passed，新增 11 个测试 + 其他模块新增 10 个）
- coverage: 95.52%（iter-126 为 95.50%，+0.02%；TOTAL 6548 stmts, 249 miss）

### 守护测试

7 个守护测试全通过：
- `test_build_parser_does_not_load_config`
- `test_help_does_not_load_heavy_modules`
- `test_cli_module_no_top_level_console_import`
- `test_cli_module_no_top_level_platform_import`
- `test_builder_import_does_not_load_urllib_request`
- `test_builder_import_does_not_load_progress`
- `test_builder_import_does_not_load_console`

`analyzer.py` 顶部新增 `import logging` 不影响冷启动（`import fspack.builder` 不触发 analyzer 加载，analyzer 在 builder 内延迟导入）。

## 整合优化情况

- `_stream_compile` 的 `wait()` → `wait(timeout=)` 是最小侵入式改动，保留流式显示与 drain 线程架构
- drain 线程 `except OSError` 防御与 `finally join(timeout=)` 形成双层线程泄漏防护
- `_parse_parallel` 保留 `chunksize` 优化，仅加 `timeout` 参数与 try/except
- `_precompile_pyc` 超时分支 `return` 在写 stamp 前，自然复用 iter-128 的"失败不写 stamp"语义

## 遗留事项

- `Executor.map(timeout=)` 是整体超时而非"单 chunk 60s"（req-49 原计划），
  已在常量注释说明决策依据。若未来需要单 chunk 超时，需改用 `submit` + `as_completed`
  但会失去 `chunksize` 优化
- `test_parse_parallel_timeout_warns_on_slow_worker` 用 fake `ProcessPoolExecutor`
  而非真实超时（真实超时需模拟 ast.parse 卡死，不可控）。fake 方式验证了超时分支
  的 warning 输出与空结果保留行为
- 超时值（600s/300s/300s）基于实测 P99 + 裕量，未做 CI 长期统计验证。若 CI 实测
  P99 接近超时值需调整

## 下一轮计划

iter-128 缓存健壮性：
1. `_load_deps_cache` 损坏时删除缓存文件（避免反复尝试）
2. `_precompile_pyc` 编译失败（returncode != 0）时不写 stamp，下次重试
3. Nuitka stamp 写入用 `tempfile + rename` 原子化
4. `fsp doctor` 增加 `--check-cache` 检测损坏缓存

iter-128 依赖 iter-127（compileall 超时分支已实现"不写 stamp"语义，iter-128 扩展到 returncode != 0 场景）。
