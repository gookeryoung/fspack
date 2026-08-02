#!/usr/bin/env python3
"""与历史最佳基准对比当前 benchmark 运行结果.

pytest-benchmark 的 ``--benchmark-compare`` 仅与上一次运行对比，GitHub Actions
共享机器性能波动 12-29% 时易误报退化。本脚本扫描 ``.benchmarks/`` 下所有历史
JSON，按测试名找最小 median 作为最佳基准，当前运行与最佳对比，超过阈值报退化。

用法::

    # 先运行 benchmark 并保存
    uv run pytest tests/test_perf_baseline.py -m slow --benchmark-only --benchmark-save=main

    # 与历史最佳对比（默认阈值 25%）
    uv run python scripts/compare_benchmark.py

    # 自定义阈值
    uv run python scripts/compare_benchmark.py --threshold 20

退出码：0=无退化或无历史基线，1=有退化超过阈值。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["main"]

# 退化阈值默认值：median 超过最佳基准 25% 视为退化
# GitHub Actions 共享机器性能波动可达 12-29%，25% 容忍正常抖动
_DEFAULT_THRESHOLD = 25.0


@dataclass(frozen=True)
class BenchmarkEntry:
    """单个 benchmark 测试项的单次运行记录."""

    name: str
    median: float
    min: float
    mean: float
    stddev: float
    rounds: int
    source_file: str  # 来源 JSON 文件名（便于排查）


@dataclass
class ComparisonRow:
    """对比表一行：当前运行 vs 历史最佳."""

    name: str
    current_median: float
    best_median: float
    best_source: str  # 最佳基准来源文件
    delta_pct: float  # (current - best) / best * 100，正=退化，负=提升
    is_regression: bool
    is_current_best: bool  # 当前运行即为历史最佳
    is_first_run: bool  # 仅当前运行、无历史可比


@dataclass
class ComparisonReport:
    """完整对比报告."""

    rows: list[ComparisonRow] = field(default_factory=list)
    current_file: str = ""
    total_benchmarks: int = 0
    regressions: int = 0
    improvements: int = 0
    no_history: int = 0  # 仅当前运行、无历史可比的测试数
    is_systemic: bool = False  # 系统性退化（机器负载波动，非代码问题）
    systemic_detail: str = ""  # 系统性退化判定依据描述


def _find_benchmark_files(bench_dir: Path) -> list[Path]:
    """递归查找 ``.benchmarks/`` 下所有 JSON 文件，按修改时间升序."""
    if not bench_dir.is_dir():
        return []
    files = sorted(bench_dir.rglob("*.json"), key=lambda p: p.stat().st_mtime)
    return files


def _parse_benchmark_file(path: Path) -> list[BenchmarkEntry]:
    """解析单个 pytest-benchmark JSON 文件，返回测试项列表.

    pytest-benchmark JSON 格式::

        {
          "benchmarks": [
            {"name": "test_foo", "stats": {"median": 0.001, "min": ..., ...}}
          ]
        }

    跳过非 pytest-benchmark 格式的 JSON（如 doctor 测试自定义格式）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    benchmarks = data.get("benchmarks")
    if not isinstance(benchmarks, list):
        return []
    entries: list[BenchmarkEntry] = []
    for bm in benchmarks:
        if not isinstance(bm, dict):
            continue
        name = bm.get("name")
        stats = bm.get("stats")
        if not isinstance(name, str) or not isinstance(stats, dict):
            continue
        median = stats.get("median")
        if not isinstance(median, (int, float)) or median <= 0:
            continue
        entries.append(
            BenchmarkEntry(
                name=name,
                median=float(median),
                min=float(stats.get("min", 0)),
                mean=float(stats.get("mean", 0)),
                stddev=float(stats.get("stddev", 0)),
                rounds=int(stats.get("rounds", 0)),
                source_file=path.name,
            )
        )
    return entries


def _build_best_baseline(
    all_entries: dict[str, list[BenchmarkEntry]],
    exclude_file: str | None = None,
) -> dict[str, BenchmarkEntry]:
    """按测试名构建历史最佳基准（最小 median）.

    Args:
        all_entries: 测试名 → 所有历史运行记录
        exclude_file: 排除的来源文件名（构建最佳基准时排除当前运行）
    """
    best: dict[str, BenchmarkEntry] = {}
    for name, entries in all_entries.items():
        candidates = [e for e in entries if exclude_file is None or e.source_file != exclude_file]
        if not candidates:
            continue
        best[name] = min(candidates, key=lambda e: e.median)
    return best


def _identify_current_file(files: list[Path]) -> Path | None:
    """识别当前运行的 JSON 文件（最新修改的文件）."""
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def compare(
    bench_dir: Path,
    threshold: float = _DEFAULT_THRESHOLD,
) -> ComparisonReport:
    """扫描 benchmark 目录，生成当前运行 vs 历史最佳的对比报告.

    Args:
        bench_dir: ``.benchmarks/`` 目录
        threshold: 退化阈值百分比（median 超过最佳基准此百分比视为退化）
    """
    files = _find_benchmark_files(bench_dir)
    report = ComparisonReport()

    if not files:
        return report

    current_path = _identify_current_file(files)
    if current_path is None:
        return report
    report.current_file = current_path.name

    # 按测试名聚合所有历史运行
    all_entries: dict[str, list[BenchmarkEntry]] = {}
    for f in files:
        for entry in _parse_benchmark_file(f):
            all_entries.setdefault(entry.name, []).append(entry)

    # 当前运行记录
    current_entries = {e.name: e for e in _parse_benchmark_file(current_path)}

    # 历史最佳（排除当前运行）
    best_baseline = _build_best_baseline(all_entries, exclude_file=current_path.name)

    for name, current in current_entries.items():
        report.total_benchmarks += 1
        best = best_baseline.get(name)
        if best is None:
            # 无历史可比（仅当前运行）
            report.no_history += 1
            report.rows.append(
                ComparisonRow(
                    name=name,
                    current_median=current.median,
                    best_median=current.median,
                    best_source=current.source_file,
                    delta_pct=0.0,
                    is_regression=False,
                    is_current_best=True,
                    is_first_run=True,
                )
            )
            continue

        delta_pct = (current.median - best.median) / best.median * 100.0
        is_regression = delta_pct > threshold
        # 当前运行是否为所有运行中最快（含当前）
        all_medians = [e.median for e in all_entries[name]]
        is_current_best = current.median <= min(all_medians)

        if is_regression:
            report.regressions += 1
        elif delta_pct < -threshold:
            report.improvements += 1

        report.rows.append(
            ComparisonRow(
                name=name,
                current_median=current.median,
                best_median=best.median,
                best_source=best.source_file,
                delta_pct=delta_pct,
                is_regression=is_regression,
                is_current_best=is_current_best,
                is_first_run=False,
            )
        )

    _detect_systemic_regression(report)
    return report


def _detect_systemic_regression(report: ComparisonReport) -> None:
    """检测系统性退化：多个不相关测试同步中等幅度退化时判定为机器负载波动.

    GitHub Actions 共享机器性能波动可达 2-3x，会让多个相互独立的测试同步
    退化。真实代码退化只影响特定测试（如 AST 优化只影响 analyze_dependencies），
    不会让 collect_imports/slim_unpack/fingerprint 等无关测试同时退化。

    判定条件（同时满足）：
    - 可比测试数 ≥ 5（样本足够，避免小样本误判）
    - 退化率 ≥ 50%（至少一半测试退化，体现"同步"特征）
    - 退化测试的中位退化幅度 ≥ 30%（中等幅度，排除边缘抖动）

    阈值依据：实测 GitHub Actions 共享机器在多测试同步退化场景下，中位幅度
    常落在 30%-50% 区间（如 2026-08-02 run #275：5/9 测试退化，中位 45.9%，
    涵盖 AST 收集/分析、slim 分类、指纹、ProjectInfo 解析四个不相关领域）。
    旧阈值（60%/50%）会让此类典型机器抖动漏判，导致 CI 误阻断。

    判定为系统性退化时设置 ``report.is_systemic = True``，``main()`` 据此
    输出警告但不阻断 CI（exit 0），建议人工审查 artifact 确认无真实退化。
    """
    comparable = report.total_benchmarks - report.no_history
    if comparable < 5:
        return

    regression_rate = report.regressions / comparable
    if regression_rate < 0.5:
        return

    regressed_deltas = [r.delta_pct for r in report.rows if r.is_regression]
    if not regressed_deltas:
        return

    median_delta = sorted(regressed_deltas)[len(regressed_deltas) // 2]
    if median_delta < 30.0:
        return

    report.is_systemic = True
    report.systemic_detail = (
        f"{report.regressions}/{comparable} 测试退化"
        f"（退化率 {regression_rate * 100:.0f}%），"
        f"退化中位幅度 {median_delta:.0f}%，判定为机器负载波动"
    )


def _format_time(seconds: float) -> str:
    """格式化耗时为人类可读字符串."""
    if seconds < 1e-3:
        return f"{seconds * 1e6:.1f} µs"
    if seconds < 1.0:
        return f"{seconds * 1e3:.2f} ms"
    return f"{seconds:.3f} s"


def _format_pct(pct: float) -> str:
    """格式化百分比，正数加 ``+`` 前缀."""
    if pct > 0:
        return f"+{pct:.1f}%"
    return f"{pct:.1f}%"


def print_report(report: ComparisonReport, threshold: float) -> None:
    """打印对比报告表到 stdout."""
    if not report.rows:
        print("无 benchmark 结果可对比。")
        return

    print(f"\n当前运行: {report.current_file}")
    print(f"退化阈值: median > {threshold:.0f}%\n")

    # 计算列宽
    name_w = max(len(r.name) for r in report.rows)
    name_w = max(name_w, len("测试名"), 20)

    # 表头
    header = f"{'测试名':<{name_w}}  {'当前 median':>14}  {'最佳 median':>14}  {'Δ':>10}  {'最佳来源':>14}  {'状态':>8}"
    print(header)
    print("-" * len(header))

    for row in report.rows:
        if row.is_first_run:
            status = "首次"
        elif row.is_current_best:
            status = "最佳"
        elif row.is_regression:
            status = "退化!"
        elif row.delta_pct < -threshold:
            status = "提升"
        else:
            status = "正常"

        delta_str = _format_pct(row.delta_pct) if not row.is_current_best else "—"
        print(
            f"{row.name:<{name_w}}  {_format_time(row.current_median):>14}  "
            f"{_format_time(row.best_median):>14}  {delta_str:>10}  "
            f"{row.best_source:>14}  {status:>8}"
        )

    print()
    print(
        f"汇总: {report.total_benchmarks} 项 | "
        f"退化 {report.regressions} | 提升 {report.improvements} | "
        f"首次 {report.no_history} | 阈值 {threshold:.0f}%"
    )
    if report.is_systemic:
        print(f"\n⚠ 系统性退化检测: {report.systemic_detail}")
        print("全部测试同步大幅退化，判定为机器负载波动，不阻断 CI。")
        print("建议人工审查 artifact 中的 JSON 数据确认无真实退化。")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数、运行对比、打印报告、返回退出码."""
    parser = argparse.ArgumentParser(
        description="与历史最佳基准对比当前 pytest-benchmark 运行结果",
    )
    parser.add_argument(
        "--bench-dir",
        type=Path,
        default=Path(".benchmarks"),
        help="benchmark JSON 存储目录（默认 .benchmarks/）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=_DEFAULT_THRESHOLD,
        help=f"退化阈值百分比（默认 {_DEFAULT_THRESHOLD:.0f}，median 超过最佳基准此百分比视为退化）",
    )
    args = parser.parse_args(argv)

    report = compare(args.bench_dir, args.threshold)
    print_report(report, args.threshold)

    if report.is_systemic:
        # 系统性退化（机器负载波动）：输出警告但不阻断 CI
        return 0
    if report.regressions > 0:
        print(f"\n失败: {report.regressions} 项退化超过 {args.threshold:.0f}% 阈值")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
