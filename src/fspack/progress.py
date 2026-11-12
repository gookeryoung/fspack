"""构建进度跟踪与 rich 可视化展示。

提供 ``BuildTracker``/``StageRecorder`` 数据类用于记录各阶段耗时与指标，
``spinner``/``iter_with_progress``/``parallel_map_with_progress`` 三个辅助函数
封装 rich.progress/Live 的实时展示。数据与渲染分离，便于测试。

HTTP 下载（含进度条）见 :class:`fspack.packaging.net.Downloader`。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import TYPE_CHECKING, TypeVar

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from fspack.console import console
from fspack.format import format_bytes_dec
from fspack.metrics import StageRecord, StageRecorder

if TYPE_CHECKING:
    from rich.status import Status

__all__ = [
    "BuildTracker",
    "StageRecord",
    "StageRecorder",
    "fmt_bytes",
    "iter_with_progress",
    "parallel_map_with_progress",
    "spinner",
]

_logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


class BuildTracker:
    """构建全流程进度跟踪器."""

    def __init__(self, *, title: str = "构建阶段汇总") -> None:
        """初始化空跟踪器，开始总计时.

        Args:
            title: 汇总表标题，区分不同流程（构建/打包/编译等）。
        """
        self._records: list[StageRecord] = []
        self._title = title
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
        table = Table(title=self._title, show_lines=False, title_style="bold blue")
        table.add_column("阶段", style="bold cyan", no_wrap=True)
        table.add_column("耗时", justify="right")
        table.add_column("缓存", justify="right")
        table.add_column("下载", justify="right")
        table.add_column("节省", justify="right")
        table.add_column("项数", justify="right")
        table.add_column("跳过", justify="right")
        table.add_column("备注", style="dim")

        total_bytes = 0
        total_saved = 0
        total_skipped = 0
        for r in self._records:
            total_bytes += r.bytes_downloaded
            total_saved += r.bytes_saved
            total_skipped += r.skipped
            cache_str = f"命中 {r.cache_hit}" if r.cache_hit else "-"
            bytes_str = fmt_bytes(r.bytes_downloaded) if r.bytes_downloaded else "-"
            saved_str = fmt_bytes(r.bytes_saved) if r.bytes_saved else "-"
            items_str = str(r.items) if r.items else "-"
            skip_str = str(r.skipped) if r.skipped else "-"
            detail_str = r.detail or "-"
            table.add_row(
                r.name,
                _fmt_seconds(r.elapsed),
                cache_str,
                bytes_str,
                saved_str,
                items_str,
                skip_str,
                detail_str,
            )

        table.add_row(
            "总计",
            _fmt_seconds(self.total_elapsed),
            "",
            fmt_bytes(total_bytes) if total_bytes else "-",
            fmt_bytes(total_saved) if total_saved else "-",
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


def fmt_bytes(n: int) -> str:
    """格式化字节数为人类可读字符串（KB/MB/GB）.

    实现搬迁至 :func:`fspack.format.format_bytes_dec`，此处保留同名薄封装
    维持 ``fspack.progress.fmt_bytes`` 引用兼容。
    """
    return format_bytes_dec(n)


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

    非 TTY 环境（stdout 被重定向，如 CI/pytest）或 item 数 <= 1 时跳过
    rich Progress（其 Live/Console 生命周期开销可显著高于实际遍历逻辑），
    退化为简单 for 循环。
    """
    total = len(items)
    use_progress = console.rich.is_terminal and total > 1
    if not use_progress:
        # 无进度条开销的快速路径
        for item in items:
            yield item
            if stage:
                stage.processed()
        return
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console.rich,
        transient=True,
    )
    with progress:
        task_id = progress.add_task(description, total=total)
        for item in items:
            yield item
            progress.advance(task_id)
            if stage:
                stage.processed()


def parallel_map_with_progress(
    items: Sequence[T],
    fn: Callable[[T], R],
    description: str,
    *,
    stage: StageRecorder | None = None,
    max_workers: int | None = None,
) -> list[R]:
    """并行执行 ``fn`` 对每个 item，显示进度条，返回结果列表（按完成顺序）。

    适用 I/O 密集场景（如 wheel 解压）：``ThreadPoolExecutor`` 利用 GIL 在
    I/O 等待时释放的特性实现并发。``fn`` 必须是线程安全的（无共享可变状态
    或自带锁）。``fn`` 抛出的异常在 ``future.result()`` 时重新抛出，与串行
    路径错误语义一致。

    ``max_workers`` 默认 ``min(8, len(items))``：上限 8 避免过多线程增加
    调度开销，下限随任务数自适应。返回结果按**完成顺序**而非输入顺序，
    调用方不应依赖位置对应（如需按输入顺序应改用 ``pool.map``）。

    非 TTY 环境（stdout 被重定向，如 CI/pytest）跳过 rich Progress，
    其 Live/Console 生命周期开销在小迭代场景下可能远高于实际并行执行时间。
    """
    total = len(items)
    if total == 0:
        return []
    workers = max_workers or min(8, total)
    results: list[R] = []
    use_progress = console.rich.is_terminal and total > 1
    if not use_progress:
        # 无进度条开销的快速路径
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fn, item) for item in items]
            for future in as_completed(futures):
                results.append(future.result())
                if stage:
                    stage.processed()
        return results
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console.rich,
        transient=True,
    )
    with progress, ThreadPoolExecutor(max_workers=workers) as pool:
        task_id = progress.add_task(description, total=total)
        futures = [pool.submit(fn, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())  # 异常在此重新抛出，与串行一致
            progress.advance(task_id)
            if stage:
                stage.processed()
    return results
