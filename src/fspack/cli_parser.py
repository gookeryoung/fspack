"""兼容垫片：``fspack.cli_parser`` 已迁移到 :mod:`fspack.cli.parser`.

保留此模块一个 minor 版本，下个 minor 删除；新代码请直接导入
``fspack.cli.parser``。
"""

from __future__ import annotations

from fspack.cli.parser import build_parser

__all__ = ["build_parser"]
