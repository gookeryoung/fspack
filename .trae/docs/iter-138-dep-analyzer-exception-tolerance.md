# iter-138: 依赖分析异常容错

## 需求清单

- [x] `_parse_file_worker` 单文件 ast.parse 失败记录到报告（`ast_errors` 字段），不静默跳过
- [x] `_parse_parallel` 单个 worker 超时不阻塞其他 worker（`submit` + `as_completed` + timeout）
- [x] QML 解析失败不影响主流程（已有，补测试）

## 迭代目标

补齐 req-49 L108-110 列出的依赖分析异常容错三项任务：
(1) `_parse_file_worker` 与 `_parse_serial` 不再静默跳过 AST 解析失败，记录
`(abs_path, error_msg)` 元组，主进程格式化为 `"<相对路径>: <错误信息>"` 写入
`DependencyReport.ast_errors` 字段；
(2) `_parse_parallel` 从 `pool.map(timeout=)` 改为 `submit` + `as_completed(timeout=)`，
单个 worker 卡死时不阻塞其他已完成 worker 的结果聚合（`map` 按提交顺序迭代，
首个 future 卡死会丢弃后续已完成结果）；
(3) `analyze_dependencies` 的 QML 循环加防御性 try/except，`parse_qml_imports`
内部已 catch OSError，但循环外加一层兜底其他异常场景。

## 改动文件清单

- `src/fspack/config/models.py`：
  - `DependencyReport` 新增 `ast_errors: tuple[str, ...] = ()` 字段（frozen dataclass
    兼容，默认空 tuple 不破坏既有构造调用）
- `src/fspack/analyzer.py`：
  - 顶部 import 增加 `as_completed`
  - 新增 `_format_ast_errors(src_dir, errors)` 辅助函数：将 `(abs_path, error_msg)`
    元组列表格式化为 `"<相对 src_dir 的 POSIX 路径>: <错误信息>"`，路径转换失败
    （不同盘符）回退到绝对路径
  - `_parse_file_worker` 返回类型从 3 元组改为 4 元组
    `tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]`，
    第 4 个元素为 ast_errors；`except (SyntaxError, OSError)` 分支返回
    `([], [], {}, [(py, str(e))])` 而非 `([], [], {})`
  - `_parse_serial` 签名增加 `all_errors: list[tuple[str, str]]` 参数，AST 解析失败
    记录到 `all_errors` 而非 `continue` 静默跳过
  - `_parse_parallel` 签名增加 `all_errors` 参数；从 `pool.map(timeout=)` 改为
    `pool.submit` + `as_completed(timeout=_PARSE_TOTAL_TIMEOUT)`；超时后未完成的
    future 被 `cancel()`（已运行的无法取消，`with` 块退出时 `shutdown` 等待）；
    warning 日志输出未完成数 `"<pending>/<total> 个文件未完成"`
  - `analyze_dependencies` 增加 `all_errors: list[tuple[str, str]]` 列表传给两个
    解析函数；QML 循环加 `try/except OSError` 防御；末尾调 `_format_ast_errors`
    写入 `DependencyReport.ast_errors`
  - `_PARSE_TOTAL_TIMEOUT` 注释更新：说明 iter-138 改用 `as_completed` 的原因
- `tests/test_analyzer.py`：
  - 4 处 worker 返回值解包从 3 元组改为 4 元组（`test_parse_file_worker_skips_syntax_error`/
    `test_parse_file_worker_normal`/`test_parse_file_worker_uses_worker_stdlib`/
    `test_parse_file_worker_falls_back_to_module_stdlib`）
  - 新增 6 个测试：
    - `test_analyze_dependencies_records_ast_errors`：单文件语法错误记录到 ast_errors
    - `test_analyze_dependencies_records_multiple_ast_errors`：多文件语法错误都记录
    - `test_analyze_dependencies_parallel_records_ast_errors`：并行路径也记录
    - `test_analyze_dependencies_qml_parse_failure_does_not_block`：QML 解析失败
      （monkeypatch 抛 OSError）不阻塞主流程
    - `test_format_ast_errors_converts_to_relative_path`：格式化为相对 POSIX 路径
    - `test_format_ast_errors_falls_back_to_abs_path`：不同盘符回退绝对路径
- `tests/test_nuitka.py`：
  - 4 个既有 `_parse_parallel` 测试适配 `submit` + `as_completed` 模式：
    - `test_parse_parallel_timeout_warns_on_slow_worker`：fake pool 从 `map` 改为
      `submit`，fake `as_completed` 抛 TimeoutError，验证未完成 future 被 cancel
    - `test_parse_parallel_normal_completes_without_timeout`：调用加 `all_errors` 参数
    - `test_parse_parallel_uses_initializer`：fake pool 改 `submit`，monkeypatch
      `as_completed` 返回 `iter(futures)`
    - `test_parse_parallel_interleave_and_chunksize` 改名 `test_parse_parallel_interleave_and_submit`：
      不再检查 `chunksize`（submit 无此参数），改为验证 `submit` 调用次数等于文件数
  - 新增 `test_parse_parallel_partial_timeout_aggregates_completed_results`：
    验证部分 worker 超时时已完成 worker 的结果仍被聚合（关键改进点）

## 关键决策与依据

### `ast_errors` 字段格式：相对路径 + 错误信息

worker 返回 `(abs_path, error_msg)` 元组，主进程 `_format_ast_errors` 转为相对
`src_dir` 的 POSIX 路径。原因：
- worker 不知道 `src_dir`（只接收 `str(py)`），无法在 worker 内转换相对路径
- 绝对路径包含 `tmp_path` 等临时目录前缀，对用户无意义
- POSIX 路径跨平台一致（Windows 反斜杠在报告中也用正斜杠）

路径转换失败（不同盘符，如 `C:` 源码 vs `D:` 缓存）回退到绝对路径，不抛 ValueError。

### `submit` + `as_completed` vs `map(timeout=)`

`Executor.map(timeout=)` 的语义：按提交顺序迭代结果，迭代时若某个结果未就绪等待
timeout，超时抛 TimeoutError。问题：首个 future 卡死时，即使后续 future 已完成也无法
获取结果（因为迭代顺序锁定）。

`submit` + `as_completed(timeout=)` 的语义：`as_completed` 按完成顺序 yield future，
某个 future 卡死不影响其他已完成 future 的结果获取。超时后抛 TimeoutError，已 yield
的 future 结果已聚合。

权衡：`submit` 失去 `map(chunksize=N)` 的批量 IPC 优化。但 `ast.parse` 是 CPU 密集
任务，单文件解析时间 >> IPC 开销，chunksize 的影响可忽略。iter-134 的 chunksize
优化主要针对减少调度开销，实际收益有限。

### 超时后 future cancel 的局限

`Future.cancel()` 仅对"已提交但未开始"的 future 有效，已运行的 future 无法取消。
`ProcessPoolExecutor` 的 `with` 块退出时 `shutdown(wait=True)` 会等待所有已提交的
future 完成。如果某个 worker 真卡死（如 ast.parse 死循环），`shutdown` 会无限等待。

这是 `ProcessPoolExecutor` 的固有限制，iter-127 的 `map(timeout=)` 也有同样问题。
本轮不解决此限制，仅确保"超时后已完成的结果保留"。彻底解决需要强制终止 worker
进程（操作 `_processes` 私有属性，不推荐）或用 `multiprocessing.Pool` 的
`terminate()` 方法（API 不同，需重构）。

### QML 循环 try/except 的防御性

`parse_qml_imports` 内部已 `except OSError: return subs`（空集合），正常情况下
不会抛异常。但 `analyze_dependencies` 循环外加一层 `try/except OSError` 兜底：
- 文件在 `rglob` 枚举后被删除（竞态）
- 权限问题在 `read_text` 之外触发
- 其他未预期的 OSError 场景

不 catch `Exception`（避免吞掉编程错误），仅 catch `OSError`（I/O 相关）。

## 代码实现情况

### `_parse_file_worker` 返回 ast_errors

```python
def _parse_file_worker(py: str) -> tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]:
    try:
        tree = ast.parse(Path(py).read_bytes())
    except (SyntaxError, OSError) as e:
        return [], [], {}, [(py, str(e))]
    # ... 正常解析 ...
    return non_stdlib_tops, stdlib_tops, subs, []
```

### `_parse_parallel` 改 submit + as_completed

```python
with ProcessPoolExecutor(
    max_workers=cpu_count,
    initializer=_init_parse_worker,
    initargs=(_STDLIB,),
) as pool:
    futures = [pool.submit(_parse_file_worker, str(p)) for p in interleaved]
    completed = 0
    try:
        for future in as_completed(futures, timeout=_PARSE_TOTAL_TIMEOUT):
            non_stdlib_tops, stdlib_tops, subs, errors = future.result()
            all_imports.extend(non_stdlib_tops)
            all_stdlib.extend(stdlib_tops)
            for pkg, sub_set in subs.items():
                all_submodules.setdefault(pkg, set()).update(sub_set)
            all_errors.extend(errors)
            completed += 1
    except FuturesTimeoutError:
        pending = len(futures) - completed
        _logger.warning(
            "AST 并行解析超时（%ds），%d/%d 个文件未完成，依赖分析可能不完整",
            int(_PARSE_TOTAL_TIMEOUT), pending, len(py_files),
        )
        for f in futures:
            if not f.done():
                f.cancel()
```

### `_format_ast_errors` 相对路径转换

```python
def _format_ast_errors(src_dir: Path, errors: list[tuple[str, str]]) -> list[str]:
    formatted: list[str] = []
    for abs_path, msg in errors:
        try:
            rel = Path(abs_path).relative_to(src_dir).as_posix()
        except ValueError:
            rel = abs_path
        formatted.append(f"{rel}: {msg}")
    return formatted
```

### QML 循环防御性 try/except

```python
for qml_file in qml_files:
    try:
        qml_qt_subs.update(parse_qml_imports(qml_file))
    except OSError as e:
        _logger.warning("QML 文件解析失败，跳过: %s: %s", qml_file, e)
```

## 测试验证结果

### 新增测试（7 个）

`test_analyzer.py`（6 个）：
- `test_analyze_dependencies_records_ast_errors`：单文件语法错误记录到 ast_errors
- `test_analyze_dependencies_records_multiple_ast_errors`：多文件语法错误都记录
- `test_analyze_dependencies_parallel_records_ast_errors`：并行路径也记录
- `test_analyze_dependencies_qml_parse_failure_does_not_block`：QML OSError 不阻塞
- `test_format_ast_errors_converts_to_relative_path`：相对 POSIX 路径格式化
- `test_format_ast_errors_falls_back_to_abs_path`：不同盘符回退绝对路径

`test_nuitka.py`（1 个）：
- `test_parse_parallel_partial_timeout_aggregates_completed_results`：部分超时
  已聚合（3 个已完成 future 结果保留，2 个未完成被 cancel）

### 既有测试适配

- 4 处 worker 返回值解包从 3 元组改为 4 元组
- 4 个 `_parse_parallel` 测试适配 `submit` + `as_completed` 模式
- `test_parse_parallel_interleave_and_chunksize` 改名
  `test_parse_parallel_interleave_and_submit`，检查 submit 次数替代 chunksize

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 4 files already formatted
- pyrefly: 0 errors
- pytest: 2063 passed, 12 skipped（iter-137 为 2056 passed，新增 7 个测试）
- coverage: 96%（>= 95% 门禁，iter-137 为 95.70%，提升 0.3%）
- 10 benchmarks: 全通过

## 整合优化情况

- `_parse_file_worker` 返回类型从 3 元组改 4 元组，与 `_parse_serial` 的
  `all_errors` 参数、`_parse_parallel` 的 `all_errors` 参数、`DependencyReport.ast_errors`
  字段形成完整错误传递链
- `_format_ast_errors` 与 `_format_*` 系列辅助函数风格一致（私有函数，主进程调用）
- `submit` + `as_completed` 模式与 `_individual_import_test`（iter-137）和
  `_compile_files`（iter-131）的 `ThreadPoolExecutor` 模式一致，但此处是
  `ProcessPoolExecutor`（CPU 密集 ast.parse 释放 GIL 有限，进程池更优）

## 遗留事项

- `ProcessPoolExecutor` 的 `shutdown(wait=True)` 在 worker 真卡死时无限等待
  （固有限制，需 `multiprocessing.Pool.terminate()` 彻底解决，重构成本高）
- `submit` 失去 `map(chunksize=N)` 的批量 IPC 优化，大文件数（500+）场景 IPC
  开销可能略增（接受，ast.parse CPU 密集，IPC 开销可忽略）
- `DependencyReport.ast_errors` 字段当前无上层消费方（CLI 未展示），后续迭代
  可在 `fsp doctor` 或构建日志中展示给用户

## 下一轮计划

iter-139 缓存目录健康检查（req-49 L111-113，阶段 3 深度健壮性）：
1. `fsp doctor` 扩展 `--check-cache` 检测损坏缓存（`.deps-*.json` 损坏、wheel 文件缺失、
  stamp 不一致）
2. 检测孤儿文件（cache 目录中不属于任何项目的 wheel）
3. 输出清理建议（`fsp cache clean` 子命令）
