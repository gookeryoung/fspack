"""兼容垫片：``fspack.jsoncache`` 已迁移到 :mod:`fspack.core.jsoncache`."""

from __future__ import annotations

import sys

from fspack.core import jsoncache as _module

sys.modules[__name__] = _module
