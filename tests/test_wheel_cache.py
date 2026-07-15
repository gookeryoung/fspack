"""wheel_cache 模块测试：wheel 文件名解析与缓存目录。."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.wheel_cache import (
    WheelInfo,
    fspack_wheel_cache_dir,
    normalize_name,
    parse_wheel_filename,
)


class TestWheelInfoFromFilename:
    """WheelInfo.from_filename 类方法（工厂方法下沉后首选入口）。."""

    def test_standard_wheel(self) -> None:
        info = WheelInfo.from_filename("requests-2.31.0-py3-none-any.whl")
        assert info is not None
        assert info.name == "requests"
        assert info.version == "2.31.0"
        assert info.python_tags == ("py3",)
        assert info.abi_tag == "none"
        assert info.platform_tags == ("any",)

    def test_pyside2_nonstandard_build_tag(self) -> None:
        info = WheelInfo.from_filename("PySide2-5.15.2.1-5.15.2-cp35.cp36.cp37.cp38.cp39.cp310-none-win_amd64.whl")
        assert info is not None
        assert info.name == "PySide2"
        assert info.version == "5.15.2.1"
        assert info.python_tags == ("cp35", "cp36", "cp37", "cp38", "cp39", "cp310")
        assert info.platform_tags == ("win_amd64",)

    def test_multi_platform_wheel(self) -> None:
        info = WheelInfo.from_filename("numpy-1.24.0-cp39-cp39-manylinux2014_x86_64.manylinux_2_28_x86_64.whl")
        assert info is not None
        assert info.name == "numpy"
        assert info.python_tags == ("cp39",)
        assert info.platform_tags == ("manylinux2014_x86_64", "manylinux_2_28_x86_64")

    def test_invalid_filename_returns_none(self) -> None:
        assert WheelInfo.from_filename("not-a-wheel.txt") is None
        assert WheelInfo.from_filename("missing-tags-1.0.whl") is None


class TestParseWheelFilenameCompat:
    """parse_wheel_filename 向后兼容包装，行为与 WheelInfo.from_filename 一致。."""

    def test_standard_wheel(self) -> None:
        info = parse_wheel_filename("requests-2.31.0-py3-none-any.whl")
        assert info is not None
        assert info.name == "requests"
        assert info.version == "2.31.0"
        assert info.python_tags == ("py3",)
        assert info.abi_tag == "none"
        assert info.platform_tags == ("any",)

    def test_invalid_filename(self) -> None:
        assert parse_wheel_filename("not-a-wheel.txt") is None

    def test_delegates_to_classmethod(self) -> None:
        """兼容包装返回值与类方法一致。."""
        filename = "numpy-1.24.0-cp39-cp39-manylinux2014_x86_64.whl"
        assert parse_wheel_filename(filename) == WheelInfo.from_filename(filename)


class TestNormalizeName:
    """PEP 503 名称归一化。."""

    def test_basic(self) -> None:
        assert normalize_name("PySide2") == "pyside2"
        assert normalize_name("Jinja2") == "jinja2"

    def test_separators(self) -> None:
        assert normalize_name("my_pkg.name") == "my-pkg-name"
        assert normalize_name("multi__sep") == "multi-sep"


class TestFspackWheelCacheDir:
    """fspack 缓存目录路径。."""

    def test_path_structure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = fspack_wheel_cache_dir()
        assert result == tmp_path / ".fspack" / "cache" / "wheels"
