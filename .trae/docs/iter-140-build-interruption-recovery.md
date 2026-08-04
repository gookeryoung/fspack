# iter-140: 构建中断恢复

## 需求清单

- [x] `fsp b` 开始时检测 `dist/` 半成品（有 runtime/ 无 exe），`--auto-clean` 自动清理
- [x] 构建异常时保存失败阶段到 `dist/.build_failed`，下次 `fsp b` 检测并提示
- [x] `fsp c` 保留 `installer.nsi` 逻辑扩展到保留失败诊断文件 `.build_failed`

## 迭代目标

补齐 req-49 L114-116 列出的构建中断恢复三项任务（阶段 3 最后一轮）：
(1) `fsp b` 开始时检测 `dist/` 半成品——dist 含构建产物但缺少编译 stamp 文件
（`.pyc_stamp`/`.nuitka_compile_stamp`），或存在 `.build_failed` 标记时，按
`--auto-clean` 决定自动清理或告警；
(2) 构建异常时写入 `dist/.build_failed` JSON 记录失败阶段、错误信息与时间戳，
下次 `fsp b` 检测到时输出失败详情供用户定位；
(3) `fsp c` 清理时保留 `installer.nsi` 与 `.build_failed`（诊断文件），`--auto-clean`
则全清不保留（全新开始构建）。

## 改动文件清单

- `src/fspack/packaging/pipeline/__init__.py`：
  - 新增常量 `_BUILD_FAILED = ".build_failed"`、`_PYC_STAMP = ".pyc_stamp"`、
    `_NUITKA_STAMP = ".nuitka_compile_stamp"`
  - 新增 `_has_dist_artifacts(dist_dir)`：检测 dist 是否含构建产物（子目录或 .exe，
    排除 NSI/诊断文件）
  - 新增 `_has_build_stamps(dist_dir)`：检测 dist 是否含编译 stamp 文件
  - 新增 `_handle_dist_incomplete(dist_dir, auto_clean)`：替代 iter-128 的
    `_warn_dist_incomplete`，扩展支持 `.build_failed` 检测与 `--auto-clean` 自动清理
  - 新增 `_save_build_failure(dist_dir, tracker, exc)`：构建异常时写入
    `.build_failed` JSON（stage/error/timestamp），写入失败 best-effort
  - 新增 `_load_build_failure(dist_dir)`：读取 `.build_failed` JSON，文件不存在或
    解析失败返回 None
  - 新增 `_remove_build_failure(dist_dir)`：构建成功后删除 `.build_failed` 标记
  - 新增 `_clean_dist_dir(dist_dir, *, keep_diagnostics)`：清空 dist 并按
    `keep_diagnostics` 决定是否保留 `installer.nsi` 与 `.build_failed`
  - 重构 `clean_dist(project)`：复用 `_clean_dist_dir(keep_diagnostics=True)`
  - `build()` 函数：新增 `auto_clean: bool = False` 参数；构建前调
    `_handle_dist_incomplete`；`except Exception` 分支调 `_save_build_failure`；
    `else` 分支调 `_remove_build_failure`
- `src/fspack/cli_parser.py`：
  - `_add_build_subparser` 新增 `--auto-clean` 参数（`action="store_true"`）
- `src/fspack/cli.py`：
  - `build()` 调用新增 `auto_clean=getattr(ns, "auto_clean", False)`
- `tests/test_builder.py`：
  - 新增 22 个测试覆盖 `_handle_dist_incomplete`（11 场景：无 dist/空 dist/仅 NSI/
    有产物无 stamp 告警/有 pyc stamp 不告警/有 nuitka stamp 不告警/auto_clean 清理/
    auto_clean 保留 NSI/.build_failed 输出失败信息/.build_failed+auto_clean 删除/
    无产物但有 .build_failed 告警）、`_save_build_failure`（4 场景：正常写入 JSON/
    records 为空记"未知"/错误超 500 字符截断/dist 不存在跳过）、`_load_build_failure`
    （3 场景：正常读取/文件不存在返回 None/JSON 损坏返回 None）、`_remove_build_failure`
    （2 场景：删除文件/文件不存在 noop）、`_clean_dist_dir`（2 场景：keep_diagnostics
    保留 .build_failed/不保留则删除）、`clean_dist` 保留 .build_failed
  - 适配 6 个既有 `_warn_dist_incomplete` 测试为 `_handle_dist_incomplete`
  - 修复 `test_save_build_failure_writes_json`：`MagicMock(name=...)` 的 name 参数
    设置 repr 名称而非属性，改用 `SimpleNamespace(name=...)`
  - 适配 8 个测试文件的 `fake_build` 签名：新增 `auto_clean: bool = False` 参数
    （test_cli/test_build_dry_run/test_size_report/test_extras/test_log_file/
    test_profile/test_cli_recursive）

## 关键决策与依据

### 半成品检测条件

dist 视为"半成品"需满足以下任一：
1. **有构建产物但缺少编译 stamp**：`_has_dist_artifacts`（子目录或 .exe，排除 NSI/
   诊断文件）为 True 且 `_has_build_stamps`（`.pyc_stamp` 或 `.nuitka_compile_stamp`）
   为 False。原因：编译 stamp 在 `_compile_user_sources` 成功后写入，缺失说明构建
   在编译阶段前中断。
2. **存在 `.build_failed` 标记**：上次构建异常退出时写入，说明构建未成功完成。

仅"有产物且有 stamp"不视为半成品（可能是成功构建后用户手动添加文件）；
仅"无产物"也不视为半成品（空 dist 或仅 NSI，正常初始状态）。

### `--auto-clean` vs 告警

- `auto_clean=False`（默认）：仅 log warning 提示用户 `fsp c` 或 `fsp b --auto-clean`，
  继续构建。原因：用户可能有意保留 dist 中的文件（如手动修改的 NSI），不强制清理。
- `auto_clean=True`：调 `_clean_dist_dir(keep_diagnostics=False)` 全清 dist（保留
  `installer.nsi` 便于重新打包，但不保留 `.build_failed`——全新开始）。

### `.build_failed` JSON 格式

```json
{
  "stage": "下载依赖",
  "error": "DependencyError: numpy wheel 下载失败",
  "timestamp": "2026-08-04T15:30:00"
}
```

- `stage`：从 `tracker.records[-1].name` 取最后完成的阶段名；records 为空时记"未知"
- `error`：`{异常类型名}: {异常消息}`，截断到 500 字符避免文件过大
- `timestamp`：`datetime.now().isoformat(timespec="seconds")`，ISO 格式便于排序

### `_clean_dist_dir` 的 `keep_diagnostics` 设计

- `keep_diagnostics=True`（`fsp c`）：保留 `installer.nsi` + `.build_failed`。
  原因：NSIS 脚本便于改代码后 `fsp p --no-build` 重打包；`.build_failed` 便于用户
  排查上次构建失败原因。
- `keep_diagnostics=False`（`fsp b --auto-clean`）：仅保留 `installer.nsi`，不保留
  `.build_failed`。原因：用户选择自动清理即表示"全新开始"，无需保留上次失败信息。

实现方式：先读取需保留的文件内容到内存，`shutil.rmtree` 删除整个 dist 目录，
重建 dist 后写回保留的文件。避免逐文件删除的复杂度（dist 内文件结构复杂，含
runtime/src/site-packages 等多层目录）。

### `build()` 异常处理流程

```python
try:
    info = _execute_build(...)
except Exception as exc:
    _save_build_failure(dist, tracker, exc)  # 写入 .build_failed
    raise
else:
    _remove_build_failure(dist)  # 清除可能残留的 .build_failed
finally:
    teardown_log_file(log_wrapper)
```

- `except Exception` 而非 `BaseException`：`KeyboardInterrupt`/`SystemExit` 不写入
  `.build_failed`（用户主动中断，非构建逻辑失败）
- `else` 分支清除 `.build_failed`：防止"上次失败 → 手动修复后重新构建成功但
  `.build_failed` 残留"导致下次 `fsp b` 误报
- `_save_build_failure` 与 `_remove_build_failure` 均为 best-effort：写入/删除失败
  仅 log warning，不阻断构建流程

### `fake_build` 签名适配

iter-140 为 `build()` 新增 `auto_clean` 参数，CLI 层透传 `auto_clean=getattr(ns,
"auto_clean", False)`。8 个测试文件中的 `fake_build` mock 需同步新增
`auto_clean: bool = False` 参数，否则 `TypeError: fake_build() got an unexpected
keyword argument 'auto_clean'`。

`test_cli_recursive.py` 中 `fake_build(*args, **kwargs)` 形式的 mock 不需修改
（已接受任意参数）。

## 代码实现情况

### `_handle_dist_incomplete` 检测与清理

```python
def _handle_dist_incomplete(dist_dir: Path, auto_clean: bool) -> None:
    if not dist_dir.is_dir():
        return
    failed_info = _load_build_failure(dist_dir)
    has_artifacts = _has_dist_artifacts(dist_dir)
    has_stamps = _has_build_stamps(dist_dir)
    if failed_info:
        # 输出上次失败信息
        console.warn(f"上次构建失败（{timestamp}）：阶段 [{stage}]")
        console.rich.print(f"  错误: {error}")
    is_incomplete = (has_artifacts and not has_stamps) or failed_info is not None
    if not is_incomplete:
        return
    if auto_clean:
        _clean_dist_dir(dist_dir, keep_diagnostics=False)
    else:
        _logger.warning("dist 目录含上次构建的残留...")
```

### `_save_build_failure` 写入失败信息

```python
def _save_build_failure(dist_dir, tracker, exc):
    if not dist_dir.is_dir():
        return
    records = tracker.records
    stage = records[-1].name if records else "未知"
    error_msg = f"{type(exc).__name__}: {exc}"
    if len(error_msg) > 500:
        error_msg = error_msg[:497] + "..."
    data = {"stage": stage, "error": error_msg,
            "timestamp": datetime.now().isoformat(timespec="seconds")}
    try:
        (dist_dir / _BUILD_FAILED).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        _logger.warning("写入 .build_failed 失败: %s", e)
```

### `_clean_dist_dir` 保留诊断文件

```python
def _clean_dist_dir(dist_dir, *, keep_diagnostics):
    keep_names = [_KEEP_NSI]
    if keep_diagnostics:
        keep_names.append(_BUILD_FAILED)
    preserved = {}
    for name in keep_names:
        path = dist_dir / name
        if path.is_file():
            preserved[name] = path.read_text(encoding="utf-8")
    shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    for name, content in preserved.items():
        (dist_dir / name).write_text(content, encoding="utf-8")
```

### `--auto-clean` CLI 参数

```python
p.add_argument(
    "--auto-clean",
    action="store_true",
    help=(
        "构建前自动清理 dist 残留（含上次失败标记 .build_failed），"
        "无需手动 fsp c。检测到半成品时：无此标志则告警并继续（可能因残留文件失败），"
        "有此标志则清空 dist 后重新构建"
    ),
)
```

## 测试验证结果

### 新增测试（22 个）

`test_builder.py`：

- `_handle_dist_incomplete`（11 个）：
  - `test_handle_dist_incomplete_no_dist`：dist 不存在时不操作
  - `test_handle_dist_incomplete_empty_dist`：空 dist 不告警
  - `test_handle_dist_incomplete_only_nsi`：仅 NSI 不告警
  - `test_handle_dist_incomplete_artifacts_no_stamp_warns`：有产物无 stamp 告警
  - `test_handle_dist_incomplete_with_pyc_stamp_no_warn`：有 pyc stamp 不告警
  - `test_handle_dist_incomplete_with_nuitka_stamp_no_warn`：有 nuitka stamp 不告警
  - `test_handle_dist_incomplete_auto_clean_removes_artifacts`：auto_clean 清理产物
  - `test_handle_dist_incomplete_auto_clean_preserves_nsi`：auto_clean 保留 NSI
  - `test_handle_dist_incomplete_build_failed_shows_warning`：.build_failed 输出失败信息
  - `test_handle_dist_incomplete_build_failed_auto_clean_removes_it`：auto_clean 删除 .build_failed
  - `test_handle_dist_incomplete_no_artifacts_with_build_failed_warns`：无产物但有 .build_failed 告警
- `_save_build_failure`（4 个）：
  - `test_save_build_failure_writes_json`：正常写入 JSON 含 stage/error/timestamp
  - `test_save_build_failure_no_records_uses_unknown`：records 为空记"未知"
  - `test_save_build_failure_truncates_long_error`：错误超 500 字符截断
  - `test_save_build_failure_dist_not_exists_skips`：dist 不存在跳过
- `_load_build_failure`（3 个）：
  - `test_load_build_failure_returns_dict`：正常读取返回 dict
  - `test_load_build_failure_no_file_returns_none`：文件不存在返回 None
  - `test_load_build_failure_invalid_json_returns_none`：JSON 损坏返回 None
- `_remove_build_failure`（2 个）：
  - `test_remove_build_failure_deletes_file`：删除文件
  - `test_remove_build_failure_no_file_noop`：文件不存在 noop
- `_clean_dist_dir`（2 个）：
  - `test_clean_dist_dir_keeps_diagnostics_preserves_build_failed`：keep_diagnostics 保留 .build_failed
  - `test_clean_dist_dir_no_diagnostics_removes_build_failed`：不保留则删除
- `clean_dist`（1 个，既有函数扩展测试）：
  - `test_clean_dist_preserves_build_failed`：fsp c 保留 .build_failed

### 适配测试

- 6 个既有 `_warn_dist_incomplete` 测试适配为 `_handle_dist_incomplete`
- 8 个测试文件的 `fake_build` 签名新增 `auto_clean: bool = False`
- `test_save_build_failure_writes_json`：`MagicMock(name=...)` → `SimpleNamespace(name=...)`

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 119 files already formatted
- pyrefly: 0 errors
- pytest: 2105 passed, 12 skipped, 10 deselected（iter-139 为 2098 passed，新增 7 个净测试）
- coverage: 95.68%（>= 95% 门禁）

## 整合优化情况

- `_handle_dist_incomplete` 替代 iter-128 的 `_warn_dist_incomplete`，统一半成品
  检测 + `.build_failed` 检测 + auto_clean 清理三个职责到一个函数
- `_clean_dist_dir` 作为唯一清理入口，`clean_dist`（`fsp c`）与
  `_handle_dist_incomplete`（`--auto-clean`）复用，通过 `keep_diagnostics` 区分
  保留策略
- `.build_failed` JSON 格式与 `.deps-*.json`/`.nuitka_failed_files.json` 等缓存
  文件风格一致（UTF-8 + indent=2 + ensure_ascii=False）
- `--auto-clean` help 文本明确描述两种行为（无标志告警/有标志清理），与
  `--dry-run`/`--profile` 等布尔开关 help 风格一致

## 遗留事项

- `.build_failed` 仅记录最后一个完成的阶段名，不包含完整阶段历史。如需更详细的
  失败诊断，可扩展为记录所有已完成阶段列表（但会增加文件大小）
- `KeyboardInterrupt`/`SystemExit` 不写入 `.build_failed`（`except Exception` 不捕获
  `BaseException`），用户 Ctrl+C 中断后不会留下失败标记——这是有意设计（主动中断
  非构建失败），但用户可能困惑为何没有 `.build_failed`。可在文档中说明
- `_clean_dist_dir` 通过读取文件内容到内存再写回的方式保留文件，对于极大的
  `installer.nsi`（罕见）可能占用内存。当前 NSI 脚本典型 < 100KB，可接受

## 下一轮计划

iter-141 打包速度端到端基线（req-49 L122-124，阶段 4 第一轮）：
1. 新增 `tests/test_build_perf_baseline.py`：小项目（1 入口、3 依赖）冷/热缓存
   构建耗时基线
2. 中项目（10 入口、20 依赖）基线
3. 用 `pytest-benchmark` 的 `pedantic` 模式确保可复现
