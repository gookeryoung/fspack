"""兼容垫片：``fspack.archive`` 已迁移到 :mod:`fspack.core.archive`."""

from __future__ import annotations

import sys

from fspack.core import archive as _module

sys.modules[__name__] = _module
