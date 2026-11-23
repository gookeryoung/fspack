"""兼容垫片：``fspack._compat`` 已迁移到 :mod:`fspack.core._compat`."""

from __future__ import annotations

import sys

from fspack.core import _compat as _module

sys.modules[__name__] = _module
