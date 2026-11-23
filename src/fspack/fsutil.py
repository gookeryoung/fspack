"""兼容垫片：``fspack.fsutil`` 已迁移到 :mod:`fspack.core.fsutil`."""

from __future__ import annotations

import sys

from fspack.core import fsutil as _module

sys.modules[__name__] = _module
