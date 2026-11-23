"""0.7.0 破坏性变更：旧模块路径的兼容垫片已删除.

下个 minor 若需要再次迁移公共路径，应先补兼容垫片与等价身份测试，再在
后续 minor 删除；本测试防止旧路径被无意重新引入。
"""

from __future__ import annotations

import importlib.util

import pytest

LEGACY_MODULES = (
    "exceptions",
    "_compat",
    "platform",
    "fsutil",
    "archive",
    "jsoncache",
    "logutil",
    "format",
    "astutil",
    "metrics",
    "console",
    "progress",
    "wizard",
    "runner_profile",
    "cli_init",
    "cli_parser",
    "cli_help",
)


@pytest.mark.parametrize("name", LEGACY_MODULES)
def test_legacy_module_path_is_removed(name: str) -> None:
    assert importlib.util.find_spec(f"fspack.{name}") is None, (
        f"fspack.{name} 是 0.7.0 已删除的兼容垫片；请使用新路径，或先补兼容垫片与迁移说明"
    )
