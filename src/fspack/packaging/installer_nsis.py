"""Windows NSIS 安装包生成：脚本生成、编译、快捷方式与注册表.

从 :mod:`fspack.packaging.installer` 拆分而来，封装 NSIS 安装包全部逻辑：
NSIS 模板、快捷方式块、注册表块、脚本生成与 makensis 编译。

依赖 :mod:`fspack.packaging.installer` 提供：
``Installer`` 基类、``_run_stage``/``_release_base``/``_DIST_INTERMEDIATE_EXCLUDES``。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fspack._compat import override
from fspack.config import ProjectInfo
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.packaging.installer import (
    _DIST_INTERMEDIATE_EXCLUDES,
    Installer,
    _release_base,
    _run_stage,
)
from fspack.platform import Platform
from fspack.progress import BuildTracker

__all__ = [
    "NsisInstaller",
    "compile_installer",
    "generate_nsis_script",
]

# 共享 logger 名：保持与原 installer.py 一致，测试 caplog 按 logger 名过滤
_logger = logging.getLogger("fspack.packaging.installer")

# NSIS File /x 参数列表（空格分隔的 /x <pattern> 序列）
_NSIS_EXCLUDE_INTERMEDIATE = " ".join(f"/x {pat}" for pat in _DIST_INTERMEDIATE_EXCLUDES)


_NSIS_TEMPLATE = """\
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WordFunc.nsh"

Name "{name} {version}"
OutFile "{out_setup}"
InstallDir "$PROGRAMFILES64\\{name}"
# 从注册表读取上次安装路径作为默认目录，避免重复选择目录
InstallDirRegKey HKLM "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{name}" "InstallLocation"
RequestExecutionLevel admin
Unicode True

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

# 安装前检测已安装版本：版本不同时询问是否先卸载旧版再安装新版
Function .onInit
  ReadRegStr $R0 HKLM "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{name}" "DisplayVersion"
  ReadRegStr $R1 HKLM "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{name}" "UninstallString"
  ${{If}} $R1 != ""
    ${{If}} $R0 == "{version}"
      # 已安装相同版本，直接覆盖不打扰
      Return
    ${{EndIf}}
    # 已安装不同版本，询问是否先卸载
    MessageBox MB_YESNO|MB_ICONQUESTION "检测到已安装 $R0 版本，是否先卸载再安装 {version}？" IDYES uninstall_old IDNO skip_uninstall
    uninstall_old:
      # 静默卸载旧版并等待完成，确保文件不被占用
      ExecWait '$R1 /S'
      Goto done
    skip_uninstall:
      # 用户选择直接覆盖安装
    done:
  ${{EndIf}}
FunctionEnd

Section "Main"
  SetOutPath "$INSTDIR"
  # /x 排除 fspack 自身产物（installer.nsi/release）、uv build 重叠产物（*.whl/*.tar.gz）
  # 与构建中间文件（.dep_cache.json/.nuitka_compile_stamp/.pyc_stamp/*.build）
  File /r /x installer.nsi /x release /x *.whl /x *.tar.gz {nsis_exclude_intermediate}*.*
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
    def build_package(
        cls,
        dist_dir: Path,
        info: ProjectInfo,
        release_dir: Path,
        *,
        tracker: BuildTracker,
    ) -> Path:
        """生成 NSIS 脚本并编译为安装包。"""
        nsi = _run_stage(
            tracker,
            "生成 NSIS 脚本",
            lambda: generate_nsis_script(info, dist_dir, release_dir),
            detail="installer.nsi",
        )
        out_setup = release_dir / f"{_release_base(info, 'windows')}-setup.exe"
        result = _run_stage(
            tracker,
            "编译 NSIS 安装包",
            lambda: compile_installer(nsi, out_setup),
            detail=out_setup.name,
        )
        console.success(f"安装包已生成: {result}")
        return result


def generate_nsis_script(project: ProjectInfo, dist_dir: Path, release_dir: Path) -> Path:
    """生成 NSIS 安装脚本到 dist_dir/installer.nsi，返回脚本路径。

    release_dir 必须是 dist_dir 的子目录，OutFile 路径相对 dist_dir 计算。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    out_setup_rel = release_dir.relative_to(dist_dir) / f"{_release_base(project, 'windows')}-setup.exe"
    out_setup_win = str(out_setup_rel).replace("/", "\\")
    content = _NSIS_TEMPLATE.format(
        name=project.name,
        version=project.version,
        out_setup=out_setup_win,
        nsis_exclude_intermediate=_NSIS_EXCLUDE_INTERMEDIATE + " " if _NSIS_EXCLUDE_INTERMEDIATE else "",
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
        subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace", cwd=nsi_path.parent)
    except FileNotFoundError as e:
        raise InstallerError("未找到 makensis，请安装 NSIS（如 sudo apt install -y nsis）") from e
    except subprocess.CalledProcessError as e:
        raise InstallerError(f"makensis 编译失败:\n{e.stderr}") from e
    if not out_setup.is_file():
        raise InstallerError(f"makensis 未产出安装包: {out_setup}")
    return out_setup
