# iter-134: AST 并行解析调优

## 需求清单

- [x] `_parse_parallel` chunksize 自适应算法优化（按文件大小加权，避免大文件扎堆）
- [x] `ProcessPoolExecutor` 改用 `initializer` 预加载 `_STDLIB` 集合，减少 worker 启动开销
- [x] 测试覆盖 chunksize 加权、initializer 预加载、大文件不扎堆、worker stdlib 分离
- [ ] 基线对比：500 文件场景提速 ≥15%（留 iter-142）

## 迭代目标

`_parse_parallel` 两项调优：(1) 新增 `_interleave_by_size` 按文件大小降序排序后
interleave 重排，使 `map(chunksize=)` 连续分块时每个 chunk 含大小文件混合，
避免大文件扎堆导致某 worker 成为瓶颈；(2) `ProcessPoolExecutor` 用
`initializer=_init_parse_worker` 在 worker 启动时预加载 `_STDLIB` 到 worker 全局
`_WORKER_STATE`，worker 内 `_parse_file_worker` 用预加载的 `_STDLIB` 分离标准库
导入，减少主进程分类循环工作量。

## 改动文件清单

- `src/fspack/analyzer.py`：
  - 新增 `_WORKER_STATE: dict[str, frozenset[str]]` 模块级 dict 容器（避免 `global` 语句，ruff PLW0603）
  - 新增 `_init_parse_worker(stdlib)` worker initializer
  - 新增 `_interleave_by_size(py_files, num_chunks)` 按大小 interleave 重排
  - `_parse_file_worker` 签名改为返回 `(non_stdlib_tops, stdlib_tops, subs)` 3 元组，用 `_WORKER_STATE["stdlib"]`（回退到模块级 `_STDLIB`）分离 stdlib
  - `_parse_serial` 签名加 `all_stdlib` 参数，用模块级 `_STDLIB` 分离
  - `_parse_parallel` 签名加 `all_stdlib` 参数，`ProcessPoolExecutor(initializer=_init_parse_worker, initargs=(_STDLIB,))`，调 `_interleave_by_size` 重排后传 `map`
  - `analyze_dependencies` 分类逻辑调整：`all_imports` 仅含 non_stdlib（local + third_party），`all_stdlib` 单独去重保序
- `tests/test_analyzer.py`：
  - 更新 `test_parse_file_worker_skips_syntax_error`/`test_parse_file_worker_normal` 为 3 元组解构
  - 新增 6 个测试：`test_interleave_by_size_distributes_large_files`/`preserves_all_files`/`empty_and_single_chunk`、`test_init_parse_worker_sets_stdlib`、`test_parse_file_worker_uses_worker_stdlib`/`falls_back_to_module_stdlib`
- `tests/test_nuitka.py`：
  - 更新 `test_parse_parallel_timeout_warns_on_slow_worker`/`normal_completes_without_timeout` 加 `all_stdlib` 参数，断言 `all_stdlib.count("os") == 5`
  - 新增 2 个测试：`test_parse_parallel_uses_initializer`（FakePool 捕获 initializer/initargs）、`test_parse_parallel_interleave_and_chunksize`（mock `_interleave_by_size` + 捕获 chunksize）

## 关键决策与依据

### interleave 重排而非 submit 自定义分块

`Executor.map(chunksize=N)` 连续切分输入列表，不支持自定义分块。改用 `submit`
手动分块会丢失 `map` 的顺序保证与 IPC 调度优化，且需重构 worker 签名为多文件。

采用 **预排序 + 连续分块**：`_interleave_by_size` 按文件大小降序排序后，按
`sized[i::num_chunks]` 形成 `num_chunks` 组并拼接。每组含大、中、小文件混合
（第 0 组含最大、第 num_chunks 大、第 2*num_chunks 大...），使 `map(chunksize=len//num_chunks)`
连续切分时每个 chunk 的总工作量大致均衡。实现简单，保持 `map` 接口与顺序保证。

### dict 容器 `_WORKER_STATE` 而非 global 语句

`initializer` 需在 worker 进程设置模块级变量。Python `global` 语句是标准做法，
但 ruff PLW0603 默认报错。改用 `_WORKER_STATE: dict[str, frozenset[str]] = {"stdlib": frozenset()}`
dict 容器，initializer 改 `_WORKER_STATE["stdlib"] = stdlib`（无需 global），
worker 读 `_WORKER_STATE["stdlib"]`。dict 是 mutable，赋值键值无需 global 语句。

### worker 内 stdlib 分离

`_parse_file_worker` 用预加载的 `_STDLIB` 将顶层导入分离为 `(non_stdlib_tops, stdlib_tops)`，
减少主进程分类循环工作量（`for imp in all_imports` 从 N 降到 N - num_stdlib，
仅需区分 local vs third_party）并减少 IPC 数据量（stdlib/non_stdlib 分开传输）。
`_WORKER_STATE["stdlib"]` 为空时回退到模块级 `_STDLIB`，保证主进程直接调用
（如单元测试）也能正确分离。

### initializer 预加载的收益边界

worker 启动时已通过 spawn import `fspack.analyzer`（连带加载 `analyzer_ast`，
构建 `STDLIB_FALLBACK`），initializer 在此之后执行。initializer 的主要收益：
(1) 将主进程已构建的 `_STDLIB` 显式传递给 worker，确保与主进程分类一致；
(2) worker 第一次 `_parse_file_worker` 调用时 `_WORKER_STATE["stdlib"]` 已就绪，
无需模块属性查找。无法减少 spawn 进程创建与模块 import 开销（需将 worker 函数
移到独立轻量模块才能避免，超出本轮范围）。

## 代码实现情况

### _interleave_by_size 加权重排

```python
def _interleave_by_size(py_files: list[Path], num_chunks: int) -> list[Path]:
    if num_chunks <= 1 or len(py_files) <= 1:
        return list(py_files)
    sized = sorted(
        py_files,
        key=lambda p: p.stat().st_size if p.exists() else 0,
        reverse=True,
    )
    interleaved: list[Path] = []
    for i in range(num_chunks):
        interleaved.extend(sized[i::num_chunks])
    return interleaved
```

### _parse_parallel initializer + interleave

```python
cpu_count = os.cpu_count() or 4
num_chunks = cpu_count * 4
chunksize = max(1, len(py_files) // num_chunks)
interleaved = _interleave_by_size(py_files, num_chunks)
with ProcessPoolExecutor(
    max_workers=cpu_count,
    initializer=_init_parse_worker,
    initargs=(_STDLIB,),
) as pool:
    results = pool.map(_parse_file_worker, [str(p) for p in interleaved],
                       chunksize=chunksize, timeout=_PARSE_TOTAL_TIMEOUT)
    for non_stdlib_tops, stdlib_tops, subs in results:
        all_imports.extend(non_stdlib_tops)
        all_stdlib.extend(stdlib_tops)
        ...
```

### worker stdlib 分离 + 回退

```python
tops, subs = collect_imports_and_submodules(tree)
stdlib_ref = _WORKER_STATE["stdlib"] or _STDLIB
stdlib_tops = [t for t in tops if t in stdlib_ref]
non_stdlib_tops = [t for t in tops if t not in stdlib_ref]
return non_stdlib_tops, stdlib_tops, subs
```

## 测试验证结果

### 新增测试（8 个）

- `test_interleave_by_size_distributes_large_files`：8 文件（4 大 4 小），num_chunks=4，
  验证每个 chunk 含至少一个大文件（不扎堆）
- `test_interleave_by_size_preserves_all_files`：10 文件重排后集合不变
- `test_interleave_by_size_empty_and_single_chunk`：空列表返回空，num_chunks=1 原序
- `test_init_parse_worker_sets_stdlib`：initializer 设置 `_WORKER_STATE["stdlib"]`
- `test_parse_file_worker_uses_worker_stdlib`：自定义 `_WORKER_STATE["stdlib"]` 后分离正确
- `test_parse_file_worker_falls_back_to_module_stdlib`：`_WORKER_STATE["stdlib"]` 为空时回退到模块级 `_STDLIB`
- `test_parse_parallel_uses_initializer`：FakePool 捕获 `initializer`/`initargs`，验证传递 `_STDLIB`
- `test_parse_parallel_interleave_and_chunksize`：mock `_interleave_by_size` + 捕获 `map` chunksize

### 更新测试（4 个）

- `test_parse_file_worker_skips_syntax_error`/`test_parse_file_worker_normal`：3 元组解构
- `test_parse_parallel_timeout_warns_on_slow_worker`/`normal_completes_without_timeout`：加 `all_stdlib` 参数，断言 `all_stdlib.count("os") == 5`

### 门禁结果

- ruff check: All checks passed!
- ruff format: 3 files already formatted
- pyrefly: 0 errors
- pytest: 2030 passed, 12 skipped（iter-133 为 2022 passed，新增 8 个测试）
- coverage: 95.68%（>= 95% 门禁，iter-133 为 95.66%，+0.02%），`analyzer.py` 100%
- 10 benchmarks: 全通过

## 整合优化情况

- `_parse_serial` 与 `_parse_parallel` 统一做 stdlib 分离，`analyze_dependencies`
  分类逻辑简化（`all_imports` 仅区分 local vs third_party，`all_stdlib` 单独去重）
- `_parse_file_worker` 回退到模块级 `_STDLIB`，主进程直接调用与 worker 路径行为一致
- `_interleave_by_size` 对 `num_chunks<=1` 或 `len<=1` 短路返回原序，避免无谓排序

## 遗留事项

- 500 文件基线对比（提速 ≥15%）留 iter-142（性能基线守护）
- `_interleave_by_size` 的 `Path.stat` I/O 开销（500 文件 ~10-50ms）相对于 AST 解析
  （数秒）可接受；若需优化可用 `os.scandir` 复用 DirEntry stat 缓存
- initializer 无法减少 spawn 进程创建与模块 import 开销；进一步优化需将 worker
  函数移到独立轻量模块（避免 import `fspack.analyzer` 连带加载），超出本轮范围

## 下一轮计划

iter-135 冷启动 import 终极惰性化：
1. `pipeline/__init__.py` 顶部 `fspack.console` 移至函数内（解决 project_memory 遗留 ~17ms）
2. `stages.py` 顶部 `BuildTracker` 类型注解改用字符串前向引用，`progress` 导入移至 `build()` 内（解决 ~8ms）
3. `wheels/downloader.py` 顶部 `threading` 移至方法内
4. 守护测试扩展：`test_pipeline_no_top_level_console_import`
