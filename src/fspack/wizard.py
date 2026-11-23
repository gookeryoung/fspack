"""兼容垫片：``fspack.wizard`` 已迁移到 :mod:`fspack.ui.wizard`."""

from __future__ import annotations

import sys

from fspack.ui import wizard as _module

sys.modules[__name__] = _module
