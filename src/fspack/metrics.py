"""构建阶段度量数据结构（无渲染依赖）.

``StageRecord`` / ``StageRecorder`` 只做「计时 + 指标累加 + 快照」，不涉及
rich/console 渲染。把它们与 :mod:`fspack.progress`（进度条、汇总表格等展示层）
分开，构建各子系统（wheels / runtime / pyc / nuitka …）即可只依赖本模块，
避免为了一个类型标注把 rich 拉进 import 链。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

__all__ = ["StageRecord", "StageRecorder"]


@dataclass
class StageRecord:
    """单阶段执行结果记录."""

    name: str
    elapsed: float
    bytes_downloaded: int = 0
    bytes_saved: int = 0
    cache_hit: int = 0
    items: int = 0
    skipped: int = 0
    detail: str = ""


class StageRecorder:
    """阶段上下文，记录阶段内累积指标。

    由 ``BuildTracker.stage()`` 返回，阶段内调用 ``add_bytes``/``hit_cache``/``skip`` 等方法
    累积数据，退出 ``with`` 块时由 ``BuildTracker`` 收集为不可变 ``StageRecord``。
    """

    __slots__ = ("_bytes", "_detail", "_hits", "_items", "_name", "_saved", "_skipped", "_start")

    def __init__(self, name: str) -> None:
        """初始化阶段记录器，开始计时."""
        self._name = name
        self._bytes = 0
        self._hits = 0
        self._items = 0
        self._skipped = 0
        self._saved = 0
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

    def add_saved_bytes(self, n: int) -> None:
        """累加精简节省字节数（wheel 精简剥离的文件总大小）."""
        if n > 0:
            self._saved += n

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
            bytes_saved=self._saved,
            cache_hit=self._hits,
            items=self._items,
            skipped=self._skipped,
            detail=self._detail,
        )
