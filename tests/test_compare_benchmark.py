"""``scripts/compare_benchmark.py`` 单元测试.

CI benchmark gate 完全依赖此脚本的判定逻辑，零测试覆盖是盲点。本测试守护
核心判定路径：systemic 检测阈值、最佳基线构建、JSON 解析容错、退出码语义。

脚本不在 ``src/fspack`` 包内，用 :mod:`importlib.util` 按文件路径加载，
避免污染 ``sys.path``。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "compare_benchmark.py"
_spec = importlib.util.spec_from_file_location("_compare_benchmark", _SCRIPT)
assert _spec is not None and _spec.loader is not None
cb = importlib.util.module_from_spec(_spec)
# dataclass(frozen=True) 内部通过 sys.modules[cls.__module__] 解析类型，
# 必须先注册模块再 exec，否则 AttributeError: 'NoneType' has no __dict__
sys.modules["_compare_benchmark"] = cb
_spec.loader.exec_module(cb)


def _row(  # noqa: PLR0913
    name: str,
    current: float,
    best: float,
    *,
    is_regression: bool = False,
    is_first_run: bool = False,
    delta_pct: float | None = None,
) -> cb.ComparisonRow:
    """构造对比行，``delta_pct`` 默认按 current/best 计算."""
    if delta_pct is None:
        delta_pct = (current - best) / best * 100.0 if best > 0 else 0.0
    return cb.ComparisonRow(
        name=name,
        current_median=current,
        best_median=best,
        best_source="hist.json",
        delta_pct=delta_pct,
        is_regression=is_regression,
        is_current_best=False,
        is_first_run=is_first_run,
    )


def _report(
    rows: list[cb.ComparisonRow],
    *,
    no_history: int = 0,
    regressions: int | None = None,
) -> cb.ComparisonReport:
    """构造对比报告，``regressions`` 默认按 rows 中 is_regression 计数."""
    if regressions is None:
        regressions = sum(1 for r in rows if r.is_regression)
    return cb.ComparisonReport(
        rows=rows,
        current_file="cur.json",
        total_benchmarks=len(rows),
        regressions=regressions,
        improvements=0,
        no_history=no_history,
    )


class TestDetectSystemicRegression:
    """_detect_systemic_regression 阈值边界.

    阈值定义（2026-08-03 iter-124 调整后）：
    - 可比测试数 ≥ 5
    - 退化率 ≥ 50%
    - 退化中位幅度 ≥ 30%

    阈值依据：run #275 实测 5/9 退化 + 中位 45.9% 应触发 systemic。
    """

    def test_typical_ci_jitter_triggers_systemic(self) -> None:
        """5/9 退化 + 中位 45.9% 应触发（run #275 真实场景）."""
        # 5 个退化项：31.9, 32.2, 45.9, 49.3, 56.8（中位 45.9）
        # 4 个正常项
        rows = [
            _row("t1", 1.319, 1.0, is_regression=True, delta_pct=31.9),
            _row("t2", 1.322, 1.0, is_regression=True, delta_pct=32.2),
            _row("t3", 1.459, 1.0, is_regression=True, delta_pct=45.9),
            _row("t4", 1.493, 1.0, is_regression=True, delta_pct=49.3),
            _row("t5", 1.568, 1.0, is_regression=True, delta_pct=56.8),
            _row("t6", 1.10, 1.0, is_regression=False, delta_pct=10.0),
            _row("t7", 1.05, 1.0, is_regression=False, delta_pct=5.0),
            _row("t8", 1.20, 1.0, is_regression=False, delta_pct=20.0),
            _row("t9", 1.15, 1.0, is_regression=False, delta_pct=15.0),
        ]
        report = _report(rows)
        cb._detect_systemic_regression(report)
        assert report.is_systemic is True
        assert "5/9" in report.systemic_detail

    def test_low_regression_rate_does_not_trigger(self) -> None:
        """4/9 退化（退化率 44% < 50%）不触发."""
        rows = [
            _row("t1", 1.45, 1.0, is_regression=True, delta_pct=45.0),
            _row("t2", 1.45, 1.0, is_regression=True, delta_pct=45.0),
            _row("t3", 1.45, 1.0, is_regression=True, delta_pct=45.0),
            _row("t4", 1.45, 1.0, is_regression=True, delta_pct=45.0),
            _row("t5", 1.10, 1.0, is_regression=False, delta_pct=10.0),
            _row("t6", 1.05, 1.0, is_regression=False, delta_pct=5.0),
            _row("t7", 1.20, 1.0, is_regression=False, delta_pct=20.0),
            _row("t8", 1.15, 1.0, is_regression=False, delta_pct=15.0),
            _row("t9", 1.08, 1.0, is_regression=False, delta_pct=8.0),
        ]
        report = _report(rows)
        cb._detect_systemic_regression(report)
        assert report.is_systemic is False

    def test_low_median_delta_does_not_trigger(self) -> None:
        """5/9 退化但中位幅度 25% < 30% 不触发（边缘抖动）."""
        rows = [
            _row("t1", 1.20, 1.0, is_regression=True, delta_pct=20.0),
            _row("t2", 1.22, 1.0, is_regression=True, delta_pct=22.0),
            _row("t3", 1.25, 1.0, is_regression=True, delta_pct=25.0),  # 中位
            _row("t4", 1.28, 1.0, is_regression=True, delta_pct=28.0),
            _row("t5", 1.30, 1.0, is_regression=True, delta_pct=30.0),
            _row("t6", 1.10, 1.0, is_regression=False, delta_pct=10.0),
            _row("t7", 1.05, 1.0, is_regression=False, delta_pct=5.0),
            _row("t8", 1.20, 1.0, is_regression=False, delta_pct=20.0),
            _row("t9", 1.15, 1.0, is_regression=False, delta_pct=15.0),
        ]
        report = _report(rows)
        cb._detect_systemic_regression(report)
        assert report.is_systemic is False

    def test_insufficient_comparable_does_not_trigger(self) -> None:
        """可比测试数 < 5 不触发（样本太少）."""
        # 4 个可比，全部退化，中位 50%——仍不触发因 comparable < 5
        rows = [
            _row("t1", 1.50, 1.0, is_regression=True, delta_pct=50.0),
            _row("t2", 1.50, 1.0, is_regression=True, delta_pct=50.0),
            _row("t3", 1.50, 1.0, is_regression=True, delta_pct=50.0),
            _row("t4", 1.50, 1.0, is_regression=True, delta_pct=50.0),
        ]
        report = _report(rows)
        cb._detect_systemic_regression(report)
        assert report.is_systemic is False

    def test_exact_threshold_boundary_triggers(self) -> None:
        """边界值：comparable=5, 退化率=50%, 中位幅度=30% 触发."""
        # 5 个可比，3 个退化（3/5=60%≥50%），退化中位 30%
        # 退化项 delta: 28, 30, 40 → 中位 30
        rows = [
            _row("t1", 1.28, 1.0, is_regression=True, delta_pct=28.0),
            _row("t2", 1.30, 1.0, is_regression=True, delta_pct=30.0),
            _row("t3", 1.40, 1.0, is_regression=True, delta_pct=40.0),
            _row("t4", 1.10, 1.0, is_regression=False, delta_pct=10.0),
            _row("t5", 1.05, 1.0, is_regression=False, delta_pct=5.0),
        ]
        report = _report(rows)
        cb._detect_systemic_regression(report)
        assert report.is_systemic is True

    def test_no_history_excluded_from_comparable(self) -> None:
        """no_history 测试不计入 comparable 分母."""
        # 10 个 total，3 个 no_history → comparable=7
        # 4 个退化（4/7=57%≥50%），中位 40% → 触发
        rows_reg = [
            _row("t1", 1.40, 1.0, is_regression=True, delta_pct=40.0),
            _row("t2", 1.40, 1.0, is_regression=True, delta_pct=40.0),
            _row("t3", 1.40, 1.0, is_regression=True, delta_pct=40.0),
            _row("t4", 1.40, 1.0, is_regression=True, delta_pct=40.0),
        ]
        rows_ok = [
            _row("t5", 1.10, 1.0, is_regression=False, delta_pct=10.0),
            _row("t6", 1.05, 1.0, is_regression=False, delta_pct=5.0),
            _row("t7", 1.20, 1.0, is_regression=False, delta_pct=20.0),
        ]
        rows_first = [
            _row("t8", 1.0, 1.0, is_first_run=True, delta_pct=0.0),
            _row("t9", 1.0, 1.0, is_first_run=True, delta_pct=0.0),
            _row("t10", 1.0, 1.0, is_first_run=True, delta_pct=0.0),
        ]
        report = _report(rows_reg + rows_ok + rows_first, no_history=3)
        cb._detect_systemic_regression(report)
        assert report.is_systemic is True
        assert "4/7" in report.systemic_detail


class TestParseBenchmarkFile:
    """_parse_benchmark_file 格式容错."""

    def test_pytest_benchmark_format(self, tmp_path: Path) -> None:
        """标准 pytest-benchmark JSON 正确解析."""
        data = {
            "benchmarks": [
                {
                    "name": "test_foo",
                    "stats": {"median": 0.001, "min": 0.0009, "mean": 0.0011, "stddev": 0.0001, "rounds": 20},
                }
            ]
        }
        p = tmp_path / "foo.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        entries = cb._parse_benchmark_file(p)
        assert len(entries) == 1
        assert entries[0].name == "test_foo"
        assert entries[0].median == 0.001
        assert entries[0].rounds == 20
        assert entries[0].source_file == "foo.json"

    def test_doctor_custom_format_skipped(self, tmp_path: Path) -> None:
        """doctor 测试自定义 JSON 格式（results 而非 benchmarks）返回空."""
        data = {"results": [{"template_id": "x", "duration_sec": 0.5}]}
        p = tmp_path / "doctor.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        entries = cb._parse_benchmark_file(p)
        assert entries == []

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        """JSON 解析失败返回空列表."""
        p = tmp_path / "broken.json"
        p.write_text("{not valid json", encoding="utf-8")
        assert cb._parse_benchmark_file(p) == []

    def test_missing_benchmarks_key_returns_empty(self, tmp_path: Path) -> None:
        """缺失 benchmarks 键返回空."""
        p = tmp_path / "empty.json"
        p.write_text(json.dumps({"machine": {}}), encoding="utf-8")
        assert cb._parse_benchmark_file(p) == []

    def test_non_positive_median_skipped(self, tmp_path: Path) -> None:
        """median ≤ 0 的条目跳过."""
        data = {
            "benchmarks": [
                {"name": "a", "stats": {"median": 0}},
                {"name": "b", "stats": {"median": -1}},
                {"name": "c", "stats": {"median": 0.001}},
            ]
        }
        p = tmp_path / "mixed.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        entries = cb._parse_benchmark_file(p)
        assert [e.name for e in entries] == ["c"]


class TestBuildBestBaseline:
    """_build_best_baseline 历史最佳（最小 median）构建."""

    def test_picks_min_median_per_name(self) -> None:
        """每个测试名取最小 median 作为最佳."""
        all_entries: dict[str, list[cb.BenchmarkEntry]] = {
            "t1": [
                cb.BenchmarkEntry("t1", 0.5, 0.4, 0.5, 0.05, 20, "a.json"),
                cb.BenchmarkEntry("t1", 0.3, 0.25, 0.3, 0.02, 20, "b.json"),
                cb.BenchmarkEntry("t1", 0.4, 0.35, 0.4, 0.03, 20, "c.json"),
            ],
            "t2": [
                cb.BenchmarkEntry("t2", 0.01, 0.009, 0.01, 0.001, 20, "a.json"),
                cb.BenchmarkEntry("t2", 0.02, 0.018, 0.02, 0.002, 20, "b.json"),
            ],
        }
        best = cb._build_best_baseline(all_entries)
        assert best["t1"].median == 0.3
        assert best["t1"].source_file == "b.json"
        assert best["t2"].median == 0.01
        assert best["t2"].source_file == "a.json"

    def test_excludes_current_file(self) -> None:
        """排除当前运行文件后构建最佳基准."""
        all_entries: dict[str, list[cb.BenchmarkEntry]] = {
            "t1": [
                cb.BenchmarkEntry("t1", 0.5, 0.4, 0.5, 0.05, 20, "cur.json"),
                cb.BenchmarkEntry("t1", 0.3, 0.25, 0.3, 0.02, 20, "hist.json"),
            ],
        }
        best = cb._build_best_baseline(all_entries, exclude_file="cur.json")
        assert best["t1"].median == 0.3
        assert best["t1"].source_file == "hist.json"

    def test_only_current_returns_empty(self) -> None:
        """仅当前运行（无历史）时返回空基准."""
        all_entries: dict[str, list[cb.BenchmarkEntry]] = {
            "t1": [cb.BenchmarkEntry("t1", 0.5, 0.4, 0.5, 0.05, 20, "cur.json")],
        }
        best = cb._build_best_baseline(all_entries, exclude_file="cur.json")
        assert best == {}


class TestCompareEndToEnd:
    """compare 端到端：扫描目录生成报告."""

    def _write_bench_file(self, path: Path, entries: list[tuple[str, float]], mtime_offset: float = 0.0) -> None:
        """写一个 pytest-benchmark JSON 文件."""
        data: dict[str, Any] = {
            "benchmarks": [
                {"name": n, "stats": {"median": m, "min": m, "mean": m, "stddev": 0, "rounds": 20}} for n, m in entries
            ]
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        if mtime_offset:
            import os

            ts = path.stat().st_mtime + mtime_offset
            os.utime(path, (ts, ts))

    def test_single_file_all_no_history(self, tmp_path: Path) -> None:
        """仅当前运行，所有测试无历史可比."""
        self._write_bench_file(tmp_path / "001.json", [("t1", 0.1), ("t2", 0.2)])
        report = cb.compare(tmp_path)
        assert report.total_benchmarks == 2
        assert report.no_history == 2
        assert report.regressions == 0
        assert report.is_systemic is False

    def test_regression_detected(self, tmp_path: Path) -> None:
        """当前比历史慢 30% 触发退化."""
        self._write_bench_file(tmp_path / "001_hist.json", [("t1", 0.100)], mtime_offset=-100)
        self._write_bench_file(tmp_path / "002_cur.json", [("t1", 0.130)], mtime_offset=0)
        report = cb.compare(tmp_path, threshold=25.0)
        assert report.regressions == 1
        assert report.rows[0].is_regression is True
        assert report.rows[0].delta_pct == pytest.approx(30.0, abs=0.01)

    def test_no_regression_within_threshold(self, tmp_path: Path) -> None:
        """当前比历史慢 20% 但阈值 25% 不触发退化."""
        self._write_bench_file(tmp_path / "001_hist.json", [("t1", 0.100)], mtime_offset=-100)
        self._write_bench_file(tmp_path / "002_cur.json", [("t1", 0.120)], mtime_offset=0)
        report = cb.compare(tmp_path, threshold=25.0)
        assert report.regressions == 0
        assert report.rows[0].is_regression is False

    def test_systemic_jitter_not_blocking(self, tmp_path: Path) -> None:
        """5/9 测试同步退化 45% 中位幅度，触发 systemic 不计入退化阻断."""
        # 历史基线：9 个测试 median=1.0
        hist_entries = [(f"t{i}", 1.0) for i in range(9)]
        # 当前：5 个退化到 1.45（+45%），4 个保持 1.0
        cur_entries = [(f"t{i}", 1.45) for i in range(5)] + [(f"t{i}", 1.0) for i in range(5, 9)]
        self._write_bench_file(tmp_path / "001_hist.json", hist_entries, mtime_offset=-100)
        self._write_bench_file(tmp_path / "002_cur.json", cur_entries, mtime_offset=0)
        report = cb.compare(tmp_path, threshold=25.0)
        # systemic 检测应触发，regressions 仍计数但 main() 不阻断
        assert report.is_systemic is True
        assert report.regressions == 5


class TestMainExitCode:
    """main 退出码语义."""

    def test_no_files_exit_zero(self, tmp_path: Path) -> None:
        """无 benchmark 文件 exit 0."""
        empty = tmp_path / "empty"
        empty.mkdir()
        assert cb.main(["--bench-dir", str(empty)]) == 0

    def test_regression_exit_one(self, tmp_path: Path) -> None:
        """单测试退化超阈值 exit 1."""
        data_hist = {
            "benchmarks": [{"name": "t1", "stats": {"median": 0.1, "min": 0.1, "mean": 0.1, "stddev": 0, "rounds": 20}}]
        }
        data_cur = {
            "benchmarks": [{"name": "t1", "stats": {"median": 0.2, "min": 0.2, "mean": 0.2, "stddev": 0, "rounds": 20}}]
        }
        (tmp_path / "001.json").write_text(json.dumps(data_hist), encoding="utf-8")
        import os

        cur = tmp_path / "002.json"
        cur.write_text(json.dumps(data_cur), encoding="utf-8")
        ts = cur.stat().st_mtime + 100
        os.utime(cur, (ts, ts))
        assert cb.main(["--bench-dir", str(tmp_path), "--threshold", "25"]) == 1

    def test_systemic_exit_zero(self, tmp_path: Path) -> None:
        """systemic 退化 exit 0（机器抖动不阻断）."""
        hist_entries = [(f"t{i}", 1.0) for i in range(9)]
        cur_entries = [(f"t{i}", 1.45) for i in range(5)] + [(f"t{i}", 1.0) for i in range(5, 9)]
        (tmp_path / "001.json").write_text(
            json.dumps(
                {
                    "benchmarks": [
                        {"name": n, "stats": {"median": m, "min": m, "mean": m, "stddev": 0, "rounds": 20}}
                        for n, m in hist_entries
                    ]
                }
            ),
            encoding="utf-8",
        )
        import os

        cur = tmp_path / "002.json"
        cur.write_text(
            json.dumps(
                {
                    "benchmarks": [
                        {"name": n, "stats": {"median": m, "min": m, "mean": m, "stddev": 0, "rounds": 20}}
                        for n, m in cur_entries
                    ]
                }
            ),
            encoding="utf-8",
        )
        ts = cur.stat().st_mtime + 100
        os.utime(cur, (ts, ts))
        assert cb.main(["--bench-dir", str(tmp_path), "--threshold", "25"]) == 0


class TestMatchCategory:
    """_match_category 类别匹配."""

    def test_core_category_matched(self) -> None:
        """test_perf_baseline.py 的 10 个核心测试匹配 core 类别."""
        for name in (
            "test_collect_imports_and_submodules_baseline",
            "test_analyze_dependencies_baseline",
            "test_classify_entry_baseline",
            "test_slim_unpack_baseline",
            "test_source_fingerprint_baseline",
            "test_project_info_from_dir_baseline",
            "test_project_info_from_dir_cached_baseline",
            "test_generate_wrapper_source_baseline",
            "test_ensure_env_cache_hit_baseline",
            "test_wheel_download_cache_hit_baseline",
        ):
            cat = cb._match_category(name)
            assert cat is not None
            assert cat.name == "core"
            assert cat.threshold == 10.0

    def test_build_perf_category_matched(self) -> None:
        """test_build_perf_baseline.py 的 4 个测试匹配 build_perf 类别."""
        for name in (
            "test_small_project_cold_cache_baseline",
            "test_small_project_warm_cache_baseline",
            "test_medium_project_cold_cache_baseline",
            "test_medium_project_warm_cache_baseline",
        ):
            cat = cb._match_category(name)
            assert cat is not None
            assert cat.name == "build_perf"
            assert cat.threshold == 25.0

    def test_nuitka_compile_category_matched(self) -> None:
        """test_nuitka_compile_baseline.py 的 4 个测试匹配 nuitka_compile 类别."""
        for name in (
            "test_serial_compile_baseline",
            "test_parallel_compile_baseline",
            "test_ccache_hit_baseline",
            "test_ccache_miss_baseline",
        ):
            cat = cb._match_category(name)
            assert cat is not None
            assert cat.name == "nuitka_compile"
            assert cat.threshold == 10.0

    def test_wheel_download_category_matched(self) -> None:
        """test_wheel_download_baseline.py 的 4 个测试匹配 wheel_download 类别."""
        for name in (
            "test_pip_parallel_download_baseline",
            "test_uv_parallel_download_baseline",
            "test_cache_hit_baseline",
            "test_cold_download_baseline",
        ):
            cat = cb._match_category(name)
            assert cat is not None
            assert cat.name == "wheel_download"
            assert cat.threshold == 10.0

    def test_entry_startup_category_matched(self) -> None:
        """test_entry_startup_baseline.py 的 4 个测试匹配 entry_startup 类别."""
        for name in (
            "test_default_startup_baseline",
            "test_lazy_import_startup_baseline",
            "test_no_site_startup_baseline",
            "test_no_site_lazy_combined_baseline",
        ):
            cat = cb._match_category(name)
            assert cat is not None
            assert cat.name == "entry_startup"
            assert cat.threshold == 15.0

    def test_unknown_test_no_match(self) -> None:
        """未知测试名不匹配任何类别."""
        assert cb._match_category("test_unknown_baseline") is None
        assert cb._match_category("t1") is None

    def test_no_collision_between_cache_hit_tests(self) -> None:
        """test_cache_hit_baseline（wheel_download）与 test_wheel_download_cache_hit_baseline
        （core）分别匹配到不同类别，验证正则无歧义."""
        assert cb._match_category("test_cache_hit_baseline").name == "wheel_download"
        assert cb._match_category("test_wheel_download_cache_hit_baseline").name == "core"

    def test_custom_categories(self) -> None:
        """自定义类别列表覆盖默认."""
        custom = (
            cb.BenchmarkCategory(
                name="custom",
                pattern=r"^test_custom_.*",
                threshold=5.0,
                description="custom",
            ),
        )
        cat = cb._match_category("test_custom_foo", custom)
        assert cat is not None
        assert cat.name == "custom"
        # 不在自定义列表中的测试不匹配
        assert cb._match_category("test_serial_compile_baseline", custom) is None

    def test_empty_categories_returns_none(self) -> None:
        """空类别列表对任何测试都不匹配."""
        assert cb._match_category("test_anything", ()) is None


class TestCompareWithCategories:
    """compare 按类别阈值判定退化."""

    def _write_bench_file(self, path: Path, entries: list[tuple[str, float]], mtime_offset: float = 0.0) -> None:
        """写一个 pytest-benchmark JSON 文件."""
        data: dict[str, Any] = {
            "benchmarks": [
                {"name": n, "stats": {"median": m, "min": m, "mean": m, "stddev": 0, "rounds": 20}} for n, m in entries
            ]
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        if mtime_offset:
            import os

            ts = path.stat().st_mtime + mtime_offset
            os.utime(path, (ts, ts))

    def test_category_threshold_applied(self, tmp_path: Path) -> None:
        """nuitka_compile 测试退化 12% 触发退化（类别阈值 10%），全局阈值 25% 不触发."""
        # 历史：test_serial_compile_baseline median=0.5
        self._write_bench_file(
            tmp_path / "001_hist.json",
            [("test_serial_compile_baseline", 0.500)],
            mtime_offset=-100,
        )
        # 当前：median=0.56，退化 12%
        self._write_bench_file(
            tmp_path / "002_cur.json",
            [("test_serial_compile_baseline", 0.560)],
            mtime_offset=0,
        )
        # 用默认类别（nuitka_compile 阈值 10%）
        report = cb.compare(tmp_path, threshold=25.0)
        assert report.regressions == 1
        row = report.rows[0]
        assert row.is_regression is True
        assert row.category == "nuitka_compile"
        assert row.threshold == 10.0
        assert row.delta_pct == pytest.approx(12.0, abs=0.1)

    def test_category_threshold_not_triggered(self, tmp_path: Path) -> None:
        """nuitka_compile 测试退化 8% 不触发退化（类别阈值 10%）。"""
        self._write_bench_file(
            tmp_path / "001_hist.json",
            [("test_serial_compile_baseline", 0.500)],
            mtime_offset=-100,
        )
        self._write_bench_file(
            tmp_path / "002_cur.json",
            [("test_serial_compile_baseline", 0.540)],
            mtime_offset=0,
        )
        report = cb.compare(tmp_path, threshold=25.0)
        assert report.regressions == 0
        row = report.rows[0]
        assert row.is_regression is False
        assert row.category == "nuitka_compile"
        assert row.threshold == 10.0

    def test_build_perf_category_higher_threshold(self, tmp_path: Path) -> None:
        """build_perf 测试退化 20% 不触发退化（类别阈值 25%），即使全局阈值 10% 也不影响."""
        self._write_bench_file(
            tmp_path / "001_hist.json",
            [("test_small_project_cold_cache_baseline", 0.005)],
            mtime_offset=-100,
        )
        self._write_bench_file(
            tmp_path / "002_cur.json",
            [("test_small_project_cold_cache_baseline", 0.006)],
            mtime_offset=0,
        )
        # 全局阈值设为 10%，但 build_perf 类别阈值 25% 应优先
        report = cb.compare(tmp_path, threshold=10.0)
        assert report.regressions == 0
        row = report.rows[0]
        assert row.category == "build_perf"
        assert row.threshold == 25.0
        # 退化 20% < 25% 类别阈值
        assert row.delta_pct == pytest.approx(20.0, abs=0.1)
        assert row.is_regression is False

    def test_unmatched_test_uses_global_threshold(self, tmp_path: Path) -> None:
        """未匹配类别的测试用全局 threshold."""
        self._write_bench_file(tmp_path / "001_hist.json", [("test_unknown_baseline", 0.100)], mtime_offset=-100)
        self._write_bench_file(tmp_path / "002_cur.json", [("test_unknown_baseline", 0.130)], mtime_offset=0)
        # 全局阈值 25%，退化 30% 触发
        report = cb.compare(tmp_path, threshold=25.0)
        assert report.regressions == 1
        row = report.rows[0]
        assert row.category == ""
        assert row.threshold == 25.0
        assert row.is_regression is True

    def test_no_categories_disables_grouping(self, tmp_path: Path) -> None:
        """categories=None 禁用类别分组，所有测试用全局 threshold."""
        self._write_bench_file(
            tmp_path / "001_hist.json",
            [("test_serial_compile_baseline", 0.500)],
            mtime_offset=-100,
        )
        # 退化 12%，全局阈值 25% 不触发，类别阈值 10% 会触发
        self._write_bench_file(
            tmp_path / "002_cur.json",
            [("test_serial_compile_baseline", 0.560)],
            mtime_offset=0,
        )
        # 传 None 禁用类别
        report = cb.compare(tmp_path, threshold=25.0, categories=None)
        assert report.regressions == 0
        row = report.rows[0]
        assert row.category == ""
        assert row.threshold == 25.0
        assert row.is_regression is False

    def test_empty_categories_disables_grouping(self, tmp_path: Path) -> None:
        """categories=() 等价于 None，禁用类别分组."""
        self._write_bench_file(
            tmp_path / "001_hist.json",
            [("test_serial_compile_baseline", 0.500)],
            mtime_offset=-100,
        )
        self._write_bench_file(
            tmp_path / "002_cur.json",
            [("test_serial_compile_baseline", 0.560)],
            mtime_offset=0,
        )
        report = cb.compare(tmp_path, threshold=25.0, categories=())
        assert report.regressions == 0
        assert report.rows[0].category == ""
        assert report.rows[0].threshold == 25.0

    def test_first_run_row_has_category(self, tmp_path: Path) -> None:
        """首次运行的测试也记录类别信息."""
        self._write_bench_file(
            tmp_path / "001.json",
            [("test_serial_compile_baseline", 0.500)],
        )
        report = cb.compare(tmp_path, threshold=25.0)
        assert report.no_history == 1
        row = report.rows[0]
        assert row.is_first_run is True
        assert row.category == "nuitka_compile"
        assert row.threshold == 10.0

    def test_mixed_categories_in_one_report(self, tmp_path: Path) -> None:
        """同一报告中多个类别的测试各自用对应阈值."""
        # build_perf（25%）+ nuitka_compile（10%）+ 未知（全局 25%）
        hist = [
            ("test_small_project_warm_cache_baseline", 0.003),
            ("test_serial_compile_baseline", 0.500),
            ("test_unknown_baseline", 0.100),
        ]
        cur = [
            # build_perf 退化 20% < 25%，不触发
            ("test_small_project_warm_cache_baseline", 0.0036),
            # nuitka_compile 退化 12% > 10%，触发
            ("test_serial_compile_baseline", 0.560),
            # 未知退化 30% > 25%，触发
            ("test_unknown_baseline", 0.130),
        ]
        self._write_bench_file(tmp_path / "001_hist.json", hist, mtime_offset=-100)
        self._write_bench_file(tmp_path / "002_cur.json", cur, mtime_offset=0)
        report = cb.compare(tmp_path, threshold=25.0)
        assert report.regressions == 2
        # 按写入顺序检查
        rows_by_name = {r.name: r for r in report.rows}
        assert rows_by_name["test_small_project_warm_cache_baseline"].is_regression is False
        assert rows_by_name["test_serial_compile_baseline"].is_regression is True
        assert rows_by_name["test_unknown_baseline"].is_regression is True


class TestMainCategoryArgs:
    """main 类别相关 CLI 参数."""

    def test_list_categories_exit_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--list-categories 列出类别后 exit 0."""
        assert cb.main(["--list-categories"]) == 0
        out = capsys.readouterr().out
        assert "build_perf" in out
        assert "nuitka_compile" in out
        assert "wheel_download" in out
        assert "entry_startup" in out
        assert "core" in out

    def test_no_categories_uses_global_threshold(self, tmp_path: Path) -> None:
        """--no-categories 禁用类别分组，用全局阈值."""
        data_hist = {
            "benchmarks": [
                {
                    "name": "test_serial_compile_baseline",
                    "stats": {"median": 0.5, "min": 0.5, "mean": 0.5, "stddev": 0, "rounds": 20},
                }
            ]
        }
        data_cur = {
            "benchmarks": [
                {
                    "name": "test_serial_compile_baseline",
                    "stats": {"median": 0.56, "min": 0.56, "mean": 0.56, "stddev": 0, "rounds": 20},
                }
            ]
        }
        (tmp_path / "001.json").write_text(json.dumps(data_hist), encoding="utf-8")
        import os

        cur = tmp_path / "002.json"
        cur.write_text(json.dumps(data_cur), encoding="utf-8")
        ts = cur.stat().st_mtime + 100
        os.utime(cur, (ts, ts))
        # 退化 12%，全局阈值 25% 不触发，但若类别启用（10%）会触发
        assert cb.main(["--bench-dir", str(tmp_path), "--no-categories", "--threshold", "25"]) == 0
        # 不带 --no-categories 时类别启用，退化 12% > 10% 触发
        assert cb.main(["--bench-dir", str(tmp_path), "--threshold", "25"]) == 1

    def test_category_regression_exit_one(self, tmp_path: Path) -> None:
        """类别阈值触发的退化 exit 1，且输出含类别信息."""
        data_hist = {
            "benchmarks": [
                {
                    "name": "test_serial_compile_baseline",
                    "stats": {"median": 0.5, "min": 0.5, "mean": 0.5, "stddev": 0, "rounds": 20},
                }
            ]
        }
        data_cur = {
            "benchmarks": [
                {
                    "name": "test_serial_compile_baseline",
                    "stats": {"median": 0.60, "min": 0.60, "mean": 0.60, "stddev": 0, "rounds": 20},
                }
            ]
        }
        (tmp_path / "001.json").write_text(json.dumps(data_hist), encoding="utf-8")
        import os

        cur = tmp_path / "002.json"
        cur.write_text(json.dumps(data_cur), encoding="utf-8")
        ts = cur.stat().st_mtime + 100
        os.utime(cur, (ts, ts))
        # 退化 20% > 10% 类别阈值
        assert cb.main(["--bench-dir", str(tmp_path), "--threshold", "25"]) == 1
