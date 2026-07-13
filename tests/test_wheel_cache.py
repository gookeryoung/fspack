"""wheel_cache 模块测试：wheel 文件名解析、缓存搜索、收割与回写。."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.wheel_cache import (
    WheelInfo,
    fspack_wheel_cache_dir,
    get_pip_cache_dir,
    get_uv_cache_dir,
    harvest_external_caches,
    normalize_name,
    parse_wheel_filename,
    save_to_cache,
    search_cache_dir,
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


class TestSearchCacheDir:
    """缓存目录搜索。."""

    def test_finds_matching_wheels(self, tmp_path: Path) -> None:
        (tmp_path / "PySide2-5.15.2.1-5.15.2-cp39-none-win_amd64.whl").touch()
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "shiboken2-5.15.2.1-cp39-none-win_amd64.whl").touch()
        (tmp_path / "requests-2.31.0-py3-none-any.whl").touch()
        (tmp_path / "other-1.0-cp38-cp38-win_amd64.whl").touch()

        result = search_cache_dir(tmp_path, {"pyside2", "shiboken2"}, "cp39", ("win_amd64",))
        names = {p.name for p in result}
        assert "PySide2-5.15.2.1-5.15.2-cp39-none-win_amd64.whl" in names
        assert "shiboken2-5.15.2.1-cp39-none-win_amd64.whl" in names
        assert "requests-2.31.0-py3-none-any.whl" not in names

    def test_empty_dir(self, tmp_path: Path) -> None:
        result = search_cache_dir(tmp_path, {"numpy"}, "cp39", ("win_amd64",))
        assert result == []


class TestHarvestExternalCaches:
    """从外部缓存收割 wheel。."""

    def test_harvest_copies_matching_wheels(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        uv_cache = tmp_path / "uv_cache"
        uv_cache.mkdir()
        (uv_cache / "PySide2-5.15.2.1-5.15.2-cp39-none-win_amd64.whl").write_bytes(b"pyside2-content")

        monkeypatch.setattr("fspack.wheel_cache.get_uv_cache_dir", lambda: uv_cache)
        monkeypatch.setattr("fspack.wheel_cache._iter_external_cache_dirs", lambda: [uv_cache])

        dest = tmp_path / "dest"
        count = harvest_external_caches({"pyside2"}, "3.9.13", ("win_amd64",), dest)
        assert count == 1
        assert (dest / "PySide2-5.15.2.1-5.15.2-cp39-none-win_amd64.whl").read_bytes() == b"pyside2-content"

    def test_harvest_skips_existing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        uv_cache = tmp_path / "uv_cache"
        uv_cache.mkdir()
        whl_name = "PySide2-5.15.2.1-5.15.2-cp39-none-win_amd64.whl"
        (uv_cache / whl_name).write_bytes(b"content")

        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / whl_name).write_bytes(b"existing")

        monkeypatch.setattr("fspack.wheel_cache._iter_external_cache_dirs", lambda: [uv_cache])
        count = harvest_external_caches({"pyside2"}, "3.9.13", ("win_amd64",), dest)
        assert count == 0
        assert (dest / whl_name).read_bytes() == b"existing"

    def test_harvest_no_matching(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        uv_cache = tmp_path / "uv_cache"
        uv_cache.mkdir()
        (uv_cache / "numpy-1.0-cp39-cp39-win_amd64.whl").touch()

        monkeypatch.setattr("fspack.wheel_cache._iter_external_cache_dirs", lambda: [uv_cache])
        dest = tmp_path / "dest"
        count = harvest_external_caches({"flask"}, "3.9.13", ("win_amd64",), dest)
        assert count == 0

    def test_harvest_no_external_caches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fspack.wheel_cache._iter_external_cache_dirs", lambda: [None, None])
        dest = tmp_path / "dest"
        count = harvest_external_caches({"flask"}, "3.9.13", ("win_amd64",), dest)
        assert count == 0


class TestSaveToCache:
    """wheel 回写缓存。."""

    def test_save_new_wheels(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        whl = src / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.write_bytes(b"content")

        cache = tmp_path / "cache"
        count = save_to_cache([whl], cache)
        assert count == 1
        assert (cache / whl.name).read_bytes() == b"content"

    def test_save_skips_existing(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        whl = src / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
        whl.write_bytes(b"new")

        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / whl.name).write_bytes(b"old")

        count = save_to_cache([whl], cache)
        assert count == 0
        assert (cache / whl.name).read_bytes() == b"old"


class TestGetUvCacheDir:
    """uv 缓存目录发现。."""

    def test_uv_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fspack.wheel_cache.shutil.which", lambda name: None)
        assert get_uv_cache_dir() is None

    def test_uv_cache_dir_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("fspack.wheel_cache.shutil.which", lambda name: "/usr/bin/uv")

        class _Result:
            stdout = str(tmp_path) + "\n"
            stderr = ""

        monkeypatch.setattr("fspack.wheel_cache.subprocess.run", lambda *a, **kw: _Result())
        assert get_uv_cache_dir() == tmp_path

    def test_uv_cache_dir_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fspack.wheel_cache.shutil.which", lambda name: "/usr/bin/uv")

        def raise_error(*a: Any, **kw: Any) -> Any:
            raise subprocess.CalledProcessError(1, "uv")

        monkeypatch.setattr("fspack.wheel_cache.subprocess.run", raise_error)
        assert get_uv_cache_dir() is None


class TestGetPipCacheDir:
    """pip 缓存目录发现。."""

    def test_pip_cache_dir_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        class _Result:
            stdout = str(tmp_path) + "\n"
            stderr = ""

        monkeypatch.setattr("fspack.wheel_cache.subprocess.run", lambda *a, **kw: _Result())
        assert get_pip_cache_dir("/usr/bin/python") == tmp_path

    def test_pip_cache_dir_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_error(*a: Any, **kw: Any) -> Any:
            raise subprocess.CalledProcessError(1, "pip")

        monkeypatch.setattr("fspack.wheel_cache.subprocess.run", raise_error)
        assert get_pip_cache_dir("/usr/bin/python") is None


class TestFspackWheelCacheDir:
    """fspack 缓存目录路径。."""

    def test_path_structure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = fspack_wheel_cache_dir()
        assert result == tmp_path / ".fspack" / "cache" / "wheels"


class TestIterExternalCacheDirs:
    """_iter_external_cache_dirs 聚合 uv/pip 缓存目录。."""

    def test_both_available(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        uv_dir = tmp_path / "uv"
        uv_dir.mkdir()
        pip_dir = tmp_path / "pip"
        pip_dir.mkdir()
        monkeypatch.setattr("fspack.wheel_cache.get_uv_cache_dir", lambda: uv_dir)
        monkeypatch.setattr("fspack.wheel_cache.get_pip_cache_dir", lambda py: pip_dir)
        monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

        from fspack.wheel_cache import _iter_external_cache_dirs

        result = _iter_external_cache_dirs()
        assert result == [uv_dir, pip_dir]

    def test_pip_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        uv_dir = tmp_path / "uv"
        uv_dir.mkdir()
        monkeypatch.setattr("fspack.wheel_cache.get_uv_cache_dir", lambda: uv_dir)
        from fspack.exceptions import DependencyError

        def raise_dep_error() -> str:
            raise DependencyError("no pip")

        monkeypatch.setattr("fspack.builder._find_pip_python", raise_dep_error)

        from fspack.wheel_cache import _iter_external_cache_dirs

        result = _iter_external_cache_dirs()
        assert result == [uv_dir]

    def test_uv_unavailable(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pip_dir = tmp_path / "pip"
        pip_dir.mkdir()
        monkeypatch.setattr("fspack.wheel_cache.get_uv_cache_dir", lambda: None)
        monkeypatch.setattr("fspack.wheel_cache.get_pip_cache_dir", lambda py: pip_dir)
        monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

        from fspack.wheel_cache import _iter_external_cache_dirs

        result = _iter_external_cache_dirs()
        assert result == [None, pip_dir]
