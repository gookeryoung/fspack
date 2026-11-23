"""兼容垫片：``fspack.logutil`` 已迁移到 :mod:`fspack.core.logutil`."""

from __future__ import annotations

import sys

from fspack.core import logutil as _module

sys.modules[__name__] = _module
