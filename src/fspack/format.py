"""兼容垫片：``fspack.format`` 已迁移到 :mod:`fspack.core.format`."""

from __future__ import annotations

import sys

from fspack.core import format as _module

sys.modules[__name__] = _module
