"""uv CLI 集成层：可执行检测、平台映射、能力探测与依赖图解析.

从 :mod:`fspack.packaging.wheels.resolver` 拆分而来，封装与 uv 二进制交互的
全部细节。上层模块（resolver 编排、parallel 下载）通过本模块使用 uv，
不直接拼 uv 命令行。

职责清单：

- ``_find_uv``/``_uv_supports_download``：uv 可执行检测与 ``pip download``
  子命令能力探测（uv 0.1.9+ 移除该子命令，改用 ``uv cache fetch``）
- ``_uv_python_platform``：pip 平台标签 → uv ``--python-platform`` 三值映射
- ``_resolve_with_uv``：``uv pip compile`` 依赖图解析（PubGrub 算法），
  uv 路径取自 :class:`~fspack.packaging.wheels.resolver.DownloadContext`
  （由编排层探测填充，本模块不自查 ``PATH``）
- ``_convert_uv_output_to_pip_format``/``_extract_resolved_lines``：uv 输出
  解析（格式转换供 pip wheel 路径解析与下载事件日志复用）

依赖 :mod:`fspack.packaging.wheels.downloader` 的 ``_run_pip`` 由上层
（resolver）惰性调用，本模块不依赖包内其他模块（除异常类型），处于依赖链
最底层。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from fspack.exceptions import DependencyError

if TYPE_CHECKING:
    # DownloadContext 仅用于类型注解（from __future__ import annotations 使注解
    # 不在运行时求值）。顶层导入 resolver 会形成循环：resolver 顶层导入本模块。
    from fspack.packaging.wheels.resolver import DownloadContext

__all__ = [
    "_UV_DOWNLOAD_WHEEL_RE",
    "_UV_RESOLVED_LINE_RE",
    "_convert_uv_output_to_pip_format",
    "_extract_resolved_lines",
    "_find_uv",
    "_resolve_with_uv",
    "_uv_python_platform",
    "_uv_supports_download",
]

_logger = logging.getLogger(__name__)

# uv pip compile 输出中匹配 ``name==version`` 的行（忽略注释/空行）
_UV_RESOLVED_LINE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+*-]+)")

# uv pip download 输出中匹配 ``Downloaded <name>.whl``/``Cached <name>.whl`` 行
# uv 0.1.x 输出形如 ``Downloaded requests-2.31.0-py3-none-any.whl``，
# 转换为 pip 兼容的 ``Saved <name>.whl`` 格式供 :func:`_parse_pip_download_wheels` 解析
_UV_DOWNLOAD_WHEEL_RE = re.compile(r"(?:Downloaded|Cached)\s+(.+?\.whl)", re.IGNORECASE)

# uv pip download --help 检测超时（秒）：uv 启动 ~10ms，5s 裕量覆盖慢速 CI
_UV_HELP_TIMEOUT = 5.0


def _find_uv() -> str | None:
    """查找 ``uv`` 可执行文件，未找到返回 ``None``。

    用于在线依赖解析（``uv pip compile``），避免 pip 的 backtracking resolver
    在复杂依赖图上报 ``resolution-too-deep``。uv 用 PubGrub 算法，能高效解析。
    """
    return shutil.which("uv")


def _uv_python_platform(platform_tags: Sequence[str]) -> str:
    """将 pip 平台标签列表映射为 uv ``--python-platform`` 粗粒度平台值.

    uv 仅接受 ``windows``/``linux``/``macos`` 三值，映射规则（按优先级）：

    - 任一 tag 含 ``win``（如 ``win_amd64``/``win32``）→ ``windows``
    - 任一 tag 以 ``macosx`` 开头（如 ``macosx_11_0_arm64``）→ ``macos``
    - 其余（manylinux/musllinux 等）→ ``linux``

    ``_resolve_with_uv`` 与 ``_download_one_with_uv`` 两处调用点共用，
    避免二值映射把 macOS 目标误解析为 linux wheel。
    """
    if any("win" in t for t in platform_tags):
        return "windows"
    if any(t.startswith("macosx") for t in platform_tags):
        return "macos"
    return "linux"


def _uv_supports_download(uv_path: str | None) -> bool:
    """检测 uv 是否支持 ``pip download`` 子命令.

    ``uv pip download`` 在 uv 0.1.0~0.1.8 中实验性支持，0.1.9+ 完全移除
    （改用 ``uv cache fetch``）。运行时调 ``uv pip download --help`` 检测：
    退出码 0 视为支持，非零（含 ``unrecognized subcommand``）视为不支持。
    uv 不可用（``uv_path`` 为 None）时直接返回 False。

    每次构建调一次（~10ms uv 启动），结果传递给 ``_download_resolved_parallel``
    避免逐包检测。
    """
    if uv_path is None:
        return False
    try:
        result = subprocess.run(
            [uv_path, "pip", "download", "--help"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_UV_HELP_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def _convert_uv_output_to_pip_format(uv_output: str) -> str:
    """将 ``uv pip download`` 输出转换为 pip download 兼容格式.

    uv 输出 ``Downloaded <name>.whl`` / ``Cached <name>.whl``，pip 输出
    ``Saved <name>.whl`` / ``File was already downloaded <name>.whl``。
    下游 :func:`_parse_pip_download_wheels` 匹配 ``Saved``/``File was already
    downloaded``，故将 uv 输出转换为 ``Saved <name>.whl`` 格式。

    Args:
        uv_output: uv pip download 的 stdout + stderr 合并文本.

    Returns:
        pip 兼容格式文本，每行 ``Saved <name>.whl``（去重保序）.
    """
    names: list[str] = []
    seen: set[str] = set()
    for line in uv_output.splitlines():
        m = _UV_DOWNLOAD_WHEEL_RE.search(line)
        if m:
            name = Path(m.group(1).strip()).name
            if name not in seen:
                names.append(name)
                seen.add(name)
    return "".join(f"Saved {name}\n" for name in names)


def _extract_resolved_lines(uv_output: str) -> list[str]:
    """从 ``uv pip compile`` 输出（不带 --generate-hashes）提取 ``name==version`` 列表.

    uv 输出形如::

        rich==13.7.0
        requests==2.31.0

    正则匹配每行首个 ``name==version`` 对，跳过注释/空行/--hash 续行。
    """
    resolved: list[str] = []
    for line in uv_output.splitlines():
        m = _UV_RESOLVED_LINE_RE.match(line.strip())
        if m:
            resolved.append(f"{m.group(1)}=={m.group(2)}")
    return resolved


def _resolve_with_uv(ctx: DownloadContext, packages: Sequence[str], *, generate_hashes: bool = False) -> str:
    """用 ``uv pip compile`` 解析依赖图，返回带哈希的 requirements 文本.

    uv 用 PubGrub 算法（SAT solver 系），能解析 pip backtracking resolver
    无法处理的复杂依赖图（避免 ``resolution-too-deep``）。

    ``--python-version``/``--python-platform`` 让 uv 按目标环境解析；
    ``--no-header`` 去除注释头部。uv 路径取自 ``ctx.uv_path``（编排层
    ``_download_online`` 探测 ``_find_uv()`` 后填充）。

    ``generate_hashes=True`` 时附加 ``--generate-hashes``，uv 输出形如::

        rich==13.7.0 \\
            --hash=sha256:xxx \\
            --hash=sha256:yyy

    供 ``pip download --require-hashes -r`` 校验。返回原始 stdout 文本，
    由调用方写入临时 requirements.txt。

    无 ``generate_hashes`` 时调用方仍可用 :func:`_extract_resolved_lines`
    提取 ``name==version`` 列表做并行下载（不校验哈希）。

    Raises:
        DependencyError: ``ctx.uv_path`` 未设置（编排层未探测到 uv）或
            uv 未解析出任何依赖。
    """
    if ctx.uv_path is None:
        raise DependencyError("未找到 uv，无法执行在线依赖解析")
    major, minor = ctx.py_version.split(".")[:2]
    # uv 的 --python-platform 只有 windows/linux/macos 粗粒度，按标签映射
    py_platform = _uv_python_platform(ctx.platform_tags)
    cmd: list[str] = [
        ctx.uv_path,
        "pip",
        "compile",
        "--python-version",
        f"{major}.{minor}",
        "--python-platform",
        py_platform,
        "--no-header",
        "--index-url",
        ctx.pypi_index,
    ]
    if generate_hashes:
        cmd.append("--generate-hashes")
    # 私有包源：额外索引与 wheel 目录
    for url in ctx.extra_index_urls:
        cmd.extend(["--extra-index-url", url])
    for link in ctx.find_links:
        cmd.extend(["--find-links", link])
    cmd.append("-")
    # uv pip compile 从 stdin 读取需求列表
    stdin_data = "\n".join(packages) + "\n"
    _logger.info("uv pip compile 解析依赖图（generate_hashes=%s）: %s", generate_hashes, " ".join(packages))
    result = subprocess.run(cmd, input=stdin_data, check=True, capture_output=True, encoding="utf-8", errors="replace")
    if not result.stdout.strip():
        raise DependencyError(f"uv pip compile 未解析出任何依赖:\n{result.stderr}")
    _logger.info("uv 解析完成，输出 %d 字节", len(result.stdout))
    return result.stdout
