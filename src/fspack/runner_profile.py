"""兼容垫片：``fspack.runner_profile`` 已迁移到 :mod:`fspack.runner.profile`.

保留此模块一个 minor 版本，下个 minor 删除；新代码请直接导入
``fspack.runner.profile``。
"""

from __future__ import annotations

import sys

from fspack.runner import profile as _module

sys.modules[__name__] = _module
