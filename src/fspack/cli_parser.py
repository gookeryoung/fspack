"""fspack CLI 参数解析器构建（facade）.

从 :mod:`fspack.cli` 拆分而来：parser 构建代码（argparse 声明）与命令分发
逻辑分离，``cli.py`` 聚焦 ``main``/dispatch。顶部仅导入轻量标准库与
``__version__``；``--mirror`` 刻意不做 argparse choices 校验（choices 会在
parser 构建期触发 ``fspack.config`` 导入 ~20ms），改由
:func:`fspack.cli._resolve_mirror` 在执行期校验。

按子命令拆分为四个子模块，本文件仅负责顶层 parser + 注册各子命令：

- :mod:`fspack.cli_cmds_build`：build / run / clean
- :mod:`fspack.cli_cmds_package`：package
- :mod:`fspack.cli_cmds_init`：init
- :mod:`fspack.cli_cmds_doctor`：doctor / cache
"""

from __future__ import annotations

import argparse

from fspack import __version__
from fspack.cli_cmds_build import (
    _add_build_subparser,
    _add_clean_subparser,
    _add_run_subparser,
)
from fspack.cli_cmds_doctor import (
    _add_cache_subparser,
    _add_doctor_subparser,
)
from fspack.cli_cmds_init import _add_init_subparser
from fspack.cli_cmds_manifest import _add_manifest_subparser
from fspack.cli_cmds_package import _add_package_subparser

__all__ = ["build_parser"]


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器."""
    parser = argparse.ArgumentParser(
        prog="fspack",
        description="极速 Python 打包器（cargo 风格短命令）。",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示 DEBUG 级别日志")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    _add_build_subparser(sub)
    _add_run_subparser(sub)
    _add_clean_subparser(sub)
    _add_package_subparser(sub)
    _add_init_subparser(sub)
    _add_manifest_subparser(sub)
    _add_doctor_subparser(sub)
    _add_cache_subparser(sub)
    return parser
