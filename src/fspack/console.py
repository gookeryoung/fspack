"""控制台输出：rich 彩色日志与构建步骤进度显示."""

from __future__ import annotations

import logging
import os
from typing import Final

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

__all__ = ["ConsoleUI", "console"]

_theme = Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "bold red",
        "success": "bold green",
        "step": "bold blue",
    }
)


def _make_console() -> Console:
    """创建 rich Console 实例。

    CI 环境（``CI`` 或 ``GITHUB_ACTIONS`` 等环境变量存在）下显式禁用
    ``legacy_windows`` 渲染：rich 在 Windows 非交互终端上会自动选择
    ``LegacyWindowsTerm``，但 GitHub Actions runner 上 ``cmd.exe`` 不支持
    ``legacy_windows_render`` 依赖的部分 API（如 ``SetConsoleTextAttribute``
    在重定向 stdout 上失败），导致 RichHandler emit 时崩溃。强制 ANSI 转义
    即可规避：Windows 10+ 与所有 POSIX 系统均原生支持。
    """
    in_ci = any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS", "BUILD_NUMBER"))
    return Console(theme=_theme, legacy_windows=False if in_ci else None)


class ConsoleUI:
    """控制台 UI：封装 rich Console 与彩色日志、步骤输出.

    模块级提供 :data:`console` 单例，调用方通过 ``console.step()``/``console.success()``
    等方法使用。需要 rich 原生组件（如 Progress/Status）时用 :attr:`rich` 属性
    获取底层 :class:`rich.console.Console`。
    """

    def __init__(self) -> None:
        self._console: Final = _make_console()

    @property
    def rich(self) -> Console:
        """底层 rich Console，供 Progress/Status 等 rich 组件使用."""
        return self._console

    def setup_logging(self, verbose: bool = False) -> None:
        """配置 root logger 使用 RichHandler，按级别着色。

        ERROR/WARNING 红黄高亮，INFO 青色，DEBUG 灰色（仅 verbose）。
        """
        level = logging.DEBUG if verbose else logging.INFO
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(level)
        root.addHandler(
            RichHandler(
                console=self._console,
                show_time=True,
                show_level=True,
                show_path=False,
                rich_tracebacks=True,
                markup=True,
            )
        )

    def step(self, title: str) -> None:
        """打印构建步骤标题."""
        self._console.print(f"[step]> {title}[/]")

    def success(self, msg: str) -> None:
        """打印成功消息."""
        self._console.print(f"[success]√[/] {msg}")

    def warn(self, msg: str) -> None:
        """打印警告消息."""
        self._console.print(f"[warning]![/] {msg}")

    def error(self, msg: str) -> None:
        """打印错误消息."""
        self._console.print(f"[error]×[/] {msg}")


console: Final[ConsoleUI] = ConsoleUI()
