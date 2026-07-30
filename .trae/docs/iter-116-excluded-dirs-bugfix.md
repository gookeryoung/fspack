# iter-116：_EXCLUDED_DIRS 缺失目录潜在 BUG 修复

## 需求清单

- [x] 修复 `_EXCLUDED_DIRS` 缺少 `.uv-cache`/`node_modules`/`.pyrefly_cache`/`htmlcov` 的潜在 BUG
- [x] 更新 cli.py 过时注释（`_RECURSIVE_SKIP_DIRS` 与 `_EXCLUDED_DIRS` 语义已分化）
- [x] 补充测试覆盖新增排除目录
- [x] 全套门禁通过（ruff/pyrefly/pytest 1840 passed/coverage ≥ 95%）

## 迭代目标

iter-115 完成共性抽取后，本轮从"跨模块同名集合一致性"角度继续扫描。发现
`_EXCLUDED_DIRS`（analyzer_fingerprint.py）与 `_RECURSIVE_SKIP_DIRS`（cli.py）
虽然注释说"共用语义"，但实际内容已分化：前者缺少 `.uv-cache`/`node_modules`/
`.pyrefly_cache`/`htmlcov` 四个目录。这会导致项目根若存在这些目录（如用户设置
`UV_CACHE_DIR=.uv-cache`），内部的第三方包 `.py` 源码会被 AST 扫描与指纹计算
误扫描，引发依赖分析多报、缓存键失效。

## 改动文件清单

### src/fspack/analyzer_fingerprint.py

`_EXCLUDED_DIRS` 新增 4 个目录：
- `.uv-cache`：uv 包缓存，含第三方包 wheel 解包源码（最高风险）
- `node_modules`：py2js 项目（如 Transcrypt）可能含 `.py` 残留
- `.pyrefly_cache`：pyrefly 类型检查缓存
- `htmlcov`：coverage HTML 报告目录

影响两层扫描逻辑（共用 `_EXCLUDED_DIRS`）：
- `analyzer.py:82/96`：`analyze_dependencies` 用 `_is_excluded` 过滤 `.py`/`.qml`
- `analyzer_fingerprint.py:90-94`：`_iter_py_entries` 用 `_EXCLUDED_DIRS` 剪枝

### src/fspack/cli.py

更新 `_RECURSIVE_SKIP_DIRS` 上方注释：旧注释"与 analyzer._EXCLUDED_DIRS 共用
语义"已过时（两者内容在 iter-89/107 期间各自演进分化）。新注释明确说明两者
语义差异：本集合用于"找 pyproject.toml"，`_EXCLUDED_DIRS` 用于"扫描 .py/.qml
源码"（额外排除 examples/tests/docs/templates 等开发期目录）。两者各自独立
维护，不强行合并以避免语义混淆。

### tests/test_analyzer.py

新增 `test_analyze_dependencies_excludes_cache_and_tool_dirs`：在 tmp_path 下
创建 `.uv-cache`/`node_modules`/`.pyrefly_cache`/`htmlcov` 四个目录，各放一个
含 `import cryptography`/`flask`/`typing`/`coverage` 的 `.py` 文件，验证
`analyze_dependencies` 不扫描这些目录（`r.ast_third_party == ()`）。

## 关键决策与依据

1. **不合并 `_RECURSIVE_SKIP_DIRS` 与 `_EXCLUDED_DIRS`**：两者语义不同
   （找 pyproject.toml vs 扫描源码），内容不同（后者多 examples/tests/docs/
   templates 开发期目录）。强行合并会引入语义混淆，违反"单一职责"。iter-114/115
   已多次确认"内容不同 + 语义不同 → 不抽取"原则。

2. **新增 4 个目录的选择依据**：
   - `.uv-cache`：uv 默认缓存在 `~/.local/share/uv`，但用户可通过 `UV_CACHE_DIR`
     环境变量或 monorepo 布局导致 `.uv-cache` 出现在项目根。内部含 wheel 解包
     的 `.py` 源码，误扫描会多报第三方包内部依赖（如 `cryptography`）。
   - `node_modules`：Python 项目通常无此目录，但全栈项目（Django+Vue 等）有。
     py2js 工具（Transcrypt/Brython）可能在 node_modules 留 `.py` 残留。
   - `.pyrefly_cache`：pyrefly 缓存通常为二进制 `.json`，但保险排除。
   - `htmlcov`：coverage HTML 报告，通常不含 `.py`，但 `coverage html` 命令
     生成时可能复制部分源码到 `htmlcov/src_*.py`，保险排除。

3. **`.venv`/`.tox` 等已存在的目录不动**：这些已经在 `_EXCLUDED_DIRS` 中，
   无需调整。本次仅补齐与 `_RECURSIVE_SKIP_DIRS` 的差异部分。

## 测试验证结果

- ruff check：All checks passed
- ruff format --check：通过
- pyrefly check：0 errors
- pytest：1840 passed, 12 skipped, 10 deselected in 17.10s
  （相比 iter-115 的 1839 passed，新增 1 个测试
  `test_analyze_dependencies_excludes_cache_and_tool_dirs`）
- coverage：95.26% ≥ 95%（与 iter-115 持平）

## 遗留事项

- 无

## 下一轮计划

- 待用户分配新需求
