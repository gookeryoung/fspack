"""Linux 安装包生成：tar.gz 便携包 + .deb 安装包。."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from fspack.builder import build
from fspack.config import MirrorConfig, ProjectInfo
from fspack.exceptions import InstallerError
from fspack.platform import Platform
from fspack.project import DEFAULT_LINUX_PY_VERSION, parse_project

__all__ = ["build_deb", "build_linux_installer", "build_tarball"]

_logger = logging.getLogger(__name__)

_IGNORE = shutil.ignore_patterns("release")


def build_tarball(dist_dir: Path, name: str, version: str, release_dir: Path) -> Path:
    """打包 dist 为 tar.gz 便携包，返回包路径。

    tar.gz 内顶层目录为 ``<name>-<version>-linux``，解压后即可运行。
    排除 dist/release/ 避免安装包递归打包自身。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    base = f"{name}-{version}-linux"
    staging = release_dir / base
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(dist_dir, staging, ignore=_IGNORE)
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
    deb_base = f"{info.name}_{info.version}_amd64"
    staging = release_dir / deb_base

    if staging.exists():
        shutil.rmtree(staging)

    pkg_dir = staging / "usr" / "lib" / info.name
    shutil.copytree(dist_dir, pkg_dir, ignore=_IGNORE)

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
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise InstallerError("未找到 dpkg-deb，请安装 dpkg-dev（如 sudo apt install -y dpkg-dev）") from e
    except subprocess.CalledProcessError as e:
        raise InstallerError(f"dpkg-deb 构建失败:\n{e.stderr}") from e

    shutil.rmtree(staging)
    _logger.info("已生成 .deb 安装包: %s", deb_path)
    return deb_path


def build_linux_installer(
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str = DEFAULT_LINUX_PY_VERSION,
    no_build: bool = False,
    dist_dir: Path | None = None,
) -> Path:
    """编排：可选 build → tar.gz 便携包 → .deb 安装包，返回 .deb 路径。."""
    project_dir = Path(project_dir).resolve()
    dist = dist_dir or project_dir / "dist"
    if no_build:
        if not dist.is_dir():
            raise InstallerError(f"未找到 dist 目录: {dist}（请先执行 fsp b）")
    else:
        build(project_dir, mirror, py_version, dist_dir=dist, target=Platform.LINUX)
    info = parse_project(project_dir, py_version)
    exe = dist / info.name
    if not exe.is_file():
        raise InstallerError(f"未找到已构建的可执行文件: {exe}（请先执行 fsp b）")
    release = dist / "release"
    build_tarball(dist, info.name, info.version, release)
    return build_deb(dist, info, release)
