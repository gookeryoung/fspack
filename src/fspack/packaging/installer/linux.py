"""Linux 安装包生成：tar.gz 便携包与 .deb 安装包.

从 :mod:`fspack.packaging.installer.base` 拆分而来，封装 Linux 安装包全部逻辑：
tar.gz 打包、.deb 构造（DEBIAN/control + /usr/lib + /usr/bin wrapper）、
单格式编排（build_tarball_release / build_deb_release）。

依赖 :mod:`fspack.packaging.installer.base` 提供 ``Installer`` 基类与
``_run_stage``/``_run_tool``；:mod:`fspack.packaging.installer.dist_prep` 提供
``_prepare_dist``/``_check_exe``/``_py_tag``/``_release_base``、
``_make_staged_archive``（tar.gz 打包）、``_DIST_IGNORE``（打包排除模式）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # noqa: F401  # 保留 patch 路径 fspack.packaging.installer.linux.subprocess.run
from pathlib import Path

from fspack._compat import override
from fspack.config import ProjectInfo
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.packaging.installer.base import Installer, _run_stage, _run_tool
from fspack.packaging.installer.dist_prep import (
    _DIST_IGNORE,
    _check_exe,
    _make_staged_archive,
    _prepare_dist,
    _py_tag,
    _release_base,
)
from fspack.packaging.installer.request import _NO_SIGN, ReleaseRequest, SignOptions
from fspack.platform import Platform
from fspack.progress import BuildTracker

__all__ = [
    "LinuxInstaller",
    "build_deb",
    "build_deb_release",
    "build_tarball",
    "build_tarball_release",
    "sign_deb_file",
]

_logger = logging.getLogger("fspack.packaging.installer")


def _deb_arch() -> str:
    """返回 .deb 包 Architecture 字段值：固定 ``amd64``（与 Linux 目标 runtime 一致）.

    Linux 目标 runtime 固定下载 python-build-standalone 的
    ``x86_64-unknown-linux-gnu`` tarball（见 :func:`standalone_tarball_name`），
    .deb 架构须与 runtime 实际架构一致，**与构建机架构无关**——此前按
    ``platform.machine()`` 取构建机架构，macOS arm64 runner 交叉构建 Linux
    目标时 .deb 被误标 arm64 而装不进 amd64 系统。未来支持 Linux arm64
    目标时在此参数化。
    """
    return "amd64"


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
        """返回 ``<entry>``（无后缀，多入口项目取默认入口名）。"""
        return info.default_entry.name

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


def build_tarball(dist_dir: Path, info: ProjectInfo, release_dir: Path, *, keep_staging: bool = False) -> Path:
    """打包 dist 为 tar.gz 便携包，返回包路径。

    tar.gz 内顶层目录为 ``<name>-<version>-<py_tag>-linux-slim``，解压后即可运行。
    排除 dist/release/ 避免安装包递归打包自身。

    ``keep_staging=True`` 时打包后保留 staging 目录（Linux ``all`` 场景 zip 复用，
    消除一次 dist 全量 copytree；由最后一个复用格式负责清理）。
    """
    base = _release_base(info, "linux")
    archive_path = _make_staged_archive(dist_dir, release_dir, base, "gztar", keep_staging=keep_staging)
    _logger.info("已生成 tar.gz 便携包: %s", archive_path)
    return archive_path


def build_deb(dist_dir: Path, info: ProjectInfo, release_dir: Path) -> Path:
    """构造 .deb 安装包，返回 .deb 路径。

    数据布局：``/usr/lib/<name>/``（dist 内容）+ ``/usr/bin/<name>``（wrapper 调用可执行文件）。
    排除 dist/release/ 避免安装包递归打包自身。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    deb_base = f"{info.name}_{info.version}-{_py_tag(info)}-slim_{_deb_arch()}"
    staging = release_dir / deb_base

    if staging.exists():
        shutil.rmtree(staging)

    pkg_dir = staging / "usr" / "lib" / info.name
    shutil.copytree(dist_dir, pkg_dir, ignore=_DIST_IGNORE)

    bin_dir = staging / "usr" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / info.name
    # wrapper 命令名用项目名；可执行文件名用默认入口名（多入口项目与构建产物命名一致）
    wrapper.write_text(f'#!/bin/sh\nexec /usr/lib/{info.name}/{info.default_entry.name} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)

    debian_dir = staging / "DEBIAN"
    debian_dir.mkdir(parents=True, exist_ok=True)
    (debian_dir / "control").write_text(
        f"Package: {info.name}\n"
        f"Version: {info.version}\n"
        f"Architecture: {_deb_arch()}\n"
        "Maintainer: fspack\n"
        f"Description: {info.name} 打包的应用\n",
        encoding="utf-8",
    )

    deb_path = release_dir / f"{deb_base}.deb"
    _run_tool(
        ["dpkg-deb", "--build", str(staging), str(deb_path)],
        not_found_msg="未找到 dpkg-deb，请安装 dpkg-dev（如 sudo apt install -y dpkg-dev）",
        fail_prefix="dpkg-deb 构建失败",
    )

    shutil.rmtree(staging)
    _logger.info("已生成 .deb 安装包: %s", deb_path)
    return deb_path


# ---- 单格式编排（tar.gz / deb）----


def build_tarball_release(req: ReleaseRequest, *, keep_staging: bool = False) -> Path:
    """编排：可选 build → 校验可执行文件 → 生成 tar.gz 便携包，返回包路径。

    ``keep_staging`` 透传 :func:`build_tarball`（多格式共享 staging 场景）。
    """
    own_tracker = req.tracker is None
    tk = req.tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(req, Platform.LINUX)
    _check_exe(dist, info, Platform.LINUX)
    release = dist / "release"
    tar_name = f"{_release_base(info, 'linux')}.tar.gz"
    result = _run_stage(
        tk,
        "生成 tar.gz 便携包",
        lambda: build_tarball(dist, info, release, keep_staging=keep_staging),
        detail=tar_name,
    )
    console.success(f"tar.gz 便携包已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result


def build_deb_release(req: ReleaseRequest, *, sign: SignOptions = _NO_SIGN) -> Path:
    """编排：可选 build → 校验可执行文件 → 构造 .deb → 可选 GPG 签名.

    ``sign.sign_deb=True`` 时用 ``gpg --detach-sign --armor`` 对 .deb 做分离签名，
    产出 ``<deb>.asc`` 签名文件。``sign.sign_deb_key`` 指定密钥 ID，未指定时用
    GPG 默认密钥。签名失败降级为 warning 不阻断构建。
    """
    own_tracker = req.tracker is None
    tk = req.tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(req, Platform.LINUX)
    _check_exe(dist, info, Platform.LINUX)
    release = dist / "release"
    deb_name = f"{info.name}_{info.version}-{_py_tag(info)}-slim_amd64.deb"
    result = _run_stage(
        tk,
        "构造 .deb 安装包",
        lambda: build_deb(dist, info, release),
        detail=deb_name,
    )
    if sign.sign_deb:
        with tk.stage("签名 .deb") as st:
            try:
                sign_deb_file(result, sign.sign_deb_key)
                st.processed(1)
                st.set_detail(f"{result.name}.asc")
            except InstallerError as e:
                _logger.warning("签名 .deb 失败，跳过: %s", e)
                st.set_detail("签名失败")
    console.success(f".deb 安装包已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result


def sign_deb_file(deb_path: Path, key_id: str | None = None) -> Path:
    """用 GPG 对 .deb 做分离签名，返回 .asc 签名文件路径.

    调用 ``gpg --detach-sign --armor [--local-user <key_id>] <deb>``，
    产出 ``<deb>.asc`` ASCII 签名文件。

    Args:
        deb_path: 待签名的 .deb 文件路径
        key_id: GPG 密钥 ID（如 ``0x12345678`` 或 ``user@example.com``），
            None 时用 GPG 默认密钥

    Returns:
        签名文件路径（``<deb>.asc``）

    Raises:
        InstallerError: gpg 未找到或签名失败
    """
    cmd: list[str] = ["gpg", "--detach-sign", "--armor"]
    if key_id:
        cmd.extend(["--local-user", key_id])
    cmd.append(str(deb_path))
    _logger.info("签名 .deb: %s", deb_path.name)
    _run_tool(
        cmd,
        not_found_msg="未找到 gpg，请安装 GnuPG（如 sudo apt install -y gnupg）",
        fail_prefix=f"gpg 签名失败 {deb_path.name}",
    )
    # gpg --detach-sign 产出 "<deb>.asc"；原 with_suffix+回退写法两次结果恒相同，属死代码，此处简化
    return Path(str(deb_path) + ".asc")
