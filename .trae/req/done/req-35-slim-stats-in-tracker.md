# 精简统计纳入 BuildTracker 汇总表

## 背景

当前 wheel 精简统计通过 `_logger.info("精简 %s: 剥离 N 个文件，节省 X.YMB / Y.YMB (Z%)")` 输出，存在两个问题：

1. **散落难汇总**：每个 wheel 单独输出一行，用户需手动累加才能知道总节省量。RimSort 项目含 PySide6/numpy/scipy/matplotlib 等多个 wheel，日志有 4-5 行精简统计，难以一眼看出总节省量。
2. **不直观**：汇总表是用户构建完成后首先看到的，但"解压 wheel(精简)"阶段仅在"备注"列显示"N wheels 解压"，未体现精简价值（节省的 MB 数）。

## 需求清单

- [x] `StageRecord` 新增 `bytes_saved: int = 0` 字段表示精简节省字节数
- [x] `StageRecorder` 新增 `add_saved_bytes(n)` 方法与 `_saved` slot 累加节省字节数
- [x] `BuildTracker.summary()` 在汇总表添加"节省"列，仅在该阶段有节省字节数时显示
- [x] `_slim_extract` 返回 `int`（skipped_bytes），保留 INFO 日志输出（逐 wheel 明细）
- [x] `_unpack_one_wheel` 返回 `int`（透传 `_slim_extract` 返回值；全量解压分支返回 0）
- [x] `slim_unpack` 在 `iter_with_progress` 循环中累加各 wheel 节省字节数到 `stage.add_saved_bytes()`
- [x] 测试覆盖：`StageRecorder.add_saved_bytes` 累加、`summary` 表"节省"列显示、`slim_unpack` 透传节省字节数
- [x] 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- 构建汇总表"解压 wheel(精简)"行"节省"列显示总节省字节数（如 "45.2MB"）
- 无精简（全量解压或无可剥离文件）时"节省"列显示 "-"，不显示误导性的 "0B"
- 保留现有 INFO 日志输出（逐 wheel 明细，便于调试单个 wheel 精简效果）
- 不破坏现有 `slim_unpack`/`_slim_extract`/`_unpack_one_wheel` 测试
- 覆盖率不下降
