"""跨 Python 版本的兼容性 shim.

本模块提供零第三方依赖（除 ``tomli``/``typing_extensions`` 回退）的
跨版本兼容导入，避免各业务模块散落 ``sys.version_info`` 分支。

提供：

- :mod:`tomllib` / :mod:`tomli` — TOML 解析统一入口，``tomllib`` 不可用时回退
  到 ``tomli``（其 API 与 ``tomllib`` 完全一致）。
- :func:`override` — Python 3.12+ :func:`typing.override`，低版本回退到
  :func:`typing_extensions.override`。
"""

from __future__ import annotations

import sys

__all__ = ["tomllib", "override"]


# ---------------------------------------------------------------------------
# TOML 解析：Python 3.11+ 标准库 tomllib，低版本回退 tomli
# ---------------------------------------------------------------------------
if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]


# ---------------------------------------------------------------------------
# typing.override：Python 3.12+ 标准库，低版本回退 typing_extensions
# ---------------------------------------------------------------------------
if sys.version_info >= (3, 12):
    from typing import override  # type: ignore[import-not-found]
else:
    from typing_extensions import override  # type: ignore[import-not-found,no-redef]
