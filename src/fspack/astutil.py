"""兼容垫片：``fspack.astutil`` 已迁移到 :mod:`fspack.core.astutil`."""

from __future__ import annotations

import sys

from fspack.core import astutil as _module

sys.modules[__name__] = _module
