"""跨 Python 版本兼容性 shim。

集中放置版本相关的回退导入，避免在各模块散落 ``# type: ignore[import-not-found]``。

当前导出：

- :func:`override` — PEP 698，3.12+ 进入 ``typing``，低版本回退 ``typing_extensions``
- :mod:`tomllib` — 3.11+ 标准库，低版本回退 ``tomli``（解析 ``pyproject.toml`` 用）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from rich.theme import Theme

import os
import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("解析 pyproject.toml 需要 tomli（Python<3.11），请安装 tomli") from e

__all__ = ["override", "tomllib"]


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
        import contextlib

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
    def make_console() -> Console:
        """创建 rich Console 实例。

        CI 环境（``CI`` 或 ``GITHUB_ACTIONS`` 等环境变量存在）下显式禁用
        ``legacy_windows`` 渲染：rich 在 Windows 非交互终端上会自动选择
        ``LegacyWindowsTerm``，但 GitHub Actions runner 上 ``cmd.exe`` 不支持
        ``legacy_windows_render`` 依赖的部分 API（如 ``SetConsoleTextAttribute``
        在重定向 stdout 上失败），导致 RichHandler emit 时崩溃。强制 ANSI 转义
        即可规避：Windows 10+ 与所有 POSIX 系统均原生支持。

        另在创建 Console 前调用 :func:`_ensure_utf8_stdio` 重配置 stdout/stderr
        为 UTF-8，避免 Windows 默认 cp1252 编码导致中文日志 UnicodeEncodeError。
        """
        CICompat.ensure_utf8_stdio()

        in_ci = any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS", "BUILD_NUMBER"))
        return Console(theme=CICompat.get_theme(), legacy_windows=False if in_ci else None)
