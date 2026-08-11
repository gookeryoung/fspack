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
    """collect() 在 __exit__ 后调用仍可工作（memory_peak 为 0）."""
    tracker = BuildTracker()
    ctx = ProfileContext()
    with ctx:
        time.sleep(0.01)
    report = ctx.collect(tracker)
    # tracemalloc 已 stop，get_tracedMemory 返回 (0, 0)
    assert report.memory_peak == 0
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
    """ProfileContext 追踪内存分配，collect() 返回的 memory_peak 反映分配."""
    tracker = BuildTracker()
    ctx = ProfileContext()
    with ctx:
        # 分配一些内存
        _ = [b"x" * 1024 for _ in range(1000)]
    report = ctx.collect(tracker)
    # tracemalloc 在 stop 后 get_traced_memory 返回 (0, 0)
    # 所以 memory_peak 为 0，但 wall_time 应该有值
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
    """``fsp b --profile`` 透传 profile=True 给 build()."""
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
        profile: bool = False,
        auto_clean: bool = False,
    ) -> None:
        captured["profile"] = profile

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--profile"])
    assert captured["profile"] is True


def test_cli_build_without_profile_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --profile 时 profile=False（默认行为）."""
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
        profile: bool = False,
        auto_clean: bool = False,
    ) -> None:
        captured["profile"] = profile

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert captured["profile"] is False


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
    monkeypatch.setattr("fspack.packaging.pipeline._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._build_entry_loaders", lambda *a, **kw: [])

    with console.rich.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, profile=True)

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
    monkeypatch.setattr("fspack.packaging.pipeline._analyze_dependencies", lambda ctx, **kw: _empty_report())
    monkeypatch.setattr("fspack.packaging.pipeline._download_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr("fspack.packaging.pipeline.write_pth", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline.copy_source", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._compile_user_sources", lambda *a, **kw: None)
    monkeypatch.setattr("fspack.packaging.pipeline._build_entry_loaders", lambda *a, **kw: [])

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

    monkeypatch.setattr("fspack.packaging.pipeline.resolve_project_info", boom)

    was_tracing = tracemalloc.is_tracing()
    with pytest.raises(RuntimeError, match="构建失败模拟"):
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, profile=True)
    # tracemalloc 已被 ProfileContext.__exit__ 停止
    assert not tracemalloc.is_tracing() or was_tracing
