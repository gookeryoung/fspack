"""NSIS 安装脚本生成与 makensis 编译。."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fspack.builder import build
from fspack.config import AppType, MirrorConfig, ProjectInfo
from fspack.console import step, success
from fspack.exceptions import InstallerError
from fspack.platform import Platform

__all__ = ["build_installer", "compile_installer", "generate_nsis_script"]

_logger = logging.getLogger(__name__)

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
  File /r /x installer.nsi /x release *.*
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


def generate_nsis_script(project: ProjectInfo, dist_dir: Path, release_dir: Path) -> Path:
    """生成 NSIS 安装脚本到 dist_dir/installer.nsi，返回脚本路径。

    release_dir 必须是 dist_dir 的子目录，OutFile 路径相对 dist_dir 计算。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    out_setup_rel = release_dir.relative_to(dist_dir) / f"{project.name}-setup.exe"
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
    nsi.write_text(content, encoding="utf-8")
    _logger.info("已生成 NSIS 脚本: %s", nsi)
    return nsi


def _build_shortcut_block(project: ProjectInfo) -> str:
    """生成开始菜单快捷方式创建指令。

    所有应用均在开始菜单创建文件夹与卸载快捷方式，便于用户卸载；
    GUI 项目额外创建程序快捷方式与桌面快捷方式。
    """
    name = project.name
    lines = [
        f'  CreateDirectory "$SMPROGRAMS\\{name}"',
        f'  CreateShortCut "$SMPROGRAMS\\{name}\\卸载 {name}.lnk" "$INSTDIR\\uninstall.exe"',
    ]
    if project.app_type is AppType.GUI:
        exe = project.exe_name
        lines.append(f'  CreateShortCut "$SMPROGRAMS\\{name}\\{name}.lnk" "$INSTDIR\\{exe}"')
        lines.append(f'  CreateShortCut "$DESKTOP\\{name}.lnk" "$INSTDIR\\{exe}"')
    return "\n".join(lines)


def _build_uninstall_shortcut_block(project: ProjectInfo) -> str:
    """生成卸载时清理快捷方式指令。

    所有应用均清理开始菜单文件夹；GUI 项目额外清理桌面快捷方式。
    """
    name = project.name
    lines = [f'  RMDir /r "$SMPROGRAMS\\{name}"']
    if project.app_type is AppType.GUI:
        lines.append(f'  Delete "$DESKTOP\\{name}.lnk"')
    return "\n".join(lines)


def _build_registry_block(project: ProjectInfo) -> str:
    """生成添加/删除程序注册表条目，使应用出现在 Windows 设置的应用列表中。."""
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
    """生成卸载时删除注册表条目的指令。."""
    key = f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{project.name}"
    return f'  DeleteRegKey HKLM "{key}"'


def compile_installer(nsi_path: Path, out_setup: Path) -> Path:
    """调用 makensis 编译 .nsi 为安装包，返回 out_setup 路径。."""
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


def build_installer(
    project_dir: Path,
    mirror: MirrorConfig,
    py_version: str | None = None,
    no_build: bool = False,
    dist_dir: Path | None = None,
) -> Path:
    """编排：可选 build → 生成 NSIS 脚本 → 编译安装包，返回安装包路径。."""
    project_dir = Path(project_dir).resolve()
    dist = dist_dir or project_dir / "dist"
    if no_build:
        if not dist.is_dir():
            raise InstallerError(f"未找到 dist 目录: {dist}（请先执行 fsp b）")
    else:
        build(project_dir, mirror, py_version, dist_dir=dist, target=Platform.WINDOWS)
    info = ProjectInfo.from_dir(project_dir, py_version)
    exe = dist / info.exe_name
    if not exe.is_file():
        raise InstallerError(f"未找到已构建的可执行文件: {exe}（请先执行 fsp b）")
    release = dist / "release"
    step("生成 NSIS 脚本")
    nsi = generate_nsis_script(info, dist, release)
    out_setup = release / f"{info.name}-setup.exe"
    step("编译 NSIS 安装包")
    result = compile_installer(nsi, out_setup)
    success(f"安装包已生成: {result}")
    return result
