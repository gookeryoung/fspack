"""平台抽象：目标平台枚举与平台相关常量。."""

from __future__ import annotations

import enum
import platform as _platform

__all__ = ["Platform", "detect_platform", "libpython_so", "wheel_platform_tags"]


class Platform(enum.Enum):
    """目标平台：Windows 或 Linux。."""

    WINDOWS = "windows"
    LINUX = "linux"


def detect_platform() -> Platform:
    """根据当前系统识别目标平台。."""
    if _platform.system() == "Windows":
        return Platform.WINDOWS
    return Platform.LINUX


def wheel_platform_tags(platform: Platform) -> tuple[str, ...]:
    """返回 pip download --platform 用的 wheel 平台标签列表。

    Linux 返回多个标签：manylinux2014（=manylinux_2_17）覆盖较老 wheel，
    manylinux_2_28 覆盖 PySide6 6.3+、numpy 2.x 等要求 glibc 2.28+ 的现代库。
    pip download --platform 可重复指定，匹配任一标签。
    """
    if platform is Platform.WINDOWS:
        return ("win_amd64",)
    return ("manylinux2014_x86_64", "manylinux_2_28_x86_64")


def libpython_so(py_xy: str, platform: Platform) -> str:
    """返回 libpython 文件名（py_xy 形如 python311）。."""
    dotted = f"{py_xy[6]}.{py_xy[7:]}"
    suffix = ".dll" if platform is Platform.WINDOWS else ".so"
    return f"libpython{dotted}{suffix}"
