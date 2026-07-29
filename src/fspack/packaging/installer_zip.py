"""跨平台 zip 便携包生成.

从 :mod:`fspack.packaging.installer` 拆分而来，封装 zip 便携包逻辑：
可选 build → 校验可执行文件 → 打包 zip（staging 目录 + make_archive）。

zip 跨平台解压即用，无需安装。依赖 :mod:`fspack.packaging.installer` 提供：
``_run_stage``/``_prepare_dist``/``_check_exe``/``_release_base``/
``_DIST_INTERMEDIATE_EXCLUDES``。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Sequence

from fspack.config import MirrorConfig, ProjectInfo
from fspack.console import console
from fspack.packaging.installer import (
    _DIST_INTERMEDIATE_EXCLUDES,
    _check_exe,
    _prepare_dist,
    _release_base,
    _run_stage,
)
from fspack.platform import Platform
from fspack.progress import BuildTracker

__all__ = ["build_zip"]

_logger = logging.getLogger("fspack.packaging.installer")

# zip 打包排除模式：release 目录 + 构建中间文件（与 NSIS /x 排除一致）
_ZIP_IGNORE = shutil.ignore_patterns("release", *_DIST_INTERMEDIATE_EXCLUDES)


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
) -> Path:
    """编排：可选 build → 校验可执行文件 → 打包 zip 便携包，返回 zip 路径。

    zip 跨平台解压即用，无需安装。文件名 ``<name>-<version>-<platform>.zip``，
    内顶层目录同名，解压后不污染当前目录。排除 ``dist/release/`` 避免递归打包。
    """
    own_tracker = tracker is None
    tk = tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(project_dir, mirror, py_version, no_build, dist_dir, target, extras=extras, tracker=tk)
    _check_exe(dist, info, target)
    release = dist / "release"
    zip_name = f"{_release_base(info, 'windows' if target is Platform.WINDOWS else 'linux')}.zip"
    result = _run_stage(
        tk,
        "生成 zip 便携包",
        lambda: _make_zip(dist, info, release, target),
        detail=zip_name,
    )
    console.success(f"zip 便携包已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result


def _make_zip(dist_dir: Path, info: ProjectInfo, release_dir: Path, target: Platform) -> Path:
    """打包 dist 为 zip 便携包，返回 zip 路径。

    顶层目录 ``<name>-<version>-<py_tag>-<platform>-slim``，排除 ``release/`` 子目录。
    用 staging 目录 + ``shutil.make_archive`` 实现，与 :func:`build_tarball` 风格一致。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    platform_suffix = "windows" if target is Platform.WINDOWS else "linux"
    base = _release_base(info, platform_suffix)
    staging = release_dir / base
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(dist_dir, staging, ignore=_ZIP_IGNORE)
    archive = shutil.make_archive(str(release_dir / base), "zip", root_dir=release_dir, base_dir=base)
    shutil.rmtree(staging)
    archive_path = Path(archive)
    _logger.info("已生成 zip 便携包: %s", archive_path)
    return archive_path
