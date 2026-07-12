"""NSIS 安装脚本生成与 makensis 编译。."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fspack.builder import build
from fspack.config import AppType, MirrorConfig, ProjectInfo
from fspack.exceptions import InstallerError
from fspack.project import DEFAULT_PY_VERSION, parse_project

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
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
{uninstall_shortcut_block}
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
    )
    nsi = dist_dir / "installer.nsi"
    nsi.write_text(content, encoding="utf-8")
    _logger.info("已生成 NSIS 脚本: %s", nsi)
    return nsi


def _build_shortcut_block(project: ProjectInfo) -> str:
    """GUI 项目生成开始菜单与桌面快捷方式创建指令，CLI 返回空串。."""
    if project.app_type is not AppType.GUI:
        return ""
    exe = project.exe_name
    name = project.name
    return (
        f'  CreateDirectory "$SMPROGRAMS\\{name}"\n'
        f'  CreateShortCut "$SMPROGRAMS\\{name}\\{name}.lnk" "$INSTDIR\\{exe}"\n'
        f'  CreateShortCut "$DESKTOP\\{name}.lnk" "$INSTDIR\\{exe}"'
    )


def _build_uninstall_shortcut_block(project: ProjectInfo) -> str:
    """GUI 项目生成卸载时清理快捷方式指令，CLI 返回空串。."""
    if project.app_type is not AppType.GUI:
        return ""
    name = project.name
    return f'  RMDir /r "$SMPROGRAMS\\{name}"\n  Delete "$DESKTOP\\{name}.lnk"'


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
    py_version: str = DEFAULT_PY_VERSION,
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
        build(project_dir, mirror, py_version, dist_dir=dist)
    info = parse_project(project_dir, py_version)
    exe = dist / info.exe_name
    if not exe.is_file():
        raise InstallerError(f"未找到已构建的可执行文件: {exe}（请先执行 fsp b）")
    release = dist / "release"
    nsi = generate_nsis_script(info, dist, release)
    out_setup = release / f"{info.name}-setup.exe"
    return compile_installer(nsi, out_setup)
