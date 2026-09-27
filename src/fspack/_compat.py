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
try:
    import tomllib  # type: ignore[import-not-found,no-redef]  # pragma: no cover
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

# ---------------------------------------------------------------------------
# datetime.UTC：Python 3.11+ 标准库，低版本回退 timezone.utc
# ---------------------------------------------------------------------------
from datetime import timezone

try:
    from datetime import UTC  # pragma: no cover
except ImportError:  # Python < 3.11
    UTC = timezone.utc  # type: ignore[misc,assignment]

# ---------------------------------------------------------------------------
# StrEnum：Python 3.11+ 标准库 enum.StrEnum，低版本回退 str+Enum 混合基类
# ---------------------------------------------------------------------------
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from enum import StrEnum  # 类型检查器只看标准库版本
else:
    try:
        from enum import StrEnum  # pragma: no cover
    except ImportError:  # Python < 3.11

        class StrEnum(str, Enum):
            """Python < 3.11 的 enum.StrEnum 兼容实现."""

            # 对齐 3.11 StrEnum 语义：str(member) 返回枚举值而非 "ClassName.MEMBER"
            __str__ = str.__str__


# ---------------------------------------------------------------------------
# typing.override：Python 3.12+ 标准库，低版本回退 typing_extensions
# ---------------------------------------------------------------------------
try:
    from typing import override  # type: ignore[import-not-found,no-redef]  # pragma: no cover
except ImportError:  # Python < 3.12
    from typing_extensions import override  # type: ignore[import-not-found,no-redef]
