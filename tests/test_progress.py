"""progress 进度跟踪与展示测试。."""

from __future__ import annotations

import io
import ssl
import time
from pathlib import Path

import pytest

from fspack.console import console
from fspack.progress import (
    BuildTracker,
    StageRecord,
    StageRecorder,
    _fmt_bytes,
    _fmt_seconds,
    download_with_progress,
    iter_with_progress,
    spinner,
)


class TestStageRecorder:
    """StageRecorder 指标累积。."""

    def test_initial_state_starts_timer(self) -> None:
        rec = StageRecorder("test")
        assert rec.name == "test"
        assert rec._bytes == 0
        assert rec._hits == 0
        assert rec._items == 0
        assert rec._skipped == 0
        assert rec._detail == ""

    def test_add_bytes_accumulates(self) -> None:
        rec = StageRecorder("t")
        rec.add_bytes(100)
        rec.add_bytes(200)
        assert rec._bytes == 300

    def test_add_bytes_ignores_non_positive(self) -> None:
        rec = StageRecorder("t")
        rec.add_bytes(0)
        rec.add_bytes(-10)
        assert rec._bytes == 0

    def test_hit_cache_default_one(self) -> None:
        rec = StageRecorder("t")
        rec.hit_cache()
        rec.hit_cache(2)
        assert rec._hits == 3

    def test_hit_cache_ignores_non_positive(self) -> None:
        rec = StageRecorder("t")
        rec.hit_cache(0)
        rec.hit_cache(-1)
        assert rec._hits == 0

    def test_processed_accumulates(self) -> None:
        rec = StageRecorder("t")
        rec.processed()
        rec.processed(5)
        assert rec._items == 6

    def test_processed_ignores_non_positive(self) -> None:
        rec = StageRecorder("t")
        rec.processed(0)
        rec.processed(-3)
        assert rec._items == 0

    def test_skip_accumulates(self) -> None:
        rec = StageRecorder("t")
        rec.skip()
        rec.skip(5)
        assert rec._skipped == 6

    def test_skip_ignores_non_positive(self) -> None:
        rec = StageRecorder("t")
        rec.skip(0)
        rec.skip(-2)
        assert rec._skipped == 0

    def test_set_detail_overwrites(self) -> None:
        rec = StageRecorder("t")
        rec.set_detail("first")
        rec.set_detail("second")
        assert rec._detail == "second"

    def test_finalize_returns_immutable_record(self) -> None:
        rec = StageRecorder("test")
        rec.add_bytes(1024)
        rec.hit_cache(2)
        rec.processed(3)
        rec.skip(4)
        rec.set_detail("ok")
        time.sleep(0.001)
        record = rec._finalize()
        assert isinstance(record, StageRecord)
        assert record.name == "test"
        assert record.bytes_downloaded == 1024
        assert record.cache_hit == 2
        assert record.items == 3
        assert record.skipped == 4
        assert record.detail == "ok"
        assert record.elapsed > 0


class TestBuildTracker:
    """BuildTracker 阶段记录与汇总。."""

    def test_empty_tracker_has_no_records(self) -> None:
        tracker = BuildTracker()
        assert tracker.records == []
        assert tracker.total_elapsed >= 0

    def test_stage_records_elapsed(self) -> None:
        tracker = BuildTracker()
        with tracker.stage("step1") as rec:
            rec.add_bytes(100)
            time.sleep(0.001)
        records = tracker.records
        assert len(records) == 1
        assert records[0].name == "step1"
        assert records[0].bytes_downloaded == 100
        assert records[0].elapsed > 0

    def test_stage_records_even_on_exception(self) -> None:
        """with 块内抛异常时，阶段仍应记录。."""
        tracker = BuildTracker()
        with pytest.raises(ValueError, match="boom"), tracker.stage("failing") as rec:
            rec.processed(1)
            raise ValueError("boom")
        records = tracker.records
        assert len(records) == 1
        assert records[0].name == "failing"
        assert records[0].items == 1

    def test_multiple_stages_preserve_order(self) -> None:
        tracker = BuildTracker()
        with tracker.stage("a"):
            pass
        with tracker.stage("b"):
            pass
        with tracker.stage("c"):
            pass
        names = [r.name for r in tracker.records]
        assert names == ["a", "b", "c"]

    def test_total_elapsed_increases(self) -> None:
        tracker = BuildTracker()
        t1 = tracker.total_elapsed
        time.sleep(0.005)
        t2 = tracker.total_elapsed
        assert t2 > t1

    def test_summary_table_contains_stage_names(self) -> None:
        tracker = BuildTracker()
        with tracker.stage("解析项目"):
            pass
        with tracker.stage("准备运行时") as rec:
            rec.add_bytes(12 * 1024 * 1024)
            rec.hit_cache(1)
            rec.set_detail("embed python")
        with tracker.stage("下载依赖") as rec:
            rec.add_bytes(8 * 1024 * 1024)
            rec.hit_cache(2)
            rec.processed(5)
            rec.set_detail("5 wheels")
        with tracker.stage("复制源码") as rec:
            rec.skip(3)
            rec.set_detail("已存在跳过")
        table = tracker.summary()
        with console.capture() as capture:
            console.print(table)
        out = capture.get()
        assert "解析项目" in out
        assert "准备运行时" in out
        assert "下载依赖" in out
        assert "复制源码" in out
        assert "总计" in out
        assert "embed python" in out
        assert "5 wheels" in out
        assert "命中 1" in out
        assert "命中 2" in out
        assert "已存在跳过" in out
        assert "跳过" in out

    def test_summary_table_shows_dashes_for_empty_fields(self) -> None:
        tracker = BuildTracker()
        with tracker.stage("空阶段"):
            pass
        table = tracker.summary()
        with console.capture() as capture:
            console.print(table)
        out = capture.get()
        assert "空阶段" in out
        assert "-" in out


class TestFmtSeconds:
    """_fmt_seconds 单位切换。."""

    def test_milliseconds(self) -> None:
        assert _fmt_seconds(0.5) == "500ms"
        assert _fmt_seconds(0.001) == "1ms"

    def test_seconds(self) -> None:
        assert _fmt_seconds(1.5) == "1.50s"
        assert _fmt_seconds(59.99) == "59.99s"

    def test_minutes(self) -> None:
        assert _fmt_seconds(125.5) == "2m5.5s"


class TestFmtBytes:
    """_fmt_bytes 单位切换。."""

    def test_bytes(self) -> None:
        assert _fmt_bytes(0) == "0B"
        assert _fmt_bytes(1023) == "1023B"

    def test_kilobytes(self) -> None:
        assert _fmt_bytes(1024) == "1.0KB"
        assert _fmt_bytes(2048) == "2.0KB"

    def test_megabytes(self) -> None:
        assert _fmt_bytes(1024 * 1024) == "1.0MB"
        assert _fmt_bytes(10 * 1024 * 1024) == "10.0MB"

    def test_gigabytes(self) -> None:
        assert _fmt_bytes(1024 * 1024 * 1024) == "1.00GB"


class _FakeResp:
    """模拟 urlopen 响应，支持分块 read(n)。."""

    def __init__(self, data: bytes, block_size: int = 64) -> None:
        self._buf = io.BytesIO(data)
        self._block_size = block_size
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._buf.read(self._block_size)
        return self._buf.read(min(n, self._block_size))

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


class TestDownloadWithProgress:
    """download_with_progress 下载与指标回写。."""

    def test_downloads_file_and_returns_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> _FakeResp:
            captured["url"] = req.full_url  # type: ignore[union-attr]
            return _FakeResp(b"hello world data")

        monkeypatch.setattr("fspack.progress.urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "out" / "file.zip"
        ctx = ssl.create_default_context()
        written = download_with_progress("https://x/test.zip", dest, ssl_ctx=ctx, label="测试下载")
        assert written == len(b"hello world data")
        assert dest.read_bytes() == b"hello world data"
        assert captured["url"] == "https://x/test.zip"

    def test_stage_receives_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "fspack.progress.urllib.request.urlopen",
            lambda req, timeout, **kw: _FakeResp(b"abc" * 100),
        )
        rec = StageRecorder("download")
        written = download_with_progress(
            "https://x/d", tmp_path / "f.zip", ssl_ctx=ssl.create_default_context(), stage=rec
        )
        assert rec._bytes == written
        assert rec._bytes == 300

    def test_no_stage_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "fspack.progress.urllib.request.urlopen",
            lambda req, timeout, **kw: _FakeResp(b"abc"),
        )
        written = download_with_progress("https://x/d", tmp_path / "f.zip", ssl_ctx=ssl.create_default_context())
        assert written == 3

    def test_propagates_network_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
            raise OSError("boom")

        monkeypatch.setattr("fspack.progress.urllib.request.urlopen", fake_urlopen)
        with pytest.raises(OSError, match="boom"):
            download_with_progress("https://x/d", tmp_path / "f.zip", ssl_ctx=ssl.create_default_context())


class TestSpinner:
    """spinner 上下文管理器。."""

    def test_spinner_executes_block(self) -> None:
        marker = {"ran": False}
        with spinner("testing"):
            marker["ran"] = True
        assert marker["ran"] is True

    def test_spinner_propagates_exception(self) -> None:
        with pytest.raises(ValueError, match="inner"), spinner("testing"):
            raise ValueError("inner")

    def test_spinner_stops_on_exception(self) -> None:
        """异常发生时 status.stop 必须被调用，避免残留 spinner。."""
        with pytest.raises(RuntimeError), spinner("testing"):
            raise RuntimeError("fail")
        # 若 stop 未调用，下次 console.status 会冲突——这里仅验证不抛
        with spinner("again"):
            pass


class TestIterWithProgress:
    """iter_with_progress 通用迭代进度。."""

    def test_iterates_all_items(self) -> None:
        items = [1, 2, 3, 4, 5]
        collected: list[int] = []
        for item in iter_with_progress(items, "处理"):
            collected.append(item)
        assert collected == items

    def test_stage_processed_per_item(self) -> None:
        rec = StageRecorder("iter")
        items = list(range(7))
        for _ in iter_with_progress(items, "处理", stage=rec):
            pass
        assert rec._items == 7

    def test_no_stage_works(self) -> None:
        items = ["a", "b"]
        result = list(iter_with_progress(items, "处理"))
        assert result == items

    def test_empty_items(self) -> None:
        rec = StageRecorder("iter")
        items: list[int] = []
        result = list(iter_with_progress(items, "处理", stage=rec))
        assert result == []
        assert rec._items == 0

    def test_propagates_exception_inside_loop(self) -> None:
        rec = StageRecorder("iter")
        with pytest.raises(ValueError, match="boom"):
            for _ in iter_with_progress([1, 2, 3], "处理", stage=rec):
                raise ValueError("boom")
        # 用户在 yield 处抛异常，progress.advance 未执行
        assert rec._items == 0


class TestIntegrationBuildTracker:
    """BuildTracker 集成：模拟一次完整构建的指标收集。."""

    def test_simulated_build_summary(self) -> None:
        tracker = BuildTracker()
        with tracker.stage("解析项目"):
            time.sleep(0.001)
        with tracker.stage("准备运行时") as rec:
            rec.add_bytes(15 * 1024 * 1024)
            rec.hit_cache(1)
            rec.set_detail("embed python")
        with tracker.stage("分析依赖") as rec:
            rec.processed(3)
            rec.set_detail("AST import")
        with tracker.stage("下载依赖") as rec:
            rec.add_bytes(9 * 1024 * 1024)
            rec.hit_cache(2)
            rec.processed(5)
            rec.set_detail("5 wheels")
        with tracker.stage("复制源码"):
            time.sleep(0.001)
        with tracker.stage("生成 C loader"):
            time.sleep(0.001)
        records = tracker.records
        assert len(records) == 6
        assert records[0].name == "解析项目"
        assert records[1].bytes_downloaded == 15 * 1024 * 1024
        assert records[1].cache_hit == 1
        assert records[2].items == 3
        assert records[3].cache_hit == 2
        assert records[3].items == 5
        total_bytes = sum(r.bytes_downloaded for r in records)
        assert total_bytes == 24 * 1024 * 1024
        table = tracker.summary()
        with console.capture() as capture:
            console.print(table)
        out = capture.get()
        assert "构建阶段汇总" in out
        assert "24.0MB" in out
