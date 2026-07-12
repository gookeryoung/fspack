"""平台抽象测试。."""

from __future__ import annotations

from fspack.platform import Platform, detect_platform, libpython_so, wheel_platform_tag


def test_platform_values() -> None:
    assert Platform.WINDOWS.value == "windows"
    assert Platform.LINUX.value == "linux"


def test_detect_platform_returns_platform() -> None:
    assert isinstance(detect_platform(), Platform)


def test_wheel_platform_tag() -> None:
    assert wheel_platform_tag(Platform.WINDOWS) == "win_amd64"
    assert wheel_platform_tag(Platform.LINUX) == "manylinux2014_x86_64"


def test_libpython_so_windows() -> None:
    assert libpython_so("python311", Platform.WINDOWS) == "libpython3.11.dll"


def test_libpython_so_linux() -> None:
    assert libpython_so("python311", Platform.LINUX) == "libpython3.11.so"


def test_libpython_so_310() -> None:
    assert libpython_so("python310", Platform.LINUX) == "libpython3.10.so"
