"""fspack 基础冒烟测试."""

from __future__ import annotations

import fspack


def test_version_is_string() -> None:
    """__version__ 应为非空字符串."""
    assert isinstance(fspack.__version__, str)
    assert fspack.__version__


def test_package_importable() -> None:
    """包应可正常导入."""
    assert hasattr(fspack, "__all__")
    assert "__version__" in fspack.__all__
