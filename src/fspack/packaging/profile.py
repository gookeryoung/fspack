"""构建耗时分析报告.

在 :func:`fspack.packaging.pipeline.build` 执行期间采集 wall time / CPU time /
内存峰值（tracemalloc），构建结束后输出耗时分析报告，识别瓶颈阶段。

公共 API：

- :class:`ProfileContext` — 上下文管理器，管理 tracemalloc 与 CPU 时间采样
- :class:`ProfileReport` — 不可变报告数据类
- :func:`print_profile_report` — 渲染表格到控制台
- :func:`profile_report_to_json` — 序列化为 JSON 字符串

典型用法::

    from fspack.packaging.profile import ProfileContext, print_profile_report

    with ProfileContext() as pc:
        build(...)
    report = pc.collect(tracker)
    print_profile_report(report)

设计决策：用标准库 ``tracemalloc`` 替代 ``psutil``，避免引入新依赖。
``tracemalloc`` 测量 Python 内存分配峰值，对于打包工具（主要内存消耗是
AST/文件列表/配置等 Python 对象）足够精确。如需测量 RSS（含 C 扩展内存），
可在后续迭代引入 ``psutil`` 作为可选依赖。
"""

from __future__ import annotations

import json
import time
import tracemalloc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.table import Table

from fspack.console import console

if TYPE_CHECKING:
    # BuildTracker / StageRecord 仅用于类型注解（``from __future__ import
    # annotations`` 使注解不在运行时求值），顶部不导入 fspack.progress 避免连锁
    # 触发 rich.progress/rich.table 加载（省 ~12ms）。fmt_bytes 在
    # print_profile_report 函数内延迟导入（实际渲染时才加载）。
    from fspack.progress import BuildTracker, StageRecord

__all__ = [
    "ProfileContext",
    "ProfileReport",
    "print_profile_report",
    "profile_report_to_json",
]


@dataclass(frozen=True)
class ProfileReport:
    """构建耗时分析报告.

    - ``wall_time``：总墙钟时间（秒，含 I/O 等待）
    - ``cpu_time``：总 CPU 时间（秒，仅进程实际占用 CPU 时间）
    - ``memory_peak``：内存峰值（字节，tracemalloc 测量的 Python 分配峰值）
    - ``stages``：各阶段记录（来自 :class:`BuildTracker`）
    """

    wall_time: float
    cpu_time: float
    memory_peak: int
    stages: tuple[StageRecord, ...]

    @property
    def cpu_ratio(self) -> float:
        """CPU 占比 = cpu_time / wall_time，``wall_time`` 为 0 时返回 0."""
        return self.cpu_time / self.wall_time if self.wall_time > 0 else 0.0


class ProfileContext:
    """耗时分析上下文，管理 ``tracemalloc`` 与 CPU 时间采样.

    用 ``with`` 语句进入时启动 ``tracemalloc`` 并记录 CPU 时间起点，
    退出时停止 ``tracemalloc``。``collect()`` 方法采集数据生成
    :class:`ProfileReport`。

    线程安全：``tracemalloc`` 全局唯一，同一进程内同时只有一个
    :class:`ProfileContext` 活跃；``tracemalloc.start()`` 多次调用不会报错，
    但 ``stop()`` 会清除所有追踪数据。
    """

    __slots__ = ("_cpu_start", "_started", "_wall_start")

    def __init__(self) -> None:
        """初始化上下文，未启动追踪."""
        self._cpu_start = 0.0
        self._wall_start = 0.0
        self._started = False

    def __enter__(self) -> ProfileContext:
        """进入上下文：启动 tracemalloc，记录 CPU 与墙钟起点."""
        self._cpu_start = time.process_time()
        self._wall_start = time.perf_counter()
        tracemalloc.start()
        self._started = True
        return self

    def __exit__(self, *exc: object) -> None:
        """退出上下文：停止 tracemalloc，标记未启动."""
        if self._started:
            tracemalloc.stop()
            self._started = False

    def collect(self, tracker: BuildTracker) -> ProfileReport:
        """采集 profile 数据，返回 :class:`ProfileReport`.

        :param tracker: :class:`BuildTracker`，提供各阶段记录与总耗时
        :return: :class:`ProfileReport`，含 wall/cpu/memory 与 stages
        """
        cpu_time = time.process_time() - self._cpu_start
        wall_time = time.perf_counter() - self._wall_start
        # tracemalloc 已 stop 时 get_traced_memory 返回 (0, 0)
        # 在 __exit__ 后调用 collect 仍可工作（数据已采集到 wall/cpu）
        if self._started:
            _, peak = tracemalloc.get_traced_memory()
        else:
            peak = 0
        return ProfileReport(
            wall_time=wall_time,
            cpu_time=cpu_time,
            memory_peak=peak,
            stages=tuple(tracker.records),
        )


def _fmt_seconds(s: float) -> str:
    """格式化耗时为人类可读字符串（ms/s/m）."""
    if s < 1:
        return f"{s * 1000:.0f}ms"
    if s < 60:
        return f"{s:.2f}s"
    return f"{int(s // 60)}m{s % 60:.1f}s"


def _fmt_percent(ratio: float) -> str:
    """格式化比率为百分比字符串."""
    return f"{ratio * 100:.1f}%"


def print_profile_report(report: ProfileReport) -> None:
    """渲染耗时分析报告表格到控制台.

    表格含两部分：

    1. 总览行：wall time / cpu time / cpu 占比 / 内存峰值
    2. 各阶段明细：阶段名 / wall time / 占总时长比例 / 缓存命中 / 下载 / 节省 / 项数

    :param report: :class:`ProfileReport` 数据
    """
    # 延迟导入：fmt_bytes 触发 fspack.progress 加载（含 rich.progress ~12ms）。
    # 仅在实际渲染报告时加载，避免 import fspack.builder 热路径触发。
    from fspack.progress import fmt_bytes

    table = Table(title="耗时分析报告", show_lines=False, title_style="bold magenta")
    table.add_column("阶段", style="bold cyan", no_wrap=True)
    table.add_column("耗时", justify="right")
    table.add_column("占比", justify="right")
    table.add_column("缓存", justify="right")
    table.add_column("下载", justify="right")
    table.add_column("节省", justify="right")
    table.add_column("项数", justify="right")
    table.add_column("备注", style="dim")

    for stage in report.stages:
        ratio = stage.elapsed / report.wall_time if report.wall_time > 0 else 0.0
        # 缓存命中率 = cache_hit / (cache_hit + items) * 100%。
        # cache_hit=0 时显示 "-"，避免 "0.0%" 干扰阅读。
        # 命中率反映阶段缓存利用效率，帮助用户识别哪些阶段缓存未命中导致重复计算。
        total_attempts = stage.cache_hit + stage.items
        if stage.cache_hit and total_attempts > 0:
            hit_rate = stage.cache_hit / total_attempts
            cache_str = f"{hit_rate * 100:.0f}%（{stage.cache_hit}/{total_attempts}）"
        else:
            cache_str = "-"
        bytes_str = fmt_bytes(stage.bytes_downloaded) if stage.bytes_downloaded else "-"
        saved_str = fmt_bytes(stage.bytes_saved) if stage.bytes_saved else "-"
        items_str = str(stage.items) if stage.items else "-"
        detail_str = stage.detail or "-"
        table.add_row(
            stage.name,
            _fmt_seconds(stage.elapsed),
            _fmt_percent(ratio),
            cache_str,
            bytes_str,
            saved_str,
            items_str,
            detail_str,
        )

    table.add_row(
        "总计",
        _fmt_seconds(report.wall_time),
        "100%",
        "",
        "",
        "",
        "",
        "",
        style="bold",
    )
    console.rich.print(table)

    # 总览表：wall / cpu / cpu 占比 / 内存峰值
    overview = Table(title="资源总览", show_lines=False, title_style="bold magenta")
    overview.add_column("指标", style="bold cyan")
    overview.add_column("值", justify="right")
    overview.add_row("墙钟时间", _fmt_seconds(report.wall_time))
    overview.add_row("CPU 时间", _fmt_seconds(report.cpu_time))
    overview.add_row("CPU 占比", _fmt_percent(report.cpu_ratio))
    overview.add_row("内存峰值", fmt_bytes(report.memory_peak))
    console.rich.print(overview)


def profile_report_to_json(report: ProfileReport) -> str:
    """序列化 :class:`ProfileReport` 为 JSON 字符串.

    便于 CI 上传到 ELK/Loki 或写入文件后续分析。

    :param report: :class:`ProfileReport` 数据
    :return: JSON 字符串，字段含 wall_time/cpu_time/memory_peak/cpu_ratio/stages
    """
    data: dict[str, Any] = {
        "wall_time": round(report.wall_time, 4),
        "cpu_time": round(report.cpu_time, 4),
        "memory_peak": report.memory_peak,
        "cpu_ratio": round(report.cpu_ratio, 4),
        "stages": [
            {
                "name": s.name,
                "elapsed": round(s.elapsed, 4),
                "bytes_downloaded": s.bytes_downloaded,
                "bytes_saved": s.bytes_saved,
                "cache_hit": s.cache_hit,
                "items": s.items,
                "cache_hit_rate": round(s.cache_hit / (s.cache_hit + s.items), 4)
                if s.cache_hit and (s.cache_hit + s.items) > 0
                else 0.0,
                "skipped": s.skipped,
                "detail": s.detail,
            }
            for s in report.stages
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)
