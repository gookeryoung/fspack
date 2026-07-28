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
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from fspack import __version__
from fspack.console import console

if TYPE_CHECKING:
    from rich.table import Table

    from fspack.platform import Platform
    from fspack.templates.project_template import ProjectTemplate

__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "TemplateBuildResult",
    "TemplateRunResult",
    "print_doctor_report",
    "run_doctor",
    "run_doctor_bench",
    "run_doctor_test",
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
    elif platform is Platform.MACOS:
        tool_checks.append(_check_clang())
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


def _check_clang() -> CheckResult:
    """检查 clang 编译器（macOS 打包必备，Xcode Command Line Tools 提供）."""
    return _check_tool_version(
        "clang",
        ["clang", "--version"],
        error_suggestion="macOS 打包需要 clang。安装：xcode-select --install 或从 App Store 安装 Xcode",
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


# ---- 模板构建测试（--test）与性能基准（--bench）----


@dataclass(frozen=True)
class TemplateRunResult:
    """单个模板运行验证结果.

    构建成功后实际运行产出的可执行文件，验证「能调用」：
    CLI 应用应正常退出（退出码 0）；GUI/Web 应用启动后进入事件循环
    不退出，用超时判定为「启动成功」并主动终止进程。

    :param success: 运行是否成功（退出码 0 或超时视为 GUI 事件循环正常）
    :param timed_out: 是否因超时被终止（True 表示应用进入事件循环不退出，
        视为 GUI/Web 应用启动成功；False 表示进程自行退出）
    :param exit_code: 进程退出码（超时被终止时为 ``None``）
    :param duration_sec: 运行耗时（秒，超时场景为超时阈值）
    :param error: 失败时的错误信息（stderr 首行，截断到 200 字符）
    """

    success: bool
    timed_out: bool
    exit_code: int | None
    duration_sec: float
    error: str = ""


# 运行验证超时（秒）：CLI 应用通常 <1s 退出，GUI/Web 进入事件循环不退出。
# 5s 给慢启动足够余量，超时后视为「启动成功」（GUI 正常运行）并主动终止。
_RUN_TIMEOUT_SEC = 5.0

# 终止进程后的等待时间（秒）：terminate 后给进程 2s 清理，仍不退出则 kill。
_TERMINATE_GRACE_SEC = 2.0


def _find_dist_exe(proj_dir: Path, name: str) -> Path | None:
    """在 ``proj_dir/dist/`` 下查找项目可执行文件.

    与 :func:`fspack.runner._find_exe` 同逻辑，但接收 ``proj_dir`` 而非
    project（doctor 在临时工作目录下构建，project 路径即 ``proj_dir``）。

    Linux 优先原生无后缀可执行文件，回退 ``.exe``（用 wine 运行）；
    Windows/macOS 仅查 ``.exe``。

    :param proj_dir: 项目根目录（含 ``dist/``）
    :param name: 可执行文件名（取自 ``pyproject.toml`` 的 ``name``）
    :return: 可执行文件路径或 ``None``（未找到）
    """
    from fspack.platform import Platform, detect_platform

    dist = proj_dir / "dist"
    candidates: list[Path]
    if detect_platform() is Platform.LINUX:
        candidates = [dist / name, dist / f"{name}.exe"]
    else:
        candidates = [dist / f"{name}.exe"]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _build_run_cmd(exe: Path) -> list[str]:
    """构造运行命令：Linux 下 ``.exe`` 用 wine，原生可执行文件直跑.

    与 :func:`fspack.runner._build_cmd` 同逻辑，独立于 runner 模块以避免
    doctor ↔ runner 循环依赖。wine 不在 PATH 时回退字符串 ``"wine"``，
    :func:`_run_template` 会捕获 :class:`FileNotFoundError` 报告未安装。

    仅作为 debug 模式不可用时的回退方案（直跑 loader exe）。
    """
    from fspack.platform import Platform, detect_platform

    if exe.suffix == ".exe" and detect_platform() is Platform.LINUX:
        wine = shutil.which("wine") or "wine"
        return [wine, str(exe)]
    return [str(exe)]


def _find_debug_python(proj_dir: Path) -> Path | None:
    """查找 debug 模式用的 embed python 路径.

    Windows 用 ``dist/runtime/python.exe``，Linux/macOS 用
    ``dist/runtime/python/bin/python3.X``（standalone python）。
    """
    from fspack.platform import Platform, detect_platform

    dist = proj_dir / "dist"
    if detect_platform() is Platform.WINDOWS:
        py = dist / "runtime" / "python.exe"
        return py if py.is_file() else None
    bin_dir = dist / "runtime" / "python" / "bin"
    pys = sorted(bin_dir.glob("python3.*"))
    return pys[0] if pys else None


def _find_wrapper(proj_dir: Path, name: str) -> Path | None:
    """查找入口包装器 ``dist/_entry_<name>.py`` 路径."""
    wrapper = proj_dir / "dist" / f"_entry_{name}.py"
    return wrapper if wrapper.is_file() else None


def _build_debug_cmd(proj_dir: Path, name: str) -> tuple[list[str], dict[str, str]] | None:
    """构造 debug 模式运行命令：embed python + 入口包装器.

    模拟 ``fsp r --debug`` 行为：用 console 子系统的 embed python 直跑
    ``_entry_<name>.py`` 包装器，使 stdout/stderr 可见（GUI subsystem
    下被 Windows 吞掉），且 wrapper 设置 Qt 插件路径、Tcl/Tk 环境变量、
    site-packages sys.path 等，避免 GUI 应用（PySide2/PyQt5/tkinter）
    因环境变量缺失启动失败。

    与直跑 loader exe 的差异：

    - debug 模式用 console 子系统，``print`` 输出可见，便于诊断
    - Linux 用原生 standalone python（不需 wine），避免 wine 下 GUI 应用
      缺 X11/Qt 插件路径问题
    - wrapper 显式设置 ``PYTHONHOME``（Linux）使 standalone python 找到标准库

    :param proj_dir: 项目根目录（含 ``dist/``）
    :param name: 入口名（``pyproject.toml`` 的 ``name``）
    :return: ``(cmd, env)`` 或 ``None``（wrapper/embed python 缺失，调用方回退直跑 exe）
    """
    from fspack.platform import Platform, detect_platform

    py = _find_debug_python(proj_dir)
    wrapper = _find_wrapper(proj_dir, name)
    if py is None or wrapper is None:
        return None
    cmd = [str(py), str(wrapper)]
    env: dict[str, str] = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if detect_platform() is not Platform.WINDOWS:
        env["PYTHONHOME"] = str(proj_dir / "dist" / "runtime" / "python")
    return cmd, env


def _run_template(
    cmd: list[str],
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = _RUN_TIMEOUT_SEC,
) -> TemplateRunResult:
    """运行已构建的可执行文件，验证可调用性.

    统一用超时策略处理 CLI/GUI/Web 应用，无需依赖 ``app_type`` 字段：

    - 进程自行退出且退出码 ``0`` → 成功（CLI 正常执行完成）
    - 进程自行退出且退出码非 ``0`` → 失败（启动崩溃，捕获 stderr 首行）
    - 超时未退出 → 视为成功（GUI/Web 进入事件循环不退出），
      主动 ``terminate`` + ``kill``，``exit_code=None``

    :param cmd: 运行命令（debug 模式为 ``[python, wrapper]``，回退为 ``[exe]``/``[wine, exe]``）
    :param env: 环境变量（debug 模式含 ``PYTHONHOME``/``PYTHONUNBUFFERED``），``None`` 继承当前环境
    :param timeout: 超时秒数（默认 :data:`_RUN_TIMEOUT_SEC`）
    :return: 运行验证结果
    """
    start = time.perf_counter()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except (OSError, ValueError) as exc:
        elapsed = time.perf_counter() - start
        return TemplateRunResult(
            success=False,
            timed_out=False,
            exit_code=None,
            duration_sec=elapsed,
            error=f"启动失败: {exc}",
        )

    try:
        _stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 超时未退出 → 视为 GUI/Web 事件循环正常运行，主动终止
        proc.terminate()
        try:
            proc.communicate(timeout=_TERMINATE_GRACE_SEC)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        elapsed = time.perf_counter() - start
        _logger.debug("模板运行超时（视为 GUI/Web 事件循环正常）: %s", " ".join(cmd))
        return TemplateRunResult(
            success=True,
            timed_out=True,
            exit_code=None,
            duration_sec=elapsed,
        )

    elapsed = time.perf_counter() - start
    if proc.returncode == 0:
        return TemplateRunResult(
            success=True,
            timed_out=False,
            exit_code=0,
            duration_sec=elapsed,
        )
    stderr_first = (stderr or "").splitlines()[0] if stderr else ""
    if len(stderr_first) > 200:
        stderr_first = stderr_first[:197] + "..."
    _logger.warning("模板运行失败 %s: 退出码 %s", " ".join(cmd), proc.returncode)
    return TemplateRunResult(
        success=False,
        timed_out=False,
        exit_code=proc.returncode,
        duration_sec=elapsed,
        error=stderr_first or f"退出码 {proc.returncode}",
    )


@dataclass(frozen=True)
class TemplateBuildResult:
    """单个模板构建结果.

    :param template_id: 模板目录名
    :param success: 构建是否成功
    :param duration_sec: 总构建耗时（秒）
    :param error: 失败时的错误信息（截断到 200 字符）
    :param dist_size: 构建产物 ``dist/`` 目录大小（字节），失败时为 0
    :param entry_count: 入口 exe 数量
    :param run_result: 构建成功后的运行验证结果；构建失败或未运行验证时为 ``None``
    """

    template_id: str
    success: bool
    duration_sec: float
    error: str = ""
    dist_size: int = 0
    entry_count: int = 0
    run_result: TemplateRunResult | None = None


def _build_single_template(  # pragma: no cover
    template: ProjectTemplate,
    work_dir: Path,
    *,
    bench: bool = False,
) -> TemplateBuildResult:
    """构建单个模板项目，返回结果.

    :param template: 项目模板
    :param work_dir: 临时工作目录（构建在此目录下的子目录进行）
    :param bench: ``True`` 时启用 ``profile=True``，输出详细性能报告
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror
    from fspack.platform import detect_platform

    proj_dir = work_dir / template.id
    shutil.copytree(template.dir, proj_dir, dirs_exist_ok=True)

    opts = BuildOptions(no_size_report=True)
    mirror = get_mirror()
    target = detect_platform()

    start = time.perf_counter()
    try:
        build(
            proj_dir,
            mirror,
            target=target,
            options=opts,
            profile=bench,
        )
    except Exception as e:
        elapsed = time.perf_counter() - start
        err_msg = str(e)[:200]
        _logger.warning("模板 %s 构建失败: %s", template.id, err_msg)
        return TemplateBuildResult(
            template_id=template.id,
            success=False,
            duration_sec=elapsed,
            error=err_msg,
        )

    elapsed = time.perf_counter() - start
    dist_dir = proj_dir / "dist"
    dist_size = _dir_size(dist_dir) if dist_dir.is_dir() else 0
    entry_count = len(list(dist_dir.glob("*.exe"))) if dist_dir.is_dir() else 0

    # 构建成功后解析项目入口：多入口项目产出的 exe 名是 [tool.fspack.entries]
    # 的键（如 cli/gui/web），不等于 template.name。用 ProjectInfo.all_entries[0]
    # 取首个入口名（与 `fsp r` 默认行为一致），避免多入口项目跳过运行验证。
    from fspack.config import ProjectInfo

    entry_name = ProjectInfo.from_dir(proj_dir).all_entries[0].name

    # 运行验证：优先用 debug 模式（embed python + wrapper），模拟 `fsp r --debug`：
    # console 子系统 stdout 可见，wrapper 设置 Qt 插件路径、Tcl/Tk 环境变量、
    # site-packages sys.path 等，避免 GUI 应用因环境变量缺失启动失败。
    # debug 模式不可用（wrapper/embed python 缺失）时回退直跑 loader exe。
    debug = _build_debug_cmd(proj_dir, entry_name)
    if debug is not None:
        cmd, env = debug
        run_result = _run_template(cmd, env)
    else:
        exe = _find_dist_exe(proj_dir, entry_name)
        if exe is not None:
            run_result = _run_template(_build_run_cmd(exe))
        else:
            run_result = None
        if exe is None and entry_count > 0:
            _logger.debug("模板 %s 未找到入口 %s 的可执行文件，跳过运行验证", template.id, entry_name)

    return TemplateBuildResult(
        template_id=template.id,
        success=True,
        duration_sec=elapsed,
        dist_size=dist_size,
        entry_count=entry_count,
        run_result=run_result,
    )


def run_doctor_test() -> None:  # pragma: no cover
    """运行所有项目模板构建，打印汇总结果.

    从 ``assets/templates/`` 加载所有项目模板，逐个复制到临时目录并执行
    :func:`fspack.builder.build`，收集成功/失败/耗时，输出汇总表格。

    用于验证打包流程对所有模板项目的兼容性，CI 中可作为回归门禁。
    """
    from fspack.templates.project_template import ProjectTemplate

    templates = ProjectTemplate.list_all()
    if not templates:
        console.warn("未找到项目模板")
        return

    console.step(f"模板构建测试（{len(templates)} 个模板）")
    results: list[TemplateBuildResult] = []

    with tempfile.TemporaryDirectory(prefix="fsp-doctor-test-") as tmp:
        work_dir = Path(tmp)
        for i, tpl in enumerate(templates, 1):
            console.rich.print(f"[cyan][{i}/{len(templates)}][/cyan] 构建 {tpl.id} ...")
            result = _build_single_template(tpl, work_dir, bench=False)
            results.append(result)
            if result.success:
                console.rich.print(
                    f"  [green]√[/green] 成功 ({result.duration_sec:.1f}s, {_format_size(result.dist_size)})"
                )
            else:
                console.rich.print(f"  [red]×[/red] 失败: {result.error}")

    _print_template_build_summary(results, bench=False)


def run_doctor_bench() -> None:  # pragma: no cover
    """运行所有项目模板构建，收集性能数据，输出性能分析报告.

    与 :func:`run_doctor_test` 相同的构建流程，但每个模板启用
    ``profile=True``，输出详细的各阶段耗时报告。最后打印汇总表格与
    性能分析（耗时排名、产物大小排名、总时间）。

    用于建立性能基准，后续优化措施可与此基准对比评估效果。
    """
    from fspack.templates.project_template import ProjectTemplate

    templates = ProjectTemplate.list_all()
    if not templates:
        console.warn("未找到项目模板")
        return

    console.step(f"性能基准测试（{len(templates)} 个模板）")
    results: list[TemplateBuildResult] = []

    with tempfile.TemporaryDirectory(prefix="fsp-doctor-bench-") as tmp:
        work_dir = Path(tmp)
        for i, tpl in enumerate(templates, 1):
            console.rich.print(f"[cyan][{i}/{len(templates)}][/cyan] 基准构建 {tpl.id} ...")
            result = _build_single_template(tpl, work_dir, bench=True)
            results.append(result)
            if result.success:
                console.rich.print(
                    f"  [green]√[/green] 成功 ({result.duration_sec:.1f}s, {_format_size(result.dist_size)})"
                )
            else:
                console.rich.print(f"  [red]×[/red] 失败: {result.error}")

    _print_template_build_summary(results, bench=True)


def _print_template_build_summary(results: list[TemplateBuildResult], *, bench: bool) -> None:
    """打印模板构建汇总表格与性能分析.

    :param results: 所有模板构建结果
    :param bench: ``True`` 时额外输出性能分析（耗时排名、产物大小排名）
    """
    from rich.table import Table

    console.rich.print()
    table = Table(title="模板构建汇总", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("模板", style="cyan")
    table.add_column("构建", justify="center")
    table.add_column("运行", justify="center")
    table.add_column("耗时", justify="right")
    table.add_column("产物大小", justify="right")
    table.add_column("入口数", justify="right")
    if bench:
        table.add_column("错误信息", style="dim")

    succeeded = 0
    total_time = 0.0
    total_size = 0
    run_ok = 0
    run_fail = 0
    run_skip = 0
    for i, r in enumerate(results, 1):
        build_str = "[green]√ 成功[/green]" if r.success else "[red]× 失败[/red]"
        if r.success:
            succeeded += 1
            total_time += r.duration_sec
            total_size += r.dist_size
        run_str, err_msg = _format_run_status(r)
        if r.success:
            if r.run_result is None:
                run_skip += 1
            elif r.run_result.success:
                run_ok += 1
            else:
                run_fail += 1
        # bench 模式错误信息：构建失败显示构建错误，构建成功但运行失败显示运行错误
        error_msg = r.error if not r.success else err_msg
        table.add_row(
            str(i),
            r.template_id,
            build_str,
            run_str,
            f"{r.duration_sec:.1f}s",
            _format_size(r.dist_size) if r.dist_size else "-",
            str(r.entry_count) if r.entry_count else "-",
            error_msg if bench else "",
        )

    console.rich.print(table)
    console.rich.print()

    # 汇总统计
    failed = len(results) - succeeded
    if failed:
        console.warn(f"构建完成：{succeeded} 成功 / {failed} 失败 / {len(results)} 总计")
    else:
        console.success(f"构建完成：{succeeded}/{len(results)} 全部成功")

    _print_run_summary(succeeded, run_ok, run_fail, run_skip)

    if succeeded > 0:
        avg_time = total_time / succeeded
        console.rich.print(f"  总耗时 {total_time:.1f}s | 平均 {avg_time:.1f}s | 总产物 {_format_size(total_size)}")

    # 性能分析（仅 --bench 模式）
    if bench and succeeded > 1:
        _print_performance_analysis(results)


def _print_run_summary(succeeded: int, run_ok: int, run_fail: int, run_skip: int) -> None:
    """打印运行验证汇总：构建成功的模板参与验证，区分成功/失败/跳过.

    :param succeeded: 构建成功的模板数
    :param run_ok: 运行验证成功数（含超时视为 GUI 事件循环正常）
    :param run_fail: 运行验证失败数（退出码非 0 或启动失败）
    :param run_skip: 跳过运行验证数（未找到可执行文件）
    """
    run_total = run_ok + run_fail
    if run_total == 0:
        if succeeded > 0 and run_skip > 0:
            console.warn(f"运行验证：{run_skip} 个模板跳过（未找到可执行文件）")
        return
    if run_fail > 0:
        console.warn(f"运行验证：{run_ok} 成功 / {run_fail} 失败 / {run_total} 验证 / {run_skip} 跳过")
    else:
        console.success(f"运行验证：{run_ok}/{run_total} 全部通过（{run_skip} 跳过）")


def _format_run_status(result: TemplateBuildResult) -> tuple[str, str]:
    """格式化运行验证状态为 rich 标记字符串.

    :param result: 模板构建结果
    :return: (运行状态字符串, 错误信息)。运行状态字符串含 rich 标记：
        - 构建失败 → ``"-"``（不运行验证）
        - ``run_result`` 为 ``None`` → ``"[dim]跳过[/]"``（未找到 exe）
        - 成功且超时 → ``"[green]√ 超时[/]"``（GUI/Web 事件循环正常）
        - 成功未超时 → ``"[green]√ 成功[/]"``（CLI 正常退出码 0）
        - 失败 → ``"[red]× 失败[/]"``
    """
    if not result.success:
        return ("-", "")
    rr = result.run_result
    if rr is None:
        return ("[dim]跳过[/]", "")
    if rr.success:
        if rr.timed_out:
            return ("[green]√ 超时[/]", "")
        return ("[green]√ 成功[/]", "")
    return ("[red]× 失败[/]", rr.error)


def _print_performance_analysis(results: list[TemplateBuildResult]) -> None:
    """打印性能分析：耗时排名、产物大小排名、瓶颈识别.

    :param results: 所有模板构建结果（仅成功的参与排名）
    """
    from rich.table import Table

    ok_results = [r for r in results if r.success]
    if len(ok_results) < 2:
        return

    console.rich.print()
    console.step("性能分析")

    # 耗时排名
    by_time = sorted(ok_results, key=lambda r: r.duration_sec, reverse=True)
    time_table = Table(title="耗时排名（降序）", show_lines=False)
    time_table.add_column("#", justify="right", style="dim")
    time_table.add_column("模板", style="cyan")
    time_table.add_column("耗时", justify="right")
    time_table.add_column("占比", justify="right")
    total = sum(r.duration_sec for r in ok_results)
    for i, r in enumerate(by_time, 1):
        ratio = r.duration_sec / total * 100 if total > 0 else 0
        marker = " [red](最慢)[/red]" if i == 1 else ""
        time_table.add_row(str(i), r.template_id, f"{r.duration_sec:.1f}s", f"{ratio:.1f}%{marker}")
    console.rich.print(time_table)

    # 产物大小排名
    by_size = sorted(ok_results, key=lambda r: r.dist_size, reverse=True)
    size_table = Table(title="产物大小排名（降序）", show_lines=False)
    size_table.add_column("#", justify="right", style="dim")
    size_table.add_column("模板", style="cyan")
    size_table.add_column("大小", justify="right")
    for i, r in enumerate(by_size, 1):
        marker = " [red](最大)[/red]" if i == 1 else ""
        size_table.add_row(str(i), r.template_id, _format_size(r.dist_size) + marker)
    console.rich.print(size_table)

    # 瓶颈识别
    slowest = by_time[0]
    fastest = by_time[-1]
    if fastest.duration_sec > 0:
        ratio = slowest.duration_sec / fastest.duration_sec
        console.rich.print(
            f"\n最慢模板 [cyan]{slowest.template_id}[/cyan] ({slowest.duration_sec:.1f}s) "
            f"是最快 [cyan]{fastest.template_id}[/cyan] ({fastest.duration_sec:.1f}s) 的 {ratio:.1f} 倍"
        )
