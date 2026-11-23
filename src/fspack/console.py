"""兼容垫片：``fspack.console`` 已迁移到 :mod:`fspack.ui.console`."""

from __future__ import annotations

import sys

from fspack.ui import console as _module

sys.modules[__name__] = _module
