"""平台抽象测试."""

from __future__ import annotations

import pytest

from fspack.platform import Platform, detect_platform, libpython_so, wheel_platform_tags


def test_platform_values() -> None:
    assert Platform.WINDOWS.value == "windows"
    assert Platform.LINUX.value == "linux"


def test_detect_platform_returns_platform() -> None:
    assert isinstance(detect_platform(), Platform)


def test_detect_platform_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.platform._platform.system", lambda: "Windows")
    assert detect_platform() == Platform.WINDOWS


def test_detect_platform_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.platform._platform.system", lambda: "Linux")
    assert detect_platform() == Platform.LINUX


def test_wheel_platform_tags() -> None:
    assert wheel_platform_tags(Platform.WINDOWS) == ("win_amd64",)
    assert wheel_platform_tags(Platform.LINUX) == ("manylinux2014_x86_64", "manylinux_2_28_x86_64")


def test_libpython_so_windows() -> None:
    assert libpython_so("python311", Platform.WINDOWS) == "libpython3.11.dll"


def test_libpython_so_linux() -> None:
    assert libpython_so("python311", Platform.LINUX) == "libpython3.11.so"


def test_libpython_so_310() -> None:
    assert libpython_so("python310", Platform.LINUX) == "libpython3.10.so"
