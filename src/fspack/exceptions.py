"""fspack 异常层级。."""

from __future__ import annotations

__all__ = [
    "DependencyError",
    "EmbedError",
    "FspackError",
    "LoaderError",
    "ProjectError",
]


class FspackError(Exception):
    """fspack 公共异常基类。."""


class ProjectError(FspackError):
    """项目解析或入口识别错误。."""


class EmbedError(FspackError):
    """embed python 下载或配置错误。."""


class LoaderError(FspackError):
    """C loader 源码生成或编译错误。."""


class DependencyError(FspackError):
    """依赖下载或解包错误。."""
