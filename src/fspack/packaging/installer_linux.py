"""Linux 安装包生成：tar.gz 便携包与 .deb 安装包.

从 :mod:`fspack.packaging.installer` 拆分而来，封装 Linux 安装包全部逻辑：
tar.gz 打包、.deb 构造（DEBIAN/control + /usr/lib + /usr/bin wrapper）、
单格式编排（build_tarball_release / build_deb_release）。

依赖 :mod:`fspack.packaging.installer` 提供：
``Installer`` 基类、``_run_stage``/``_prepare_dist``/``_check_exe``/
``_py_tag``/``_release_base``/``_DIST_INTERMEDIATE_EXCLUDES``。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from fspack._compat import override
from fspack.config import MirrorConfig, ProjectInfo
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.packaging.installer import (
    _DIST_INTERMEDIATE_EXCLUDES,
    Installer,
    _check_exe,
    _prepare_dist,
    _py_tag,
    _release_base,
    _run_stage,
)
from fspack.platform import Platform
from fspack.progress import BuildTracker

__all__ = [
    "LinuxInstaller",
    "build_deb",
    "build_deb_release",
    "build_tarball",
    "build_tarball_release",
]

_logger = logging.getLogger("fspack.packaging.installer")

# Linux 打包排除模式：release 目录 + 构建中间文件（与 NSIS /x 排除一致）
_LINUX_IGNORE = shutil.ignore_patterns("release", *_DIST_INTERMEDIATE_EXCLUDES)


class LinuxInstaller(Installer):
    """Linux 安装包生成器：tar.gz 便携包 + .deb 安装包。"""

    @classmethod
    @override
    def target_platform(cls) -> Platform:
        """Linux 平台。"""
        return Platform.LINUX

    @classmethod
    @override
    def exe_filename(cls, info: ProjectInfo) -> str:
        """返回 ``<name>``（无后缀）。"""
        return info.name

    @classmethod
    @override
    def build_package(
        cls,
        dist_dir: Path,
        info: ProjectInfo,
        release_dir: Path,
        *,
        tracker: BuildTracker,
    ) -> Path:
        """生成 tar.gz 便携包与 .deb 安装包，返回 .deb 路径。"""
        tar_name = f"{_release_base(info, 'linux')}.tar.gz"
        _run_stage(
            tracker,
            "生成 tar.gz 便携包",
            lambda: build_tarball(dist_dir, info, release_dir),
            detail=tar_name,
        )
        deb_name = f"{info.name}_{info.version}-{_py_tag(info)}-slim_amd64.deb"
        result = _run_stage(
            tracker,
            "构造 .deb 安装包",
            lambda: build_deb(dist_dir, info, release_dir),
            detail=deb_name,
        )
        console.success(f"安装包已生成: {result}")
        return result


def build_tarball(dist_dir: Path, info: ProjectInfo, release_dir: Path) -> Path:
    """打包 dist 为 tar.gz 便携包，返回包路径。

    tar.gz 内顶层目录为 ``<name>-<version>-<py_tag>-linux-slim``，解压后即可运行。
    排除 dist/release/ 避免安装包递归打包自身。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    base = _release_base(info, "linux")
    staging = release_dir / base
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(dist_dir, staging, ignore=_LINUX_IGNORE)
    archive = shutil.make_archive(str(release_dir / base), "gztar", root_dir=release_dir, base_dir=base)
    shutil.rmtree(staging)
    archive_path = Path(archive)
    _logger.info("已生成 tar.gz 便携包: %s", archive_path)
    return archive_path


def build_deb(dist_dir: Path, info: ProjectInfo, release_dir: Path) -> Path:
    """构造 .deb 安装包，返回 .deb 路径。

    数据布局：``/usr/lib/<name>/``（dist 内容）+ ``/usr/bin/<name>``（wrapper 调用可执行文件）。
    排除 dist/release/ 避免安装包递归打包自身。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    deb_base = f"{info.name}_{info.version}-{_py_tag(info)}-slim_amd64"
    staging = release_dir / deb_base

    if staging.exists():
        shutil.rmtree(staging)

    pkg_dir = staging / "usr" / "lib" / info.name
    shutil.copytree(dist_dir, pkg_dir, ignore=_LINUX_IGNORE)

    bin_dir = staging / "usr" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / info.name
    wrapper.write_text(f'#!/bin/sh\nexec /usr/lib/{info.name}/{info.name} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)

    debian_dir = staging / "DEBIAN"
    debian_dir.mkdir(parents=True, exist_ok=True)
    (debian_dir / "control").write_text(
        f"Package: {info.name}\n"
        f"Version: {info.version}\n"
        "Architecture: amd64\n"
        "Maintainer: fspack\n"
        f"Description: {info.name} 打包的应用\n",
        encoding="utf-8",
    )

    deb_path = release_dir / f"{deb_base}.deb"
    cmd = ["dpkg-deb", "--build", str(staging), str(deb_path)]
    _logger.info("构建 .deb: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise InstallerError("未找到 dpkg-deb，请安装 dpkg-dev（如 sudo apt install -y dpkg-dev）") from e
    except subprocess.CalledProcessError as e:
        raise InstallerError(f"dpkg-deb 构建失败:\n{e.stderr}") from e

    shutil.rmtree(staging)
    _logger.info("已生成 .deb 安装包: %s", deb_path)
    return deb_path


# ---- 单格式编排（tar.gz / deb）----


def build_tarball_release(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
) -> Path:
    """编排：可选 build → 校验可执行文件 → 生成 tar.gz 便携包，返回包路径。"""
    own_tracker = tracker is None
    tk = tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(project_dir, mirror, py_version, no_build, dist_dir, Platform.LINUX)
    _check_exe(dist, info, Platform.LINUX)
    release = dist / "release"
    tar_name = f"{_release_base(info, 'linux')}.tar.gz"
    result = _run_stage(
        tk,
        "生成 tar.gz 便携包",
        lambda: build_tarball(dist, info, release),
        detail=tar_name,
    )
    console.success(f"tar.gz 便携包已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result


def build_deb_release(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
) -> Path:
    """编排：可选 build → 校验可执行文件 → 构造 .deb 安装包，返回 .deb 路径。"""
    own_tracker = tracker is None
    tk = tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(project_dir, mirror, py_version, no_build, dist_dir, Platform.LINUX)
    _check_exe(dist, info, Platform.LINUX)
    release = dist / "release"
    deb_name = f"{info.name}_{info.version}-{_py_tag(info)}-slim_amd64.deb"
    result = _run_stage(
        tk,
        "构造 .deb 安装包",
        lambda: build_deb(dist, info, release),
        detail=deb_name,
    )
    console.success(f".deb 安装包已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result
