"""fspack 公开 Python API（供脚本/CI 以库方式调用）.

CLI 内部模块不保证稳定；需要以 Python 方式集成时请只依赖本模块导出的符号。
本模块会加载构建/打包/运行相关模块，``import fspack.api`` 不适用于 ``--help``
这类轻路径。
"""

from __future__ import annotations

from fspack.builder import build, clean_dist, resolve_project_info
from fspack.packaging.installer import ReleaseRequest, build_release
from fspack.runner import RunOptions, run

__all__ = [
    "ReleaseRequest",
    "RunOptions",
    "build",
    "build_release",
    "clean_dist",
    "resolve_project_info",
    "run",
]
