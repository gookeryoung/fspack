"""旧模块路径兼容垫片：一个 minor 版本内必须与新路径指向同一模块对象."""

from __future__ import annotations

import importlib

import pytest

SHIMS = {
    "exceptions": "fspack.core.exceptions",
    "_compat": "fspack.core._compat",
    "platform": "fspack.core.platform",
    "fsutil": "fspack.core.fsutil",
    "archive": "fspack.core.archive",
    "jsoncache": "fspack.core.jsoncache",
    "logutil": "fspack.core.logutil",
    "format": "fspack.core.format",
    "astutil": "fspack.core.astutil",
    "metrics": "fspack.core.metrics",
    "console": "fspack.ui.console",
    "progress": "fspack.ui.progress",
    "wizard": "fspack.ui.wizard",
}


@pytest.mark.parametrize(("old", "new"), SHIMS.items())
def test_legacy_module_is_same_object_as_new_module(old: str, new: str) -> None:
    """``import fspack.old`` 与 ``import fspack.new`` 必须拿到同一模块对象.

    这样 monkeypatch 旧路径属性时，新路径调用方同样生效；下个 minor 删除
    垫片后本测试一并删除。
    """
    assert importlib.import_module(f"fspack.{old}") is importlib.import_module(new)


def test_cli_parser_and_help_reexport_remain() -> None:
    """``cli_parser``/``cli_help`` 垫片继续 re-export 公开符号."""
    from fspack.cli.help import HELP_TEXT
    from fspack.cli.parser import build_parser
    from fspack.cli_help import HELP_TEXT as legacy_text
    from fspack.cli_parser import build_parser as legacy_parser

    assert legacy_parser is build_parser
    assert legacy_text == HELP_TEXT


def test_public_api_exports_core_entry_points() -> None:
    """``fspack.api`` 是稳定的库入口，导出 build/package/run 三类能力."""
    from fspack import api

    for name in ("build", "build_release", "run", "clean_dist", "ReleaseRequest", "RunOptions"):
        assert callable(getattr(api, name)) or getattr(api, name) is not None
