"""兼容垫片：``fspack.cli_help`` 已迁移到 :mod:`fspack.cli.help`.

保留此模块一个 minor 版本，下个 minor 删除；新代码请直接导入
``fspack.cli.help``。
"""

from __future__ import annotations

from fspack.cli.help import HELP_SNAPSHOT_PY, HELP_TEXT

__all__ = ["HELP_SNAPSHOT_PY", "HELP_TEXT"]
