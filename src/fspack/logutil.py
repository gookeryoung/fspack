"""日志路径缩略：默认单行、CJK 宽度感知；``-v`` 保留完整路径.

默认输出只保留阶段级信息，日志里的绝对路径（``C:\\Users\\...\\runtime\\x.dll``）
在 80 列重定向终端会折成 2-3 行，占掉大量行数。本模块提供：

- :func:`short_path`：默认按显示宽度做「中间省略」截断，保留盘符/根与前缀、
  以及文件名；``-v``（:func:`configure` 置 verbose）直接返回完整路径。
  被截断时把完整路径写一条 DEBUG 日志——``-v`` 与 CI 默认日志文件（root
  置 DEBUG）仍能找回原值，默认终端不显示。
- :func:`display_width` / :func:`truncate_middle`：按 East Asian Width 计算
  列宽（CJK 全角字符按 2 列），不按 ``len()`` 硬切，避免中文路径被切在半个
  字符上或宽度算错。

只依赖标准库（``unicodedata``/``logging``），不引入 rich，避免抬高 import
fspack 的成本。verbose 与宽度由 CLI 在 :func:`fspack.cli.main` 里按 ``-v``
与终端宽度配置。
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from os import PathLike

__all__ = [
    "configure",
    "display_width",
    "is_verbose",
    "reset",
    "short_path",
    "truncate_middle",
]

_logger = logging.getLogger(__name__)

# 默认路径预算（显示列）：80 列重定向终端的日志前缀约占 20 列，正文前缀
# （如「embed python 已缓存且 hash 匹配: 」）再占一段，留 36 列给路径可保证
# 常见消息落在单行内。交互终端（120 列）上上限放宽到 72 列。
_DEFAULT_PATH_WIDTH = 36
_MAX_PATH_WIDTH = 72


@dataclass
class _State:
    """当前进程的输出配置（CLI 每次执行前 :func:`configure` 覆盖）."""

    verbose: bool = False
    path_width: int = _DEFAULT_PATH_WIDTH


_state = _State()


def configure(*, verbose: bool, path_width: int | None = None) -> None:
    """配置 verbose 与路径预算（由 CLI 在每次命令执行前调用）.

    :param verbose: ``-v`` 时为 True，:func:`short_path` 返回完整路径。
    :param path_width: 路径显示列数预算；None 保持默认（36）。
    """
    _state.verbose = verbose
    if path_width is not None:
        _state.path_width = max(24, min(_MAX_PATH_WIDTH, path_width))


def reset() -> None:
    """恢复默认状态（CLI 单次执行结束后复位，避免测试间串味）."""
    _state.verbose = False
    _state.path_width = _DEFAULT_PATH_WIDTH


def is_verbose() -> bool:
    """当前是否处于 ``-v``（DEBUG）模式."""
    return _state.verbose


def display_width(text: str) -> int:
    """返回文本在等宽终端上的显示列数（全角/宽字符按 2 列）."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _take_head(text: str, budget: int) -> str:
    """从头部取不超过 ``budget`` 列的前缀."""
    used = 0
    out: list[str] = []
    for ch in text:
        width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + width > budget:
            break
        out.append(ch)
        used += width
    return "".join(out)


def _take_tail(text: str, budget: int) -> str:
    """从尾部取不超过 ``budget`` 列的后缀."""
    used = 0
    out: list[str] = []
    for ch in reversed(text):
        width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + width > budget:
            break
        out.append(ch)
        used += width
    return "".join(reversed(out))


def truncate_middle(text: str, max_width: int, *, ellipsis: str = "…") -> str:
    """按显示宽度做中间省略截断，优先保住文件名.

    切分预算：尾部（文件名所在）至少占一半，且尽量容纳完整文件名；剩余给
    头部（盘符/根/上级目录）。宽度不足或空串时原样/空串返回。

    :param text: 原始文本（通常是路径）。
    :param max_width: 目标显示列数。
    :param ellipsis: 省略标记，默认 ``…``。
    :return: 宽度不超过 ``max_width`` 的文本。
    """
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    mark_width = display_width(ellipsis)
    budget = max_width - mark_width
    if budget <= 0:
        return _take_head(ellipsis, max_width)
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    tail_budget = min(budget, max(display_width(name), budget // 2))
    head_budget = budget - tail_budget
    return f"{_take_head(text, head_budget)}{ellipsis}{_take_tail(text, tail_budget)}"


def short_path(path: str | PathLike[str], *, max_width: int | None = None) -> str:
    """返回用于日志的路径文本：``-v`` 完整，默认中间省略为单行.

    :param path: 路径对象或字符串。
    :param max_width: 覆盖默认宽度预算（显示列）。
    :return: 单行路径文本。
    """
    text = str(path)
    if _state.verbose:
        return text
    width = max_width if max_width is not None else _state.path_width
    if display_width(text) <= width:
        return text
    # 完整路径不因截断而丢失：落到 DEBUG，``-v`` 与 CI 默认日志文件仍可找回
    _logger.debug("完整路径: %s", text)
    return truncate_middle(text, width)
