# iter-50 构建产物统计表

## 需求清单

- [x] progress.py 导出 `fmt_bytes` 公开函数
- [x] builder.py 新增 `_print_artifacts_stats` 函数
- [x] build() 末尾调用统计输出
- [x] 测试覆盖
- [x] 全套门禁通过

## 迭代目标

在构建汇总表后增加"构建产物统计"表，让用户一眼看到 dist 总大小、各子目录大小、
可执行文件大小与精简节省汇总，直观评估产物体积分布与精简效果。

## 改动文件清单

- [src/fspack/progress.py](../../src/fspack/progress.py)
  - `_fmt_bytes` 重命名为 `fmt_bytes`（公开），加入 `__all__`
  - `BuildTracker.summary()` 内部引用更新为 `fmt_bytes`
- [src/fspack/builder.py](../../src/fspack/builder.py)
  - 导入 `Table` 与 `fmt_bytes`
  - 新增 [_print_artifacts_stats](../../src/fspack/builder.py#L174-L201) 函数
  - [build()](../../src/fspack/builder.py#L554-L555) 末尾 `summary()` 后调用
- [tests/test_progress.py](../../tests/test_progress.py)
  - 导入更新：`_fmt_bytes` → `fmt_bytes`
  - `TestFmtBytes` 类引用更新
- [tests/test_builder.py](../../tests/test_builder.py)
  - 导入新增 `_dir_size`/`_print_artifacts_stats`/`BuildTracker`
  - 新增 6 个 `_print_artifacts_stats` 测试：正常输出/精简节省显示/无节省省略/
    子目录缺失/exe 缺失/dist 目录缺失
- [docs/changelog.rst](../../docs/changelog.rst)：新增"构建产物统计表"条目

## 关键决策与依据

### 1. 精简节省从 tracker.records 累加而非重新计算

方案 A：在 `_print_artifacts_stats` 中重新统计未剥离的体积 vs 原始体积。
方案 B：从 `tracker.records` 累加 `bytes_saved`。

选 B：精简节省数据已在 wheel 精简与标准库精简阶段记录到 tracker，
重新计算需要保留原始 wheel 副本（不现实）。从 records 累加与 summary() 表
"节省"列总计一致，单一数据源避免不一致。

### 2. 目录大小计算在 spinner 中执行

dist 目录可能含数千文件（PySide6 wheel 解压后 2000+ 文件），递归 stat 可能耗时
数百毫秒。在 `with spinner("统计产物大小")` 中执行避免 UI 卡顿。

### 3. 表格用树状字符表示层级

```
| dist 总大小    | 125.4MB |
| ├ runtime     | 45.2MB  |
| ├ src         | 2.1MB   |
| ├ build       | 0.5MB   |
| └ 可执行文件   | 1.5MB   |
| 精简节省       | 68.3MB  |
```

`├`/`└` 字符直观表达 runtime/src/build/exe 是 dist 的子项，精简节省独立一行
（非 dist 子项，是"避免的体积"）。Rich Table 自动处理中文字符宽度对齐。

### 4. fmt_bytes 改为公开函数

builder.py 需要格式化字节数，原 `_fmt_bytes` 是私有。改为公开 `fmt_bytes` 并
加入 `__all__`，让 builder.py 复用格式化逻辑（KB/MB/GB 单位切换），避免重复实现。

### 5. 无精简节省时省略"精简节省"行

`if saved_total:` 判断，无节省时不显示该行（避免显示 "0B" 误导用户认为有精简）。
这与 summary() 表"节省"列用 "-" 表示 0 的逻辑一致。

## 代码实现情况

### _print_artifacts_stats

```python
def _print_artifacts_stats(tracker: BuildTracker, dist_dir: Path, exes: list[Path]) -> None:
    with spinner("统计产物大小"):
        dist_total = _dir_size(dist_dir) if dist_dir.is_dir() else 0
        runtime_size = _dir_size(dist_dir / "runtime") if (dist_dir / "runtime").is_dir() else 0
        src_size = _dir_size(dist_dir / "src") if (dist_dir / "src").is_dir() else 0
        build_size = _dir_size(dist_dir / "build") if (dist_dir / "build").is_dir() else 0
        exe_size = sum(e.stat().st_size for e in exes if e.is_file())
        saved_total = sum(r.bytes_saved for r in tracker.records)

    table = Table(title="构建产物统计", show_lines=False, title_style="bold green")
    table.add_column("项目", style="bold cyan", no_wrap=True)
    table.add_column("大小", justify="right")
    table.add_row("dist 总大小", fmt_bytes(dist_total))
    table.add_row("├ runtime", fmt_bytes(runtime_size))
    table.add_row("├ src", fmt_bytes(src_size))
    table.add_row("├ build", fmt_bytes(build_size))
    table.add_row("└ 可执行文件", fmt_bytes(exe_size))
    if saved_total:
        table.add_row("精简节省", fmt_bytes(saved_total), style="bold yellow")
    console.rich.print(table)
```

### build() 末尾调用

```python
console.rich.print(tracker.summary())
_print_artifacts_stats(tracker, cfg.dist_dir, exes)
```

## 测试验证结果

- ruff check：All checks passed
- ruff format：47 files already formatted
- pyrefly check：0 errors
- pytest（非 slow）：987 passed（比 iter-49 多 6 个新测试）, 21 deselected
- 覆盖率：97.06%（progress.py 100%，builder.py 96%）

## 遗留事项

无。

## 下一轮计划

无。构建产物统计表完成，与 iter-49 的"节省"列形成完整的精简效果可视化闭环。
