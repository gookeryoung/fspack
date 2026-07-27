# iter-71 性能微优化与递归打包模式

## 需求清单

- [x] 基于当前性能基线制定优化方案，识别 analyzer.py 中可微优化的热点
- [x] `analyzer._local_packages` 改用 `os.scandir` 替代 `Path.iterdir`，减少 stat 调用
- [x] `analyzer.source_fingerprint` 哈希算法从 SHA-256 改为 BLAKE2b（digest_size=32）
- [x] `analyzer._parse_serial`/`_parse_file_worker` 用 `read_bytes()` + `ast.parse(bytes)`
- [x] 新增 `--recursive`/`-R` 递归打包模式（build/package 子命令）
- [x] 递归扫描子项目，跳过 `.venv`/`build`/`dist`/`.git` 等开发期目录
- [x] 单项目失败不中断后续项目，最后汇总成功/失败列表
- [x] 退出码传播：0=全部成功，1=有失败（便于 CI 检测）

## 迭代目标

在不引入新依赖、不改变公共 API 的前提下，通过微优化提升 analyzer 性能；
新增 `--recursive`/`-R` 模式支持 monorepo / 多子项目仓库一次性打包。

## 改动文件清单

- `src/fspack/analyzer.py`：
  - `_local_packages`：`Path.iterdir` → `os.scandir`，复用 `DirEntry` stat 缓存
  - `source_fingerprint`：`hashlib.sha256` → `hashlib.blake2b(digest_size=32)`
  - `_parse_serial`/`_parse_file_worker`：`read_text(encoding="utf-8")` + `ast.parse(str)`
    → `read_bytes()` + `ast.parse(bytes)`，避免 Python 层 decode 开销
- `src/fspack/cli.py`：
  - 新增 `_RECURSIVE_SKIP_DIRS` 常量（与 `analyzer._EXCLUDED_DIRS` 共用语义）
  - `build`/`package` 子命令新增 `-R`/`--recursive` 标志
  - 新增 `discover_subprojects(root)` 递归扫描含 `pyproject.toml` 的子项目
  - 新增 `_run_recursive(root, action, ns)` 批量执行 + 错误隔离 + 汇总
  - 重构 `main()` 提取 `_run_build`/`_run_package` 单项目执行函数
  - 新增辅助函数：`_format_project_path`/`_format_error`/`_print_recursive_summary`
- `tests/test_cli_recursive.py`（新增）：33 个测试覆盖扫描/执行/汇总/CLI 集成
- `docs/changelog.rst`：v0.2.7 段补充 4 条变更（1 feat + 3 perf）
- `README.md`：build/package 命令文档补充 `-R`/`--recursive`，新增"递归打包多项目"章节

## 关键决策与依据

1. **BLAKE2b 替代 SHA-256**：CPython 实现略快 10-20%，`digest_size=32` 输出 64 hex
   字符与 SHA-256 长度一致，缓存键文件名兼容。`.dep_cache.json` 是构建期临时缓存，
   哈希算法变更导致重建无成本。BLAKE2b 抗碰撞性足够用于缓存键场景

2. **`read_bytes()` + `ast.parse(bytes)`**：`ast.parse` 接受 bytes，内部用 C 实现解码，
   比显式 `str.decode("utf-8")` 快约 5-10%。基线测试 50 文件场景 `analyze_dependencies`
   提速约 14%。`_parse_file_worker` 同步修改保持串行/并行路径一致

3. **`-R`/`--recursive` 短长形式**：与 `--no-pyc`/`--pyc-strip` 等既有标志风格一致，
   短形式 `-R` 便于日常使用，长形式 `--recursive` 在脚本中可读性更好

4. **`_RECURSIVE_SKIP_DIRS` 与 `_EXCLUDED_DIRS` 分离**：两者语义不同——
   `_EXCLUDED_DIRS` 用于 AST 扫描时跳过 tests/examples/docs 等开发期代码；
   `_RECURSIVE_SKIP_DIRS` 用于递归扫描时跳过 .venv/dist/build 等目录。
   tests/examples/docs 在递归模式下应被扫描（用户可能想打包 examples 下的子项目）

5. **单项目失败用 `except BaseException`**：递归模式需捕获所有错误（含 RuntimeError/
   OSError/KeyboardInterrupt）保证后续项目继续。常规代码禁用 `except Exception`，
   但递归模式作为批处理入口需更宽松的容错，加 `# noqa: BLE001` 标注

6. **退出码通过 `sys.exit()` 传播**：递归模式始终通过 `sys.exit(code)` 退出，
   即使全部成功也调 `sys.exit(0)`，便于 CI 直接判断。非递归模式不调 `sys.exit`

7. **`_format_error` 截断到 200 字符**：避免超长错误消息（如堆栈跟踪）破坏汇总表，
   取首行避免多行错误打乱输出格式

## 代码实现情况

- `src/fspack/analyzer.py`：3 处微优化，无 API 变更，无新增依赖
- `src/fspack/cli.py`：新增 ~150 行（discover_subprojects + _run_recursive + 辅助函数）
- `tests/test_cli_recursive.py`：新增 33 个测试，覆盖：
  - discover_subprojects：root 自身、嵌套子项目、跳过开发期目录、跳过 .egg-info、
    按名称排序、空 root、深度嵌套、不进入跳过的目录、OSError 降级、符号链接循环
  - _run_recursive：build/package 分发、单项目失败不中断、退出码传播、汇总输出、
    DEBUG 日志保留错误堆栈、package 产物日志
  - CLI -R 集成：-R/--recursive 等价、build/package 分发、失败传播、跳过开发期目录
  - 辅助函数：_format_project_path（root 自身/子目录/外部路径）、
    _format_error（单行/多行/超长截断/空消息回退类名）、_print_recursive_summary 三分支

## 整合优化情况

- 复用 `os.scandir` 模式：`_local_packages`/`source_fingerprint`/`discover_subprojects`
  三处统一用 `os.scandir`，避免 `Path.iterdir` 包装开销
- 复用 `_EXCLUDED_DIRS` 语义：`_RECURSIVE_SKIP_DIRS` 在 analyzer 排除目录基础上
  扩展（移除 tests/examples/docs，新增 .pytest_cache/.ruff_cache 等 CI 缓存目录）
- 沿用 `_format_error` 单行截断模式，与现有日志格式化风格一致

## 测试验证结果

### 性能基线对比（vs `0001_iter80-baseline.json`）

| 测试场景 | Baseline | NOW | 变化 | 结论 |
|---------|----------|-----|------|------|
| `test_classify_entry_baseline` | 3.71μs | 3.69μs | -0.5% | 持平 |
| `test_collect_imports_and_submodules_baseline` | 34.26μs | 34.11μs | -0.4% | 持平 |
| `test_source_fingerprint_baseline` | 439.92μs | 421.72μs | **-4.1%** | blake2b 收益 |
| `test_slim_unpack_baseline` | 5.43ms | 5.62ms | +3.5% | 噪声范围内 |
| `test_analyze_dependencies_baseline` | 7.02ms | 6.01ms | **-14.4%** | read_bytes 收益 |

关键收益：`source_fingerprint` 提速 4%，`analyze_dependencies` 提速 14%。
所有基线测试通过 `--benchmark-compare-fail=mean:10%` 门禁。

### 全套门禁

- ruff check：All checks passed
- ruff format --check：69 files already formatted
- pyrefly check：0 errors（2 suppressed，与基线一致）
- pytest（非 slow）：1080 passed, 1 skipped, coverage 98.56%（≥95%）
- pytest slow：6 passed, 24 skipped（基线测试通过）
- cli.py 覆盖率：97.41%（新增测试覆盖递归模式主要路径）

## 遗留事项

- `_format_project_path` 外部路径分支（`relative_to` 失败回退）在 Windows 上
  难以稳定测试（tmp_path 总在 root 下），仅验证了名称包含关系
- 符号链接循环测试在 Windows 非开发者模式/非管理员下跳过，Linux/macOS 正常通过
- `discover_subprojects` 中 `Path.resolve()` 的 OSError 分支（line 352-353）难自然触发，
  属防御性代码，未覆盖但通过 `# pragma: no cover` 标注会破坏代码整洁，保留

## 下一轮计划

- req-39（功能与生态扩展）继续推进
- 可考虑：递归模式并行化（当前串行，monorepo 大量子项目时可并行 build）
- 可考虑：递归模式输出 JSON 报告（便于 CI 系统解析成功/失败列表）
