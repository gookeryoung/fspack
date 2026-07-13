"""wheel_cache 模块测试：wheel 文件名解析与匹配。."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.wheel_cache import (
    WheelInfo,
    fspack_wheel_cache_dir,
    normalize_name,
    parse_wheel_filename,
    wheel_matches,
)


class TestParseWheelFilename:
    """wheel 文件名解析。."""

    def test_standard_wheel(self) -> None:
        info = parse_wheel_filename("requests-2.31.0-py3-none-any.whl")
        assert info is not None
        assert info.name == "requests"
        assert info.version == "2.31.0"
        assert info.python_tags == ("py3",)
        assert info.abi_tag == "none"
        assert info.platform_tags == ("any",)

    def test_pyside2_nonstandard_build_tag(self) -> None:
        info = parse_wheel_filename("PySide2-5.15.2.1-5.15.2-cp35.cp36.cp37.cp38.cp39.cp310-none-win_amd64.whl")
        assert info is not None
        assert info.name == "PySide2"
        assert info.version == "5.15.2.1"
        assert info.python_tags == ("cp35", "cp36", "cp37", "cp38", "cp39", "cp310")
        assert info.platform_tags == ("win_amd64",)

    def test_multi_platform_wheel(self) -> None:
        info = parse_wheel_filename("numpy-1.24.0-cp39-cp39-manylinux2014_x86_64.manylinux_2_28_x86_64.whl")
        assert info is not None
        assert info.name == "numpy"
        assert info.python_tags == ("cp39",)
        assert info.platform_tags == ("manylinux2014_x86_64", "manylinux_2_28_x86_64")

    def test_invalid_filename(self) -> None:
        assert parse_wheel_filename("not-a-wheel.txt") is None
        assert parse_wheel_filename("missing-tags-1.0.whl") is None


class TestNormalizeName:
    """PEP 503 名称归一化。."""

    def test_basic(self) -> None:
        assert normalize_name("PySide2") == "pyside2"
        assert normalize_name("Jinja2") == "jinja2"

    def test_separators(self) -> None:
        assert normalize_name("my_pkg.name") == "my-pkg-name"
        assert normalize_name("multi__sep") == "multi-sep"


class TestWheelMatches:
    """wheel 匹配逻辑。."""

    def test_exact_match(self) -> None:
        info = WheelInfo("PySide2", "5.15.2.1", ("cp39",), "none", ("win_amd64",))
        assert wheel_matches(info, {"pyside2"}, "cp39", ("win_amd64",))

    def test_name_mismatch(self) -> None:
        info = WheelInfo("PySide2", "5.15.2.1", ("cp39",), "none", ("win_amd64",))
        assert not wheel_matches(info, {"flask"}, "cp39", ("win_amd64",))

    def test_python_tag_mismatch(self) -> None:
        info = WheelInfo("PySide2", "5.15.2.1", ("cp38",), "none", ("win_amd64",))
        assert not wheel_matches(info, {"pyside2"}, "cp39", ("win_amd64",))

    def test_python_tag_py3_compatible(self) -> None:
        info = WheelInfo("requests", "2.31.0", ("py3",), "none", ("any",))
        assert wheel_matches(info, {"requests"}, "cp39", ("win_amd64",))

    def test_python_tag_py39_compatible(self) -> None:
        info = WheelInfo("requests", "2.31.0", ("py39",), "none", ("any",))
        assert wheel_matches(info, {"requests"}, "cp39", ("win_amd64",))

    def test_platform_any_matches_all(self) -> None:
        info = WheelInfo("requests", "2.31.0", ("py3",), "none", ("any",))
        assert wheel_matches(info, {"requests"}, "cp39", ("win_amd64",))
        assert wheel_matches(info, {"requests"}, "cp39", ("manylinux2014_x86_64",))

    def test_platform_mismatch(self) -> None:
        info = WheelInfo("numpy", "1.24.0", ("cp39",), "cp39", ("linux_x86_64",))
        assert not wheel_matches(info, {"numpy"}, "cp39", ("win_amd64",))

    def test_multi_platform_tag_match(self) -> None:
        info = WheelInfo("numpy", "1.24.0", ("cp39",), "cp39", ("manylinux2014_x86_64", "manylinux_2_28_x86_64"))
        assert wheel_matches(info, {"numpy"}, "cp39", ("manylinux_2_28_x86_64",))


class TestFspackWheelCacheDir:
    """fspack 缓存目录路径。."""

    def test_path_structure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = fspack_wheel_cache_dir()
        assert result == tmp_path / ".fspack" / "cache" / "wheels"
