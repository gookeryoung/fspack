# iter-67 wheel 并行下载

## 需求清单

- [x] wheel 并行下载：`_download_online` uv 解析路径改为 ThreadPoolExecutor 并行

## 迭代目标

将 `_download_online` 中 uv 解析成功后的串行 `pip download --no-deps -r requirements.txt`
改为 `ThreadPoolExecutor` 并行下载每个已解析的精确版本 wheel，提升 I/O 密集网络下载场景
吞吐量。同时保持 sdist 回退逻辑与所有现有测试通过。

## 改动文件清单

- `src/fspack/packaging/wheel_pip.py`：
  - 新增 `_PARALLEL_DOWNLOAD_WORKERS = 8` 常量
  - 新增 `_download_resolved_parallel` 函数：用 ThreadPoolExecutor 并发调度
    `_download_one_resolved`，单包场景直接串行（避免线程池开销），多包场景失败时
    收集 failed 列表触发 sdist 回退（仅重试失败包）
  - 新增 `_download_one_resolved` 函数：下载单个 `pkg==ver` wheel，`with_index=True`
    时附加 `-i <pypi_index>`（sdist 回退重试场景）
  - 新增 `_merge_parallel_results` 函数：合并各任务 stdout 供 `_parse_pip_download_wheels` 解析
  - 重构 `_download_online` uv 解析成功分支：移除 `req_file` 写入与 `-r` 调用，
    改为调 `_download_resolved_parallel`
  - 引入 `from concurrent.futures import ThreadPoolExecutor, as_completed` 与
    `from collections.abc import Iterable`
- `src/fspack/packaging/wheels.py`：facade re-export 新增 `_download_one_resolved`/
  `_download_resolved_parallel`/`_merge_parallel_results`
- `tests/test_wheels.py`：
  - 适配 4 个现有测试：`test_download_online_uv_resolved_uses_no_deps`/
    `test_download_wheels_uv_path_integration`/`test_download_online_uv_sdist_fallback`/
    `test_download_online_uv_resolved_passes_extra_sources`，将 patch 从
    `_stream_subprocess` 改为 `subprocess.run`，断言从 `-r`/`--progress-bar` 改为
    精确版本需求字符串
  - 新增 7 个测试：`test_download_resolved_parallel_multiple_packages`/
    `test_download_resolved_parallel_partial_failure_sdist_fallback`/
    `test_download_one_resolved_with_index`/
    `test_download_one_resolved_without_index`/
    `test_download_one_resolved_pip_missing`/
    `test_merge_parallel_results_concat_stdout`/
    `test_merge_parallel_results_skip_empty_stdout`
  - import 新增 `_download_one_resolved`/`_merge_parallel_results`

## 关键决策与依据

1. **单包场景直接串行**：`len(resolved) == 1` 时跳过 ThreadPoolExecutor，
   直接 `_download_one_resolved`。避免线程池调度开销（单包场景无并行收益），
   但仍走 try/except 触发 sdist 回退（保留原行为）

2. **并行度 8**：`min(_PARALLEL_DOWNLOAD_WORKERS, len(resolved))`。
   I/O 密集网络下载，8 个并发平衡 PyPI 限流与吞吐量。单个 wheel 下载耗时差异大
   （几 KB 元数据 vs 数百 MB 二进制），线程池自动调度

3. **不流式输出 stderr**：并行模式多进程 stderr 交错混乱，改为 `subprocess.run`
   捕获输出。单包场景量小无需进度条。`_stream_subprocess` 仅保留给 sdist 构建路径
   （`_build_sdist_wheels` 中调用）

4. **sdist 回退仅重试失败包**：并行下载失败的包收集到 `failed` 列表，用首个失败
   stderr 解析 missing 包名触发 `_handle_sdist_fallback`，构建后仅重试 failed 列表
   （而非全部 resolved），避免重复下载已成功的包

5. **`_merge_parallel_results` 合并 stdout**：各任务 stdout 用 `\n` 拼接，
   供 `_parse_pip_download_wheels` 解析 wheel 文件名。stderr 不合并（并行时
   各进程 stderr 独立，合并无意义），返回空字符串

6. **删除未使用的 `stream` 参数**：`_download_one_resolved` 原设计支持
   `stream=True` 走 `_stream_subprocess`，但实际无调用方使用，按 YAGNI 原则删除

## 代码实现情况

- `wheel_pip.py` 行数：从 ~608 行增至 ~628 行（+20 行，含 3 个新函数与注释）
- `wheel_pip.py` 覆盖率：100%（282 statements, 0 missing）
- 总测试数：1044（+7 新增，0 失败）
- 总覆盖率：98.58%

## 整合优化情况

- 移除 `_download_online` 中的 `req_file` 临时文件写入与 `try/finally` 清理，
  消除临时文件残留风险
- 移除未使用的 `stream` 参数，简化 `_download_one_resolved` 签名

## 测试验证结果

- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors（2 suppressed，与基线一致）
- pytest（非 slow）：1044 passed，覆盖率 98.58%（≥95%）
- `test_wheels.py`：105 测试全通过（+7 新增，4 适配）

## 性能预期

- **理论提速**：N 个独立 wheel 下载从串行 `O(sum(T_i))` 降为并行 `O(max(T_i))`
  （受线程数 8 限制，N > 8 时分批）。典型场景（10 个依赖，单包平均 2s 下载）
  从 ~20s 降为 ~4s（8 并发）
- **实际验证**：测试环境无网络，无法实测。基线对比需在真实 PyPI 下载场景验证
- **退化风险**：单包场景无并行开销（直接串行），无退化。多包场景线程池调度
  开销 < 1ms，相比网络下载耗时可忽略

## 遗留事项

- 真实 PyPI 下载场景的基线对比验证（需网络环境）
- `_PARALLEL_DOWNLOAD_WORKERS = 8` 是否需要根据网络环境动态调整（如带宽限流场景
  降为 4）

## 下一轮计划

iter-68：CLI 启动懒加载优化（`fsp` 入口延迟导入重模块，目标 `fsp --help` ≤100ms）
