# iter-49 精简统计纳入 BuildTracker 汇总表

## 需求清单

- [x] `StageRecord` 新增 `bytes_saved: int = 0` 字段表示精简节省字节数
- [x] `StageRecorder` 新增 `add_saved_bytes(n)` 方法与 `_saved` slot 累加节省字节数
- [x] `BuildTracker.summary()` 在汇总表添加"节省"列
- [x] `_slim_extract` 返回 `int`（skipped_bytes），保留 INFO 日志输出
- [x] `_unpack_one_wheel` 返回 `int`（透传 `_slim_extract` 返回值；全量解压分支返回 0）
- [x] `slim_unpack` 在 `iter_with_progress` 循环中累加各 wheel 节省字节数到 `stage.add_saved_bytes()`
- [x] 测试覆盖：`StageRecorder.add_saved_bytes` 累加、`summary` 表"节省"列显示、`slim_unpack` 透传节省字节数
- [x] 全套门禁通过

## 迭代目标

将 wheel 精简统计从散落的 INFO 日志提升到构建汇总表，让用户构建完成后一眼看到总节省量。

## 改动文件清单

- [src/fspack/progress.py](../../src/fspack/progress.py)
  - `StageRecord` 新增 `bytes_saved: int = 0` 字段
  - `StageRecorder` 新增 `_saved` slot、`add_saved_bytes(n)` 方法，`_finalize()` 透传 `bytes_saved`
  - `BuildTracker.summary()` 新增"节省"列，总计行累加所有阶段节省字节数
- [src/fspack/slim/base.py](../../src/fspack/slim/base.py)
  - `_slim_extract` 返回 `int`（skipped_bytes），保留 INFO 日志输出
  - `_unpack_one_wheel` 返回 `int`（透传 `_slim_extract` 返回值；全量解压分支返回 0）
  - `slim_unpack` 累加各 wheel 节省字节数到 `stage.add_saved_bytes()`
- [src/fspack/builder.py](../../src/fspack/builder.py)
  - 新增 `_dir_size(path)` 辅助函数递归计算目录总字节数
  - `_trim_stdlib` 剥离目录前调 `_dir_size` 统计，`stage.add_saved_bytes()` 累加
- [tests/test_progress.py](../../tests/test_progress.py)
  - `test_initial_state_starts_timer` 增加 `_saved == 0` 断言
  - 新增 `test_add_saved_bytes_accumulates`/`test_add_saved_bytes_ignores_non_positive`
  - `test_finalize_returns_immutable_record` 增加 `bytes_saved == 2048` 断言
  - 新增 `test_summary_table_shows_saved_bytes_column`/`test_summary_table_saved_column_dashes_when_zero`
    /`test_summary_table_total_saved_aggregates_across_stages`
- [tests/test_slim.py](../../tests/test_slim.py)
  - 新增 `test_stage_records_saved_bytes`/`test_stage_saved_bytes_zero_when_nothing_stripped`
    /`test_stage_saved_bytes_aggregates_multiple_wheels`
- [tests/test_builder.py](../../tests/test_builder.py)
  - 新增 `test_trim_stdlib_linux_records_saved_bytes`：Linux 剥离目录统计字节数
  - 更新 `test_trim_stdlib_windows_skips`/`test_trim_stdlib_missing_stdlib_skips`/
    `test_trim_stdlib_idempotent` 增加 `bytes_saved` 断言
  - 新增 `test_dir_size_empty_dir`/`test_dir_size_nested_files`：`_dir_size` 单元测试
- [docs/changelog.rst](../../docs/changelog.rst)：新增"节省列"条目

## 关键决策与依据

### 1. _slim_extract 返回 skipped_bytes 而非回调

方案 A：`_slim_extract` 接收 `stage` 参数直接调 `add_saved_bytes`。
方案 B：`_slim_extract` 返回 `int`，`slim_unpack` 累加。

选 B：`_slim_extract`/`_unpack_one_wheel` 不依赖 `StageRecorder`（slim 模块不依赖 progress 模块的运行时实例），
保持 slim 模块可独立测试（test_slim.py 中 `stage=None` 场景仍可工作）。
`slim_unpack` 作为编排层负责把返回值回填到 stage，职责清晰。

### 2. 全量解压分支返回 0

`_unpack_one_wheel` 中 `_detect_top_pkg` 返回 None 的兜底全量解压分支返回 `0`，
`_full_unpack`（wheel 文件名无法解析）不返回值（`slim_unpack` 中直接调用，不累加）。
这样"节省"列仅在真正精简解压时显示数值，全量解压显示 "-"，不误导用户。

### 3. 保留 INFO 日志输出

汇总表显示总节省量（聚合视图），INFO 日志显示逐 wheel 明细（调试视图）。
两者互补：用户日常看汇总表了解总效果，调试单个 wheel 精简问题时看 `-v` 日志。
不删除 INFO 日志避免破坏现有 `test_slim_stats_stripped_files_and_bytes` 测试。

### 4. "节省"列位置在"下载"与"项数"之间

汇总表列顺序：阶段 | 耗时 | 缓存 | 下载 | 节省 | 项数 | 跳过 | 备注
"节省"与"下载"相邻，都是字节数，便于对照（下载 X MB / 节省 Y MB）。
总计行同时累加 `bytes_downloaded` 与 `bytes_saved`，一眼看出总下载与总节省。

## 代码实现情况

### StageRecorder.add_saved_bytes

```python
def add_saved_bytes(self, n: int) -> None:
    """累加精简节省字节数（wheel 精简剥离的文件总大小）."""
    if n > 0:
        self._saved += n
```

### _slim_extract 返回 skipped_bytes

```python
def _slim_extract(...) -> int:
    skipped_bytes = 0
    for info in zf.infolist():
        ...
        if slim_rules.matches_exclude(info.filename):
            if not info.is_dir():
                skipped_files += 1
                skipped_bytes += info.file_size
            continue
        ...
    if skipped_files:
        _logger.info("精简 %s: 剥离 %d 个文件，节省 %.1fMB / %.1fMB (%.0f%%)", ...)
    return skipped_bytes
```

### slim_unpack 累加到 stage

```python
total_saved = 0
for whl in iter_with_progress(sorted_wheels, "解压 wheel", stage=stage):
    info = WheelInfo.from_filename(whl.name)
    if info is None:
        _full_unpack(whl, site_packages_dir)
    else:
        whl_pkg = normalize_name(info.name)
        total_saved += _unpack_one_wheel(whl, site_packages_dir, whl_pkg, merged, slim_rules)
    count += 1
if stage is not None and count:
    stage.set_detail(f"{count} wheels 解压")
    if total_saved:
        stage.add_saved_bytes(total_saved)
```

## 测试验证结果

- ruff check：All checks passed
- ruff format：47 files already formatted
- pyrefly check：0 errors
- pytest（非 slow）：981 passed（比 iter-48 多 13 个新测试）, 21 deselected
- 覆盖率：97.04%（progress.py 100%，slim/base.py 99%，builder.py 96%）

## 遗留事项

无。

## 下一轮计划

无。本次精简统计可视化闭环完成。
