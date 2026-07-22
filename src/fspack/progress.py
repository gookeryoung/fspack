"""构建进度跟踪与 rich 可视化展示。

提供 ``BuildTracker``/``StageRecorder`` 数据类用于记录各阶段耗时与指标，
``spinner``/``iter_with_progress`` 两个辅助函数封装 rich.progress/Live 的实时展示。
数据与渲染分离，便于测试。

HTTP 下载（含进度条）见 :class:`fspack.packaging.net.Downloader`。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Sequence, TypeVar

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from fspack.console import console

if TYPE_CHECKING:
    from rich.status import Status

__all__ = [
    "BuildTracker",
    "StageRecord",
    "StageRecorder",
    "iter_with_progress",
    "spinner",
]

_logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class StageRecord:
    """单阶段执行结果记录."""

    name: str
    elapsed: float
    bytes_downloaded: int = 0
    cache_hit: int = 0
    items: int = 0
    skipped: int = 0
    detail: str = ""


class StageRecorder:
    """阶段上下文，记录阶段内累积指标。

    由 ``BuildTracker.stage()`` 返回，阶段内调用 ``add_bytes``/``hit_cache``/``skip`` 等方法
    累积数据，退出 ``with`` 块时由 ``BuildTracker`` 收集为不可变 ``StageRecord``。
    """

    __slots__ = ("_bytes", "_detail", "_hits", "_items", "_name", "_skipped", "_start")

    def __init__(self, name: str) -> None:
        """初始化阶段记录器，开始计时."""
        self._name = name
        self._bytes = 0
        self._hits = 0
        self._items = 0
        self._skipped = 0
        self._detail = ""
        self._start = time.perf_counter()

    @property
    def name(self) -> str:
        """阶段名称."""
        return self._name

    def add_bytes(self, n: int) -> None:
        """累加下载字节数."""
        if n > 0:
            self._bytes += n

    def hit_cache(self, n: int = 1) -> None:
        """累加缓存命中次数."""
        if n > 0:
            self._hits += n

    def processed(self, n: int = 1) -> None:
        """累加处理项数."""
        if n > 0:
            self._items += n

    def skip(self, n: int = 1) -> None:
        """累加跳过项数（dist 已有依赖等场景）."""
        if n > 0:
            self._skipped += n

    def set_detail(self, text: str) -> None:
        """设置备注文本，覆盖既有值."""
        self._detail = text

    def _finalize(self) -> StageRecord:
        """结束计时并返回不可变记录."""
        return StageRecord(
            name=self._name,
            elapsed=time.perf_counter() - self._start,
            bytes_downloaded=self._bytes,
            cache_hit=self._hits,
            items=self._items,
            skipped=self._skipped,
            detail=self._detail,
        )


class BuildTracker:
    """构建全流程进度跟踪器."""

    def __init__(self) -> None:
        """初始化空跟踪器，开始总计时."""
        self._records: list[StageRecord] = []
        self._start = time.perf_counter()

    @contextmanager
    def stage(self, name: str) -> Iterator[StageRecorder]:
        """进入一个构建阶段，返回 ``StageRecorder`` 上下文."""
        rec = StageRecorder(name)
        try:
            yield rec
        finally:
            self._records.append(rec._finalize())

    @property
    def total_elapsed(self) -> float:
        """自创建以来的总耗时（秒）."""
        return time.perf_counter() - self._start

    @property
    def records(self) -> list[StageRecord]:
        """已完成阶段记录列表（拷贝）."""
        return list(self._records)

    def summary(self) -> Table:
        """渲染汇总表格."""
        table = Table(title="构建阶段汇总", show_lines=False, title_style="bold blue")
        table.add_column("阶段", style="bold cyan", no_wrap=True)
        table.add_column("耗时", justify="right")
        table.add_column("缓存", justify="right")
        table.add_column("下载", justify="right")
        table.add_column("项数", justify="right")
        table.add_column("跳过", justify="right")
        table.add_column("备注", style="dim")

        total_bytes = 0
        total_skipped = 0
        for r in self._records:
            total_bytes += r.bytes_downloaded
            total_skipped += r.skipped
            cache_str = f"命中 {r.cache_hit}" if r.cache_hit else "-"
            bytes_str = _fmt_bytes(r.bytes_downloaded) if r.bytes_downloaded else "-"
            items_str = str(r.items) if r.items else "-"
            skip_str = str(r.skipped) if r.skipped else "-"
            detail_str = r.detail or "-"
            table.add_row(r.name, _fmt_seconds(r.elapsed), cache_str, bytes_str, items_str, skip_str, detail_str)

        table.add_row(
            "总计",
            _fmt_seconds(self.total_elapsed),
            "",
            _fmt_bytes(total_bytes) if total_bytes else "-",
            "",
            str(total_skipped) if total_skipped else "-",
            "",
            style="bold",
        )
        return table


def _fmt_seconds(s: float) -> str:
    """格式化耗时为人类可读字符串."""
    if s < 1:
        return f"{s * 1000:.0f}ms"
    if s < 60:
        return f"{s:.2f}s"
    return f"{int(s // 60)}m{s % 60:.1f}s"


def _fmt_bytes(n: int) -> str:
    """格式化字节数为人类可读字符串（KB/MB/GB）."""
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    return f"{n / 1024 / 1024 / 1024:.2f}GB"


@contextmanager
def spinner(label: str) -> Iterator[None]:
    """显示旋转符 ``label`` 直到 ``with`` 块退出。

    不封装子进程：调用方在 ``with`` 块内自行调 ``subprocess.run``。
    异常会正常传播，不会吞噬。
    """
    status: Status = console.rich.status(label, spinner="dots")
    status.start()
    try:
        yield
    finally:
        status.stop()


def iter_with_progress(
    items: Sequence[T],
    description: str,
    *,
    stage: StageRecorder | None = None,
) -> Iterator[T]:
    """遍历 ``items``，显示进度条，每个 item 处理完后调 ``stage.processed()``。

    生成器函数：``with progress:`` 块在生成器迭代过程中保持活跃，
    在 ``StopIteration`` 或异常时通过 ``with`` 的 ``__exit__`` 正确停止进度条。
    """
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console.rich,
        transient=True,
    )
    total = len(items)
    with progress:
        task_id = progress.add_task(description, total=total)
        for item in items:
            yield item
            progress.advance(task_id)
            if stage:
                stage.processed()
