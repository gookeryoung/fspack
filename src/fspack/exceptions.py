"""兼容垫片：``fspack.exceptions`` 已迁移到 :mod:`fspack.core.exceptions`."""

from __future__ import annotations

import sys

from fspack.core import exceptions as _module

sys.modules[__name__] = _module
