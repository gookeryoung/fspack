"""``fsp doctor`` 工具检查.

检查 fspack 打包所需外部工具的可用性与版本：mingw-w64/gcc/clang/NSIS/
wine/pip/uv/Pillow。按平台过滤（Windows 查 mingw/NSIS，Linux 查 gcc/wine，
macOS 查 clang），通用工具 pip/uv/Pillow 两平台都查。

设计要点：

- 工具检查用 ``subprocess.run([tool, "--version"], ...)`` + ``shell=False``，
  超时 :data:`_VERSION_TIMEOUT`（5s）兜底防卡死，失败返回 :class:`CheckResult`
  标记缺失
- 可选工具（wine/uv 等）缺失降级为 WARN（``warn_only=True``），不阻塞打包
- pip 优先用 PATH 中的命令（pip 缺失时改用 pip3 探测），均不在 PATH 时
  回退 ``python -m pip`` 确保诊断当前解释器环境
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from fspack.doctor.models import CheckResult, CheckStatus

__all__ = [
    "_VERSION_TIMEOUT",
    "_check_clang",
    "_check_gcc",
    "_check_makensis_on_linux",
    "_check_mingw",
    "_check_nsis",
    "_check_pillow",
    "_check_pip",
    "_check_tool_version",
    "_check_uv",
    "_check_wine",
]

# subprocess.run 超时（秒）：工具版本查询应秒级返回，5s 兜底防卡死
_VERSION_TIMEOUT = 5


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
    """检查 NSIS 安装包编译器（Windows .exe 安装包必备）.

    优先探测 fspack 缓存的 makensis（``<cache_root>/nsis``，见
    :mod:`fspack.packaging.installer.nsis_tool`；只读探测不触发下载），
    未缓存时回退 PATH 中的 makensis。
    """
    from fspack.packaging.installer.nsis_tool import find_cached_makensis

    cached = find_cached_makensis()
    cmd = [str(cached), "-VERSION"] if cached is not None else ["makensis", "-VERSION"]
    return _check_tool_version(
        "NSIS",
        cmd,
        error_suggestion=(
            "生成 Windows 安装包需要 NSIS。安装：choco install nsis 或 "
            "https://nsis.sourceforge.io/Download；也可将 NSIS 归档"
            "（nsis-3.11.zip 或 portable 变体）放入 fspack 缓存的 nsis 目录，"
            "或留空由首次打安装包时自动下载"
        ),
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
    _install_suggestion = "wheel 下载需要 pip。安装：python -m ensurepip --default-pip"
    if shutil.which("pip") is not None:
        return _check_tool_version("pip", ["pip", "--version"], error_suggestion=_install_suggestion)
    # pip 命令缺失时优先用 pip3 探测版本（部分环境只安装 pip3 入口脚本）
    if shutil.which("pip3") is not None:
        return _check_tool_version("pip", ["pip3", "--version"], error_suggestion=_install_suggestion)
    # pip/pip3 命令均不在 PATH，回退 python -m pip 确保诊断当前解释器环境
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
