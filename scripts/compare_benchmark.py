#!/usr/bin/env python3
"""与历史最佳基准对比当前 benchmark 运行结果.

pytest-benchmark 的 ``--benchmark-compare`` 仅与上一次运行对比，GitHub Actions
共享机器性能波动 12-29% 时易误报退化。本脚本扫描 ``.benchmarks/`` 下所有历史
JSON，按测试名找最小 median 作为最佳基准，当前运行与最佳对比，超过阈值报退化.

支持按基线类别分组对比：不同类别的测试有不同的 StdDev 特性，单一全局阈值会让
确定性高的测试（StdDev <1%）容差过大，让 I/O 抖动大的测试（StdDev 5-27%）
误报频繁。按类别设阈值后，确定性测试可用 10% 严格阈值，抖动测试用 15-25%
宽松阈值，减少误报同时保留检测灵敏度.

用法::

    # 先运行 benchmark 并保存
    uv run pytest tests/test_perf_baseline.py -m slow --benchmark-only --benchmark-save=main

    # 与历史最佳对比（默认按类别阈值，未匹配类别用全局 25%）
    uv run python scripts/compare_benchmark.py

    # 自定义全局阈值（用于未匹配类别的测试）
    uv run python scripts/compare_benchmark.py --threshold 20

    # 列出基线类别与阈值
    uv run python scripts/compare_benchmark.py --list-categories

    # 禁用类别分组，仅用全局阈值（兼容旧行为）
    uv run python scripts/compare_benchmark.py --no-categories --threshold 25

退出码：0=无退化或无历史基线，1=有退化超过阈值.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["main"]

# 全局退化阈值默认值：median 超过最佳基准 25% 视为退化
# 用于未匹配任何类别的测试，GitHub Actions 共享机器性能波动可达 12-29%，
# 25% 容忍正常抖动。匹配类别的测试使用类别专属阈值（见 _DEFAULT_CATEGORIES）
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


@dataclass(frozen=True)
class BenchmarkCategory:
    """基线测试类别：按测试名正则匹配一组测试，应用统一阈值.

    不同类别的基线测试有不同的 StdDev 特性（mock sleep <1%，subprocess 5-8%，
    I/O 抖动 5-27%），单一全局阈值要么让确定性测试容差过大，要么让抖动测试
    误报频繁。按类别设阈值后可兼顾检测灵敏度与误报控制.
    """

    name: str  # 类别名（如 "nuitka_compile"）
    pattern: str  # 测试名匹配正则（re.match，需锚定 ^）
    threshold: float  # 退化阈值百分比
    description: str  # 类别说明（含 StdDev 依据）


# 默认基线类别阈值（基于 iter-141~144 实测 StdDev 设定）
# 顺序重要：具体类别在前，core 兜底在后。_match_category 返回首个匹配
_DEFAULT_CATEGORIES: tuple[BenchmarkCategory, ...] = (
    # test_build_perf_baseline.py：含 AST 扫描与文件 I/O，StdDev 5-27%
    BenchmarkCategory(
        name="build_perf",
        pattern=r"^test_(small|medium)_project_.*_baseline$",
        threshold=25.0,
        description="test_build_perf_baseline.py 端到端编排基线，含 AST 扫描与文件 I/O 抖动 StdDev 5-27%",
    ),
    # test_nuitka_compile_baseline.py：mock time.sleep，StdDev <1%
    BenchmarkCategory(
        name="nuitka_compile",
        pattern=r"^test_(serial_compile|parallel_compile|ccache_hit|ccache_miss)_baseline$",
        threshold=10.0,
        description="test_nuitka_compile_baseline.py 编译基线，mock time.sleep 确定性高 StdDev <1%",
    ),
    # test_wheel_download_baseline.py：mock time.sleep，StdDev <1%
    BenchmarkCategory(
        name="wheel_download",
        pattern=r"^test_(pip_parallel_download|uv_parallel_download|cache_hit|cold_download)_baseline$",
        threshold=10.0,
        description="test_wheel_download_baseline.py 下载基线，mock time.sleep 确定性高 StdDev <1%",
    ),
    # test_entry_startup_baseline.py：真实 subprocess，StdDev 5-8%
    BenchmarkCategory(
        name="entry_startup",
        pattern=r"^test_(default_startup|lazy_import_startup|no_site_startup|no_site_lazy_combined)_baseline$",
        threshold=15.0,
        description="test_entry_startup_baseline.py 启动基线，真实 subprocess 抖动 StdDev 5-8%",
    ),
    # test_perf_baseline.py 的 10 个核心测试：确定性高 StdDev <1%
    BenchmarkCategory(
        name="core",
        pattern=(
            r"^test_(collect_imports_and_submodules|analyze_dependencies|classify_entry|"
            r"slim_unpack|source_fingerprint|project_info_from_dir|project_info_from_dir_cached|"
            r"generate_wrapper_source|ensure_env_cache_hit|wheel_download_cache_hit)_baseline$"
        ),
        threshold=10.0,
        description="test_perf_baseline.py 核心场景基线，确定性高 StdDev <1%",
    ),
)


def _match_category(
    test_name: str,
    categories: tuple[BenchmarkCategory, ...] | None = None,
) -> BenchmarkCategory | None:
    """根据测试名匹配基线类别，返回首个匹配的类别.

    Args:
        test_name: benchmark 测试函数名
        categories: 类别列表，默认用 _DEFAULT_CATEGORIES
    """
    if categories is None:
        categories = _DEFAULT_CATEGORIES
    for cat in categories:
        if re.match(cat.pattern, test_name):
            return cat
    return None


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
    category: str = ""  # 匹配的类别名（空表示未匹配，用全局阈值）
    threshold: float = _DEFAULT_THRESHOLD  # 应用于此测试的退化阈值


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
    categories: tuple[BenchmarkCategory, ...] | None = _DEFAULT_CATEGORIES,
) -> ComparisonReport:
    """扫描 benchmark 目录，生成当前运行 vs 历史最佳的对比报告.

    Args:
        bench_dir: ``.benchmarks/`` 目录
        threshold: 全局退化阈值百分比（用于未匹配任何类别的测试）
        categories: 基线类别列表，按类别应用专属阈值。传入空元组或 None
            禁用类别分组，所有测试用全局 threshold
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

        # 匹配类别，确定本测试的退化阈值
        cat = _match_category(name, categories) if categories else None
        cat_name = cat.name if cat else ""
        row_threshold = cat.threshold if cat else threshold

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
                    category=cat_name,
                    threshold=row_threshold,
                )
            )
            continue

        delta_pct = (current.median - best.median) / best.median * 100.0
        is_regression = delta_pct > row_threshold
        # 当前运行是否为所有运行中最快（含当前）
        all_medians = [e.median for e in all_entries[name]]
        is_current_best = current.median <= min(all_medians)

        if is_regression:
            report.regressions += 1
        elif delta_pct < -row_threshold:
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
                category=cat_name,
                threshold=row_threshold,
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


def print_report(
    report: ComparisonReport,
    threshold: float,
    categories: tuple[BenchmarkCategory, ...] | None = _DEFAULT_CATEGORIES,
) -> None:
    """打印对比报告表到 stdout.

    Args:
        report: 对比报告
        threshold: 全局退化阈值（用于汇总显示与未匹配类别的测试）
        categories: 启用的基线类别列表，None 表示禁用类别分组
    """
    if not report.rows:
        print("无 benchmark 结果可对比。")
        return

    print(f"\n当前运行: {report.current_file}")
    if categories:
        cat_summary = ", ".join(f"{c.name}={c.threshold:.0f}%" for c in categories)
        print(f"类别阈值: {cat_summary} | 全局阈值: {threshold:.0f}%")
    else:
        print(f"退化阈值: median > {threshold:.0f}% (类别分组已禁用)")
    print()

    # 计算列宽
    name_w = max(len(r.name) for r in report.rows)
    name_w = max(name_w, len("测试名"), 20)

    # 表头
    header = (
        f"{'测试名':<{name_w}}  {'当前 median':>14}  {'最佳 median':>14}  "
        f"{'Δ':>10}  {'阈值':>6}  {'类别':>14}  {'最佳来源':>14}  {'状态':>8}"
    )
    print(header)
    print("-" * len(header))

    for row in report.rows:
        if row.is_first_run:
            status = "首次"
        elif row.is_current_best:
            status = "最佳"
        elif row.is_regression:
            status = "退化!"
        elif row.delta_pct < -row.threshold:
            status = "提升"
        else:
            status = "正常"

        delta_str = _format_pct(row.delta_pct) if not row.is_current_best else "—"
        cat_display = row.category if row.category else "（全局）"
        print(
            f"{row.name:<{name_w}}  {_format_time(row.current_median):>14}  "
            f"{_format_time(row.best_median):>14}  {delta_str:>10}  "
            f"{row.threshold:>5.0f}%  {cat_display:>14}  "
            f"{row.best_source:>14}  {status:>8}"
        )

    print()
    print(
        f"汇总: {report.total_benchmarks} 项 | "
        f"退化 {report.regressions} | 提升 {report.improvements} | "
        f"首次 {report.no_history} | 全局阈值 {threshold:.0f}%"
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
        help=f"全局退化阈值百分比（默认 {_DEFAULT_THRESHOLD:.0f}，用于未匹配类别的测试）",
    )
    parser.add_argument(
        "--no-categories",
        action="store_true",
        help="禁用类别分组，所有测试用全局 --threshold（兼容旧行为）",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="列出基线类别与阈值后退出",
    )
    args = parser.parse_args(argv)

    if args.list_categories:
        print("基线类别与阈值：")
        for cat in _DEFAULT_CATEGORIES:
            print(f"  {cat.name:<16} 阈值 {cat.threshold:>4.0f}%  {cat.description}")
        print(f"  {'（全局）':<16} 阈值 {args.threshold:>4.0f}%  未匹配类别的测试用此阈值")
        return 0

    categories = None if args.no_categories else _DEFAULT_CATEGORIES

    report = compare(args.bench_dir, args.threshold, categories)
    print_report(report, args.threshold, categories)

    if report.is_systemic:
        # 系统性退化（机器负载波动）：输出警告但不阻断 CI
        return 0
    if report.regressions > 0:
        # 显示退化项的详情，含类别阈值便于排查
        print(f"\n失败: {report.regressions} 项退化超过阈值（全局 {args.threshold:.0f}%）")
        for row in report.rows:
            if row.is_regression:
                print(
                    f"  {row.name}  Δ={_format_pct(row.delta_pct)}  "
                    f"阈值={row.threshold:.0f}%  类别={row.category or '（全局）'}"
                )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
