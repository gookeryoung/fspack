# 性能微优化与递归打包模式（iter-71）

## 背景

req-39 规划 iter-71~80 的功能增强与生态建设，但用户在 iter-70 完成后
提出「基于当前性能基线制定优化方案，并增加 --recursive/-R 打包模式」
的紧急需求，需要插入一轮迭代（iter-71）。

### 现状基线（2026-07-27，iter-70 完成后）

**性能基线**（已保存为 `.benchmarks/Windows-CPython-3.11-64bit/0001_iter80-baseline.json`）：

| 测试场景 | Median | Mean | 适用性 |
|---------|--------|------|--------|
| `test_classify_entry_baseline` | 3.6 μs | 3.7 μs | 已优化 |
| `test_collect_imports_and_submodules_baseline` | 33.5 μs | 34.3 μs | 单文件 AST 难优化 |
| `test_source_fingerprint_baseline` | 428.4 μs | 439.9 μs | 已用 os.scandir |
| `test_slim_unpack_baseline` | 5.2 ms | 5.4 ms | 已并行解压 |
| `test_analyze_dependencies_baseline` | 6.8 ms | 7.0 ms | 50 文件串行路径 |

**已识别可优化点**：

- `analyzer._local_packages` 仍用 `Path.iterdir()`，可改 `os.scandir` 减少
  `stat` 调用（影响 `analyze_dependencies` 与 `source_fingerprint`）
- `source_fingerprint` 用 `hashlib.sha256`，可改 `hashlib.blake2b`
  （CPython 实现略快，64 字节 hex 足够唯一）
- `_parse_serial` 用 `py.read_text(encoding="utf-8")` 后 `ast.parse(str)`，
  可改 `py.read_bytes()` + `ast.parse(bytes)`，避免 Python 层 decode
  开销（ast.parse 内部用 C 解码）
- `source_fingerprint` 的 `sorted(os.scandir(...))` 在大目录下产生中间 list，
  可用 `list.sort()` 原地排序减少一次拷贝

**功能缺口**：

- 无 `--recursive/-R` 打包模式：monorepo 或多项目仓库需逐个 `cd <sub> && fsp b`，
  无法一次性打包所有子项目。常见场景如：
  - `examples/` 下多个独立示例项目
  - 工作区下多个微服务子项目
  - 同一仓库的 cli/gui/web 多个入口项目

## 迭代任务

- [x] **iter-71a 性能微优化**：
  - `_local_packages` 改用 `os.scandir`（避免 `Path.iterdir` 内部 `scandir`
    + `Path` 包装开销）
  - `source_fingerprint` 哈希算法改 `blake2b`（digest_size=32，hex 64 字符，
    与原 sha256 输出长度一致，缓存键兼容）
  - `_parse_serial` 用 `read_bytes()` + `ast.parse(bytes)`（ast.parse 接受
    bytes，内部 C 实现解码快于 Python 层 `.decode("utf-8")`）
  - **基线对比**：`test_source_fingerprint_baseline` 与
    `test_analyze_dependencies_baseline` 退化 ≤ 10%
- [x] **iter-71b --recursive/-R 打包模式**：
  - `fsp b --recursive/-R [project]`：递归扫描 project 目录下所有含
    `pyproject.toml` 的子目录（含 project 自身），依次执行 build
  - `fsp p --recursive/-R [project]`：递归扫描并依次执行 package
  - 跳过 `.venv`/`.tox`/`dist`/`build`/`.git`/`__pycache__` 等开发期目录
  - 多项目失败时不中断后续项目，最后汇总成功/失败列表
  - 单项目失败时打印错误并继续，最终返回非零退出码（如有失败）

## 验收标准

- 每次迭代全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- 性能基线测试退化 ≤ 10%（`--benchmark-compare` 验证）
- `--recursive` 模式正确扫描子项目（含跳过逻辑、汇总输出）
- 递归模式测试覆盖：扫描多个子项目、跳过开发期目录、单项目失败不中断

## 风险与缓解

- **blake2b 兼容性**：缓存键变化导致 `.dep_cache.json` 失效重建。
  缓解：`.dep_cache.json` 是构建期临时缓存，重建无成本，且 blake2b 输出
  与 sha256 长度一致（64 hex），文件名字段无影响
- **递归扫描误打包**：`examples/`/`tests/`/`docs/` 等开发期目录下的
  `pyproject.toml` 不应被扫描。缓解：与 `analyzer._EXCLUDED_DIRS` 共用
  排除规则，且只扫描 `pyproject.toml` 存在的目录
- **递归模式失败传播**：单项目失败不应中断整批。缓解：捕获每个项目的
  异常，打印后继续，最终统一汇总
