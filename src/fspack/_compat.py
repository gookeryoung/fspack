"""跨 Python 版本兼容性 shim。

集中放置版本相关的回退导入，避免在各模块散落 ``# type: ignore[import-not-found]``。

当前导出：

- :func:`override` — PEP 698，3.12+ 进入 ``typing``，低版本回退 ``typing_extensions``
- :mod:`tomllib` — 3.11+ 标准库，低版本回退 ``tomli``（解析 ``pyproject.toml`` 用）
"""

from __future__ import annotations

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
