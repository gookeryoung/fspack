"""构建上下文与公共常量：``BuildContext`` 数据类 + 路径 + 默认资源.

所有阶段函数共享的不可变上下文聚合在此。被 :mod:`runtime_stage`、
:mod:`deps_stage`、:mod:`compile_stage` 顶层导入，避免循环（它们之间无相互
顶层引用）。阶段函数通过 ``BuildContext`` 聚合并发参数，避免重复传递 6-8 个
参数。目标平台通过 ``ctx.cfg.target`` 访问。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.config import (
    BuildConfig,
    BuildOptions,
    ProjectInfo,
    wheel_cache_dir,
)

if TYPE_CHECKING:
    from fspack.progress import BuildTracker

__all__ = [
    "_DEFAULT_ICON",
    "_MAX_LOADER_WORKERS",
    "BuildContext",
    "default_icon_path",
    "fspack_wheel_cache_dir",
]

_logger = logging.getLogger(__name__)

# 默认 icon：打包在 fspack 包内，随 wheel 分发
# context.py 在 src/fspack/packaging/pipeline/ 下，parent.parent.parent 即 src/fspack/
_DEFAULT_ICON = Path(__file__).parent.parent.parent / "assets" / "icons" / "app.ico"

# 多入口 loader 并行编译上限（iter-133）：subprocess 释放 GIL，线程足够并行。
# 4 上限平衡并行收益与 Windows 资源限制（mingw/gcc 子进程句柄/内存），
# 与 _MAX_COMPILE_WORKERS（nuitka 模块）保持一致。
_MAX_LOADER_WORKERS = 4


@dataclass(frozen=True)
class BuildContext:
    """构建流水线共享上下文，聚合阶段函数共用的构建配置与状态.

    避免 :func:`runtime_stage._prepare_runtime`/:func:`deps_stage._analyze_dependencies`/
    :func:`deps_stage._download_dependencies`/:func:`compile_stage._compile_user_sources`/
    :func:`compile_stage._build_entry_loaders` 等阶段函数重复接收 6-8 个参数。
    目标平台通过 :attr:`cfg.target` 访问，无需单独字段。
    """

    tracker: BuildTracker
    info: ProjectInfo
    cfg: BuildConfig
    opts: BuildOptions
    runtime_dir: Path


def default_icon_path() -> Path:
    """返回 fspack 自带的默认 icon 路径（``assets/icons/app.ico``）."""
    return _DEFAULT_ICON


def fspack_wheel_cache_dir() -> Path:
    """返回 fspack wheel 缓存目录（``FSPACK_CACHE_DIR`` 环境变量 > 默认 ``~/.fspack/cache/wheels``）."""
    return wheel_cache_dir()
