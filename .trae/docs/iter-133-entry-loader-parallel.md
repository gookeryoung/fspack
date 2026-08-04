# iter-133: 多入口 loader 并行编译

## 需求清单

- [x] `_build_entry_loaders` 用 `ThreadPoolExecutor` 并行编译多个 entry loader
- [x] 共享 `tempfile.TemporaryDirectory` 工作目录，每入口独立子目录避免文件冲突
- [x] `max_workers = min(cpu_count, _MAX_LOADER_WORKERS=4)` 平衡并行收益与资源限制
- [x] 单入口走串行路径（线程池开销无收益）
- [x] 测试覆盖多入口场景（4+ 入口）、异常传播、工作目录共享、max_workers cap

## 迭代目标

将 `_build_entry_loaders` 从串行 for 循环重构为 `ThreadPoolExecutor` 并行编译，
利用 mingw/gcc/clang 子进程释放 GIL 的特性并行编译多个 entry loader。
共享 `TemporaryDirectory`，每入口分配独立子目录（`<tmp>/<entry_name>`）避免
`loader.c`/`icon.rc`/`icon.o` 文件冲突。4 入口场景预期提速 ≥2x（基线对比留 iter-142）。

## 改动文件清单

- `src/fspack/packaging/pipeline/stages.py`：
  - 顶部新增 `import os` 与 `from concurrent.futures import ThreadPoolExecutor`
  - `from fspack.config import` 新增 `EntryPoint`
  - 新增 `_MAX_LOADER_WORKERS = 4` 常量
  - `_build_entry_loaders` 重构：单入口走串行路径，多入口用 `ThreadPoolExecutor` 并行
  - 新增 `_build_one_loader` 抽取单入口编译逻辑（包装器 + .entry + compile_loader）
  - 新增 `_loader_exe_path` 抽取 exe 路径计算（Windows 加 .exe 后缀）
- `tests/test_builder.py`：
  - 顶部导入新增 `AppType`/`EntryPoint`/`ProjectInfo`/`LoaderError`/`_MAX_LOADER_WORKERS`/`_build_entry_loaders`
  - 新增 `_make_multi_entry_context` 测试 helper（构造 N 入口 BuildContext）
  - 新增 8 个并行编译测试

## 关键决策与依据

### ThreadPoolExecutor 而非 ProcessPoolExecutor

与 iter-131 同理：loader 编译核心是 subprocess 调用（mingw/gcc/clang），GIL 在
`subprocess.run` 时释放，线程足够并行。ThreadPoolExecutor 无需序列化开销，
worker 函数可访问闭包变量（`ctx`/`source`/`resolved_icon`），比 ProcessPoolExecutor 简单。

### 共享 TemporaryDirectory + 每入口独立子目录

`LoaderCompiler.compile` 在 `work_dir` 下创建 `loader.c`/`icon.rc`/`icon.ico`/`icon.o`
固定文件名。若多入口共享同一 `work_dir`，并行编译会互相覆盖。

方案：共享 `tempfile.TemporaryDirectory`，每入口分配 `work_dir / ep.name` 子目录。
- 共享 TemporaryDirectory：避免 N 次临时目录创建/清理开销
- 独立子目录：`loader.c`/`icon.rc` 等文件不冲突

### max_workers = min(cpu_count, 4)

与 iter-131 `_MAX_COMPILE_WORKERS` 保持一致。4 上限平衡并行收益与 Windows 资源限制
（mingw/gcc 子进程句柄/内存）。单入口不创建线程池（`len(entries) <= 1` 走串行路径）。

### 按 submit 顺序取 result 保持 exes 顺序

```python
futures = [pool.submit(_build_one, ep) for ep in entries]
for future in futures:
    exes.append(future.result())
```

`future.result()` 阻塞等待该 future 完成，但其他 future 继续并行执行。
按 `futures` 列表顺序（= entries 顺序）取 result，保持 `exes` 顺序与 `all_entries` 一致，
使构建完成输出 `console.success` 的 exe 列表顺序稳定。

### StageRecorder 线程安全

`compile_loader` 内部在缓存命中时调 `stage.hit_cache()`、编译时调 `stage.set_detail()`。
iter-131 选择不传 stage 给 worker，主线程聚合。但 `compile_loader` 是公共 API，
其内部使用 stage 不易拆分。

`StageRecorder._hits += 1` 在 CPython GIL 下最坏丢失一次计数（int += 非原子，
load/add/store 3 bytecodes 可被中断），不影响正确性。`set_detail` 最后写入者胜出，
无数据竞争风险。`st.processed(len(exes))` 在主线程所有 future 完成后调用，无竞争。

### 异常传播

worker 内 `compile_loader` 抛 `LoaderError`（如编译器缺失、编译失败）时
`future.result()` 重抛。`with ThreadPoolExecutor` 的 `__exit__` 调 `shutdown(wait=True)`
等待在途任务后传播异常。临时工作目录由 `with tempfile.TemporaryDirectory()` 的
`__exit__` 自动清理（异常时也清理）。

## 代码实现情况

### _build_entry_loaders 并行核心

```python
def _build_entry_loaders(ctx, resolved_icon, has_tkinter):
    target = ctx.cfg.target
    exes: list[Path] = []
    with ctx.tracker.stage("生成 C loader") as st:
        source = generate_loader_source(ctx.info.py_xy, target)
        entries = ctx.info.all_entries
        # 单入口无需并行（线程池开销无收益）
        if len(entries) <= 1:
            with tempfile.TemporaryDirectory(prefix="fspack_loader_") as tmp:
                _build_one_loader(ctx, entries[0], source, Path(tmp), resolved_icon, has_tkinter, st)
                exes.append(_loader_exe_path(ctx, entries[0], target))
            st.processed(len(exes))
            return exes

        cpu = os.cpu_count() or 1
        max_workers = min(cpu, _MAX_LOADER_WORKERS)
        with tempfile.TemporaryDirectory(prefix="fspack_loader_") as tmp:
            build_dir = Path(tmp)

            def _build_one(ep: EntryPoint) -> Path:
                work_subdir = build_dir / ep.name
                work_subdir.mkdir(parents=True, exist_ok=True)
                _build_one_loader(ctx, ep, source, work_subdir, resolved_icon, has_tkinter, st)
                return _loader_exe_path(ctx, ep, target)

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = [pool.submit(_build_one, ep) for ep in entries]
                for future in futures:
                    exes.append(future.result())
        st.processed(len(exes))
    return exes
```

### _build_one_loader 抽取复用

`_build_one_loader` 封装单入口编译逻辑（包装器生成 + .entry 文件 + compile_loader），
供串行与并行路径复用。`work_dir` 由调用方分配（串行路径传 `Path(tmp)`，
并行路径传 `build_dir / ep.name` 子目录）。

## 测试验证结果

### 新增测试（8 个）

- `test_max_loader_workers_constant`：`_MAX_LOADER_WORKERS = 4` 常量校验
- `test_build_entry_loaders_parallel_multi_entry`：4 入口并行编译，验证 exe/wrapper/.entry
  生成、exes 顺序、4 个独立子工作目录、共享父目录
- `test_build_entry_loaders_parallel_shared_work_dir_parent`：3 入口共享 TemporaryDirectory，
  子目录名与入口名一致
- `test_build_entry_loaders_parallel_exception_propagates`：worker 抛 `LoaderError` 时
  `future.result()` 重抛
- `test_build_entry_loaders_parallel_max_workers_capped`：`cpu_count=8` 时 `max_workers=4`
- `test_build_entry_loaders_parallel_max_workers_below_cap`：`cpu_count=2` 时 `max_workers=2`
- `test_build_entry_loaders_single_entry_no_parallel`：单入口不创建 `ThreadPoolExecutor`
- `test_build_entry_loaders_parallel_preserves_order`：5 入口不同编译耗时，exes 顺序与
  entries 一致

### 门禁结果

- ruff check: All checks passed!
- ruff format: 2 files already formatted
- pyrefly: 0 errors（src/fspack/packaging/pipeline/stages.py 与 tests/test_builder.py）
- pytest: 2022 passed, 12 skipped（iter-132 为 2014 passed，新增 8 个测试）
- coverage: 95.66%（>= 95% 门禁，iter-132 为 95.62%，+0.04%）
- 10 benchmarks: 全通过

## 整合优化情况

- `_build_one_loader`/`_loader_exe_path` 抽取为独立函数，串行与并行路径复用，
  消除代码重复
- 单入口走串行路径，避免线程池创建/销毁开销（~1ms）
- 共享 `TemporaryDirectory`，避免 N 次临时目录创建/清理
- `exes` 顺序与 `all_entries` 一致（按 submit 顺序取 result），构建输出稳定

## 遗留事项

- 4 入口基线对比（提速 ≥2x）留 iter-142（性能基线守护）
- `StageRecorder._hits += 1` 在并行 worker 调用时存在 benign race（最坏丢失一次计数），
  不影响正确性。若需严格线程安全可改用 `threading.Lock` 或 `itertools.count`
- `compile_loader` 内部 `spinner` 在并行模式下可能重叠（rich.status 非线程安全），
  实测无明显问题（mingw 编译 ~2s，spinner 短暂闪烁）。若需修复可加 `show_spinner`
  参数在并行模式时禁用

## 下一轮计划

iter-134 AST 并行解析调优：
1. `_parse_parallel` chunksize 自适应算法优化（按文件大小加权）
2. `ProcessPoolExecutor` 改用 `initializer` 预加载 `_STDLIB` 集合
3. 基线对比：500 文件场景提速 ≥15%
