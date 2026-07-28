"""平台抽象测试."""

from __future__ import annotations

import pytest

from fspack.platform import MACOS_ARCHS, Platform, detect_platform, libpython_so, wheel_platform_tags


def test_platform_values() -> None:
    assert Platform.WINDOWS.value == "windows"
    assert Platform.LINUX.value == "linux"
    assert Platform.MACOS.value == "macos"


def test_macos_archs_constant() -> None:
    """MACOS_ARCHS 包含 x86_64 与 arm64 两个架构."""
    assert MACOS_ARCHS == ("x86_64", "arm64")


def test_detect_platform_returns_platform() -> None:
    assert isinstance(detect_platform(), Platform)


def test_detect_platform_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.platform._platform.system", lambda: "Windows")
    assert detect_platform() == Platform.WINDOWS


def test_detect_platform_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.platform._platform.system", lambda: "Linux")
    assert detect_platform() == Platform.LINUX


def test_detect_platform_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """detect_platform 识别 Darwin 系统为 MACOS."""
    monkeypatch.setattr("fspack.platform._platform.system", lambda: "Darwin")
    assert detect_platform() == Platform.MACOS


def test_wheel_platform_tags() -> None:
    assert wheel_platform_tags(Platform.WINDOWS) == ("win_amd64",)
    assert wheel_platform_tags(Platform.LINUX) == ("manylinux2014_x86_64", "manylinux_2_28_x86_64")


def test_wheel_platform_tags_macos() -> None:
    """macOS 返回 x86_64 与 arm64 双架构标签（macOS 11.0+）."""
    tags = wheel_platform_tags(Platform.MACOS)
    assert "macosx_11_0_x86_64" in tags
    assert "macosx_11_0_arm64" in tags


def test_libpython_so_windows() -> None:
    assert libpython_so("python311", Platform.WINDOWS) == "libpython3.11.dll"


def test_libpython_so_linux() -> None:
    assert libpython_so("python311", Platform.LINUX) == "libpython3.11.so"


def test_libpython_so_macos() -> None:
    """macOS libpython 后缀为 .dylib."""
    assert libpython_so("python311", Platform.MACOS) == "libpython3.11.dylib"
    assert libpython_so("python310", Platform.MACOS) == "libpython3.10.dylib"


def test_libpython_so_310() -> None:
    assert libpython_so("python310", Platform.LINUX) == "libpython3.10.so"
