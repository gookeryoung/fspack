"""``--profile`` 耗时分析报告单元测试.

覆盖 :mod:`fspack.packaging.profile` 与 CLI 层 ``--profile`` 标志：

- :class:`ProfileReport` 数据类字段与 ``cpu_ratio`` 属性
- :class:`ProfileContext` 上下文管理器：进入/退出/采集/异常清理
- :func:`print_profile_report`：渲染表格不抛异常
- :func:`profile_report_to_json`：JSON 序列化字段完整
- CLI ``--profile`` 标志透传
- :func:`fspack.packaging.pipeline.build` 集成：profile=True 输出报告
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from fspack import cli
from fspack.console import console
from fspack.packaging.profile import (
    ProfileContext,
    ProfileReport,
    print_profile_report,
    profile_report_to_json,
)
from fspack.progress import BuildTracker, StageRecord

# ---- ProfileReport 数据类 ----


def _make_stage(  # noqa: PLR0913
    name: str = "测试阶段",
    elapsed: float = 0.5,
    bytes_downloaded: int = 0,
    bytes_saved: int = 0,
    cache_hit: int = 0,
    items: int = 0,
    skipped: int = 0,
    detail: str = "",
) -> StageRecord:
    """构造 StageRecord 用于测试."""
    return StageRecord(
        name=name,
        elapsed=elapsed,
        bytes_downloaded=bytes_downloaded,
        bytes_saved=bytes_saved,
        cache_hit=cache_hit,
        items=items,
        skipped=skipped,
        detail=detail,
    )


def test_profile_report_fields() -> None:
    """ProfileReport 字段正确赋值."""
    stages = (_make_stage("阶段A", elapsed=1.0), _make_stage("阶段B", elapsed=2.0))
    report = ProfileReport(
        wall_time=3.0,
        cpu_time=1.5,
        memory_peak=1024 * 1024,
        stages=stages,
    )
    assert report.wall_time == 3.0
    assert report.cpu_time == 1.5
    assert report.memory_peak == 1024 * 1024
    assert len(report.stages) == 2
    assert report.stages[0].name == "阶段A"
    assert report.stages[1].name == "阶段B"


def test_profile_report_cpu_ratio() -> None:
    """cpu_ratio = cpu_time / wall_time."""
    report = ProfileReport(
        wall_time=4.0,
        cpu_time=1.0,
        memory_peak=0,
        stages=(),
    )
    assert report.cpu_ratio == 0.25


def test_profile_report_cpu_ratio_zero_wall_time() -> None:
    """wall_time=0 时 cpu_ratio 返回 0（避免除零）."""
    report = ProfileReport(
        wall_time=0.0,
        cpu_time=0.5,
        memory_peak=0,
        stages=(),
    )
    assert report.cpu_ratio == 0.0


def test_profile_report_is_frozen() -> None:
    """ProfileReport 是 frozen dataclass，不可变."""
    report = ProfileReport(wall_time=1.0, cpu_time=0.5, memory_peak=0, stages=())
    with pytest.raises(AttributeError):
        report.wall_time = 2.0  # type: ignore[misc]


# ---- ProfileContext 上下文管理器 ----


def test_profile_context_enter_exit() -> None:
    """ProfileContext 进入时启动 tracemalloc，退出时停止."""
    import tracemalloc

    ctx = ProfileContext()
    assert not ctx._started
    with ctx:
        assert ctx._started
        assert tracemalloc.is_tracing()
    assert not ctx._started
    assert not tracemalloc.is_tracing()


def test_profile_context_collect_returns_report() -> None:
    """collect() 返回 ProfileReport，含 wall/cpu/memory 数据."""
    tracker = BuildTracker()
    with tracker.stage("测试阶段"):
        time.sleep(0.01)

    ctx = ProfileContext()
    with ctx:
        time.sleep(0.05)
    report = ctx.collect(tracker)

    assert isinstance(report, ProfileReport)
    assert report.wall_time > 0
    assert report.cpu_time >= 0
    assert report.memory_peak >= 0
    assert len(report.stages) == 1
    assert report.stages[0].name == "测试阶段"


def test_profile_context_collect_after_exit() -> None:
    """collect() 在 __exit__ 后调用仍能读到正确的内存峰值.

    修复前 ``__exit__`` 直接调用 ``tracemalloc.stop()``，导致后续 ``collect()``
    调用 ``get_traced_memory()`` 返回 ``(0, 0)``，``memory_peak`` 恒为 0。
    修复后 ``__exit__`` 先采集峰值再 stop，缓存到 ``_memory_peak`` 供
    ``collect()`` 读取。
    """
    tracker = BuildTracker()
    ctx = ProfileContext()
    with ctx:
        # 分配约 1MB 内存确保峰值 > 0
        _ = [b"x" * 1024 for _ in range(1024)]
    report = ctx.collect(tracker)
    assert report.memory_peak > 0
    assert report.wall_time > 0


def test_profile_context_exception_cleanup() -> None:
    """with 块内抛异常时 tracemalloc 仍被正确停止."""
    import tracemalloc

    ctx = ProfileContext()
    with pytest.raises(RuntimeError, match="测试异常"), ctx:
        raise RuntimeError("测试异常")
    assert not ctx._started
    assert not tracemalloc.is_tracing()


def test_profile_context_tracks_memory_allocation() -> None:
    """ProfileContext 追踪内存分配，collect() 返回的 memory_peak 反映分配.

    修复后 ``__exit__`` 先采集峰值再 stop tracemalloc，因此即使 ``collect()``
    在 ``with`` 块外被调用，``memory_peak`` 仍能反映追踪期间分配的内存。
    """
    tracker = BuildTracker()
    ctx = ProfileContext()
    with ctx:
        # 分配约 1MB 内存确保峰值 > 0
        _ = [b"x" * 1024 for _ in range(1024)]
    report = ctx.collect(tracker)
    assert report.memory_peak > 0
    assert report.wall_time > 0


# ---- print_profile_report ----


def test_print_profile_report_renders_without_error() -> None:
    """print_profile_report 渲染表格不抛异常."""
    stages = (
        _make_stage("解析项目", elapsed=0.1, items=1, detail="app 0.1"),
        _make_stage("下载运行时", elapsed=1.5, bytes_downloaded=10 * 1024 * 1024, cache_hit=1),
        _make_stage("复制源码", elapsed=0.3, items=5),
    )
    report = ProfileReport(
        wall_time=2.0,
        cpu_time=0.8,
        memory_peak=2 * 1024 * 1024,
        stages=stages,
    )

    with console.rich.capture() as capture:
        print_profile_report(report)

    out = capture.get()
    assert "耗时分析报告" in out
    assert "资源总览" in out
    assert "解析项目" in out
    assert "下载运行时" in out
    assert "复制源码" in out
    assert "总计" in out
    assert "墙钟时间" in out
    assert "CPU 时间" in out
    assert "CPU 占比" in out
    assert "内存峰值" in out


def test_print_profile_report_empty_stages() -> None:
    """print_profile_report 渲染空 stages 列表不抛异常."""
    report = ProfileReport(
        wall_time=0.5,
        cpu_time=0.2,
        memory_peak=0,
        stages=(),
    )

    with console.rich.capture() as capture:
        print_profile_report(report)

    out = capture.get()
    assert "耗时分析报告" in out
    assert "总计" in out


def test_print_profile_report_shows_cache_and_bytes() -> None:
    """print_profile_report 在 stage 含 cache_hit/bytes_downloaded 时显示."""
    stages = (_make_stage("下载", elapsed=1.0, bytes_downloaded=1024, cache_hit=2, bytes_saved=512),)
    report = ProfileReport(wall_time=1.0, cpu_time=0.5, memory_peak=0, stages=stages)

    with console.rich.capture() as capture:
        print_profile_report(report)

    out = capture.get()
    # cache_hit=2, items=0 → 命中率 100%（2/2）
    assert "100%" in out
    assert "2/2" in out
    assert "1.0KB" in out
    assert "512B" in out


# ---- profile_report_to_json ----


def test_profile_report_to_json_outputs_valid_json() -> None:
    """profile_report_to_json 输出合法 JSON 字符串."""
    stages = (
        _make_stage("阶段A", elapsed=1.0, bytes_downloaded=1024, cache_hit=1, items=5, detail="备注"),
        _make_stage("阶段B", elapsed=2.0, bytes_saved=2048, skipped=3),
    )
    report = ProfileReport(
        wall_time=3.0,
        cpu_time=1.5,
        memory_peak=4096,
        stages=stages,
    )

    output = profile_report_to_json(report)
    parsed = json.loads(output)

    assert isinstance(parsed, dict)
    assert parsed["wall_time"] == 3.0
    assert parsed["cpu_time"] == 1.5
    assert parsed["memory_peak"] == 4096
    assert parsed["cpu_ratio"] == 0.5
    assert len(parsed["stages"]) == 2
    assert parsed["stages"][0]["name"] == "阶段A"
    assert parsed["stages"][0]["elapsed"] == 1.0
    assert parsed["stages"][0]["bytes_downloaded"] == 1024
    assert parsed["stages"][0]["cache_hit"] == 1
    assert parsed["stages"][0]["items"] == 5
    assert parsed["stages"][0]["cache_hit_rate"] == round(1 / 6, 4)
    assert parsed["stages"][0]["detail"] == "备注"
    assert parsed["stages"][1]["name"] == "阶段B"
    assert parsed["stages"][1]["bytes_saved"] == 2048
    assert parsed["stages"][1]["skipped"] == 3


def test_profile_report_to_json_empty_stages() -> None:
    """profile_report_to_json 空 stages 时输出空数组."""
    report = ProfileReport(wall_time=0.5, cpu_time=0.2, memory_peak=0, stages=())

    output = profile_report_to_json(report)
    parsed = json.loads(output)

    assert parsed["stages"] == []
    assert parsed["wall_time"] == 0.5


def test_profile_report_to_json_contains_chinese() -> None:
    """profile_report_to_json 保留中文（ensure_ascii=False）."""
    stages = (_make_stage("解析项目", detail="中文备注"),)
    report = ProfileReport(wall_time=1.0, cpu_time=0.5, memory_peak=0, stages=stages)

    output = profile_report_to_json(report)
    assert "解析项目" in output
    assert "中文备注" in output
    assert "\\u" not in output  # 不应出现 unicode 转义


# ---- CLI 透传 ----


def _make_minimal_project(tmp_path: Path) -> Path:
    """创建最小可解析项目."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return tmp_path


def test_cli_build_profile_flag_passed_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp b --profile`` 透传 profile 开关给 build()."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: ProfileOptions | None = None,
        auto_clean: bool = False,
    ) -> None:
        captured["profile"] = profile

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--profile"])
    assert captured["profile"].enabled is True
    assert captured["profile"].out is None
    assert captured["profile"].compare is None


def test_cli_build_profile_out_and_compare_passed_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--profile-out``/``--profile-compare`` 透传给 build()，缺省值为 last."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: ProfileOptions | None = None,
        auto_clean: bool = False,
    ) -> None:
        captured["profile"] = profile

    out_dir = tmp_path / "perflogs"
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--profile", "--profile-out", str(out_dir), "--profile-compare"])
    assert captured["profile"].out == out_dir.resolve()
    # --profile-compare 不带值 → 哨兵 "trend"（历次趋势表）
    assert captured["profile"].compare == "trend"
    # last=与最近一次对比；正整数=近 N 次趋势
    cli.main(["b", str(tmp_path), "--profile", "--profile-compare", "last"])
    assert captured["profile"].compare == "last"
    cli.main(["b", str(tmp_path), "--profile", "--profile-compare", "5"])
    assert captured["profile"].compare == "5"

    ref = tmp_path / "base.json"
    cli.main(["b", str(tmp_path), "--profile", "--profile-compare", str(ref)])
    assert captured["profile"].compare == str(ref)


def test_cli_build_profile_out_requires_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--profile-out`` 未配合 ``--profile`` 时报 ProjectError（退出码 2）."""
    _make_minimal_project(tmp_path)

    def fake_build(**kwargs: Any) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("未启用 --profile 时不应执行构建")

    monkeypatch.setattr("fspack.builder.build", fake_build)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["b", str(tmp_path), "--profile-out", str(tmp_path / "out.json")])
    assert exc_info.value.code == 2


def test_cli_build_profile_compare_requires_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--profile-compare`` 未配合 ``--profile`` 时报 ProjectError（退出码 2）."""
    _make_minimal_project(tmp_path)

    def fake_build(**kwargs: Any) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("未启用 --profile 时不应执行构建")

    monkeypatch.setattr("fspack.builder.build", fake_build)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["b", str(tmp_path), "--profile-compare"])
    assert exc_info.value.code == 2


def test_cli_run_profile_out_and_compare_passed_to_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp r`` 的 ``--profile-out``/``--profile-compare`` 透传给 run()，缺省值为 last."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_run(project: Path, rest_args: list[str] | None = None, options: RunOptions | None = None) -> None:
        captured["options"] = options

    monkeypatch.setattr("fspack.runner.run", fake_run)
    out_dir = tmp_path / "perflogs"
    cli.main(["r", str(tmp_path), "--profile", "--profile-out", str(out_dir), "--profile-compare"])
    assert captured["options"].profile.enabled is True
    assert captured["options"].profile.out == out_dir.resolve()
    # --profile-compare 不带值 → 哨兵 "trend"（历次趋势表）
    assert captured["options"].profile.compare == "trend"
    cli.main(["r", str(tmp_path), "--profile", "--profile-compare", "last"])
    assert captured["options"].profile.compare == "last"

    ref = tmp_path / "base.json"
    cli.main(["r", str(tmp_path), "--profile", "--profile-compare", str(ref)])
    assert captured["options"].profile.compare == str(ref)


def test_cli_run_profile_out_requires_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp r --profile-out`` 未配合 ``--profile`` 时报 ProjectError（退出码 2）."""
    _make_minimal_project(tmp_path)

    def fake_run(**kwargs: Any) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("未启用 --profile 时不应执行运行")

    monkeypatch.setattr("fspack.runner.run", fake_run)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["r", str(tmp_path), "--profile-out", str(tmp_path / "out.json")])
    assert exc_info.value.code == 2


def test_cli_run_profile_compare_requires_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp r --profile-compare`` 未配合 ``--profile`` 时报 ProjectError（退出码 2）."""
    _make_minimal_project(tmp_path)

    def fake_run(**kwargs: Any) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError("未启用 --profile 时不应执行运行")

    monkeypatch.setattr("fspack.runner.run", fake_run)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["r", str(tmp_path), "--profile-compare"])
    assert exc_info.value.code == 2


# ---- profile_log：性能日志落盘 / 加载 / 查找 / 对比 ----


from fspack.packaging.profile_log import (  # noqa: E402
    PROFILE_LOG_SCHEMA,
    RUN_LOG_GLOB,
    RUN_PROFILE_LOG_SCHEMA,
    ProfileLogMeta,
    ProfileOptions,
    compare_with_baseline,
    find_latest_log,
    find_recent_logs,
    load_profile_log,
    print_profile_compare,
    print_profile_trend,
    save_profile_log,
    save_profile_report,
)
from fspack.runner import RunOptions  # noqa: E402

_META = ProfileLogMeta(name="app", version="0.1.0", python="3.13.14", platform="windows")


def _make_report(wall: float = 1.0, stages: tuple[StageRecord, ...] = ()) -> ProfileReport:
    """构造 ProfileReport 用于 profile_log 测试."""
    return ProfileReport(wall_time=wall, cpu_time=wall * 0.5, memory_peak=1024, stages=stages)


def test_save_profile_report_directory_mode(tmp_path: Path) -> None:
    """目录模式：自动命名 fsp-b-*.json，目录自动创建，内容含元数据."""
    out_dir = tmp_path / "nested" / ".benchmarks"
    report = _make_report(stages=(_make_stage("解析项目", elapsed=0.5),))

    path = save_profile_report(report, out_dir, _META)

    assert path.parent == out_dir
    assert path.name.startswith("fsp-b-") and path.name.endswith(".json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == PROFILE_LOG_SCHEMA
    assert data["project"] == {"name": "app", "version": "0.1.0"}
    assert data["python"] == "3.13.14"
    assert data["platform"] == "windows"
    assert data["wall_time"] == 1.0
    assert data["stages"][0]["name"] == "解析项目"
    assert data["created"]  # ISO 时间戳非空


def test_save_profile_report_file_mode(tmp_path: Path) -> None:
    """文件模式：.json 后缀直写指定文件名，父目录自动创建."""
    out_file = tmp_path / "logs" / "manual.json"
    path = save_profile_report(_make_report(), out_file, _META)
    assert path == out_file
    assert out_file.is_file()


def test_save_profile_report_same_second_sequence(tmp_path: Path) -> None:
    """同秒冲突：第二次落盘自动追加 -2 序号，不覆盖既有日志."""
    first = save_profile_report(_make_report(), tmp_path, _META)
    second = save_profile_report(_make_report(), tmp_path, _META)
    assert first != second
    assert second.name != first.name
    # 同秒序号 -2，或跨秒新时间戳——均不覆盖
    assert first.is_file() and second.is_file()


def test_load_profile_log_roundtrip(tmp_path: Path) -> None:
    """保存后加载 roundtrip：字段一致."""
    path = save_profile_report(_make_report(stages=(_make_stage("依赖解析", elapsed=0.2),)), tmp_path, _META)
    data = load_profile_log(path)
    assert data["wall_time"] == 1.0
    assert data["stages"][0]["elapsed"] == 0.2


def test_load_profile_log_errors(tmp_path: Path) -> None:
    """加载失败三情形：文件不存在 / 非法 JSON / schema 不符，均抛 ValueError."""
    with pytest.raises(ValueError, match="不存在"):
        load_profile_log(tmp_path / "missing.json")
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="合法 JSON"):
        load_profile_log(bad_json)
    bad_schema = tmp_path / "schema.json"
    bad_schema.write_text('{"schema": "other/1"}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        load_profile_log(bad_schema)


def test_find_latest_log(tmp_path: Path) -> None:
    """按文件名时间戳取最新；exclude 排除本次日志；空目录返回 None."""
    older = tmp_path / "fsp-b-20260101-100000.json"
    newer = tmp_path / "fsp-b-20260102-100000.json"
    other = tmp_path / "unrelated.json"
    for p in (older, newer, other):
        p.write_text("{}", encoding="utf-8")
    assert find_latest_log(tmp_path) == newer
    assert find_latest_log(tmp_path, exclude=newer) == older
    assert find_latest_log(tmp_path / "no-such-dir") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_latest_log(empty) is None


def test_find_recent_logs(tmp_path: Path) -> None:
    """时间升序返回最近 N 条；exclude 排除本次；limit<=0 不截断；空目录返回空列表."""
    names = ["fsp-b-20260101-100000.json", "fsp-b-20260102-100000.json", "fsp-b-20260103-100000.json"]
    for n in [*names, "unrelated.json"]:
        (tmp_path / n).write_text("{}", encoding="utf-8")
    # 默认：时间升序（旧→新），非同类前缀不入选
    assert [p.name for p in find_recent_logs(tmp_path)] == names
    # limit=2 取最近两条
    assert [p.name for p in find_recent_logs(tmp_path, limit=2)] == names[1:]
    # exclude 排除最新一条
    excluded = tmp_path / names[2]
    assert [p.name for p in find_recent_logs(tmp_path, exclude=excluded)] == names[:2]
    # limit<=0 不截断
    assert len(find_recent_logs(tmp_path, limit=0)) == 3
    assert find_recent_logs(tmp_path / "no-such-dir") == []


def _log_dict(
    wall: float = 1.0,
    cpu: float = 0.5,
    mem: int = 1024,
    stages: list[dict[str, Any]] | None = None,
    version: str = "0.1.0",
) -> dict[str, Any]:
    """构造性能日志 dict（模拟 load_profile_log 返回值）."""
    return {
        "schema": PROFILE_LOG_SCHEMA,
        "created": "2026-08-24T10:00:00",
        "project": {"name": "app", "version": version},
        "python": "3.13.14",
        "platform": "windows",
        "wall_time": wall,
        "cpu_time": cpu,
        "memory_peak": mem,
        "cpu_ratio": cpu / wall if wall else 0.0,
        "stages": stages or [],
    }


def test_print_profile_compare_renders(tmp_path: Path) -> None:
    """对比表渲染：总览差异带符号百分比，显著阶段/新增/移除/折叠行齐全."""
    current = _log_dict(
        wall=1.2,
        stages=[
            {"name": "依赖解析", "elapsed": 0.8},
            {"name": "解析项目", "elapsed": 0.3},
            {"name": "新阶段", "elapsed": 0.2},
        ],
    )
    baseline = _log_dict(
        wall=1.0,
        stages=[
            {"name": "依赖解析", "elapsed": 0.4},
            {"name": "解析项目", "elapsed": 0.31},
            {"name": "旧阶段", "elapsed": 0.1},
        ],
    )
    baseline_path = tmp_path / "fsp-b-20260101-100000.json"

    with console.rich.capture() as capture:
        print_profile_compare(current, baseline, baseline_path)
    out = capture.get()

    assert "性能对比" in out
    assert "基准: fsp-b-20260101-100000.json" in out
    # 总览差异：1.2 - 1.0 = +0.20s +20.0% ▲
    assert "+20.0%" in out
    # 显著阶段：0.8 - 0.4 = +0.40s +100.0%
    assert "依赖解析" in out
    assert "+100.0%" in out
    # 不显著阶段折叠（0.3 vs 0.31）
    assert "其余 1 个阶段" in out
    assert "差异不显著" in out
    # 新增/移除阶段
    assert "新阶段" in out
    assert "新增" in out
    assert "旧阶段" in out
    assert "移除" in out


def test_print_profile_compare_environment_notes() -> None:
    """环境不一致（版本/Python/平台）时表尾注明，提示对比需谨慎."""
    current = _log_dict(version="0.2.0")
    current["python"] = "3.14.0"
    current["platform"] = "linux"
    baseline = _log_dict(version="0.1.0")

    with console.rich.capture() as capture:
        print_profile_compare(current, baseline, Path("base.json"))
    # rich 会按终端宽度对 caption 自动换行，断言前去除全部空白
    flat = "".join(capture.get().split())
    assert "项目版本0.1.0→0.2.0" in flat
    assert "Python3.13.14→3.14.0" in flat
    assert "平台windows→linux" in flat


def test_print_profile_compare_improvement_green() -> None:
    """改善方向（耗时减少）渲染 ▼ 与负百分比."""
    current = _log_dict(wall=0.8)
    baseline = _log_dict(wall=1.0)

    with console.rich.capture() as capture:
        print_profile_compare(current, baseline, Path("base.json"))
    out = capture.get()
    assert "-20.0%" in out
    assert "▼" in out


# ---- profile_log：历次趋势表（print_profile_trend）----


def test_print_profile_trend_renders(tmp_path: Path) -> None:
    """趋势表渲染：历史行 + 本次/中位数/vs 中位数统计行（中位数抗单次抖动）."""
    current = _log_dict(wall=3.0, cpu=1.5)
    history = [
        (tmp_path / "a.json", _log_dict(wall=2.0, cpu=1.0)),
        (tmp_path / "b.json", _log_dict(wall=4.0, cpu=2.0)),
    ]

    with console.rich.capture() as capture:
        print_profile_trend(current, history)
    out = capture.get()

    assert "性能趋势" in out
    assert "历史 2 次" in out
    assert "本次" in out
    # 中位数 = median(2.0, 4.0) = 3.0，本次 3.0 → 持平 ＝
    assert "中位数(n=2)" in out
    assert "3.00s" in out
    assert "本次 vs 中位数" in out
    # created 均为同一 ISO 时间，历史行时间列渲染 MM-DD HH:MM:SS
    assert "08-24 10:00:00" in out


def test_print_profile_trend_env_filter(tmp_path: Path) -> None:
    """环境不一致行以 * 展示且不参与中位数；全部不一致时退化为全部并注明."""
    current = _log_dict(wall=1.0)
    # 环境一致（2 次）：中位数 = median(2.0, 4.0) = 3.0
    matched = [(tmp_path / "m1.json", _log_dict(wall=2.0)), (tmp_path / "m2.json", _log_dict(wall=4.0))]
    # 环境不一致（版本不同）：不参与统计
    mismatched = [(tmp_path / "x1.json", _log_dict(wall=100.0, version="9.9.9"))]

    with console.rich.capture() as capture:
        print_profile_trend(current, [*matched, *mismatched])
    flat = "".join(capture.get().split())
    assert "中位数(n=2)" in flat
    # rich 会按终端宽度对 caption 自动换行，断言前去除全部空白
    assert "*环境不一致1次" in flat

    # 全部不一致：统计退化用全部历史，注明
    with console.rich.capture() as capture:
        print_profile_trend(current, mismatched)
    flat = "".join(capture.get().split())
    assert "中位数(n=1)" in flat
    assert "无环境一致历史" in flat


def test_print_profile_trend_stage_deviation(tmp_path: Path) -> None:
    """阶段偏离表：显著项列出；不显著折叠；新增/移除阶段单列；无偏离不渲染."""
    current = _log_dict(
        wall=2.0,
        stages=[
            {"name": "依赖解析", "elapsed": 0.8},
            {"name": "解析项目", "elapsed": 0.3},
            {"name": "新阶段", "elapsed": 0.1},
        ],
    )
    history = [
        (
            tmp_path / "a.json",
            _log_dict(
                wall=1.0,
                stages=[
                    {"name": "依赖解析", "elapsed": 0.4},
                    {"name": "解析项目", "elapsed": 0.31},
                    {"name": "旧阶段", "elapsed": 0.1},
                ],
            ),
        )
    ]

    with console.rich.capture() as capture:
        print_profile_trend(current, history)
    out = capture.get()
    assert "性能阶段偏离" in out
    # 显著：0.8 vs 0.4 中位数 → +100.0%
    assert "依赖解析" in out
    assert "+100.0%" in out
    # 新增/移除阶段
    assert "新阶段" in out
    assert "新增" in out
    assert "旧阶段" in out
    assert "移除" in out

    # 无显著偏离（差异低于阈值）时不渲染偏离表
    quiet_cur = _log_dict(wall=1.0, stages=[{"name": "解析项目", "elapsed": 0.3}])
    quiet_hist = [(tmp_path / "b.json", _log_dict(wall=1.0, stages=[{"name": "解析项目", "elapsed": 0.31}]))]
    with console.rich.capture() as capture:
        print_profile_trend(quiet_cur, quiet_hist)
    assert "阶段偏离" not in capture.get()


def test_compare_with_baseline_trend_branch(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """compare=trend/正整数走趋势分支：读目录历史渲染；畸形历史跳过不中断."""
    # 造一份畸形（最旧）+ 两份合法历史 + 本次
    (tmp_path / "fsp-b-20260101-100000.json").write_text("{broken", encoding="utf-8")
    save_profile_log(_log_dict(wall=2.0), tmp_path / "fsp-b-20260102-100000.json")
    save_profile_log(_log_dict(wall=4.0), tmp_path / "fsp-b-20260103-100000.json")
    log_path = save_profile_log(_log_dict(wall=3.0), tmp_path / "fsp-b-20260104-100000.json")

    with console.rich.capture() as capture, caplog.at_level("WARNING"):
        compare_with_baseline(log_path, tmp_path, "trend")
    out = capture.get()
    assert "性能趋势" in out
    assert "中位数(n=2)" in out  # 畸形文件被跳过
    assert "跳过无法读取" in caplog.text

    # 正整数语义：近 1 次历史
    with console.rich.capture() as capture:
        compare_with_baseline(log_path, tmp_path, "1")
    out = capture.get()
    assert "历史 1 次" in out


def test_compare_with_baseline_trend_no_history(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """无历史日志时趋势分支告警跳过，不抛异常."""
    log_path = save_profile_log(_log_dict(), tmp_path / "fsp-b-20260101-100000.json")
    with caplog.at_level("WARNING"):
        compare_with_baseline(log_path, tmp_path, "trend")
    assert "未找到可对比的历史" in caplog.text


# ---- profile_log：启动剖析（fsp r --profile）日志 ----


def _run_log_dict(
    wall: float = 0.05,
    stages: list[dict[str, Any]] | None = None,
    entry: str = "app",
    debug: bool = False,
) -> dict[str, Any]:
    """构造启动剖析日志 dict（模拟 load_profile_log 返回值，毫秒→秒）."""
    return {
        "schema": RUN_PROFILE_LOG_SCHEMA,
        "created": "2026-08-24T10:00:00",
        "project": {"name": "app", "version": "0.1.0"},
        "python": "3.13.14",
        "platform": "windows",
        "entry": entry,
        "debug": debug,
        "wall_time": wall,
        "returncode": 0,
        "stages": stages or [],
    }


def test_save_profile_log_run_prefix(tmp_path: Path) -> None:
    """启动剖析日志：目录模式按 fsp-r- 前缀自动命名，run schema 可加载回读."""
    out_dir = tmp_path / ".benchmarks"
    data = _run_log_dict(stages=[{"name": "环境准备", "elapsed": 0.005}])
    path = save_profile_log(data, out_dir, prefix="fsp-r-")
    assert path.parent == out_dir
    assert path.name.startswith("fsp-r-")
    assert path.suffix == ".json"
    loaded = load_profile_log(path)
    assert loaded["schema"] == RUN_PROFILE_LOG_SCHEMA
    assert loaded["entry"] == "app"
    assert loaded["stages"][0]["name"] == "环境准备"


def test_find_latest_log_prefix_filter(tmp_path: Path) -> None:
    """构建与启动剖析日志同目录共存：按前缀过滤互不干扰."""
    (tmp_path / "fsp-b-20260824-100000.json").write_text("{}", encoding="utf-8")
    (tmp_path / "fsp-r-20260824-090000.json").write_text("{}", encoding="utf-8")
    (tmp_path / "fsp-r-20260824-110000.json").write_text("{}", encoding="utf-8")
    latest_build = find_latest_log(tmp_path)
    latest_run = find_latest_log(tmp_path, pattern=RUN_LOG_GLOB)
    assert latest_build is not None
    assert latest_run is not None
    assert latest_build.name == "fsp-b-20260824-100000.json"
    assert latest_run.name == "fsp-r-20260824-110000.json"


def test_print_profile_compare_run_mode() -> None:
    """启动剖析对比：无 cpu/内存字段时总览仅墙钟行，小阈值阶段差异可见."""
    current = _run_log_dict(
        wall=0.08,
        stages=[{"name": "环境准备", "elapsed": 0.014}, {"name": "用户入口执行", "elapsed": 0.03}],
    )
    baseline = _run_log_dict(
        wall=0.05,
        stages=[{"name": "环境准备", "elapsed": 0.008}, {"name": "用户入口执行", "elapsed": 0.032}],
    )

    with console.rich.capture() as capture:
        print_profile_compare(current, baseline, Path("base.json"), stage_min_delta=0.005)
    out = capture.get()
    assert "墙钟时间" in out
    # 启动剖析无 CPU/内存字段，总览自适应跳过
    assert "CPU 时间" not in out
    assert "内存峰值" not in out
    # 环境准备 8ms→14ms 差 6ms 超 5ms 阈值且 +75%，列入显著项；入口执行差 5ms 不显著
    assert "环境准备" in out
    assert "+75.0%" in out
    assert "差异不显著" in out


def test_print_profile_compare_run_env_notes() -> None:
    """启动剖析对比：入口名与调试模式不一致时表尾注明."""
    current = _run_log_dict(entry="cli", debug=True)
    baseline = _run_log_dict(entry="gui", debug=False)

    with console.rich.capture() as capture:
        print_profile_compare(current, baseline, Path("base.json"))
    # rich 会按终端宽度对 caption 自动换行，断言前去除全部空白
    flat = "".join(capture.get().split())
    assert "入口gui→cli" in flat
    assert "调试模式开（基准为关）" in flat


def test_print_profile_compare_schema_mismatch() -> None:
    """构建日志与启动剖析日志不可比：抛 ValueError 提示类型不一致."""
    current = _run_log_dict()
    baseline = _log_dict()

    with pytest.raises(ValueError, match="类型不一致"):
        print_profile_compare(current, baseline, Path("base.json"))


def test_cli_build_without_profile_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --profile 时 profile 开关为关（默认行为）."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: ProfileOptions | None = None,
        auto_clean: bool = False,
    ) -> None:
        captured["profile"] = profile

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert captured["profile"].enabled is False


# ---- build() 集成：profile=True 输出报告 ----


def _empty_report() -> Any:
    """构造空 DependencyReport 用于 mock."""
    from fspack.config import DependencyReport

    return DependencyReport(
        declared=(),
        ast_third_party=(),
        ast_stdlib=(),
        ast_local=(),
        ast_submodules={},
    )


def test_build_with_profile_outputs_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build(profile=True) 构建结束后输出耗时分析报告."""
    from fspack.config import get_mirror
    from fspack.packaging.pipeline import build
    from fspack.platform import Platform

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    # mock 写操作避免实际下载
    monkeypatch.setattr(
        "fspack.packaging.pipeline._prepare_runtime",
        lambda ctx: ctx.cfg.dist_dir / "site-packages",
    )
    monkeypatch.setattr("fspack.packaging.pipeline.executor._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline.executor._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.executor.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor._build_entry_loaders", lambda *a, **kw: [])

    with console.rich.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, profile=ProfileOptions(enabled=True))

    out = capture.get()
    assert "耗时分析报告" in out
    assert "资源总览" in out
    assert "墙钟时间" in out
    assert "CPU 时间" in out
    assert "内存峰值" in out


def test_build_without_profile_no_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build(profile=False) 不输出耗时分析报告."""
    from fspack.config import get_mirror
    from fspack.packaging.pipeline import build
    from fspack.platform import Platform

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    monkeypatch.setattr(
        "fspack.packaging.pipeline._prepare_runtime",
        lambda ctx: ctx.cfg.dist_dir / "site-packages",
    )
    monkeypatch.setattr("fspack.packaging.pipeline.executor._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline.executor._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.executor.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor._build_entry_loaders", lambda *a, **kw: [])

    with console.rich.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    out = capture.get()
    assert "耗时分析报告" not in out
    assert "资源总览" not in out


def test_build_profile_cleans_up_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build(profile=True) 异常时也正确清理 tracemalloc."""
    import tracemalloc

    from fspack.config import get_mirror
    from fspack.packaging.pipeline import build
    from fspack.platform import Platform

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("构建失败模拟")

    monkeypatch.setattr("fspack.packaging.pipeline.executor.resolve_project_info", boom)

    was_tracing = tracemalloc.is_tracing()
    with pytest.raises(RuntimeError, match="构建失败模拟"):
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, profile=ProfileOptions(enabled=True))
    # tracemalloc 已被 ProfileContext.__exit__ 停止
    assert not tracemalloc.is_tracing() or was_tracing


# ---- 末尾阶段并行：SBOM + manifest 并行执行 ----


def test_build_sbom_manifest_parallel_in_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SBOM 与 manifest 在不同线程并行执行（验证 ThreadPoolExecutor 启用）.

    mock generate_sbom 和 generate_manifest 记录调用线程 ID，断言两者在不同
    线程执行（证明并行而非串行）。使用 :class:`threading.Barrier` 强制两个
    任务都到达同步点后才返回：若两者在同一线程串行执行，Barrier 永远等不到
    2 个 parties，超时抛 :class:`BrokenBarrierError` 让测试失败。
    """
    import threading

    from fspack.config import get_mirror
    from fspack.packaging.pipeline import build
    from fspack.platform import Platform

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    # mock 构建主体避免实际下载/编译
    monkeypatch.setattr(
        "fspack.packaging.pipeline._prepare_runtime",
        lambda ctx: ctx.cfg.dist_dir / "site-packages",
    )
    monkeypatch.setattr("fspack.packaging.pipeline.executor._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline.executor._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.executor.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.executor._build_entry_loaders", lambda *a, **kw: [])

    sbom_threads: set[int] = set()
    manifest_threads: set[int] = set()
    # Barrier(2) 强制两个任务同时活跃：只有两个任务都在不同线程执行时才能通过
    barrier: threading.Barrier = threading.Barrier(2)

    def fake_generate_sbom(dist_dir: Path, info: object) -> Path:
        """记录 SBOM 调用线程 ID，等待 manifest 也到达后返回."""
        sbom_threads.add(threading.get_ident())
        barrier.wait(timeout=2.0)
        return dist_dir / "release" / "app-0.1-sbom.json"

    def fake_generate_manifest(dist_dir: Path, info: object) -> Path:
        """记录 manifest 调用线程 ID，等待 sbom 也到达后返回."""
        manifest_threads.add(threading.get_ident())
        barrier.wait(timeout=2.0)
        return dist_dir / "release" / "app-0.1-manifest.json"

    monkeypatch.setattr("fspack.packaging.sbom.generate_sbom", fake_generate_sbom)
    monkeypatch.setattr("fspack.packaging.manifest.generate_manifest", fake_generate_manifest)

    with console.rich.capture():
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    # 两者都被调用
    assert len(sbom_threads) == 1
    assert len(manifest_threads) == 1
    # 在不同线程执行（证明 ThreadPoolExecutor 并行）
    assert sbom_threads != manifest_threads, "SBOM 与 manifest 应在不同线程并行执行"
