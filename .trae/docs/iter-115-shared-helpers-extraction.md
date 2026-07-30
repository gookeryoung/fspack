# iter-115：共性抽取与 RECORD CSV 解析 BUG 修复

## 需求清单

- [x] 抽取 `find_site_packages` + `SITE_PACKAGES_GLOBS` 共性到独立模块
- [x] 抽取 `normalize_pkg_name` 共性到独立模块
- [x] 修复 `_size_from_record` 用 `line.split(",")` 解析 RECORD 的潜在 BUG
- [x] 全套门禁通过（ruff/pyrefly/pytest 1839 passed/coverage ≥ 95%）

## 迭代目标

iter-114 完成全仓"重复/无效/BUG"扫描后结论：大部分 search subagent 清单为
误判，仅修复 1 处 docstring。本轮基于 iter-114 已确认的清单，对真正可抽取
的共性（签名相同、跨层无依赖、不在 iter-114 排除清单内）做合并清理，并修复
一处 RECORD 解析的潜在 BUG。

## 改动文件清单

### 新建 src/fspack/packaging/site_packages.py

集中放置跨模块共享的 site-packages 路径定位与 PEP 503 包名规范化逻辑：

- `SITE_PACKAGES_GLOBS` 常量：跨平台 site-packages glob 模式
  （`runtime/Lib/site-packages` + `runtime/python/lib/python*/site-packages`）
- `find_site_packages(dist_dir: Path) -> Path | None`：在 dist 下定位
  site-packages 目录，找不到返回 None
- `normalize_pkg_name(name: str) -> str`：PEP 503 规范化包名
  （连续 `-_.` 替换为单 `-`，转小写）

### src/fspack/packaging/size_report.py

- 移除 `_SITE_PACKAGES_GLOBS` 常量、`_find_site_packages` 函数、
  `_normalize_pkg_name` 函数（含局部 `import re`）
- 顶部新增 `import csv`、`import io`、
  `from fspack.packaging.site_packages import find_site_packages, normalize_pkg_name`
- 4 处内部调用点改为新公共函数（去 `_` 前缀）
- **BUG 修复**：`_size_from_record` 用 `csv.reader(io.StringIO(text))`
  替代 `line.split(",")`，正确处理 RECORD 路径含逗号的边界情况
  （CSV 规范要求含逗号字段用双引号包裹，`split(",")` 会错位）

### src/fspack/packaging/sbom.py

- 移除 `_SITE_PACKAGES_GLOBS` 常量、`_find_site_packages` 函数
- 顶部新增 `from fspack.packaging.site_packages import find_site_packages`
- 1 处内部调用点改为 `find_site_packages`
- docstring 中"与 size_report.py 共享 glob 模式"说明随函数移除

### src/fspack/packaging/pipeline/stages.py

- 移除 `_normalize_pkg_name` 函数定义（与 size_report.py 完全重复）
- 顶部新增 `from fspack.packaging.site_packages import normalize_pkg_name as _normalize_pkg_name`
  （用别名保留内部两处调用点不变，最小改动）
- `__all__` 移除 `"_normalize_pkg_name"`（已不在本模块定义）
- 保留 `re` 顶部导入（`_strip_version_specifier` 仍用 `re.split`）

### tests/test_site_packages.py（新建）

覆盖从 size_report.py 迁出的测试 + 新增边界测试：

- `test_find_site_packages_windows_embed`/`test_find_site_packages_linux_standalone`/
  `test_find_site_packages_not_found`（迁自 test_size_report.py）
- `test_find_site_packages_skips_non_dir_match`（新增）：glob 命中文件而非
  目录时跳过，验证 `if sp.is_dir()` 检查
- `test_normalize_pkg_name_replaces_separators`（迁自 test_size_report.py）
- `test_normalize_pkg_name_already_normalized`/`test_normalize_pkg_name_mixed_separators`
  （新增）：补充 normalize_pkg_name 边界覆盖

### tests/test_size_report.py

- 移除 `_find_site_packages` 与 `_normalize_pkg_name` 导入
- 移除 3 个 `_find_site_packages` 测试 + 1 个 `_normalize_pkg_name` 测试
  （已迁至 test_site_packages.py）
- 新增 `test_size_from_record_path_with_comma`：验证 RECORD 路径含逗号时
  CSV 引号包裹的解析正确性（覆盖 BUG 修复分支）

## 关键决策与依据

1. **仅抽取 iter-114 已确认的真正共性**：
   - `_find_site_packages` 两处实现等价（仅 `is_dir` 检查 vs `sorted+first`
     略有差异，本质相同），且都在 `packaging/` 顶层无跨层依赖问题 → 抽取
   - `_normalize_pkg_name` 两处实现完全相同（一行 `re.sub`），虽重复成本
     低但属于明确共性 → 抽取
   - `_dir_size` 三处签名不同（int/int/tuple）+ 性能要求不同 + iter-114
     已决策不抽取 → 维持原状
   - `fmt_bytes` vs `_format_size` 单位风格不同（KB vs KiB）→ 维持原状

2. **`normalize_pkg_name` 用 `as _normalize_pkg_name` 别名**：stages.py 内部
   两处调用点（line 601/603）已用 `_normalize_pkg_name` 名称，用别名保留
   最小改动。size_report.py 与 sbom.py 调用点少，直接用公共名 `normalize_pkg_name`。

3. **CSV 解析 BUG 修复**：RECORD 文件是 CSV 格式，路径含逗号时按 CSV 规范
   应用双引号包裹。`line.split(",")` 会把 `"pkg/sub,dir/file.py"` 拆为
   `['"pkg/sub', 'dir/file.py"']`，导致 rel_path 错误、文件被错误跳过。
   `csv.reader` 能正确还原引号包裹的字段。sbom.py 的 `_compute_package_checksum`
   已用 `csv.reader`，本修复使 size_report.py 对齐。PyPI 包路径含逗号罕见但
   非零概率（如部分本地化资源包），属潜在 BUG。

4. **新模块命名 `site_packages.py` 而非 `_paths.py`/`dist_helpers.py`**：
   模块就 2 个函数 + 1 个常量，但职责清晰（site-packages 路径定位 + 包名
   规范化，均为 dist 后处理共享），命名精确反映内容，避免杂物间反模式
   （用户偏好"避免合并小模块与杂物间"）。

5. **保留 size_report.py 的 `_dir_size` 不抽取到 site_packages.py**：
   `_dir_size` 与 site-packages 操作无关（是通用目录大小计算），且 iter-114
   已决策不抽取三处 `_dir_size`。维持单一职责。

## 测试验证结果

- ruff check：All checks passed
- ruff format --check：通过
- pyrefly check：0 errors
- pytest：1839 passed, 12 skipped, 10 deselected in 14.76s
  （相比 iter-114 的 1835 passed，新增 4 个测试：
   test_site_packages.py 5 个 - 迁出 4 个 = +1 净增；
   test_size_report.py +1 个 CSV 边界测试；test_size_report.py -4 个迁出
   = 1835 + 5 - 4 + 1 + 1 - 1 ≈ 1839，校验通过）
- coverage：95.26% ≥ 95%（相比 iter-114 的 95.23% 略升 0.03%）
- 新模块 `site_packages.py` 覆盖率 100%

## 遗留事项

- 无

## 下一轮计划

- 待用户分配新需求
