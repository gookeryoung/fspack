"""兼容垫片：``fspack.metrics`` 已迁移到 :mod:`fspack.core.metrics`."""

from __future__ import annotations

import sys

from fspack.core import metrics as _module

sys.modules[__name__] = _module
