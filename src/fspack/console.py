"""控制台输出：rich 彩色日志与构建步骤进度显示。."""

from __future__ import annotations

import logging
from typing import Final

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

__all__ = ["console", "error", "setup_logging", "step", "success", "warn"]

_theme = Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "bold red",
        "success": "bold green",
        "step": "bold blue",
    }
)

console: Final = Console(theme=_theme)


def setup_logging(verbose: bool = False) -> None:
    """配置 root logger 使用 RichHandler，按级别着色。

    ERROR/WARNING 红黄高亮，INFO 青色，DEBUG 灰色（仅 verbose）。
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(
        RichHandler(
            console=console,
            show_time=True,
            show_level=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True,
        )
    )


def step(title: str) -> None:
    """打印构建步骤标题。."""
    console.print(f"[step]> {title}[/]")


def success(msg: str) -> None:
    """打印成功消息。."""
    console.print(f"[success]√[/] {msg}")


def warn(msg: str) -> None:
    """打印警告消息。."""
    console.print(f"[warning]![/] {msg}")


def error(msg: str) -> None:
    """打印错误消息。."""
    console.print(f"[error]×[/] {msg}")
