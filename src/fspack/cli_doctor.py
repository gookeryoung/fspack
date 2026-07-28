"""``fsp doctor`` 环境诊断子命令.

检查 fspack 打包所需工具的可用性与版本，显示 Python 版本、平台、镜像源
配置、缓存目录大小，输出绿/黄/红三色诊断结果与修复建议，帮助用户前置
发现环境问题，避免打包中途失败。

诊断项分两类：

- **工具检查**：mingw-w64/gcc/NSIS/wine/pip/uv/Pillow 等外部依赖
  （按平台过滤，Windows 不查 gcc/wine，Linux 不查 mingw/NSIS）
- **环境信息**：Python 版本、平台、镜像源、缓存目录大小

设计要点：

- 工具检查用 ``subprocess.run([tool, "--version"], ...)``
  + ``shell=False``，超时 5s，失败返回 ``CheckResult`` 标记缺失
- 缓存目录大小扫描用 ``os.scandir`` 递归累加文件大小，避免引入额外依赖
- 报告渲染用 :class:`rich.table.Table`，复用 :data:`fspack.console.console`
- 所有 check 函数返回 :class:`CheckResult`，主入口 :func:`run_doctor`
  聚合为 :class:`DoctorReport`，便于测试断言
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from fspack import __version__
from fspack.console import console

if TYPE_CHECKING:
    from rich.table import Table

    from fspack.platform import Platform

__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "print_doctor_report",
    "run_doctor",
]

_logger = logging.getLogger(__name__)

# subprocess.run 超时（秒）：工具版本查询应秒级返回，5s 兜底防卡死
_VERSION_TIMEOUT = 5


class CheckStatus(str, Enum):
    """诊断项状态：OK 绿 / WARN 黄 / ERROR 红.

    继承 ``str`` 便于序列化与测试断言（``status == "ok"``）。
    """

    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    """单项诊断结果.

    :param name: 诊断项名称（如 ``"mingw-w64"``/``"Python"``）
    :param status: 状态（:class:`CheckStatus`）
    :param detail: 详细信息（如版本号 ``"13.2.0"`` 或路径 ``"C:\\...\\gcc.exe"``）
    :param suggestion: 修复建议（ERROR/WARN 时填，OK 时为空字符串）
    """

    name: str
    status: CheckStatus
    detail: str
    suggestion: str = ""


@dataclass(frozen=True)
class DoctorReport:
    """完整诊断报告：环境信息 + 工具检查结果列表."""

    env_info: tuple[CheckResult, ...]
    tool_checks: tuple[CheckResult, ...]

    @property
    def has_error(self) -> bool:
        """是否存在 ERROR 级别诊断项（任一即阻塞打包）."""
        return any(c.status is CheckStatus.ERROR for c in self.tool_checks)

    @property
    def has_warn(self) -> bool:
        """是否存在 WARN 级别诊断项（不阻塞但建议处理）."""
        return any(c.status is CheckStatus.WARN for c in self.tool_checks)


def run_doctor() -> DoctorReport:
    """执行环境诊断，返回完整报告.

    按当前平台过滤工具检查项（Windows 查 mingw/NSIS，Linux 查 gcc/wine），
    聚合环境信息与工具检查结果。不抛异常：所有失败转为 :class:`CheckResult`
    标记 ERROR/WARN，便于报告统一渲染。
    """
    from fspack.config import DEFAULT_MIRROR, MIRRORS
    from fspack.config.cache import cache_root
    from fspack.platform import Platform, detect_platform

    platform = detect_platform()
    env_info: list[CheckResult] = [
        _check_python(),
        _check_platform_info(platform),
        _check_fspack_version(),
        _check_mirror_config(DEFAULT_MIRROR, MIRRORS),
        _check_cache_dir(cache_root()),
    ]

    tool_checks: list[CheckResult] = []
    # 通用工具：pip/uv/Pillow（两平台都需要）
    tool_checks.append(_check_pip())
    tool_checks.append(_check_uv())
    tool_checks.append(_check_pillow())

    if platform is Platform.WINDOWS:
        tool_checks.append(_check_mingw())
        tool_checks.append(_check_nsis())
    else:
        tool_checks.append(_check_gcc())
        tool_checks.append(_check_wine())
        tool_checks.append(_check_makensis_on_linux())

    return DoctorReport(
        env_info=tuple(env_info),
        tool_checks=tuple(tool_checks),
    )


# ---- 环境信息检查 ----


def _check_python() -> CheckResult:
    """检查当前 Python 解释器版本与路径."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return CheckResult(
        name="Python",
        status=CheckStatus.OK,
        detail=f"{version} ({sys.executable})",
    )


def _check_platform_info(platform: Platform) -> CheckResult:
    """检查目标平台标识."""
    return CheckResult(
        name="平台",
        status=CheckStatus.OK,
        detail=platform.value,
    )


def _check_fspack_version() -> CheckResult:
    """检查 fspack 自身版本."""
    return CheckResult(
        name="fspack",
        status=CheckStatus.OK,
        detail=__version__,
    )


def _check_mirror_config(default_mirror: str, mirrors: Mapping[str, object]) -> CheckResult:
    """检查镜像源配置：默认镜像名 + 可用镜像列表."""
    available = ", ".join(mirrors.keys())
    detail = f"默认={default_mirror}；可用={available}"
    return CheckResult(
        name="镜像源",
        status=CheckStatus.OK,
        detail=detail,
    )


def _check_cache_dir(cache_root: Path) -> CheckResult:
    """检查缓存目录：路径 + 总大小（递归扫描）.

    目录不存在视为 OK（首次使用尚未下载缓存），大小显示 0 B。
    """
    if not cache_root.exists():
        return CheckResult(
            name="缓存目录",
            status=CheckStatus.OK,
            detail=f"{cache_root}（尚未创建）",
        )
    try:
        size_bytes = _dir_size(cache_root)
    except OSError as exc:
        return CheckResult(
            name="缓存目录",
            status=CheckStatus.WARN,
            detail=f"{cache_root}",
            suggestion=f"扫描缓存目录失败: {exc}（不影响打包，仅诊断信息缺失）",
        )
    return CheckResult(
        name="缓存目录",
        status=CheckStatus.OK,
        detail=f"{cache_root}（{_format_size(size_bytes)}）",
    )


def _dir_size(path: Path) -> int:
    """递归计算目录总字节数（不含符号链接循环）."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _format_size(size_bytes: int) -> str:
    """字节数格式化为人类可读（如 ``"123.4 MiB"``）."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ("KiB", "MiB", "GiB", "TiB")
    size = float(size_bytes) / 1024
    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


# ---- 工具检查 ----


def _check_tool_version(
    name: str,
    cmd: list[str],
    *,
    parse_version: bool = True,
    error_suggestion: str = "",
    warn_only: bool = False,
) -> CheckResult:
    """通用工具版本检查：执行 ``cmd`` 捕获 stdout 第一行作为版本.

    :param name: 工具显示名
    :param cmd: 完整命令（如 ``["gcc", "--version"]``），``shell=False`` 安全
    :param parse_version: ``True`` 取 stdout 第一行作为版本；``False`` 仅判断
        可执行文件存在（用于 wine 等版本输出多行的工具）
    :param error_suggestion: ERROR 时的修复建议
    :param warn_only: ``True`` 时缺失降级为 WARN（不阻塞打包的可选工具）
    """
    if shutil.which(cmd[0]) is None:
        status = CheckStatus.WARN if warn_only else CheckStatus.ERROR
        return CheckResult(
            name=name,
            status=status,
            detail="未找到",
            suggestion=error_suggestion,
        )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        status = CheckStatus.WARN if warn_only else CheckStatus.ERROR
        return CheckResult(
            name=name,
            status=status,
            detail=f"执行失败: {exc}",
            suggestion=error_suggestion,
        )
    if result.returncode != 0:
        status = CheckStatus.WARN if warn_only else CheckStatus.ERROR
        stderr_first = (result.stderr or "").splitlines()[0] if result.stderr else ""
        return CheckResult(
            name=name,
            status=status,
            detail=f"退出码 {result.returncode}: {stderr_first}",
            suggestion=error_suggestion,
        )
    if not parse_version:
        return CheckResult(name=name, status=CheckStatus.OK, detail="可用")
    version_line = (result.stdout or "").splitlines()[0] if result.stdout else "可用"
    return CheckResult(name=name, status=CheckStatus.OK, detail=version_line.strip())


def _check_mingw() -> CheckResult:
    """检查 mingw-w64 交叉编译器（Windows 打包必备）."""
    return _check_tool_version(
        "mingw-w64",
        ["x86_64-w64-mingw32-gcc", "--version"],
        error_suggestion="Windows 打包需要 mingw-w64 交叉编译器。安装：choco install mingw 或 https://www.mingw-w64.org/downloads",
    )


def _check_gcc() -> CheckResult:
    """检查 gcc 编译器（Linux 打包必备）."""
    return _check_tool_version(
        "gcc",
        ["gcc", "--version"],
        error_suggestion="Linux 打包需要 gcc。安装：sudo apt install gcc 或 sudo yum install gcc",
    )


def _check_nsis() -> CheckResult:
    """检查 NSIS 安装包编译器（Windows .exe 安装包必备）."""
    return _check_tool_version(
        "NSIS",
        ["makensis", "-VERSION"],
        error_suggestion="生成 Windows 安装包需要 NSIS。安装：choco install nsis 或 https://nsis.sourceforge.io/Download",
    )


def _check_makensis_on_linux() -> CheckResult:
    """检查 Linux 上的 makensis（交叉打 Windows 安装包时需要）."""
    return _check_tool_version(
        "NSIS (交叉打包)",
        ["makensis", "-VERSION"],
        error_suggestion="Linux 交叉打 Windows 安装包需要 NSIS。安装：sudo apt install nsis 或仅打 zip/tar.gz 跳过",
        warn_only=True,
    )


def _check_wine() -> CheckResult:
    """检查 wine（Linux 运行 .exe 验证用，可选）."""
    return _check_tool_version(
        "wine",
        ["wine", "--version"],
        parse_version=False,
        error_suggestion="Linux 下 `fsp r` 运行 .exe 需要 wine。安装：sudo apt install wine 或在 Windows 上验证",
        warn_only=True,
    )


def _check_pip() -> CheckResult:
    """检查 pip 模块（wheel 下载必备）."""
    # 用 sys.executable 确保诊断当前解释器环境，非 PATH 中的 python
    if shutil.which("pip") is None and shutil.which("pip3") is None:
        # pip 命令不在 PATH 也可能以 python -m pip 形式可用
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT,
                check=False,
            )
            if result.returncode == 0:
                version_line = result.stdout.splitlines()[0] if result.stdout else "可用"
                return CheckResult(name="pip", status=CheckStatus.OK, detail=version_line.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
        return CheckResult(
            name="pip",
            status=CheckStatus.ERROR,
            detail="未找到",
            suggestion="wheel 下载需要 pip。安装：python -m ensurepip --default-pip 或 https://pip.pypa.io/en/stable/installation/",
        )
    return _check_tool_version(
        "pip",
        ["pip", "--version"],
        error_suggestion="wheel 下载需要 pip。安装：python -m ensurepip --default-pip",
    )


def _check_uv() -> CheckResult:
    """检查 uv（可选的快速 wheel 解析器）."""
    return _check_tool_version(
        "uv",
        ["uv", "--version"],
        error_suggestion="uv 是可选的快速 wheel 解析器。安装：pip install uv 或 https://docs.astral.sh/uv/",
        warn_only=True,
    )


def _check_pillow() -> CheckResult:
    """检查 Pillow 库（图标转换必备）."""
    try:
        import PIL
    except ImportError:
        return CheckResult(
            name="Pillow",
            status=CheckStatus.ERROR,
            detail="未安装",
            suggestion="图标转换需要 Pillow>=9.4.0。安装：pip install 'Pillow>=9.4.0'",
        )
    version = PIL.__version__
    # 检查版本 >= 9.4.0（bitmap_format="png" 参数最低版本）
    try:
        major, minor = version.split(".")[:2]
        if (int(major), int(minor)) < (9, 4):
            return CheckResult(
                name="Pillow",
                status=CheckStatus.WARN,
                detail=f"{version}（过低）",
                suggestion="Pillow < 9.4.0 不支持 bitmap_format='png'，ICO 小尺寸条目 alpha 退化为 1-bit。升级：pip install 'Pillow>=9.4.0'",
            )
    except (ValueError, IndexError):
        # 版本号解析失败，跳过版本检查仅报告已安装
        pass
    return CheckResult(name="Pillow", status=CheckStatus.OK, detail=version)


# ---- 报告渲染 ----


def print_doctor_report(report: DoctorReport) -> None:
    """渲染诊断报告到控制台：环境信息表 + 工具检查表 + 汇总.

    颜色映射：OK=绿、WARN=黄、ERROR=红。表格用 :class:`rich.table.Table`，
    通过 :data:`fspack.console.console` 输出，复用现有日志配置（颜色/编码）。
    """
    console.step("环境信息")
    console.rich.print(_build_table("环境信息", report.env_info))

    console.rich.print()
    console.step("工具检查")
    console.rich.print(_build_table("工具检查", report.tool_checks))

    console.rich.print()
    _print_summary(report)


def _build_table(title: str, results: tuple[CheckResult, ...]) -> Table:
    """构建 rich Table：名称 / 状态 / 详情 / 修复建议."""
    from rich.table import Table

    table = Table(title=title, show_lines=False)
    table.add_column("名称", style="cyan", no_wrap=True)
    table.add_column("状态", justify="center")
    table.add_column("详情")
    table.add_column("修复建议", style="yellow")

    for result in results:
        status_str, style = _format_status(result.status)
        table.add_row(
            result.name,
            f"[{style}]{status_str}[/]",
            result.detail,
            result.suggestion,
        )
    return table


def _format_status(status: CheckStatus) -> tuple[str, str]:
    """状态枚举转中文 + rich 样式字符串."""
    if status is CheckStatus.OK:
        return ("√ OK", "green")
    if status is CheckStatus.WARN:
        return ("! WARN", "yellow")
    return ("× ERROR", "red")


def _print_summary(report: DoctorReport) -> None:
    """打印汇总：错误数 / 警告数 / 总体结论."""
    errors = sum(1 for c in report.tool_checks if c.status is CheckStatus.ERROR)
    warns = sum(1 for c in report.tool_checks if c.status is CheckStatus.WARN)
    oks = sum(1 for c in report.tool_checks if c.status is CheckStatus.OK)

    if errors:
        console.error(f"诊断完成：{oks} OK / {warns} 警告 / {errors} 错误")
        console.error(f"存在 {errors} 项错误，可能导致打包失败，请按'修复建议'处理")
    elif warns:
        console.warn(f"诊断完成：{oks} OK / {warns} 警告 / {errors} 错误")
        console.warn("存在警告项，不阻塞打包但建议处理")
    else:
        console.success(f"诊断完成：{oks} OK / {warns} 警告 / {errors} 错误")
        console.success("环境就绪，可以开始打包")
