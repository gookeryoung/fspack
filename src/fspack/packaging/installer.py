"""安装包生成：Windows NSIS 与 Linux tar.gz + .deb。

提取 :class:`Installer` 基类封装通用编排流程：

1. 可选 ``build()`` 先构建项目到 dist
2. 校验可执行文件已存在
3. 调 :meth:`build_package` 生成具体格式的安装包

子类实现 :meth:`build_package` 定制产物格式（NSIS exe / tar.gz + .deb）。
"""

from __future__ import annotations

import abc
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from fspack.builder import build
from fspack.config import MirrorConfig, ProjectInfo
from fspack.console import step, success
from fspack.exceptions import InstallerError
from fspack.platform import Platform

if sys.version_info >= (3, 12):  # pragma: no cover
    from typing import override
else:
    from typing_extensions import override  # type: ignore[import-not-found,unused-ignore]

__all__ = [
    "Installer",
    "LinuxInstaller",
    "NsisInstaller",
    "build_deb",
    "build_installer",
    "build_linux_installer",
    "build_tarball",
    "compile_installer",
    "generate_nsis_script",
]

_logger = logging.getLogger(__name__)


# ---- 基类 ----


class Installer(abc.ABC):
    """安装包生成器基类。

    封装通用编排流程：可选 ``build()`` → 校验可执行文件 → :meth:`build_package`。

    子类实现：
    - :meth:`target_platform`：目标平台（决定 ``build()`` 的 target 参数）
    - :meth:`exe_filename`：可执行文件名（Windows 为 ``<name>.exe``，Linux 为 ``<name>``）
    - :meth:`build_package`：生成具体安装包，返回产物路径
    """

    @classmethod
    @abc.abstractmethod
    def target_platform(cls) -> Platform:
        """目标平台。"""

    @classmethod
    @abc.abstractmethod
    def exe_filename(cls, info: ProjectInfo) -> str:
        """返回可执行文件名（用于校验已构建产物存在）。"""

    @classmethod
    @abc.abstractmethod
    def build_package(cls, dist_dir: Path, info: ProjectInfo, release_dir: Path) -> Path:
        """生成安装包，返回产物路径。"""

    @classmethod
    def build_installer(
        cls,
        project_dir: Path,
        mirror: MirrorConfig,
        py_version: str | None = None,
        no_build: bool = False,
        dist_dir: Path | None = None,
    ) -> Path:
        """编排：可选 build → 校验可执行文件 → build_package，返回安装包路径。"""
        project_dir = Path(project_dir).resolve()
        dist = dist_dir or project_dir / "dist"
        if no_build:
            if not dist.is_dir():
                raise InstallerError(f"未找到 dist 目录: {dist}（请先执行 fsp b）")
        else:
            build(project_dir, mirror, py_version, dist_dir=dist, target=cls.target_platform())
        info = ProjectInfo.from_dir(project_dir, py_version)
        exe = dist / cls.exe_filename(info)
        if not exe.is_file():
            raise InstallerError(f"未找到已构建的可执行文件: {exe}（请先执行 fsp b）")
        release = dist / "release"
        return cls.build_package(dist, info, release)


# ---- NSIS 安装包（Windows）----


_NSIS_TEMPLATE = """\
!include "MUI2.nsh"

Name "{name} {version}"
OutFile "{out_setup}"
InstallDir "$PROGRAMFILES64\\{name}"
RequestExecutionLevel admin
Unicode True

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Section "Main"
  SetOutPath "$INSTDIR"
  # /x 排除 fspack 自身产物（installer.nsi/release）与 uv build 重叠产物（*.whl/*.tar.gz）
  File /r /x installer.nsi /x release /x *.whl /x *.tar.gz *.*
  WriteUninstaller "$INSTDIR\\uninstall.exe"
{shortcut_block}
{registry_block}
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
{uninstall_shortcut_block}
{uninstall_registry_block}
SectionEnd
"""


class NsisInstaller(Installer):
    """Windows NSIS 安装包生成器。"""

    @classmethod
    @override
    def target_platform(cls) -> Platform:
        """Windows 平台。"""
        return Platform.WINDOWS

    @classmethod
    @override
    def exe_filename(cls, info: ProjectInfo) -> str:
        """返回 ``<name>.exe``。"""
        return info.exe_name

    @classmethod
    @override
    def build_package(cls, dist_dir: Path, info: ProjectInfo, release_dir: Path) -> Path:
        """生成 NSIS 脚本并编译为安装包。"""
        step("生成 NSIS 脚本")
        nsi = generate_nsis_script(info, dist_dir, release_dir)
        out_setup = release_dir / f"{info.name}-{info.version}-setup.exe"
        step("编译 NSIS 安装包")
        result = compile_installer(nsi, out_setup)
        success(f"安装包已生成: {result}")
        return result


def generate_nsis_script(project: ProjectInfo, dist_dir: Path, release_dir: Path) -> Path:
    """生成 NSIS 安装脚本到 dist_dir/installer.nsi，返回脚本路径。

    release_dir 必须是 dist_dir 的子目录，OutFile 路径相对 dist_dir 计算。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    out_setup_rel = release_dir.relative_to(dist_dir) / f"{project.name}-{project.version}-setup.exe"
    out_setup_win = str(out_setup_rel).replace("/", "\\")
    content = _NSIS_TEMPLATE.format(
        name=project.name,
        version=project.version,
        out_setup=out_setup_win,
        shortcut_block=_build_shortcut_block(project),
        uninstall_shortcut_block=_build_uninstall_shortcut_block(project),
        registry_block=_build_registry_block(project),
        uninstall_registry_block=_build_uninstall_registry_block(project),
    )
    nsi = dist_dir / "installer.nsi"
    # 用 UTF-8-SIG（带 BOM）写入，makensis 依 BOM 识别 UTF-8，
    # 否则按 ANSI 代码页解析导致中文（注释/快捷方式名）报 Bad text encoding
    nsi.write_text(content, encoding="utf-8-sig")
    _logger.info("已生成 NSIS 脚本: %s", nsi)
    return nsi


def _build_shortcut_block(project: ProjectInfo) -> str:
    """生成开始菜单与桌面快捷方式创建指令。

    所有应用类型默认生成：开始菜单文件夹、程序快捷方式、卸载快捷方式、桌面快捷方式。
    """
    name = project.name
    exe = project.exe_name
    lines = [
        f'  CreateDirectory "$SMPROGRAMS\\{name}"',
        f'  CreateShortCut "$SMPROGRAMS\\{name}\\{name}.lnk" "$INSTDIR\\{exe}"',
        f'  CreateShortCut "$SMPROGRAMS\\{name}\\卸载 {name}.lnk" "$INSTDIR\\uninstall.exe"',
        f'  CreateShortCut "$DESKTOP\\{name}.lnk" "$INSTDIR\\{exe}"',
    ]
    return "\n".join(lines)


def _build_uninstall_shortcut_block(project: ProjectInfo) -> str:
    """生成卸载时清理快捷方式指令（所有应用类型均清理）。"""
    name = project.name
    return f'  RMDir /r "$SMPROGRAMS\\{name}"\n  Delete "$DESKTOP\\{name}.lnk"'


def _build_registry_block(project: ProjectInfo) -> str:
    """生成添加/删除程序注册表条目，使应用出现在 Windows 设置的应用列表中。"""
    name = project.name
    version = project.version
    exe = project.exe_name
    key = f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{name}"
    return (
        f'  WriteRegStr HKLM "{key}" "DisplayName" "{name}"\n'
        f'  WriteRegStr HKLM "{key}" "DisplayVersion" "{version}"\n'
        f'  WriteRegStr HKLM "{key}" "UninstallString" \'"$INSTDIR\\uninstall.exe"\'\n'
        f'  WriteRegStr HKLM "{key}" "QuietUninstallString" \'"$INSTDIR\\uninstall.exe" /S\'\n'
        f'  WriteRegStr HKLM "{key}" "InstallLocation" "$INSTDIR"\n'
        f'  WriteRegStr HKLM "{key}" "Publisher" "fspack"\n'
        f'  WriteRegStr HKLM "{key}" "DisplayIcon" "$INSTDIR\\{exe}"\n'
        f'  WriteRegDWORD HKLM "{key}" "NoModify" 1\n'
        f'  WriteRegDWORD HKLM "{key}" "NoRepair" 1'
    )


def _build_uninstall_registry_block(project: ProjectInfo) -> str:
    """生成卸载时删除注册表条目的指令。"""
    key = f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{project.name}"
    return f'  DeleteRegKey HKLM "{key}"'


def compile_installer(nsi_path: Path, out_setup: Path) -> Path:
    """调用 makensis 编译 .nsi 为安装包，返回 out_setup 路径。"""
    cmd = ["makensis", str(nsi_path)]
    _logger.info("编译安装包: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=nsi_path.parent)
    except FileNotFoundError as e:
        raise InstallerError("未找到 makensis，请安装 NSIS（如 sudo apt install -y nsis）") from e
    except subprocess.CalledProcessError as e:
        raise InstallerError(f"makensis 编译失败:\n{e.stderr}") from e
    if not out_setup.is_file():
        raise InstallerError(f"makensis 未产出安装包: {out_setup}")
    return out_setup


# ---- Linux 安装包（tar.gz + .deb）----


_LINUX_IGNORE = shutil.ignore_patterns("release")


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
    def build_package(cls, dist_dir: Path, info: ProjectInfo, release_dir: Path) -> Path:
        """生成 tar.gz 便携包与 .deb 安装包，返回 .deb 路径。"""
        step("生成 tar.gz 便携包")
        build_tarball(dist_dir, info.name, info.version, release_dir)
        step("构造 .deb 安装包")
        result = build_deb(dist_dir, info, release_dir)
        success(f"安装包已生成: {result}")
        return result


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
    deb_base = f"{info.name}_{info.version}_amd64"
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
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise InstallerError("未找到 dpkg-deb，请安装 dpkg-dev（如 sudo apt install -y dpkg-dev）") from e
    except subprocess.CalledProcessError as e:
        raise InstallerError(f"dpkg-deb 构建失败:\n{e.stderr}") from e

    shutil.rmtree(staging)
    _logger.info("已生成 .deb 安装包: %s", deb_path)
    return deb_path


# ---- 函数式 API（委托给类）----


def build_installer(
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
) -> Path:
    """编排：可选 build → 生成 NSIS 脚本 → 编译安装包，返回安装包路径。"""
    return NsisInstaller.build_installer(project_dir, mirror, py_version, no_build=no_build, dist_dir=dist_dir)


def build_linux_installer(
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
) -> Path:
    """编排：可选 build → tar.gz 便携包 → .deb 安装包，返回 .deb 路径。"""
    return LinuxInstaller.build_installer(project_dir, mirror, py_version, no_build=no_build, dist_dir=dist_dir)
