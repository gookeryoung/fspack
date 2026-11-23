"""兼容垫片：``fspack.progress`` 已迁移到 :mod:`fspack.ui.progress`."""

from __future__ import annotations

import sys

from fspack.ui import progress as _module

sys.modules[__name__] = _module
