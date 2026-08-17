"""跨平台 zip 便携包生成.

从 :mod:`fspack.packaging.installer.base` 拆分而来，封装 zip 便携包逻辑：
可选 build → 校验可执行文件 → 打包 zip（staging 目录 + make_archive）。

zip 跨平台解压即用，无需安装。依赖 :mod:`fspack.packaging.installer.base` 提供：
``_run_stage``/``_prepare_dist``/``_check_exe``/``_release_base``、
``_make_staged_archive``（zip 打包）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from fspack.config import MirrorConfig, ProjectInfo
from fspack.console import console
from fspack.packaging.installer.base import (
    _check_exe,
    _make_staged_archive,
    _prepare_dist,
    _release_base,
    _run_stage,
)
from fspack.platform import Platform
from fspack.progress import BuildTracker

__all__ = ["build_zip"]

_logger = logging.getLogger("fspack.packaging.installer")


def _zip_platform_suffix(target: Platform) -> str:
    """目标平台 → zip 文件名平台后缀（windows/macos/linux）.

    修复前 macOS 目标误用 ``"linux"``，导致 zip 文件名与实际平台不符。
    """
    if target is Platform.WINDOWS:
        return "windows"
    if target is Platform.MACOS:
        return "macos"
    return "linux"


def build_zip(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    target: Platform = Platform.WINDOWS,
    *,
    tracker: BuildTracker | None = None,
    extras: Sequence[str] | None = None,
    reuse_staging: bool = False,
) -> Path:
    """编排：可选 build → 校验可执行文件 → 打包 zip 便携包，返回 zip 路径。

    zip 跨平台解压即用，无需安装。文件名 ``<name>-<version>-<platform>.zip``，
    内顶层目录同名，解压后不污染当前目录。排除 ``dist/release/`` 避免递归打包。

    ``reuse_staging=True`` 时复用同 base 的既有 staging（Linux ``all`` 场景由
    tar.gz 保留，跳过一次 dist 全量 copytree；staging 不存在时自动回退）。
    """
    own_tracker = tracker is None
    tk = tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(project_dir, mirror, py_version, no_build, dist_dir, target, extras=extras, tracker=tk)
    _check_exe(dist, info, target)
    release = dist / "release"
    zip_name = f"{_release_base(info, _zip_platform_suffix(target))}.zip"
    result = _run_stage(
        tk,
        "生成 zip 便携包",
        lambda: _make_zip(dist, info, release, target, reuse_staging=reuse_staging),
        detail=zip_name,
    )
    console.success(f"zip 便携包已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result


def _make_zip(
    dist_dir: Path,
    info: ProjectInfo,
    release_dir: Path,
    target: Platform,
    *,
    reuse_staging: bool = False,
) -> Path:
    """打包 dist 为 zip 便携包，返回 zip 路径。

    顶层目录 ``<name>-<version>-<py_tag>-<platform>-slim``，排除 ``release/`` 子目录。
    用 staging 目录 + ``shutil.make_archive`` 实现，与 :func:`build_tarball` 风格一致。
    ``reuse_staging=True`` 时复用同 base 既有 staging（Linux 下与 tar.gz 同名共享）。
    """
    platform_suffix = _zip_platform_suffix(target)
    base = _release_base(info, platform_suffix)
    archive_path = _make_staged_archive(dist_dir, release_dir, base, "zip", reuse_staging=reuse_staging)
    _logger.info("已生成 zip 便携包: %s", archive_path)
    return archive_path
