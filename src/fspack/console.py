"""控制台输出：rich 彩色日志与构建步骤进度显示.

本模块同时承载 :class:`CICompat`（CI 环境兼容 shim）：它依赖 rich，位于
本模块（``_compat`` 仅保留 ``override``/``tomllib`` 零第三方依赖的 shim，
避免仅需 ``override`` 的模块连带加载 rich）。

Win7 等 legacy Windows 控制台兼容（无 VT 处理、CP936 点阵字体）：
Unicode 盒线字符（U+2500 区段）与 ``√``/``×`` 等 East Asian Ambiguous
宽度字符实际按 **2 格**渲染，而 rich 按每字符 1 格计量——计算的渲染
宽度不够实际显示宽度，表格/进度条行在物理控制台被硬换行，边框错位
呈现乱码。检测到 legacy 控制台时向 rich 报告 ASCII 编码（``ascii_only``
渲染：Table/Rule/Tree/Progress 全部退回每字符恒 1 格的 ASCII），中文
文本经 ``WriteConsoleW`` 输出不受影响；``ConsoleUI`` 的 ``√``/``×``
标记同步退回 ASCII（``v``/``x``）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from typing import IO, Any, Final

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

__all__ = ["CICompat", "ConsoleUI", "console"]


class _AsciiEncodingStream:
    """stdout 代理流：向 rich 报告 ASCII 编码以启用 ``ascii_only`` 渲染.

    rich 的 ``ascii_only`` 由输出流 ``encoding`` 派生（非 ``utf`` 前缀即
    启用），仅影响结构字符的选型（表格盒线/进度条/分隔线/树形引导线退回
    ASCII），不替换文本内容。代理只覆盖 ``encoding`` 属性，``write``/
    ``flush``/``isatty``/``fileno`` 等全部委托原流，实际输出编码不变。
    """

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    @property
    def encoding(self) -> str:
        return "ascii"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class CICompat:
    """CI 环境兼容性 shim。"""

    @staticmethod
    def get_theme() -> Theme:
        """获取 rich Console 实例的主题。"""
        return Theme(
            {
                "info": "cyan",
                "warning": "yellow",
                "error": "bold red",
                "success": "bold green",
                "step": "bold blue",
            }
        )

    @staticmethod
    def ensure_utf8_stdio() -> None:
        """将 stdout/stderr 重配置为 UTF-8 编码。"""
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream is None:
                continue
            encoding = getattr(stream, "encoding", None)
            if encoding and encoding.lower() in ("utf-8", "utf8"):
                continue
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                with contextlib.suppress(OSError, ValueError):  # 流已关闭或不支持重配置，忽略
                    reconfigure(encoding="utf-8")

    @staticmethod
    def is_legacy_windows_console() -> bool:
        """检测是否 Win7 等旧版 Windows 的 legacy 控制台（无 VT 处理）.

        判定条件：Windows 平台 + stdout 为真实 tty + 控制台不支持 VT
        转义（Win7/8 的 conhost；Win10+ 默认开启）。重定向输出（管道/
        文件，含 pytest capture）不算——宽度补偿只针对真实控制台显示，
        rich 对重定向流本就退化为纯文本无盒线布局问题。

        :return: legacy Windows 控制台返回 ``True``；探测失败按非 legacy
            处理（保持现代渲染路径）。
        """
        if sys.platform != "win32":
            return False
        try:
            if not sys.stdout.isatty():
                return False
            from rich.console import detect_legacy_windows

            return detect_legacy_windows()
        except (ImportError, AttributeError, OSError, ValueError):
            return False

    @classmethod
    def make_console(cls) -> Console:
        """创建 rich Console 实例。

        CI 环境（``CI`` 或 ``GITHUB_ACTIONS`` 等环境变量存在）下显式禁用
        ``legacy_windows`` 渲染：rich 在 Windows 非交互终端上会自动选择
        ``LegacyWindowsTerm``，但 GitHub Actions runner 上 ``cmd.exe`` 不支持
        ``legacy_windows_render`` 依赖的部分 API（如 ``SetConsoleTextAttribute``
        在重定向 stdout 上失败），导致 RichHandler emit 时崩溃。强制 ANSI 转义
        即可规避：Windows 10+ 与所有 POSIX 系统均原生支持。

        legacy Windows 控制台（Win7/8，非 CI 且 stdout 为真实 tty）下显式
        启用 ``legacy_windows`` 并以 :class:`_AsciiEncodingStream` 代理
        stdout：见模块 docstring 的宽度乱码治理说明。

        另在创建 Console 前调用 :meth:`ensure_utf8_stdio` 重配置 stdout/stderr
        为 UTF-8，避免 Windows 默认 cp1252 编码导致中文日志 UnicodeEncodeError。
        """
        cls.ensure_utf8_stdio()

        in_ci = any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS", "BUILD_NUMBER"))
        legacy = not in_ci and cls.is_legacy_windows_console()
        legacy_windows: bool | None = None
        if in_ci:
            legacy_windows = False
        elif legacy:
            legacy_windows = True
        file: Any = _AsciiEncodingStream(sys.stdout) if legacy else None
        return Console(
            theme=cls.get_theme(),
            legacy_windows=legacy_windows,
            file=file,
        )


class ConsoleUI:
    """控制台 UI：封装 rich Console 与彩色日志、步骤输出.

    模块级提供 :data:`console` 单例，调用方通过 ``console.step()``/``console.success()``
    等方法使用。需要 rich 原生组件（如 Progress/Status）时用 :attr:`rich` 属性
    获取底层 :class:`rich.console.Console`。
    """

    def __init__(self) -> None:
        self._legacy_console = CICompat.is_legacy_windows_console()
        self._console: Console = CICompat.make_console()

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
        """打印成功消息（legacy 控制台用 ASCII ``v`` 避免 Ambiguous 宽度偏移）."""
        mark = "v" if self._legacy_console else "√"
        self._console.print(f"[success]{mark}[/] {msg}")

    def warn(self, msg: str) -> None:
        """打印警告消息."""
        self._console.print(f"[warning]![/] {msg}")

    def error(self, msg: str) -> None:
        """打印错误消息（legacy 控制台用 ASCII ``x`` 避免 Ambiguous 宽度偏移）."""
        mark = "x" if self._legacy_console else "×"
        self._console.print(f"[error]{mark}[/] {msg}")


console: Final[ConsoleUI] = ConsoleUI()
