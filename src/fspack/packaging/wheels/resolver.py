"""Wheel 依赖解析与在线下载：uv pip compile 解析 + pip download 并行下载.

从 :mod:`fspack.packaging.wheels.downloader` 拆分而来，封装在线依赖解析与下载逻辑。

核心流程：

1. ``_run_pip_download``：先用 ``--no-index`` 离线解析，失败回退到 ``_download_online``
2. ``_download_online``：优先用 ``uv pip compile`` 解析依赖图（PubGrub 算法），
   再用 ``pip download --no-deps`` 逐个下载；uv 不可用回退到 ``pip download`` 完整解析
3. ``_download_resolved_parallel``：用 ``ThreadPoolExecutor`` 并行下载 uv 解析出的
   精确版本 wheel，失败时通过 sdist 回退重试

依赖 :mod:`fspack.packaging.wheels.sdist` 提供 ``_handle_sdist_fallback``（顶层导入）。
依赖 :mod:`fspack.packaging.wheels.downloader` 提供 ``_run_pip``（惰性导入避免循环依赖：
``downloader`` 顶层导入本模块，本模块不能顶层导入 ``downloader``）。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

from fspack.config import is_offline
from fspack.exceptions import DependencyError
from fspack.packaging.wheels.sdist import _handle_sdist_fallback

__all__ = [
    "_UV_DOWNLOAD_WHEEL_RE",
    "_UV_RESOLVED_LINE_RE",
    "_convert_uv_output_to_pip_format",
    "_download_one_resolved",
    "_download_one_with_uv",
    "_download_online",
    "_download_resolved_parallel",
    "_find_uv",
    "_merge_parallel_results",
    "_resolve_with_uv",
    "_run_pip_download",
    "_uv_supports_download",
]

_logger = logging.getLogger(__name__)

# 并行下载线程数上限：I/O 密集网络下载，8 个并发平衡 PyPI 限流与吞吐量
# 单个 wheel 下载耗时差异大（几 KB 元数据 vs 数百 MB 二进制），线程池自动调度
_PARALLEL_DOWNLOAD_WORKERS = 8

# uv pip compile 输出中匹配 ``name==version`` 的行（忽略注释/空行）
_UV_RESOLVED_LINE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+*-]+)")

# uv pip download 输出中匹配 ``Downloaded <name>.whl``/``Cached <name>.whl`` 行
# uv 0.1.x 输出形如 ``Downloaded requests-2.31.0-py3-none-any.whl``，
# 转换为 pip 兼容的 ``Saved <name>.whl`` 格式供 :func:`_parse_pip_download_wheels` 解析
_UV_DOWNLOAD_WHEEL_RE = re.compile(r"(?:Downloaded|Cached)\s+(.+?\.whl)", re.IGNORECASE)

# uv pip download --help 检测超时（秒）：uv 启动 ~10ms，5s 裕量覆盖慢速 CI
_UV_HELP_TIMEOUT = 5.0

# pip download stdout 中匹配 wheel 完整路径（"Saved <path>.whl" / "File was already
# downloaded <path>.whl"）。用于单包下载完成事件日志：从路径读 stat 获取字节数，
# 让用户在并行下载场景下能看到每个 wheel 的下载进展（避免 10MB+ wheel 静默下载
# 被误判为"卡住"）。
_PIP_SAVED_WHEEL_RE = re.compile(r"(?:Saved|File was already downloaded)\s+(.+\.whl)", re.IGNORECASE)


def _log_download_event(req: str, stdout: str, stderr: str, elapsed: float, cache_dir: Path | None = None) -> None:
    """打印单包下载完成事件日志：req、wheel 文件名、大小、耗时.

    从 stdout 解析 wheel 完整路径（pip ``Saved``/``File was already downloaded`` 行），
    读 ``stat().st_size`` 获取字节数。解析失败时退化为仅打印 req 与耗时。

    pip 路径 stdout 含完整路径，``cache_dir`` 可不传；uv 路径 stdout 仅含 wheel
    文件名，需 ``cache_dir`` 拼接才能定位文件取大小。

    并行模式下多线程并发调用，``logging.info`` 单次调用线程安全，事件不会交错。
    """
    from fspack._util.format import format_bytes_dec

    wheel_path: Path | None = None
    for line in stdout.splitlines():
        m = _PIP_SAVED_WHEEL_RE.search(line)
        if m:
            wheel_path = Path(m.group(1).strip())
            break
    size_label = ""
    if wheel_path is None and cache_dir is not None:
        # uv 输出 ``Downloaded <name>.whl`` 仅文件名，拼接 cache_dir 取大小
        for line in (stdout + "\n" + stderr).splitlines():
            m = _UV_DOWNLOAD_WHEEL_RE.search(line)
            if m:
                wheel_path = cache_dir / Path(m.group(1).strip()).name
                break
    if wheel_path is not None and wheel_path.is_file():
        size_label = f" ({format_bytes_dec(wheel_path.stat().st_size)})"
    _logger.info("已下载 %s%s, 耗时 %.1fs", req, size_label, elapsed)


def _find_uv() -> str | None:
    """查找 ``uv`` 可执行文件，未找到返回 ``None``。

    用于在线依赖解析（``uv pip compile``），避免 pip 的 backtracking resolver
    在复杂依赖图上报 ``resolution-too-deep``。uv 用 PubGrub 算法，能高效解析。
    """
    return shutil.which("uv")


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


def _resolve_with_uv(  # noqa: PLR0913
    packages: Sequence[str],
    py_version: str,
    platform_tags: Sequence[str],
    pypi_index: str,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
    generate_hashes: bool = False,
) -> str:
    """用 ``uv pip compile`` 解析依赖图，返回带哈希的 requirements 文本.

    uv 用 PubGrub 算法（SAT solver 系），能解析 pip backtracking resolver
    无法处理的复杂依赖图（避免 ``resolution-too-deep``）。

    ``--python-version``/``--python-platform`` 让 uv 按目标环境解析；
    ``--no-header`` 去除注释头部。

    ``generate_hashes=True`` 时附加 ``--generate-hashes``，uv 输出形如::

        rich==13.7.0 \\
            --hash=sha256:xxx \\
            --hash=sha256:yyy

    供 ``pip download --require-hashes -r`` 校验。返回原始 stdout 文本，
    由调用方写入临时 requirements.txt。

    无 ``generate_hashes`` 时调用方仍可用正则提取 ``name==version`` 列表
    做并行下载（不校验哈希）。
    """
    uv = _find_uv()
    if uv is None:
        raise DependencyError("未找到 uv，无法执行在线依赖解析")
    major, minor = py_version.split(".")[:2]
    # uv 的 --python-platform 只有 windows/linux/mac 粗粒度
    py_platform = "windows" if any("win" in t for t in platform_tags) else "linux"
    cmd: list[str] = [
        uv,
        "pip",
        "compile",
        "--python-version",
        f"{major}.{minor}",
        "--python-platform",
        py_platform,
        "--no-header",
        "--index-url",
        pypi_index,
    ]
    if generate_hashes:
        cmd.append("--generate-hashes")
    # 私有包源：额外索引与 wheel 目录
    for url in extra_index_urls:
        cmd.extend(["--extra-index-url", url])
    for link in find_links:
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


def _run_pip_download(  # noqa: PLR0913
    filtered: list[str],
    base_args: list[str],
    py: str,
    py_version: str,
    platform_tags: Sequence[str],
    pypi_index: str,
    cache_dir: Path,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
    require_hashes: bool = False,
) -> subprocess.CompletedProcess[str]:
    """执行 pip download：先用 ``--no-index`` 离线解析，失败回退到在线解析下载.

    离线解析时除默认的 ``cache_dir`` 外，**同时搜索用户提供的 ``find_links``
    本地 wheel 目录**，扩大本地搜索范围。这使离线模式下用户可通过
    ``--find-links /path/to/local/wheels`` 指定额外的本地 wheel 仓库。

    离线模式（``FSPACK_OFFLINE=1``）下 ``--no-index`` 解析失败时立即抛
    :class:`DependencyError`，不回退到在线下载避免超时卡死。错误信息列出
    缺失的依赖名、本地缓存路径与已搜索的 find-links 路径，便于用户预下载
    wheel 放入缓存或新增 find-links 路径。

    ``require_hashes=True`` 时离线解析成功仍跳过哈希校验（缓存目录 wheel 已首次
    校验）；离线失败回退到在线时强制走 uv --generate-hashes 路径校验哈希。
    """
    # 惰性导入打破循环依赖：downloader 顶层导入本模块，本模块不能顶层导入 downloader
    from fspack.packaging.wheels.downloader import _run_pip

    # 构造用户提供的 find-links 参数：附加到 base_args 已有的 --find-links <cache_dir> 之后
    user_find_links_args: list[str] = []
    for link in find_links:
        user_find_links_args.extend(["--find-links", link])

    # 先用 --no-index 从本地缓存 + 用户 find-links 解析（离线模式），命中则跳过网络查询
    result = _run_pip(
        [*base_args, *user_find_links_args, "--no-index", *filtered],
        f"检查缓存 {len(filtered)} 个依赖",
        suppress_error=True,
    )
    if result is None:
        if is_offline():
            searched = [str(cache_dir), *find_links]
            raise DependencyError(
                f"离线模式下依赖缓存未命中: {', '.join(filtered)}，"
                f"已搜索路径: {'; '.join(searched)}。"
                f"请预先下载 wheel 放入上述路径之一，或通过 --find-links 指定本地 wheel 目录，"
                f"或取消 FSPACK_OFFLINE 环境变量"
            )
        _logger.info("缓存解析失败，回退到在线解析下载")
        return _download_online(
            filtered,
            base_args,
            py,
            py_version,
            platform_tags,
            pypi_index,
            cache_dir,
            extra_index_urls=extra_index_urls,
            find_links=find_links,
            require_hashes=require_hashes,
        )
    _logger.info("缓存解析成功，跳过网络查询")
    return result


def _download_online(  # noqa: PLR0913
    filtered: list[str],
    base_args: list[str],
    py: str,
    py_version: str,
    platform_tags: Sequence[str],
    pypi_index: str,
    cache_dir: Path,
    *,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
    require_hashes: bool = False,
) -> subprocess.CompletedProcess[str]:
    """在线解析并下载依赖 wheel。

    优先用 ``uv pip compile`` 解析依赖图（PubGrub 算法，避免 pip 的
    ``resolution-too-deep``），再用 ``uv pip download --no-deps``（uv 可用且
    支持该子命令时）或 ``pip download --no-deps`` 逐个下载已解析的精确版本
    wheel。uv 下载比 pip 快 2-5x（无 Python 解释器启动开销 + Rust HTTP 客户端）。

    uv 不可用、不支持 ``pip download`` 子命令（0.1.9+ 移除）或解析失败时回退
    到 ``pip download`` 完整解析+下载（stream=True），保留 sdist 回退
    （``pip wheel --no-deps`` 从 sdist 构建纯 Python wheel）。

    ``require_hashes=True``（iter-103）时强制走 uv 路径并启用 ``--generate-hashes``，
    生成带哈希的 requirements.txt 后用单次 ``pip download --require-hashes -r``
    下载（无法并行，因 ``--require-hashes`` 要求所有包同 requirements 文件）。
    uv 不可用时 warning 降级为不校验哈希（避免阻塞构建）。

    iter-132 优化：``_find_uv()`` 在本函数顶部调一次，共享给 require_hashes
    检查、uv 解析与 ``_download_resolved_parallel`` 的 uv 下载路径，避免重复
    ``shutil.which`` 调用。``_uv_supports_download`` 也调一次，结果传给并行
    下载阶段，避免逐包检测 ``uv pip download --help``。
    """
    # 惰性导入打破循环依赖：downloader 顶层导入本模块，本模块不能顶层导入 downloader
    from fspack.packaging.wheels.downloader import _run_pip

    # 共享 uv 路径检测：require_hashes 检查、uv 解析、uv 下载共用一次 _find_uv()
    uv_path = _find_uv()
    # 检测 uv 是否支持 pip download 子命令（0.1.9+ 移除），结果传给并行下载阶段
    uv_can_download = _uv_supports_download(uv_path)

    # 构造私有包源参数：透传给 pip download 与 pip wheel
    extra_args: list[str] = []
    for url in extra_index_urls:
        extra_args.extend(["--extra-index-url", url])
    for link in find_links:
        extra_args.extend(["--find-links", link])

    # require_hashes=True：强制走 uv --generate-hashes 路径
    if require_hashes:
        if uv_path is None:
            _logger.warning("require_hashes=True 但 uv 不可用，降级为不校验哈希")
        else:
            return _download_with_hashes(
                filtered,
                base_args,
                extra_args,
                pypi_index,
                py_version,
                platform_tags,
                cache_dir,
                extra_index_urls=extra_index_urls,
                find_links=find_links,
            )

    # 尝试用 uv 解析依赖图（不带哈希）
    resolved: list[str] | None = None
    if uv_path is not None:
        try:
            uv_output = _resolve_with_uv(
                filtered,
                py_version,
                platform_tags,
                pypi_index,
                extra_index_urls=extra_index_urls,
                find_links=find_links,
            )
            resolved = _extract_resolved_lines(uv_output)
        except (DependencyError, subprocess.CalledProcessError) as e:
            _logger.warning("uv 解析失败，回退到 pip 完整解析: %s", e)

    if resolved is not None:
        # uv 解析成功：用 ThreadPoolExecutor 并行下载
        # uv 可用且支持 pip download 时用 uv 下载（比 pip 快 2-5x），否则用 pip
        downloader = "uv pip download" if uv_can_download else "pip download"
        _logger.info(
            "并行下载 %d 个已解析依赖（最多 %d 并发，%s，镜像 %s）",
            len(resolved),
            _PARALLEL_DOWNLOAD_WORKERS,
            downloader,
            pypi_index,
        )
        return _download_resolved_parallel(
            resolved,
            base_args,
            extra_args,
            py,
            pypi_index,
            cache_dir,
            extra_index_urls=extra_index_urls,
            find_links=find_links,
            uv_path=uv_path if uv_can_download else None,
            py_version=py_version,
            platform_tags=platform_tags,
        )

    # uv 不可用或解析失败：回退到 pip 完整解析+下载
    try:
        result = _run_pip(
            [*base_args, "-i", pypi_index, *extra_args, *filtered],
            f"pip download {len(filtered)} 个依赖（镜像 {pypi_index}）",
            stream=True,
        )
        assert result is not None  # suppress_error=False，不会返回 None
        return result
    except DependencyError as e:
        # sdist 回退：解析无 wheel 的包，用 pip wheel 从 sdist 构建后重试
        _handle_sdist_fallback(e, py, pypi_index, cache_dir, extra_index_urls=extra_index_urls, find_links=find_links)
        result = _run_pip(
            [*base_args, "-i", pypi_index, *extra_args, *filtered],
            f"pip download 重试 {len(filtered)} 个依赖（镜像 {pypi_index}）",
            stream=True,
        )
        assert result is not None  # suppress_error=False，不会返回 None
        return result


def _download_with_hashes(  # noqa: PLR0913
    filtered: list[str],
    base_args: list[str],
    extra_args: list[str],
    pypi_index: str,
    py_version: str,
    platform_tags: Sequence[str],
    cache_dir: Path,
    *,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """``require_hashes=True`` 路径：uv 生成带哈希 requirements + pip download 校验.

    用 ``uv pip compile --generate-hashes`` 生成包含所有依赖哈希的 requirements.txt，
    写入临时文件后用 ``pip download --require-hashes -r <tmp>`` 一次性下载校验。
    无法并行（pip ``--require-hashes`` 要求所有包在同一 requirements 文件中）。

    临时 requirements.txt 在 ``cache_dir`` 下（避免 tempfile 目录权限问题），
    下载完成后删除。
    """
    import contextlib
    import tempfile

    from fspack.packaging.wheels.downloader import _run_pip

    # uv pip compile --generate-hashes 输出带哈希的 requirements 文本
    requirements_text = _resolve_with_uv(
        filtered,
        py_version,
        platform_tags,
        pypi_index,
        extra_index_urls=extra_index_urls,
        find_links=find_links,
        generate_hashes=True,
    )

    # 写入临时 requirements.txt（cache_dir 下，便于 pip --find-links <cache_dir> 复用）
    with tempfile.NamedTemporaryFile(
        mode="w", suffix="-requirements.txt", dir=str(cache_dir), delete=False, encoding="utf-8"
    ) as f:
        f.write(requirements_text)
        req_path = f.name
    try:
        cmd = [
            *base_args,
            *extra_args,
            "--require-hashes",
            "-r",
            req_path,
        ]
        _logger.info("pip download --require-hashes -r %s（%d 个依赖，镜像 %s）", req_path, len(filtered), pypi_index)
        result = _run_pip(
            cmd, f"pip download --require-hashes {len(filtered)} 个依赖（镜像 {pypi_index}）", stream=True
        )
        assert result is not None  # suppress_error=False
        return result
    finally:
        with contextlib.suppress(OSError):
            Path(req_path).unlink()


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


def _download_resolved_parallel(  # noqa: PLR0913
    resolved: list[str],
    base_args: list[str],
    extra_args: list[str],
    py: str,
    pypi_index: str,
    cache_dir: Path,
    *,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
    uv_path: str | None = None,
    py_version: str = "",
    platform_tags: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """并行下载 uv 解析出的精确版本 wheel.

    用 :class:`~concurrent.futures.ThreadPoolExecutor` 并发调用 ``uv pip
    download --no-deps``（uv 可用时）或 ``pip download --no-deps``
    （uv 不可用或单包 uv 下载失败回退），I/O 密集网络下载场景下
    相比串行 ``-r requirements.txt`` 显著提速。

    失败处理：单个包下载失败时收集其异常。若全部成功则合并 stdout 返回；
    若有失败则尝试 sdist 回退（解析首个失败的 stderr 提取缺失包名），
    构建后仅重试失败的包，最终合并所有 stdout 返回。

    Args:
        resolved: uv 解析出的精确版本需求列表（如 ``["numpy==1.24.0", ...]``）。
        base_args: pip download 基础参数（不含 ``-i index`` 与包名）。
        extra_args: 私有包源参数（``--extra-index-url``/``--find-links`` 展开）。
        py: pip 解释器路径。
        pypi_index: PyPI 索引 URL，sdist 回退时传给 pip wheel 与重试命令。
        cache_dir: wheel 缓存目录。
        extra_index_urls: 额外索引 URL（sdist 回退用）。
        find_links: 本地 wheel 目录（sdist 回退用）。
        uv_path: uv 可执行文件路径，非 None 时优先用 ``uv pip download`` 下载
            （比 pip 快 2-5x）。单包 uv 下载失败时自动回退到 pip download。
        py_version: 目标 Python 版本（如 ``3.11.9``），uv 下载时用于
            ``--python-version`` 跨版本解析。
        platform_tags: 目标平台标签列表（如 ``("win_amd64",)``），uv 下载时
            映射为 ``--python-platform windows|linux``。
    """

    def _download_worker(req: str, *, stream: bool = False) -> subprocess.CompletedProcess[str]:
        """单包下载 worker：优先 uv，失败回退 pip.

        ``with_index=True`` 始终附加 ``-i``/``--index-url <pypi_index>``：并行下载
        路径已在在线模式（``--no-index`` 离线解析失败后回退），需用用户配置的镜像源
        而非 pip/uv 默认的 pypi.org（国内访问慢/超时）。``--find-links <cache_dir>``
        仍优先检查本地缓存，命中时 pip/uv 不会访问网络。

        ``stream`` 仅透传给 pip 路径（uv 下载快且无进度条，无需流式）。
        """

        if uv_path is not None:
            try:
                return _download_one_with_uv(
                    uv_path,
                    req,
                    cache_dir,
                    extra_args,
                    py_version=py_version,
                    platform_tags=platform_tags,
                    pypi_index=pypi_index,
                    with_index=True,
                )
            except subprocess.CalledProcessError as uv_err:
                _logger.info("uv 下载 %s 失败，回退到 pip: %s", req, (uv_err.stderr or "").strip()[:200])
        return _download_one_resolved(req, base_args, extra_args, pypi_index, with_index=True, stream=stream)

    # 单包场景直接串行，避免线程池开销，但仍走 sdist 回退
    # stream=True 流式输出 pip 进度条，让用户看到实时下载速度
    if len(resolved) == 1:
        try:
            return _download_worker(resolved[0], stream=True)
        except subprocess.CalledProcessError as e:
            _logger.warning("单包下载失败，尝试 sdist 回退: %s", resolved[0])
            fallback_err = DependencyError(f"依赖下载失败:\n{e.stderr}")
            _handle_sdist_fallback(
                fallback_err, py, pypi_index, cache_dir, extra_index_urls=extra_index_urls, find_links=find_links
            )
            return _download_one_resolved(resolved[0], base_args, extra_args, pypi_index, with_index=True, stream=True)

    workers = min(_PARALLEL_DOWNLOAD_WORKERS, len(resolved))
    succeeded: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    failed: list[tuple[str, subprocess.CalledProcessError]] = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wheel-dl") as executor:
        future_to_req = {executor.submit(_download_worker, req): req for req in resolved}
        for future in as_completed(future_to_req):
            req = future_to_req[future]
            try:
                result = future.result()
                succeeded.append((req, result))
            except subprocess.CalledProcessError as e:
                failed.append((req, e))

    if not failed:
        return _merge_parallel_results(succeeded)

    # 有失败包：sdist 回退（合并所有失败包的 stderr 解析 missing 包名）
    # 注意：并行模式下每个包的 stderr 独立捕获，必须合并才能解析出所有 sdist-only 包
    # （如 win-unicode-console==0.5 无 wheel，--only-binary=:all: 失败）
    _logger.warning("并行下载 %d 个失败，尝试 sdist 回退: %s", len(failed), [r for r, _ in failed])
    combined_stderr = "\n".join(e.stderr or "" for _, e in failed)
    fallback_err = DependencyError(f"依赖下载失败:\n{combined_stderr}")
    _handle_sdist_fallback(
        fallback_err, py, pypi_index, cache_dir, extra_index_urls=extra_index_urls, find_links=find_links
    )
    # sdist 构建后重试失败的包（带 -i index，因 sdist 构建的 wheel 在本地缓存）
    retry_results: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    for req, _ in failed:
        result = _download_one_resolved(req, base_args, extra_args, pypi_index, with_index=True)
        retry_results.append((req, result))
    return _merge_parallel_results([*succeeded, *retry_results])


def _download_one_with_uv(  # noqa: PLR0913
    uv_path: str,
    req: str,
    cache_dir: Path,
    extra_args: list[str],
    *,
    py_version: str,
    platform_tags: Sequence[str],
    pypi_index: str,
    with_index: bool,
) -> subprocess.CompletedProcess[str]:
    """用 ``uv pip download --no-deps`` 下载单个已解析 wheel.

    uv 比 pip 快 2-5x：无 Python 解释器启动开销（~150ms/次）+ Rust HTTP
    客户端（reqwest 并发连接）。单包场景下 uv 启动 ~10ms vs pip ~150ms，
    50 包并行场景下总启动开销从 ~7.5s 降至 ~0.5s。

    uv 输出 ``Downloaded <name>.whl``/``Cached <name>.whl`` 格式，通过
    :func:`_convert_uv_output_to_pip_format` 转换为 ``Saved <name>.whl`` 格式，
    兼容下游 :func:`_parse_pip_download_wheels` 解析。

    Args:
        uv_path: uv 可执行文件路径.
        req: 精确版本需求字符串（如 ``numpy==1.24.0``）。
        cache_dir: wheel 缓存目录（uv ``-d`` 参数）。
        extra_args: 私有包源参数（``--extra-index-url``/``--find-links`` 展开）。
        py_version: 目标 Python 版本（如 ``3.11.9``），用于 ``--python-version``。
        platform_tags: 目标平台标签列表，映射为 ``--python-platform``。
        pypi_index: PyPI 索引 URL，``with_index=True`` 时附加 ``--index-url``。
        with_index: True 时附加 ``--index-url <pypi_index>``，使用用户配置的镜像源。
            并行下载路径（``_download_worker``）与 sdist 回退重试均传 ``True``：
            前者需用配置镜像而非 uv 默认 pypi.org（国内访问慢/超时），
            后者需从网络下载其他包。

    Raises:
        subprocess.CalledProcessError: uv 非零退出时抛出，由调用方捕获后回退 pip。
        FileNotFoundError: uv 消失时转为 :class:`DependencyError`。
    """
    major, minor = py_version.split(".")[:2] if py_version else ("", "")
    py_platform = "windows" if any("win" in t for t in platform_tags) else "linux"
    cmd: list[str] = [
        uv_path,
        "pip",
        "download",
        "--no-deps",
        "-d",
        str(cache_dir),
        "--find-links",
        str(cache_dir),
    ]
    if major and minor:
        cmd.extend(["--python-version", f"{major}.{minor}"])
    cmd.extend(["--python-platform", py_platform])
    if with_index:
        cmd.extend(["--index-url", pypi_index])
    cmd.extend(extra_args)
    cmd.append(req)
    _logger.info("uv 下载 %s（镜像 %s）", req, pypi_index if with_index else "默认")
    start = time.perf_counter()
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 uv: {uv_path}") from e
    # uv 输出转换为 pip 兼容的 "Saved <name>.whl" 格式
    pip_stdout = _convert_uv_output_to_pip_format(result.stdout + "\n" + result.stderr)
    # 用原始 uv 输出（含 "Downloaded X.whl" 行）让 _log_download_event 走 uv fallback
    # 路径：pip_stdout 转换后是 "Saved X.whl"（仅文件名），is_file() 会失败；
    # uv 原始输出 "Downloaded X.whl" 也是文件名，但拼接 cache_dir 后能定位文件取大小
    _log_download_event(req, result.stdout, result.stderr, time.perf_counter() - start, cache_dir)
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=pip_stdout, stderr=result.stderr)


def _download_one_resolved(  # noqa: PLR0913
    req: str,
    base_args: list[str],
    extra_args: list[str],
    pypi_index: str,
    *,
    with_index: bool,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """下载单个已解析 wheel（``pip download --no-deps <req>``）.

    ``stream=False``（默认，并行模式）用 ``subprocess.run`` 捕获 stdout/stderr，
    不流式输出（多进程 stderr 交错混乱）。``stream=True``（单包模式）用
    :func:`fspack.packaging.wheels.downloader._stream_subprocess` 实时流式输出
    pip 进度条到终端，让用户看到实时下载速度（避免 10MB+ wheel 静默下载被
    误判为卡住）。

    下载开始/完成通过 :func:`_log_download_event` 打印事件日志（含 req、文件
    大小、耗时），``stream=True`` 时进度条与事件日志配合提供完整下载反馈。

    Args:
        req: 精确版本需求字符串（如 ``numpy==1.24.0``）。
        base_args: pip download 基础参数（不含 ``-i index`` 与包名）。
        extra_args: 私有包源参数（``--extra-index-url``/``--find-links`` 展开）。
        pypi_index: PyPI 索引 URL，``with_index=True`` 时附加 ``-i <pypi_index>``。
        with_index: True 时附加 ``-i <pypi_index>``，使用用户配置的镜像源。
            并行下载路径（``_download_worker``）与 sdist 回退重试均传 ``True``：
            前者需用配置镜像而非 pip 默认 pypi.org（国内访问慢/超时），
            后者需从网络下载其他包。``--find-links <cache_dir>`` 仍优先检查本地缓存。
        stream: True 时流式输出 pip 进度条到终端（单包场景），False 时静默
            捕获（并行场景避免多进程 stderr 交错）。
    """
    if with_index:
        cmd = [*base_args, "--no-deps", "-i", pypi_index, *extra_args, req]
    else:
        cmd = [*base_args, "--no-deps", *extra_args, req]
    _logger.info("pip 下载 %s（镜像 %s）", req, pypi_index if with_index else "默认")
    start = time.perf_counter()
    try:
        if stream:
            # 单包场景流式输出 pip 进度条，让用户看到实时下载速度。
            # 通过模块属性访问 _stream_subprocess，便于测试 monkeypatch。
            from fspack.packaging.wheels import downloader as _dl

            result = _dl._stream_subprocess(cmd)
        else:
            result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 pip: {cmd[0]}") from e
    except subprocess.CalledProcessError:
        # 重新抛出原异常，保留 stderr 供 sdist 回退解析
        raise
    _log_download_event(req, result.stdout, result.stderr, time.perf_counter() - start)
    return result


def _merge_parallel_results(
    results: Iterable[tuple[str, subprocess.CompletedProcess[str]]],
) -> subprocess.CompletedProcess[str]:
    """合并并行下载结果：拼接 stdout 供 :func:`_parse_pip_download_wheels` 解析.

    stderr 不合并（并行时各进程 stderr 独立，合并无意义），返回空字符串。
    """
    stdout_parts = [r.stdout for _, r in results if r.stdout]
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="\n".join(stdout_parts), stderr="")
