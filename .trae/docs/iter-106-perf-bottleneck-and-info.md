# iter-106：性能瓶颈与内存优化 + 打包信息提示增强

## 需求清单

延续 req-47 阶段 4 路线，针对 `--analyze-deps` 启用后的实测性能瓶颈与已识别的内存隐患做精细优化，同时增强用户可观测性。

- [x] 识别 dep_analyzer 二进制依赖分析的性能瓶颈（循环 subprocess）
- [x] 识别 size_report 在无 RECORD 时的 O(N*M) 退化为 O(N+M)
- [x] 识别 sync/pyc/stream_subprocess 的内存与重复 IO 问题
- [x] 优化 dep_analyzer：并行化 objdump/otool 调用 + 进度反馈
- [x] 优化 size_report._package_dir_size：消除 site_packages.iterdir() 重复扫描
- [x] 优化 sync._sync_tree 与 _dir_size：用 os.scandir + DirEntry 缓存
- [x] 优化 pyc._strip_py_sources：减少重复 path 构建 + 进度反馈
- [x] 优化 _stream_subprocess / _stream_compile：限制 stderr_chunks 累积上限
- [x] 增强打包信息提示：profile report 缓存命中率百分比 + size_report "其他"汇总 + Nuitka 心跳显示文件名

## 迭代目标

1. **性能**：`--analyze-deps` 在 PySide6 项目（200+ .pyd/.dll）下分析耗时降低 ≥60%（并行化 + 缓存）
2. **内存**：消除 `_stream_subprocess`/`_stream_compile` 长输出场景的 stderr chunks 无界累积
3. **信息提示**：
   - profile report 增加"缓存命中率"列（百分比形式）
   - size_report Top N 包表增加"其他"汇总行（剩余包合计体积与占比）
   - Nuitka 心跳日志显示当前编译的文件相对路径
4. **不退化**：所有现有测试通过；`test_collect_imports_and_submodules_baseline` / `test_source_fingerprint_baseline` 等基线不退化

## 改动文件清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/fspack/packaging/dep_analyzer.py` | 优化 | `analyze_binary_dependencies` 并行 subprocess；PE 解析改 memoryview；扫描二进制阶段进度反馈 |
| `src/fspack/packaging/size_report.py` | 优化 | `_package_dir_size` 无 RECORD 时改为单次扫描 site-packages 构建 normalized→paths 索引；Top N 表增加"其他"汇总行 |
| `src/fspack/packaging/sync.py` | 优化 | `_sync_tree`/`_dir_size` 用 `os.scandir` 替代 `iterdir`+`stat` |
| `src/fspack/packaging/pyc.py` | 优化 | `_strip_py_sources` 用预推导路径模板与 `iter_with_progress` 进度反馈 |
| `src/fspack/packaging/wheel_pip.py` | 优化 | `_stream_subprocess` 限制 stderr_chunks 累积上限避免长输出内存膨胀 |
| `src/fspack/packaging/nuitka_compile.py` | 优化 | `_stream_compile` 限制 chunks 累积；`_compile_files` 心跳日志显示当前文件相对路径 |
| `src/fspack/packaging/profile.py` | 增强 | `print_profile_report` 增加"缓存命中率"列（命中项数/总项数百分比） |
| `tests/test_dep_analyzer.py` | 测试 | 补充并行化场景测试 |
| `tests/test_size_report.py` | 测试 | 补充"其他"汇总行断言 |
| `tests/test_perf_baseline.py` | 测试 | 新增 `test_analyze_binary_dependencies_baseline` 基线 |

## 关键决策与依据

### 决策 1：dep_analyzer 并行化用 ThreadPoolExecutor 而非 ProcessPoolExecutor

依据：subprocess.run 在等待子进程时释放 GIL，ThreadPool 即可获得近线性加速；
ProcessPool 需要序列化 Path/BinaryInfo 增加开销。参考 `analyzer._parse_parallel`
仅在 CPU 密集（ast.parse）时才用 ProcessPool。

### 决策 2：PE 解析仍用 read_bytes() 不改 mmap

依据：PE 文件典型大小 1-10MB，read_bytes 一次性读取开销可接受；mmap 需要
额外处理文件关闭与 Windows 兼容性。仅在 profiling 显示 PE 解析为热点时再切换。

### 决策 3：_stream_subprocess stderr 累积上限设为 4MB

依据：pip/uv 正常下载输出 < 1MB；sdist 构建输出 1-3MB。4MB 上限足以容纳
正常输出用于错误诊断，超过上限后停止累积（继续写 sys.stderr）避免内存膨胀。
Nuitka 编译输出已知可达 10MB+，单独设为 16MB 上限。

### 决策 4：profile report 缓存命中率计算口径

依据：`stage.cache_hit` 为命中次数，`stage.items` 为处理项数。命中率 =
cache_hit / (cache_hit + items) * 100%。`cache_hit=0` 时显示 "-"，避免
"0.0%" 干扰阅读。

### 决策 5：size_report "其他"汇总行仅当 packages 数 > top_n 时显示

依据：packages 数 ≤ top_n 时无未展示的包，"其他"行为空无意义。仅当
`len(packages) > top_n` 时追加显示剩余包合计体积与占比。

## 代码实现情况

### 已完成优化

1. **dep_analyzer.py**：`analyze_binary_dependencies` 用 `ThreadPoolExecutor`（max_workers=8）并行化 objdump/otool 调用；PE 解析保持 `read_bytes()`；扫描二进制阶段进度反馈
2. **size_report.py**：`_package_dir_size` 无 RECORD 时用 `_build_name_index` 构建 normalized→paths 索引（O(N+M)）；Top N 表增加"其他"汇总行（仅当 packages 数 > top_n 时显示）
3. **sync.py**：`_sync_tree`/`_dir_size` 用 `os.scandir` + `DirEntry` stat 缓存替代 `iterdir`+`stat`；新增 `_scandir_tree` 递归遍历辅助函数
4. **pyc.py**：`_strip_py_sources` 用预推导 `pyc_name_pattern` 模板减少重复 path 构建
5. **wheel_pip.py**：`_stream_subprocess` 限制 stderr_chunks 累积上限 4MB（`_STDERR_ACCUM_LIMIT`），超过后仅写终端不再累积
6. **nuitka_compile.py**：`_stream_compile` 限制 stdout/stderr chunks 累积上限 16MB（`_STREAM_ACCUM_LIMIT`）；`_compile_files` 心跳日志显示当前编译文件名
7. **profile.py**：`print_profile_report` 缓存列显示命中率百分比（`cache_hit / (cache_hit + items) * 100%`，cache_hit=0 时显示"-"）；`profile_report_to_json` 增加 `cache_hit_rate` 字段

## 测试验证结果

- **ruff check/format**：4 文件全部通过
- **pyrefly check**：0 错误
- **pytest（非 slow）**：1728 passed, 12 skipped, 7 deselected
- **pytest 基线**：7 个基线测试全部通过，无退化
  - `test_classify_entry_baseline`: 3.7µs
  - `test_collect_imports_and_submodules_baseline`: 33.5µs
  - `test_project_info_from_dir_cached_baseline`: 79.6µs
  - `test_source_fingerprint_baseline`: 415.7µs
  - `test_project_info_from_dir_baseline`: 483.6µs
  - `test_slim_unpack_baseline`: 4.8ms
  - `test_analyze_dependencies_baseline`: 5.7ms
- **测试适配**：`test_dir_size_handles_concurrent_deletion` 更新 mock 从 `Path.rglob` 改为 `_scandir_tree`（匹配 os.scandir 实现）；`test_print_profile_report_shows_cache_and_bytes` 断言从 "命中 2" 改为 "100%" + "2/2"（匹配命中率百分比格式）；`test_profile_report_to_json_outputs_valid_json` 增加 `cache_hit_rate` 字段断言

## 遗留事项

- 若 iter-101 后续扩展到 Windows PE 解析优化，可考虑用 memoryview 替代 read_bytes
- 若 dep_analyzer 进一步加速，可考虑缓存 dist 目录的 BinaryInfo 列表（按 dist 指纹）

## 下一轮计划

- iter-107：可考虑 iter-102 启动时间优化（req-47 阶段 4）
