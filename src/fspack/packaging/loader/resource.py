"""Windows loader exe 资源段生成：VS_VERSIONINFO 与 application manifest.

嵌入资源段（版本信息 + manifest + icon）的 exe，相比资源段空白的 mingw 小型
可执行文件，在 Windows Defender 等杀软启发式评分中显著更低——空白资源段的
小型 PE 是 loader 型恶意软件的典型特征，补全版本信息/manifest 后可大幅降低误报。

公共 API：

- :class:`LoaderVersionInfo` — VS_VERSIONINFO 元数据数据类
- :func:`generate_resource_rc` — 生成 ``.rc`` 源文件内容（icon + VERSIONINFO + manifest 引用）
- :func:`generate_app_manifest` — 生成 application manifest XML（asInvoker + DPI + supportedOS）

设计要点：

- ``.rc`` 顶部 ``#pragma code_page(65001)`` 声明 UTF-8 编码，使中文 CompanyName /
  FileDescription 等字段被 windres 正确转为 UTF-16LE 存入 PE 资源段
- StringFileInfo 的 Translation 固定 ``0x0409 0x04B0``（英语 + Unicode），兼容性最佳
- manifest 声明 ``asInvoker``（loader 本身不提权，NSIS 安装器单独请求 admin）、
  PerMonitorV2 DPI 感知、Win7/8/8.1/10/11 supportedOS
- 纯函数无副作用，便于单测覆盖字段填充与转义
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "LoaderVersionInfo",
    "generate_app_manifest",
    "generate_resource_rc",
]

# Windows supportedOS GUID（Win7/8/8.1/10-11），manifest 声明后系统按目标版本提供行为
# Win10/11 共用同一 GUID（{8e0f7a12-...}），Win11 不需单独声明
_SUPPORTED_OS_GUIDS: tuple[str, ...] = (
    "{35138b9a-5d96-4fbd-8e2d-a2440225f93a}",  # Windows 7
    "{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}",  # Windows 8
    "{1f676c76-80e1-4239-95bb-83d0f6d0da78}",  # Windows 8.1
    "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}",  # Windows 10/11
)


def _xml_escape(s: str) -> str:
    """转义 XML 文本中的特殊字符（``&`` ``<`` ``>``）。

    手动实现而非 ``xml.sax.saxutils.escape``，避免导入 ``xml.sax`` 链式触发
    ``urllib.request`` 加载（~15ms），保持 ``import fspack.builder`` 轻量。
    项目名仅可能含 ``&``/``<``/``>`` 三种破坏 XML 结构的字符，无需转义引号
    （名字出现在属性值中，但项目名不含引号）。
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True)
class LoaderVersionInfo:
    """Windows loader exe 的 VS_VERSIONINFO 资源段元数据.

    嵌入 exe 资源段后，资源管理器「属性→详细信息」可显示公司、产品、版本等字段。
    字段缺省时由 :func:`generate_resource_rc` 回退到 ``name``，保证资源段不出现空值
    （空值字段反而增加可疑度）。
    """

    name: str  # ProductName / InternalName
    version: str  # 形如 "0.4.9" 的版本字符串，转 quad 后填 FILEVERSION/PRODUCTVERSION
    description: str  # FileDescription（空则回退 name）
    author: str  # CompanyName（空则回退 name）
    exe_filename: str  # OriginalFilename（如 "fspack.exe"）


def _version_to_quad(version: str) -> str:
    """版本字符串转 4 段数字逗号串（如 ``"0.4.9"`` → ``"0,4,9,0"``）.

    ``FILEVERSION``/``PRODUCTVERSION`` 要求 4 个 16 位整数。不足 4 段补 0，
    超出取前 4 段。非数字段（如 ``"0.4.9rc1"`` 的 ``"9rc1"``）取前导数字，无数字记 0。
    """
    parts: list[str] = []
    for part in version.split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(num or "0")
    while len(parts) < 4:
        parts.append("0")
    return ",".join(parts[:4])


def _rc_escape(s: str) -> str:
    """转义 rc 字符串值中的双引号（rc 用双引号包裹字符串，内部引号转义为 ``""``）."""
    return s.replace('"', '""')


def generate_app_manifest(name: str, version: str) -> str:
    """生成 application manifest XML 字符串.

    内容包含：

    - ``assemblyIdentity``（win32，name 形如 ``fspack.<name>``，version 为点分隔 quad）
    - ``trustInfo`` 声明 ``asInvoker``（loader 不提权，安装器单独请求 admin）
    - ``compatibility`` 声明 Win7/8/8.1/10/11 supportedOS
    - ``windowsSettings`` 声明 PerMonitorV2 DPI 感知（PySide GUI 高 DPI 适配）

    ``name`` 经 XML 转义避免 ``&``/``<`` 破坏文档结构。manifest 的 ``version``
    属性用点分隔（如 ``0.4.9.0``），与 .rc 的 ``FILEVERSION``（逗号分隔 ``0,4,9,0``）
    不同——Windows SxS 要求 assemblyIdentity version 为点分隔，逗号格式会导致
    "应用程序并行配置不正确"错误。
    """
    safe_name = _xml_escape(name)
    dotted = _version_to_quad(version).replace(",", ".")
    guid_lines = "\n".join(f'      <supportedOS Id="{g}"/>' for g in _SUPPORTED_OS_GUIDS)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">\n'
        f'  <assemblyIdentity type="win32" name="fspack.{safe_name}" version="{dotted}"/>\n'
        '  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">\n'
        "    <security>\n"
        "      <requestedPrivileges>\n"
        '        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>\n'
        "      </requestedPrivileges>\n"
        "    </security>\n"
        "  </trustInfo>\n"
        '  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">\n'
        "    <application>\n"
        f"{guid_lines}\n"
        "    </application>\n"
        "  </compatibility>\n"
        '  <application xmlns="urn:schemas-microsoft-com:asm.v3">\n'
        "    <windowsSettings>\n"
        '      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true</dpiAware>\n'
        '      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>\n'
        "    </windowsSettings>\n"
        "  </application>\n"
        "</assembly>\n"
    )


def generate_resource_rc(info: LoaderVersionInfo | None, *, has_icon: bool) -> str:
    """生成 ``.rc`` 源文件内容（icon 引用 + VERSIONINFO + manifest 引用）.

    Args:
        info: 版本信息元数据；``None`` 时省略 VERSIONINFO 块（仅 icon + manifest）
        has_icon: 是否含 icon 资源；``True`` 时 ``#include`` 后追加 ``1 ICON "icon.ico"``

    manifest 引用 ``1 24 "app.manifest"`` 总是存在（RT_MANIFEST 资源类型=24，ID=1），
    与 icon/version 是否存在无关——manifest 单独嵌入即可显著降低启发式可疑度。

    ``#pragma code_page(65001)`` 声明 UTF-8，使中文 CompanyName/FileDescription 被
    windres 正确解析。字符串值经 :func:`_rc_escape` 转义双引号。
    """
    lines: list[str] = [
        "#include <windows.h>",
        "#pragma code_page(65001)",
        "",
    ]
    if has_icon:
        lines.append('1 ICON "icon.ico"')
        lines.append("")
    if info is not None:
        quad = _version_to_quad(info.version)
        # 缺省字段回退到 name，避免资源段出现空值字段
        company = info.author or info.name
        description = info.description or info.name
        lines.extend(
            [
                "1 VERSIONINFO",
                f"FILEVERSION {quad}",
                f"PRODUCTVERSION {quad}",
                "FILEFLAGSMASK 0x3fL",
                "FILEFLAGS 0x0L",
                "FILEOS 0x40004L",
                "FILETYPE 0x1L",
                "FILESUBTYPE 0x0L",
                "BEGIN",
                '    BLOCK "StringFileInfo"',
                "    BEGIN",
                '        BLOCK "040904b0"',
                "        BEGIN",
                f'            VALUE "CompanyName", "{_rc_escape(company)}"',
                f'            VALUE "FileDescription", "{_rc_escape(description)}"',
                f'            VALUE "ProductName", "{_rc_escape(info.name)}"',
                f'            VALUE "ProductVersion", "{_rc_escape(info.version)}"',
                f'            VALUE "InternalName", "{_rc_escape(info.name)}"',
                f'            VALUE "OriginalFilename", "{_rc_escape(info.exe_filename)}"',
                '            VALUE "LegalCopyright", ""',
                "        END",
                "    END",
                '    BLOCK "VarFileInfo"',
                "    BEGIN",
                '        VALUE "Translation", 0x409, 0x4b0',
                "    END",
                "END",
                "",
            ]
        )
    lines.append('1 24 "app.manifest"')
    lines.append("")
    return "\n".join(lines)
