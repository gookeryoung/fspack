"""macOS 安装包生成：.pkg 安装包与 .dmg 磁盘镜像.

从 :mod:`fspack.packaging.installer.base` 拆分而来，封装 macOS 安装包全部逻辑：
.pkg 通过 ``pkgbuild`` 生成、.dmg 通过 ``hdiutil`` 生成、可选 ``codesign``
ad-hoc 签名。单格式编排（build_pkg_release / build_dmg_release）。

依赖 :mod:`fspack.packaging.installer.base` 提供：
``Installer`` 基类、``_run_stage``/``_prepare_dist``/``_check_exe``/``_release_base``、
``_run_tool``（pkgbuild/hdiutil/codesign 调用）、``_DIST_IGNORE``（打包排除模式）。

工具链（均为 macOS 系统自带，无需额外安装）：

- ``pkgbuild``：构造 .pkg 安装包（Command Line Tools 提供）
- ``hdiutil``：构造 .dmg 磁盘镜像（macOS 系统自带）
- ``codesign``：ad-hoc 签名（Command Line Tools 提供，``--sign -`` 表示 ad-hoc）

数据布局：

- ``.pkg``：``pkgbuild --root <staging>``，staging 内为 dist 内容（exe + runtime），
  安装到 ``/Applications/<name>/``（通过 ``--install-location`` 控制）
- ``.dmg``：staging 内为 dist 内容 + ``/Applications`` 软链接（拖拽安装），
  ``hdiutil create -srcfolder <staging> -format UDZO`` 压缩镜像
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # noqa: F401  # 保留 patch 路径 fspack.packaging.installer.macos.subprocess.run
from pathlib import Path
from typing import Sequence

from fspack._compat import override
from fspack.config import MirrorConfig, ProjectInfo
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.packaging.installer.base import (
    _DIST_IGNORE,
    Installer,
    _check_exe,
    _prepare_dist,
    _release_base,
    _run_stage,
    _run_tool,
)
from fspack.platform import Platform
from fspack.progress import BuildTracker

__all__ = [
    "MacInstaller",
    "build_dmg",
    "build_dmg_release",
    "build_mac_installer",
    "build_pkg",
    "build_pkg_release",
]

_logger = logging.getLogger("fspack.packaging.installer")

# macOS 安装目标位置（拖拽到 /Applications 即安装）
_MACOS_INSTALL_LOCATION = "/Applications"


def _bundle_identifier(info: ProjectInfo) -> str:
    """返回 macOS bundle identifier：``com.fspack.<name>``.

    pkgbuild ``--identifier`` 要求反向域名格式，用 ``com.fspack.<name>`` 兜底
    避免用户未配置时出错。后续可扩展为读取 ``[tool.fspack] bundle_id``。
    """
    return f"com.fspack.{info.name}"


def _run_macos_tool(cmd: list[str], *, error_hint: str) -> None:
    """执行 macOS 专属工具（pkgbuild/hdiutil/codesign），失败抛 InstallerError.

    委托 :func:`fspack.packaging.installer.base._run_tool` 统一异常处理，保留
    macOS 专属消息格式（未找到时附加"macOS 工具，需在 macOS 上运行"提示）。

    Args:
        cmd: 命令与参数列表（如 ``["pkgbuild", "--root", ...]``）
        error_hint: 失败时附加到"未找到"异常消息的修复建议
    """
    _run_tool(
        cmd,
        not_found_msg=f"未找到 {cmd[0]}，{error_hint}（macOS 工具，需在 macOS 上运行）",
        fail_prefix=f"{cmd[0]} 执行失败",
    )


def _codesign_adhoc(path: Path) -> None:
    """对 ``path`` 做 ad-hoc 签名（``codesign --force --sign -``）.

    ad-hoc 签名不提供开发者 ID，仅用于本地执行权限（Gatekeeper 仍会提示未签名）。
    真实分发需用 Apple Developer ID 签名（``--sign "Developer ID Application: ..."``）。
    """
    _run_macos_tool(
        ["codesign", "--force", "--sign", "-", str(path)],
        error_hint="请安装 Xcode Command Line Tools",
    )


class MacInstaller(Installer):
    """macOS 安装包生成器：.pkg 安装包 + .dmg 磁盘镜像。"""

    @classmethod
    @override
    def target_platform(cls) -> Platform:
        """macOS 平台。"""
        return Platform.MACOS

    @classmethod
    @override
    def exe_filename(cls, info: ProjectInfo) -> str:
        """返回 ``<name>``（无后缀，与 Linux 一致）。"""
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
        codesign: bool = False,
    ) -> Path:
        """生成 .pkg 与 .dmg，返回 .dmg 路径.

        Args:
            codesign: 是否对产物做 ad-hoc 签名（``codesign --sign -``）
        """
        pkg_name = f"{_release_base(info, 'macos')}.pkg"
        _run_stage(
            tracker,
            "构造 .pkg 安装包",
            lambda: build_pkg(dist_dir, info, release_dir, codesign=codesign),
            detail=pkg_name,
        )
        dmg_name = f"{_release_base(info, 'macos')}.dmg"
        result = _run_stage(
            tracker,
            "构造 .dmg 磁盘镜像",
            lambda: build_dmg(dist_dir, info, release_dir, codesign=codesign),
            detail=dmg_name,
        )
        console.success(f"安装包已生成: {result}")
        return result

    @classmethod
    @override
    def build_installer(  # noqa: PLR0913
        cls,
        project_dir: Path,
        mirror: MirrorConfig,
        py_version: str | None = None,
        no_build: bool = False,
        dist_dir: Path | None = None,
        *,
        tracker: BuildTracker | None = None,
        codesign: bool = False,
        extras: Sequence[str] | None = None,
    ) -> Path:
        """编排：可选 build → 校验可执行文件 → build_package，返回 .dmg 路径.

        重写基类以透传 ``codesign`` 到 :meth:`build_package`。
        """
        own_tracker = tracker is None
        tk = tracker or BuildTracker(title="打包阶段汇总")
        dist, info = _prepare_dist(
            project_dir, mirror, py_version, no_build, dist_dir, cls.target_platform(), extras=extras, tracker=tk
        )
        exe = dist / cls.exe_filename(info)
        if not exe.is_file():
            raise InstallerError(f"未找到已构建的可执行文件: {exe}（请先执行 fsp b）")
        release = dist / "release"
        result = cls.build_package(dist, info, release, tracker=tk, codesign=codesign)
        if own_tracker:
            console.rich.print(tk.summary())
        return result


def build_pkg(
    dist_dir: Path,
    info: ProjectInfo,
    release_dir: Path,
    *,
    codesign: bool = False,
) -> Path:
    """构造 .pkg 安装包，返回 .pkg 路径.

    用 ``pkgbuild --root <staging> --identifier <bundle_id> --version <version>
    --install-location /Applications/<name> <out.pkg>`` 生成安装包。

    排除 dist/release/ 避免安装包递归打包自身。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    base = _release_base(info, "macos")
    staging = release_dir / f"{base}.pkg-staging"

    if staging.exists():
        shutil.rmtree(staging)

    # staging 顶层目录 = 安装到 /Applications/<name> 的内容
    pkg_root = staging / info.name
    shutil.copytree(dist_dir, pkg_root, ignore=_DIST_IGNORE)

    pkg_path = release_dir / f"{base}.pkg"
    cmd = [
        "pkgbuild",
        "--root",
        str(staging),
        "--identifier",
        _bundle_identifier(info),
        "--version",
        info.version,
        "--install-location",
        _MACOS_INSTALL_LOCATION,
        str(pkg_path),
    ]
    _run_macos_tool(cmd, error_hint="请安装 Xcode Command Line Tools")

    shutil.rmtree(staging)

    if codesign:
        _codesign_adhoc(pkg_path)

    _logger.info("已生成 .pkg 安装包: %s", pkg_path)
    return pkg_path


def build_dmg(
    dist_dir: Path,
    info: ProjectInfo,
    release_dir: Path,
    *,
    codesign: bool = False,
) -> Path:
    """构造 .dmg 磁盘镜像，返回 .dmg 路径.

    staging 内含 dist 内容（顶层目录 = 应用名）+ ``/Applications`` 软链接
    （用户拖拽安装）。用 ``hdiutil create -volname <name> -srcfolder <staging>
    -format UDZO`` 生成压缩镜像。

    排除 dist/release/ 避免镜像递归打包自身。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    base = _release_base(info, "macos")
    staging = release_dir / f"{base}.dmg-staging"

    if staging.exists():
        shutil.rmtree(staging)

    # 应用目录（拖拽到 /Applications）
    app_dir = staging / info.name
    shutil.copytree(dist_dir, app_dir, ignore=_DIST_IGNORE)

    # /Applications 软链接（拖拽安装入口）
    apps_link = staging / "Applications"
    try:
        apps_link.symlink_to("/Applications", target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows/非 macOS 环境无法创建软链接（测试场景），用空目录占位
        # 真实 macOS 打包时 symlink_to 会成功
        _logger.warning("无法创建 /Applications 软链接（非 macOS 环境？），跳过")
        apps_link.mkdir(exist_ok=True)

    dmg_path = release_dir / f"{base}.dmg"
    cmd = [
        "hdiutil",
        "create",
        "-volname",
        info.name,
        "-srcfolder",
        str(staging),
        "-ov",
        "-format",
        "UDZO",
        str(dmg_path),
    ]
    _run_macos_tool(cmd, error_hint="hdiutil 为 macOS 系统自带工具")

    shutil.rmtree(staging)

    if codesign:
        _codesign_adhoc(dmg_path)

    _logger.info("已生成 .dmg 磁盘镜像: %s", dmg_path)
    return dmg_path


# ---- 单格式编排（pkg / dmg）----


def build_pkg_release(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
    codesign: bool = False,
    extras: Sequence[str] | None = None,
) -> Path:
    """编排：可选 build → 校验可执行文件 → 构造 .pkg 安装包，返回 .pkg 路径。"""
    own_tracker = tracker is None
    tk = tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(
        project_dir, mirror, py_version, no_build, dist_dir, Platform.MACOS, extras=extras, tracker=tk
    )
    _check_exe(dist, info, Platform.MACOS)
    release = dist / "release"
    pkg_name = f"{_release_base(info, 'macos')}.pkg"
    result = _run_stage(
        tk,
        "构造 .pkg 安装包",
        lambda: build_pkg(dist, info, release, codesign=codesign),
        detail=pkg_name,
    )
    console.success(f".pkg 安装包已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result


def build_dmg_release(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
    codesign: bool = False,
    extras: Sequence[str] | None = None,
) -> Path:
    """编排：可选 build → 校验可执行文件 → 构造 .dmg 磁盘镜像，返回 .dmg 路径。"""
    own_tracker = tracker is None
    tk = tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(
        project_dir, mirror, py_version, no_build, dist_dir, Platform.MACOS, extras=extras, tracker=tk
    )
    _check_exe(dist, info, Platform.MACOS)
    release = dist / "release"
    dmg_name = f"{_release_base(info, 'macos')}.dmg"
    result = _run_stage(
        tk,
        "构造 .dmg 磁盘镜像",
        lambda: build_dmg(dist, info, release, codesign=codesign),
        detail=dmg_name,
    )
    console.success(f".dmg 磁盘镜像已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result


def build_mac_installer(  # noqa: PLR0913
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
    *,
    tracker: BuildTracker | None = None,
    codesign: bool = False,
    extras: Sequence[str] | None = None,
) -> Path:
    """编排：可选 build → .pkg 安装包 → .dmg 磁盘镜像，返回 .dmg 路径。"""
    return MacInstaller.build_installer(
        project_dir,
        mirror,
        py_version,
        no_build=no_build,
        dist_dir=dist_dir,
        tracker=tracker,
        codesign=codesign,
        extras=extras,
    )
