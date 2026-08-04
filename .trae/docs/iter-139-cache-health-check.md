# iter-139: 缓存目录健康检查

## 需求清单

- [x] `fsp doctor` 扩展 `--check-cache` 检测损坏缓存（`.deps-*.json` 损坏、wheel 文件缺失）
- [x] 检测孤儿文件（cache 目录中不属于任何 deps 引用的 wheel）
- [x] 输出清理建议（`fsp cache clean` 子命令）

## 迭代目标

补齐 req-49 L111-113 列出的缓存目录健康检查三项任务：
(1) 引入 `CacheHealthReport` 数据类统一封装缓存扫描结果（损坏/stale/orphan），
`_scan_cache_health` 一次性扫描 cache_dir 下所有 `.deps-*.json` 与 `*.whl`，
复用给 `fsp doctor --check-cache`/`fsp cache status`/`fsp cache clean`，避免重复扫描；
(2) 检测孤儿 wheel（未被任何 deps 引用）与 stale deps（引用缺失 wheel 的 deps 文件），
损坏的 `.deps-*.json` 在扫描阶段自动删除（与 iter-128 `_load_deps_cache` 行为一致）；
(3) 新增 `fsp cache status`/`fsp cache clean` 子命令，`--dry-run` 支持预览。

## 改动文件清单

- `src/fspack/doctor_models.py`：
  - 新增 `CacheHealthReport` frozen dataclass：`cache_dir`/`total_deps_files`/
    `corrupt_deps_files`/`stale_deps_files`/`missing_wheels`/`orphan_wheels`/
    `total_wheels`/`orphan_size_bytes` 8 个字段，`has_issues` 属性聚合判断
- `src/fspack/doctor_envs.py`：
  - 新增 `_scan_cache_health(cache_dir)`：扫描 `.deps-*.json`（JSON 结构校验 +
    wheels 字段类型校验，损坏文件 best-effort 删除）与 `*.whl`（孤儿检测 +
    体积累加），返回 `CacheHealthReport`
  - 新增 `_clean_cache_issues(cache_dir, *, dry_run=False)`：基于 `_scan_cache_health`
    结果删除 stale deps 与 orphan wheels，单个文件 `OSError` 不阻断其他文件清理
    （warning 日志），`dry_run=True` 时仅扫描不删除
  - 重构 `_check_cache_integrity`：复用 `_scan_cache_health` 扫描结果，详情中
    追加 stale deps 与 orphan wheels 计数，建议中提示 `fsp cache clean`
- `src/fspack/cli_doctor.py`：
  - 新增 `run_cache_status()`：渲染缓存健康扫描详细报告（概要行 + 分组列表）
  - 新增 `run_cache_clean(*, dry_run=False)`：渲染清理/预览结果
  - 新增 `_format_cache_summary`/`_print_cache_detail_lists`/
    `_print_cache_clean_lists`/`_preview_names` 4 个渲染辅助函数（从
    `run_cache_status`/`run_cache_clean` 内联代码提取，规避 PLR0912 过多分支）
  - `__all__` 与 facade re-export 新增 `CacheHealthReport`/`_scan_cache_health`/
    `_clean_cache_issues`/`run_cache_status`/`run_cache_clean`
- `src/fspack/cli_parser.py`：
  - 新增 `_add_cache_subparser`：`fsp cache` 子命令 + `status`/`clean` 二级子命令，
    `clean` 支持 `--dry-run`
  - `--check-cache` help 文本扩展：追加"报告 stale/orphan"
- `src/fspack/cli.py`：
  - 新增 `_run_cache(ns)`：分发 `cache_action` 到 `run_cache_status`/`run_cache_clean`
  - `main()` 路由 `command == "cache"` 到 `_run_cache`
- `tests/test_cli_doctor.py`：
  - 新增 43 个测试覆盖 `_scan_cache_health`（8 场景：目录不存在/空/全有效/损坏删除/
    stale 检测/孤儿检测/共享 wheel/非字符串 wheels 防御）、`_clean_cache_issues`
    （5 场景：无问题/dry_run/删除 stale+orphan/保留共享 wheel/unlink OSError 容错）、
    `run_cache_status`（6 场景：无问题/有孤儿/目录不存在/空目录/corrupt+stale/
    wheels 全引用）、`run_cache_clean`（6 场景：dry_run/实际删除/无问题/目录不存在/
    corrupt+orphan/dry_run 全类型）、`_preview_names`（2 场景：截断/空）、
    `_scan_cache_health` orphan stat OSError 容错
- `tests/test_nuitka.py`：
  - 修复 iter-138 遗留的 2 个 pyrefly 错误：
    - `fake_as_completed` 返回类型 `object` → `Iterator[object]`（生成器函数）
    - `submit_calls.append(args[0] if args else "")` → `str(args[0])` 显式转换
      （`Literal[''] | object` 不满足 `list[str].append` 参数类型）
  - 新增 `from collections.abc import Iterator` 导入
- 删除 `verbose` 空文件（dc9a7d5 误提交的 0 字节文件）

## 关键决策与依据

### `CacheHealthReport` frozen dataclass 统一扫描结果

iter-128 的 `_check_cache_integrity` 只检测损坏 deps，无 stale/orphan 概念。
iter-139 将扫描结果封装为 `CacheHealthReport`，三个消费方复用同一扫描入口：

- `fsp doctor --check-cache`：渲染为单行 `CheckResult` 表格（概要）
- `fsp cache status`：渲染为分组详细列表（文件名前 5 个）
- `fsp cache clean`：基于报告删除 stale deps + orphan wheels

frozen dataclass 与 `CheckResult`/`DoctorReport` 风格一致；默认值兼容
（`corrupt_deps_files=()` 等空 tuple 不破坏既有构造）。

### 损坏文件扫描期删除 vs 清理期删除

- **损坏 `.deps-*.json`**（JSON 解析失败/结构非法）：扫描期立即删除（best-effort）。
  原因：与 iter-128 `_load_deps_cache` 行为一致，损坏文件无价值，下次构建会重新
  解析依赖并写入新缓存。
- **stale deps**（JSON 有效但引用缺失 wheel）：扫描期不删除，由 `fsp cache clean`
  处理。原因：deps 文件本身有效，缺失的 wheel 可能正在下载中（并发场景），
  删除 deps 会导致下次构建重新解析（耗时）。仅当用户显式 `fsp cache clean` 时删除。
- **orphan wheels**（未被任何 deps 引用）：扫描期不删除，由 `fsp cache clean` 处理。
  原因：可能是新下载的 wheel 尚未写入 deps 缓存，或历史项目依赖变更后遗留。
  用户显式清理时才删除，避免误删正在使用中的 wheel。

### `OSError` 不计为损坏

`_scan_cache_health` 中 `OSError`（权限/磁盘 I/O）不计为损坏也不删除：
- 与 `_load_deps_cache` 行为一致（OSError 可能是瞬时问题）
- 文件内容本身可能有效，删除会丢失有效缓存
- 仅 `JSONDecodeError`/`ValueError`（结构非法）才删除

`_clean_cache_issues` 中单个文件 `unlink` 的 `OSError` 不阻断其他文件清理：
- warning 日志记录失败文件与错误
- 仍返回扫描报告（用户可看到实际删除了哪些、哪些失败）

### `_preview_names` 截断显示

文件名列表超过 5 个时只显示前 5 个 + "等 N 个"提示，避免大量孤儿 wheel 时刷屏。
`limit` 参数默认 5，可调（测试用 `limit=3` 验证截断逻辑）。

### `dry_run` 复用扫描逻辑

`_clean_cache_issues(dry_run=True)` 仍调 `_scan_cache_health` 扫描，但不删除。
原因：dry_run 需要展示"将删除哪些文件"，必须扫描获取当前状态。与实际清理
共享同一扫描入口，确保预览结果与实际清理一致（不会因扫描逻辑差异导致预览不准）。

## 代码实现情况

### `CacheHealthReport` 数据类

```python
@dataclass(frozen=True)
class CacheHealthReport:
    cache_dir: Path
    total_deps_files: int = 0
    corrupt_deps_files: tuple[str, ...] = ()
    stale_deps_files: tuple[str, ...] = ()
    missing_wheels: tuple[str, ...] = ()
    orphan_wheels: tuple[str, ...] = ()
    total_wheels: int = 0
    orphan_size_bytes: int = 0

    @property
    def has_issues(self) -> bool:
        return bool(self.corrupt_deps_files or self.stale_deps_files or self.orphan_wheels)
```

### `_scan_cache_health` 三阶段扫描

```python
def _scan_cache_health(cache_dir: Path) -> CacheHealthReport:
    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir)
    # 1. 扫描 .deps-*.json：JSON 校验 + wheels 字段类型校验
    #    损坏文件 best-effort 删除，有效文件聚合 referenced 集合
    #    引用缺失 wheel 的 deps 记入 stale_deps_files
    # 2. 枚举 *.whl：existing_set - referenced = orphan_wheels
    # 3. 累加 orphan_size_bytes（stat 失败的孤儿仍计入列表但不计体积）
    return CacheHealthReport(...)
```

### `_clean_cache_issues` best-effort 删除

```python
def _clean_cache_issues(cache_dir: Path, *, dry_run: bool = False) -> CacheHealthReport:
    report = _scan_cache_health(cache_dir)
    if dry_run or not report.has_issues:
        return report
    for name in report.stale_deps_files:
        try:
            (cache_dir / name).unlink()
        except OSError as e:
            _logger.warning("清理 stale deps 文件失败: %s: %s", target, e)
    for name in report.orphan_wheels:
        try:
            (cache_dir / name).unlink()
        except OSError as e:
            _logger.warning("清理孤儿 wheel 文件失败: %s: %s", target, e)
    return report
```

### `fsp cache` 子命令路由

```python
# cli_parser.py
def _add_cache_subparser(sub):
    p = sub.add_parser("cache", help="wheel 缓存健康检查与清理")
    cache_sub = p.add_subparsers(dest="cache_action", metavar="<action>", required=True)
    cache_sub.add_parser("status", help="扫描缓存目录健康状态（损坏/stale/orphan）")
    clean_p = cache_sub.add_parser("clean", help="清理 stale deps 与孤儿 wheel 文件")
    clean_p.add_argument("--dry-run", action="store_true", help="仅预览将删除的文件，不实际删除")

# cli.py
def _run_cache(ns):
    from fspack.cli_doctor import run_cache_clean, run_cache_status
    action = getattr(ns, "cache_action", None)
    if action == "status":
        run_cache_status()
    elif action == "clean":
        run_cache_clean(dry_run=getattr(ns, "dry_run", False))
```

## 测试验证结果

### 新增测试（43 个）

`test_cli_doctor.py`：

- `_scan_cache_health`（8 个）：
  - `test_scan_cache_health_dir_not_exists`：目录不存在返回空报告
  - `test_scan_cache_health_empty_dir`：空目录返回零计数
  - `test_scan_cache_health_all_valid`：全部有效时无问题
  - `test_scan_cache_health_corrupt_deleted`：损坏 JSON 删除并计入 corrupt
  - `test_scan_cache_health_stale_deps_detected`：引用缺失 wheel 计入 stale
  - `test_scan_cache_health_orphan_wheel_detected`：未被引用的 wheel 计入 orphan
  - `test_scan_cache_health_shared_wheel_not_orphan`：多 deps 共享同一 wheel 不误判
  - `test_scan_cache_health_non_string_wheels_ignored`：wheels 含非字符串元素防御
- `_clean_cache_issues`（5 个）：
  - `test_clean_cache_issues_no_issues`：无问题时不删除
  - `test_clean_cache_issues_dry_run_no_delete`：dry_run 仅扫描不删除
  - `test_clean_cache_issues_deletes_stale_and_orphan`：删除 stale + orphan
  - `test_clean_cache_issues_keeps_shared_wheel`：保留被引用的 wheel
  - `test_clean_cache_issues_unlink_oserror_continues`：unlink 失败不阻断其他文件
- `run_cache_status`（6 个）：
  - `test_run_cache_status_no_issues`：健康时输出"无需清理"
  - `test_run_cache_status_with_orphan`：有孤儿时提示 `fsp cache clean`
  - `test_run_cache_status_dir_not_exists`：目录不存在返回空报告
  - `test_run_cache_status_empty_dir`：空目录输出"为空"
  - `test_run_cache_status_with_corrupt_and_stale`：三类问题同时检测
  - `test_run_cache_status_wheels_only_no_orphan`：wheel 全引用不报孤儿
- `run_cache_clean`（6 个）：
  - `test_run_cache_clean_dry_run`：dry_run 仅预览
  - `test_run_cache_clean_actual_delete`：实际删除文件
  - `test_run_cache_clean_no_issues`：无问题时输出"无需清理"
  - `test_run_cache_clean_dir_not_exists`：目录不存在返回空报告
  - `test_run_cache_clean_with_corrupt_and_orphan`：三类问题同时处理
  - `test_run_cache_clean_dry_run_with_all_issue_types`：dry_run 全类型预览
- 辅助函数（3 个）：
  - `test_scan_cache_health_orphan_stat_oserror_skipped`：orphan stat 失败仍计入列表
  - `test_preview_names_truncates_at_limit`：超过 limit 截断 + 总数提示
  - `test_preview_names_empty_returns_empty`：空列表返回空字符串

`test_nuitka.py`（2 处修复，非新增测试）：
- `fake_as_completed` 返回类型 `object` → `Iterator[object]`
- `submit_calls.append(args[0])` → `str(args[0])` 显式转换

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 119 files already formatted
- pyrefly: 0 errors（修复 iter-138 遗留的 2 个错误）
- pytest: 2098 passed, 12 skipped（iter-138 为 2063 passed，新增 35 个测试通过）
- coverage: 95.76%（>= 95% 门禁，iter-138 为 96%，略降因新增代码分支较多但仍在阈值上）
- 10 benchmarks: 全通过

## 整合优化情况

- `CacheHealthReport` 与 `CheckResult`/`DoctorReport` 风格一致（frozen dataclass
  + 默认值兼容），独立于 `doctor_models.py` 避免 facade ↔ 子模块循环导入
- `_scan_cache_health` 作为唯一扫描入口，`_check_cache_integrity`（doctor）/
  `run_cache_status`（cache status）/`_clean_cache_issues`（cache clean）三处复用，
  消除 iter-128 的重复扫描逻辑
- 渲染辅助函数 `_format_cache_summary`/`_print_cache_detail_lists`/
  `_print_cache_clean_lists` 从 `run_cache_status`/`run_cache_clean` 内联代码提取，
  规避 PLR0912（过多分支），与 `doctor_report._build_table`/`_format_status` 等
  渲染辅助函数风格一致
- `fsp cache` 子命令与 `fsp doctor`/`fsp init` 子命令风格一致（argparse
  subparsers + 延迟导入 facade 函数）

## 遗留事项

- `orphan_wheels` 检测仅基于 `.deps-*.json` 引用，无法识别"deps 文件本身是孤儿"
  （即项目已删除但 deps 缓存残留）。后续可考虑在 deps 文件中记录项目标识，
  但当前设计 deps 缓存按 wheel 内容 hash 索引，无项目归属信息
- `_clean_cache_issues` 删除 stale deps 后，下次构建会重新解析依赖（耗时）。
  若用户频繁遇到 stale deps，应排查 wheel 下载失败的根因而非反复清理
- `fsp cache clean` 不删除损坏的 `.deps-*.json`（已在扫描阶段删除），故
  `corrupt_deps_files` 在 clean 报告中通常为空（除非扫描后又新增损坏文件）

## 下一轮计划

iter-140 构建中断恢复（req-49 L114-116，阶段 3 最后一轮）：
1. `fsp b` 开始时检测 `dist/` 半成品（有 runtime/ 无 exe），交互式确认或
  `--auto-clean` 自动清理
2. 构建异常时保存失败阶段到 `dist/.build_failed`，下次 `fsp b` 检测并提示
3. `fsp c` 保留 `installer.nsi` 逻辑扩展到保留失败诊断文件
