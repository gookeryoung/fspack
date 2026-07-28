"""缓存目录与离线模式配置测试."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.config.cache import (
    cache_root,
    ccache_cache_dir,
    embed_cache_dir,
    is_offline,
    loader_cache_dir,
    nuitka_cache_dir,
    standalone_cache_dir,
    tkinter_cache_dir,
    wheel_cache_dir,
)


class TestCacheRoot:
    """``cache_root`` 环境变量覆盖与默认值."""

    def test_cache_root_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未设置环境变量时回退到 ``~/.fspack/cache``."""
        monkeypatch.delenv("FSPACK_CACHE_DIR", raising=False)
        assert cache_root() == Path.home() / ".fspack" / "cache"

    def test_cache_root_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """``FSPACK_CACHE_DIR`` 覆盖默认缓存根目录."""
        custom = tmp_path / "custom-cache"
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(custom))
        assert cache_root() == custom

    def test_cache_root_env_empty_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``FSPACK_CACHE_DIR`` 设为空字符串时回退到默认值."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", "")
        assert cache_root() == Path.home() / ".fspack" / "cache"


class TestSubCacheDirs:
    """各子模块缓存目录基于 ``cache_root`` 派生."""

    def test_embed_cache_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """embed 缓存目录 = ``<cache_root>/embed``."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
        assert embed_cache_dir() == tmp_path / "embed"

    def test_standalone_cache_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """standalone 缓存目录 = ``<cache_root>/standalone``."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
        assert standalone_cache_dir() == tmp_path / "standalone"

    def test_wheel_cache_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """wheel 缓存目录 = ``<cache_root>/wheels``."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
        assert wheel_cache_dir() == tmp_path / "wheels"

    def test_nuitka_cache_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """nuitka 缓存目录 = ``<cache_root>/nuitka``."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
        assert nuitka_cache_dir() == tmp_path / "nuitka"

    def test_loader_cache_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """loader 缓存目录 = ``<cache_root>/loaders``."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
        assert loader_cache_dir() == tmp_path / "loaders"

    def test_ccache_cache_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ccache 二进制缓存目录 = ``<cache_root>/ccache``."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
        assert ccache_cache_dir() == tmp_path / "ccache"

    def test_tkinter_cache_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """tkinter 缓存目录 = ``<cache_root>/tkinter``."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
        assert tkinter_cache_dir() == tmp_path / "tkinter"

    def test_all_subdirs_consistent_with_root(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """所有子目录共享同一 cache_root（环境变量一处设置全局生效）."""
        monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
        root = cache_root()
        assert embed_cache_dir().parent == root
        assert standalone_cache_dir().parent == root
        assert wheel_cache_dir().parent == root
        assert nuitka_cache_dir().parent == root
        assert loader_cache_dir().parent == root
        assert ccache_cache_dir().parent == root
        assert tkinter_cache_dir().parent == root


class TestIsOffline:
    """``FSPACK_OFFLINE`` 环境变量解析."""

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", "ON"])
    def test_is_offline_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """``1``/``true``/``yes``/``on``（不区分大小写）启用离线模式."""
        monkeypatch.setenv("FSPACK_OFFLINE", value)
        assert is_offline() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "random"])
    def test_is_offline_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """``0``/``false``/``no``/``off``/空/其他值关闭离线模式."""
        monkeypatch.setenv("FSPACK_OFFLINE", value)
        assert is_offline() is False

    def test_is_offline_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未设置环境变量时默认关闭离线模式."""
        monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
        assert is_offline() is False
