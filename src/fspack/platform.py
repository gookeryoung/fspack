"""兼容垫片：``fspack.platform`` 已迁移到 :mod:`fspack.core.platform`."""

from __future__ import annotations

import sys

from fspack.core import platform as _module

sys.modules[__name__] = _module
