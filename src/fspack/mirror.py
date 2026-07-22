"""国内镜像源配置."""

from __future__ import annotations

from fspack.config import MirrorConfig

__all__ = ["DEFAULT_MIRROR", "MIRRORS", "get_mirror"]

MIRRORS: dict[str, MirrorConfig] = {
    "huawei": MirrorConfig(
        name="华为云",
        python_base="https://mirrors.huaweicloud.com/python",
        pypi_index="https://mirrors.huaweicloud.com/pypi/simple/",
    ),
    "aliyun": MirrorConfig(
        name="阿里云",
        python_base="https://npmmirror.com/mirrors/python",
        pypi_index="https://mirrors.aliyun.com/pypi/simple/",
    ),
    "tsinghua": MirrorConfig(
        name="清华",
        python_base="https://mirrors.tuna.tsinghua.edu.cn/python",
        pypi_index="https://pypi.tuna.tsinghua.edu.cn/simple/",
    ),
}

DEFAULT_MIRROR = "aliyun"


def get_mirror(name: str | None = None) -> MirrorConfig:
    """按名称获取镜像配置，name 为 None 时返回默认镜像."""
    key = name or DEFAULT_MIRROR
    if key not in MIRRORS:
        raise KeyError(f"未知镜像源: {key}，可选: {', '.join(MIRRORS)}")
    return MIRRORS[key]
