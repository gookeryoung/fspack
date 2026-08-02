"""平台抽象：目标平台枚举与平台相关常量.

顶部仅导入 ``enum``；标准库 ``platform`` 延迟到 :func:`detect_platform`
内导入——Windows 上 ``import platform`` 连带加载 ``_wmi``（~1.5ms），
而 ``Platform`` 枚举本身（如 ``--target`` 解析、类型注解）无需它。
"""

from __future__ import annotations

import enum

__all__ = ["MACOS_ARCHS", "Platform", "detect_platform", "libpython_so", "wheel_platform_tags"]


class Platform(enum.Enum):
    """目标平台：Windows、Linux 或 macOS."""

    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"


# macOS 支持的 CPU 架构（python-build-standalone 提供 x86_64 + arm64 tarball）
MACOS_ARCHS = ("x86_64", "arm64")


def detect_platform() -> Platform:
    """根据当前系统识别目标平台."""
    import platform as _platform

    system = _platform.system()
    if system == "Windows":
        return Platform.WINDOWS
    if system == "Darwin":
        return Platform.MACOS
    return Platform.LINUX


def wheel_platform_tags(platform: Platform) -> tuple[str, ...]:
    """返回 pip download --platform 用的 wheel 平台标签列表。

    Linux 返回多个标签：manylinux2014（=manylinux_2_17）覆盖较老 wheel，
    manylinux_2_28 覆盖 PySide6 6.3+、numpy 2.x 等要求 glibc 2.28+ 的现代库。
    pip download --platform 可重复指定，匹配任一标签。

    macOS 返回 x86_64 与 arm64 双架构标签（universal wheel 优先匹配，
    否则按本机架构选择）：macosx_11_0_x86_64 覆盖 Intel Mac（macOS 11.0+），
    macosx_11_0_arm64 覆盖 Apple Silicon（macOS 11.0+）。
    """
    if platform is Platform.WINDOWS:
        return ("win_amd64",)
    if platform is Platform.MACOS:
        return ("macosx_11_0_x86_64", "macosx_11_0_arm64")
    return ("manylinux2014_x86_64", "manylinux_2_28_x86_64")


def libpython_so(py_xy: str, platform: Platform) -> str:
    """返回 libpython 文件名（py_xy 形如 python311）."""
    dotted = f"{py_xy[6]}.{py_xy[7:]}"
    if platform is Platform.WINDOWS:
        suffix = ".dll"
    elif platform is Platform.MACOS:
        suffix = ".dylib"
    else:
        suffix = ".so"
    return f"libpython{dotted}{suffix}"
