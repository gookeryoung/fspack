"""跨 Python 版本的兼容性 shim.

本模块提供零第三方依赖（除 ``tomli``/``typing_extensions`` 回退）的
跨版本兼容导入，避免各业务模块散落 ``sys.version_info`` 分支。

提供：

- :mod:`tomllib` / :mod:`tomli` — TOML 解析统一入口，``tomllib`` 不可用时回退
  到 ``tomli``（其 API 与 ``tomllib`` 完全一致）。
- :func:`override` — Python 3.12+ :func:`typing.override`，低版本回退到
  :func:`typing_extensions.override`。
- :class:`StrEnum` — Python 3.11+ :class:`enum.StrEnum`，低版本回退到
  ``class StrEnum(str, Enum)`` 自定义实现。
- :data:`UTC` — Python 3.11+ :data:`datetime.UTC`，低版本回退到
  :data:`datetime.timezone.utc`。
"""

from __future__ import annotations

__all__ = ["UTC", "StrEnum", "override", "tomllib"]


# ---------------------------------------------------------------------------
# TOML 解析：Python 3.11+ 标准库 tomllib，低版本回退 tomli
# ---------------------------------------------------------------------------
import tomllib  # type: ignore[import-not-found,no-redef]  # pragma: no cover

# ---------------------------------------------------------------------------
# datetime.UTC：Python 3.11+ 标准库，低版本回退 timezone.utc
# ---------------------------------------------------------------------------
from datetime import UTC  # pragma: no cover

# ---------------------------------------------------------------------------
# StrEnum：Python 3.11+ 标准库 enum.StrEnum，低版本回退 str+Enum 混合基类
# ---------------------------------------------------------------------------
from enum import StrEnum

# ---------------------------------------------------------------------------
# typing.override：Python 3.12+ 标准库，低版本回退 typing_extensions
# ---------------------------------------------------------------------------
from typing import override  # type: ignore[import-not-found,no-redef]  # pragma: no cover
