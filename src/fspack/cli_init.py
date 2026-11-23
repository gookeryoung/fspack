"""兼容垫片：``fspack.templates.init`` 已迁移到 :mod:`fspack.templates.init`.

保留此模块一个 minor 版本，下个 minor 删除；新代码请直接导入
``fspack.templates.init``。
"""

from __future__ import annotations

import sys

from fspack.templates import init as _module

sys.modules[__name__] = _module
