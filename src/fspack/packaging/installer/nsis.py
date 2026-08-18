"""Windows NSIS 安装包生成：脚本生成、编译、快捷方式与注册表.

从 :mod:`fspack.packaging.installer.base` 拆分而来，封装 NSIS 安装包全部逻辑：
NSIS 模板、快捷方式块、注册表块、脚本生成与 makensis 编译。

依赖 :mod:`fspack.packaging.installer.base` 提供 ``Installer`` 基类与
``_run_stage``/``_run_tool``；:mod:`fspack.packaging.installer.dist_prep` 提供
``_prepare_dist``/``_check_exe``/``_release_base``/``_DIST_INTERMEDIATE_EXCLUDES``。
"""

from __future__ import annotations

import logging
import subprocess  # noqa: F401  # 保留 patch 路径 fspack.packaging.installer.nsis.subprocess.run
from pathlib import Path

from fspack._compat import override
from fspack.config import ProjectInfo
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.packaging.installer.base import Installer, _run_stage, _run_tool
from fspack.packaging.installer.dist_prep import (
    _DIST_INTERMEDIATE_EXCLUDES,
    _check_exe,
    _prepare_dist,
    _release_base,
)
from fspack.packaging.installer.request import _NO_SIGN, ReleaseRequest, SignOptions
from fspack.packaging.win7_scan import iter_pe_files
from fspack.platform import Platform
from fspack.progress import BuildTracker

__all__ = [
    "NsisInstaller",
    "compile_installer",
    "dist_needs_ucrt",
    "generate_nsis_script",
    "sign_exe_file",
    "sign_exe_files",
]

# 共享 logger 名：保持与原 installer.py 一致，测试 caplog 按 logger 名过滤
_logger = logging.getLogger("fspack.packaging.installer")

# NSIS File /x 参数列表（空格分隔的 /x <pattern> 序列）
_NSIS_EXCLUDE_INTERMEDIATE = " ".join(f"/x {pat}" for pat in _DIST_INTERMEDIATE_EXCLUDES)

# UCRT 依赖时 .onInit 注入的检测段：缺失时告知 KB 编号并让用户选择是否继续。
# 注意：本块作为 format 的值传入（非模板片段），不使用 {{}} 转义，直接写 NSIS 语法。
_NSIS_UCRT_CHECK_BLOCK = """\
  # UCRT 检测：产物依赖 Universal C Runtime，Win7/Win8 系统需微软更新
  # （Win7=KB2999226 / Win8=KB2999264）提供 ucrtbase.dll，缺失则装后无法启动
  ${IfNot} ${FileExists} "$SYSDIR\\ucrtbase.dll"
    MessageBox MB_YESNO|MB_ICONEXCLAMATION "未检测到系统 Universal C Runtime（ucrtbase.dll）。$\\r$\\n$\\r$\\n本程序依赖 UCRT 运行库：Windows 7 需安装更新 KB2999226（Windows 8/8.1 为 KB2999264），Windows 10/11 自带无需处理。缺少 UCRT 时程序安装后将无法启动。$\\r$\\n$\\r$\\n是否仍要继续安装？（建议选否，先安装 UCRT 更新后再运行本安装包）" IDYES ucrt_ok
    Abort
  ${EndIf}
  ucrt_ok:
"""

# UCRT 依赖二进制标记（PE 导入表 dll 名为 ASCII 明文，子串检测零漏报）
_UCRT_MARKER = b"api-ms-win-crt-"


def dist_needs_ucrt(dist_dir: Path) -> bool:
    """dist 内 PE 是否依赖 UCRT（api-ms-win-crt-* 导入），决定 NSIS 是否生成检测段.

    导入表 dll 名以 ASCII 明文存储，二进制子串检测零漏报；第三方数据段
    偶发嵌含该前缀字符串仅致安装时多一次提示（无害），不值得全量 PE
    导入表解析（P1 的 win7 扫描已产出精确统计，本检测用于独立的
    ``fsp p`` 打包路径）。
    """
    return any(_UCRT_MARKER in path.read_bytes() for path in iter_pe_files(dist_dir))


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
  ReadRegStr $R2 HKLM "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{name}" "InstallLocation"
  ${{If}} $R2 != ""
  ${{AndIf}} ${{FileExists}} "$R2\\uninstall.exe"
    ${{If}} $R0 == "{version}"
      # 已安装相同版本，直接覆盖不打扰
      Return
    ${{EndIf}}
    # 已安装不同版本，询问是否先卸载
    MessageBox MB_YESNO|MB_ICONQUESTION "检测到已安装 $R0 版本，是否先卸载再安装 {version}？" IDYES uninstall_old IDNO skip_uninstall
    uninstall_old:
      # _?= 让卸载器在原位置运行不自我复制到 temp，ExecWait 才能等待真正完成
      # 否则卸载器会复制自身到 %TEMP% 并立即退出原进程，ExecWait 不等待
      ExecWait '"$R2\\uninstall.exe" /S _?=$R2' $R3
      Goto done
    skip_uninstall:
      # 用户选择直接覆盖安装
    done:
  ${{EndIf}}
{ucrt_check_block}
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

    @classmethod
    @override
    def build_installer(cls, req: ReleaseRequest, *, sign: SignOptions = _NO_SIGN) -> Path:
        """编排：可选 build → 签名 dist exe → 生成 NSIS → 签名 setup.exe.

        ``sign.sign_exe=True`` 且 ``sign.sign_exe_certificate`` 非空时：
        1. 在 NSIS 编译前签名 dist 下所有入口 exe（使安装包内打包签名 exe）
        2. 在 NSIS 编译后签名 setup.exe（使安装包自身携带签名）

        签名失败降级为 warning 不阻断构建（签名仅为分发增强）。
        """
        own_tracker = req.tracker is None
        tk = req.tracker or BuildTracker(title="打包阶段汇总")
        dist, info = _prepare_dist(req, Platform.WINDOWS)
        _check_exe(dist, info, Platform.WINDOWS)

        # 签名 dist exe（NSIS 编译前，使安装包内打包签名 exe）
        if sign.sign_exe and sign.sign_exe_certificate is not None:
            sign_exe_files(dist, info, sign.sign_exe_certificate, sign.sign_exe_password, tracker=tk)

        release = dist / "release"
        result = cls.build_package(dist, info, release, tracker=tk)

        # 签名 setup.exe（NSIS 编译后，使安装包自身携带签名）
        if sign.sign_exe and sign.sign_exe_certificate is not None:
            with tk.stage("签名安装包") as st:
                try:
                    sign_exe_file(result, sign.sign_exe_certificate, sign.sign_exe_password)
                    st.processed(1)
                    st.set_detail(result.name)
                except InstallerError as e:
                    _logger.warning("签名安装包失败，跳过: %s", e)
                    st.set_detail("签名失败")

        console.success(f"安装包已生成: {result}")
        if own_tracker:
            console.rich.print(tk.summary())
        return result


def generate_nsis_script(project: ProjectInfo, dist_dir: Path, release_dir: Path) -> Path:
    """生成 NSIS 安装脚本到 dist_dir/installer.nsi，返回脚本路径。

    release_dir 必须是 dist_dir 的子目录，OutFile 路径相对 dist_dir 计算。
    """
    release_dir.mkdir(parents=True, exist_ok=True)
    out_setup_rel = release_dir.relative_to(dist_dir) / f"{_release_base(project, 'windows')}-setup.exe"
    out_setup_win = str(out_setup_rel).replace("/", "\\")
    needs_ucrt = dist_needs_ucrt(dist_dir)
    if needs_ucrt:
        _logger.info("产物依赖 UCRT，NSIS 安装包将检测目标机 ucrtbase.dll 并在缺失时提示")
    content = _NSIS_TEMPLATE.format(
        name=project.name,
        version=project.version,
        out_setup=out_setup_win,
        nsis_exclude_intermediate=_NSIS_EXCLUDE_INTERMEDIATE + " " if _NSIS_EXCLUDE_INTERMEDIATE else "",
        ucrt_check_block=_NSIS_UCRT_CHECK_BLOCK if needs_ucrt else "",
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
    _run_tool(
        ["makensis", str(nsi_path)],
        not_found_msg="未找到 makensis，请安装 NSIS（如 sudo apt install -y nsis）",
        fail_prefix="makensis 编译失败",
        cwd=nsi_path.parent,
        produces=out_setup,
    )
    return out_setup


def sign_exe_file(
    exe_path: Path,
    certificate: Path,
    password: str | None,
    *,
    timestamp_url: str = "http://timestamp.digicert.com",
) -> None:
    """用 signtool 对单个 exe 做代码签名.

    调用 ``signtool sign /f <pfx> /p <password> /t <timestamp> <exe>``，
    需 Windows SDK 自带 signtool.exe（在 PATH 中或通过 Windows SDK 安装）。

    Args:
        exe_path: 待签名的 exe 文件路径
        certificate: PFX 证书文件路径
        password: PFX 证书密码，None 时省略 /p 参数（空密码证书）
        timestamp_url: RFC 3161 时间戳服务器 URL，默认 DigiCert

    Raises:
        InstallerError: signtool 未找到或签名失败
    """
    cmd: list[str] = ["signtool", "sign", "/f", str(certificate)]
    if password:
        cmd.extend(["/p", password])
    cmd.extend(["/t", timestamp_url, str(exe_path)])
    _logger.info("签名 exe: %s", exe_path.name)
    _run_tool(
        cmd,
        not_found_msg="未找到 signtool，请安装 Windows SDK 并将 signtool 加入 PATH",
        fail_prefix=f"signtool 签名失败 {exe_path.name}",
    )


def sign_exe_files(
    dist_dir: Path,
    info: ProjectInfo,
    certificate: Path,
    password: str | None,
    *,
    tracker: BuildTracker,
) -> int:
    """签名 dist 下所有入口 exe（主 exe + 多入口 exe），返回签名文件数.

    在 NSIS 编译前调用，使安装包内打包的 exe 已携带签名。签名单入口项目的
    ``<name>.exe`` 与多入口项目的所有 ``<entry_name>.exe``。

    签名失败不阻断构建（warning 后继续），签名仅为分发增强，非必需。
    """
    signed = 0
    for ep in info.all_entries:
        exe_name = f"{ep.name}.exe"
        exe_path = dist_dir / exe_name
        if not exe_path.is_file():
            _logger.warning("签名跳过：exe 不存在 %s", exe_path)
            continue
        try:
            sign_exe_file(exe_path, certificate, password)
            signed += 1
        except InstallerError as e:
            _logger.warning("签名 %s 失败，跳过: %s", exe_name, e)
    if signed:
        with tracker.stage("签名 exe") as st:
            st.processed(signed)
            st.set_detail(f"{signed} 个 exe")
    return signed
