"""跨 Python 版本兼容性 shim。

集中放置版本相关的回退导入，避免在各模块散落 ``# type: ignore[import-not-found]``。
当前仅导出 :func:`override`（PEP 698，3.12+ 进入 ``typing``，低版本回退 ``typing_extensions``）。
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

__all__ = ["override"]
