# 构建产物统计表

## 背景

当前构建完成后仅输出 `BuildTracker.summary()` 阶段汇总表（阶段耗时/下载/节省/项数），
用户无法直观看到最终产物（dist 目录）的体积分布——哪个部分占大头（runtime/src/exe）、
精简节省相对产物总大小的比例。需增加"构建产物统计"表，让用户一眼看到：

1. dist 总大小（最终发布包体积，用户最关心的指标）
2. 各子目录大小（runtime/src/build）
3. 可执行文件大小（入口 .exe/ELF）
4. 精简节省（从 tracker.records 累加 bytes_saved，与产物总大小对照）

## 需求清单

- [x] progress.py 导出 `fmt_bytes` 公开函数（原 `_fmt_bytes` 改为 `fmt_bytes`）
- [x] builder.py 新增 `_print_artifacts_stats(tracker, dist_dir, exes)` 输出构建产物统计表
- [x] 统计 dist 总大小、runtime、src、build、可执行文件大小
- [x] 精简节省从 tracker.records 累加 bytes_saved（不重新计算）
- [x] 在 `build()` 末尾 `summary()` 之后调用
- [x] 计算目录大小在 spinner 中执行（避免阻塞 UI）
- [x] 测试覆盖 `_print_artifacts_stats` 各场景
- [x] 全套门禁通过

## 验收标准

- 构建完成后输出"构建产物统计"表，含 dist 总大小、各子目录大小、可执行文件大小、精简节省
- 表格用 Rich Table 渲染，与 summary() 风格一致
- 目录大小计算不阻塞 UI（spinner 提示）
- 精简节省与 summary() 表的"节省"列总计一致
- 覆盖率不下降
