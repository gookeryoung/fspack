"""Wheel 依赖解析与在线下载：uv pip compile 解析 + pip download 并行下载.

从 :mod:`fspack.packaging.wheel_pip` 拆分而来，封装在线依赖解析与下载逻辑。

核心流程：

1. ``_run_pip_download``：先用 ``--no-index`` 离线解析，失败回退到 ``_download_online``
2. ``_download_online``：优先用 ``uv pip compile`` 解析依赖图（PubGrub 算法），
   再用 ``pip download --no-deps`` 逐个下载；uv 不可用回退到 ``pip download`` 完整解析
3. ``_download_resolved_parallel``：用 ``ThreadPoolExecutor`` 并行下载 uv 解析出的
   精确版本 wheel，失败时通过 sdist 回退重试

依赖 :mod:`fspack.packaging.wheel_sdist` 提供 ``_handle_sdist_fallback``（顶层导入）。
依赖 :mod:`fspack.packaging.wheel_pip` 提供 ``_run_pip``（惰性导入避免循环依赖：
``wheel_pip`` 顶层导入本模块，本模块不能顶层导入 ``wheel_pip``）。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

from fspack.config import is_offline
from fspack.exceptions import DependencyError
from fspack.packaging.wheel_sdist import _handle_sdist_fallback

__all__ = [
    "_UV_RESOLVED_LINE_RE",
    "_download_one_resolved",
    "_download_online",
    "_download_resolved_parallel",
    "_find_uv",
    "_merge_parallel_results",
    "_resolve_with_uv",
    "_run_pip_download",
]

_logger = logging.getLogger(__name__)

# 并行下载线程数上限：I/O 密集网络下载，8 个并发平衡 PyPI 限流与吞吐量
# 单个 wheel 下载耗时差异大（几 KB 元数据 vs 数百 MB 二进制），线程池自动调度
_PARALLEL_DOWNLOAD_WORKERS = 8

# uv pip compile 输出中匹配 ``name==version`` 的行（忽略注释/空行）
_UV_RESOLVED_LINE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+*-]+)")


def _find_uv() -> str | None:
    """查找 ``uv`` 可执行文件，未找到返回 ``None``。

    用于在线依赖解析（``uv pip compile``），避免 pip 的 backtracking resolver
    在复杂依赖图上报 ``resolution-too-deep``。uv 用 PubGrub 算法，能高效解析。
    """
    return shutil.which("uv")


def _resolve_with_uv(  # noqa: PLR0913
    packages: Sequence[str],
    py_version: str,
    platform_tags: Sequence[str],
    pypi_index: str,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
) -> list[str]:
    """用 ``uv pip compile`` 解析依赖图，返回精确版本需求列表。

    uv 用 PubGrub 算法（SAT solver 系），能解析 pip backtracking resolver
    无法处理的复杂依赖图（避免 ``resolution-too-deep``）。解析结果为
    ``name==version`` 列表，供 ``pip download --no-deps`` 逐个下载。

    ``--python-version``/``--python-platform`` 让 uv 按目标环境解析；
    ``--no-header`` 去除注释头部，便于解析。输出经 stdout 捕获后逐行提取
    ``name==version`` 对。
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
    # 私有包源：额外索引与 wheel 目录
    for url in extra_index_urls:
        cmd.extend(["--extra-index-url", url])
    for link in find_links:
        cmd.extend(["--find-links", link])
    cmd.append("-")
    # uv pip compile 从 stdin 读取需求列表
    stdin_data = "\n".join(packages) + "\n"
    _logger.info("uv pip compile 解析依赖图: %s", " ".join(packages))
    result = subprocess.run(cmd, input=stdin_data, check=True, capture_output=True, encoding="utf-8", errors="replace")
    resolved: list[str] = []
    for line in result.stdout.splitlines():
        m = _UV_RESOLVED_LINE_RE.match(line.strip())
        if m:
            resolved.append(f"{m.group(1)}=={m.group(2)}")
    if not resolved:
        raise DependencyError(f"uv pip compile 未解析出任何依赖:\n{result.stderr}")
    _logger.info("uv 解析出 %d 个依赖（含传递依赖）", len(resolved))
    return resolved


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
) -> subprocess.CompletedProcess[str]:
    """执行 pip download：先用 ``--no-index`` 离线解析，失败回退到在线解析下载.

    离线解析时除默认的 ``cache_dir`` 外，**同时搜索用户提供的 ``find_links``
    本地 wheel 目录**，扩大本地搜索范围。这使离线模式下用户可通过
    ``--find-links /path/to/local/wheels`` 指定额外的本地 wheel 仓库。

    离线模式（``FSPACK_OFFLINE=1``）下 ``--no-index`` 解析失败时立即抛
    :class:`DependencyError`，不回退到在线下载避免超时卡死。错误信息列出
    缺失的依赖名、本地缓存路径与已搜索的 find-links 路径，便于用户预下载
    wheel 放入缓存或新增 find-links 路径。
    """
    # 惰性导入打破循环依赖：wheel_pip 顶层导入本模块，本模块不能顶层导入 wheel_pip
    from fspack.packaging.wheel_pip import _run_pip

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
) -> subprocess.CompletedProcess[str]:
    """在线解析并下载依赖 wheel。

    优先用 ``uv pip compile`` 解析依赖图（PubGrub 算法，避免 pip 的
    ``resolution-too-deep``），再用 ``pip download --no-deps`` 逐个下载已解析
    的精确版本 wheel（不触发 pip 的 resolver）。``--progress-bar on`` 强制
    pip 输出进度条到 stderr（即使被管道捕获），通过 ``_stream_subprocess``
    实时流式输出到终端。

    uv 不可用或解析失败时回退到 ``pip download`` 完整解析+下载（stream=True），
    保留 sdist 回退（``pip wheel --no-deps`` 从 sdist 构建纯 Python wheel）。
    """
    # 惰性导入打破循环依赖：wheel_pip 顶层导入本模块，本模块不能顶层导入 wheel_pip
    from fspack.packaging.wheel_pip import _run_pip

    # 构造私有包源参数：透传给 pip download 与 pip wheel
    extra_args: list[str] = []
    for url in extra_index_urls:
        extra_args.extend(["--extra-index-url", url])
    for link in find_links:
        extra_args.extend(["--find-links", link])

    # 尝试用 uv 解析依赖图
    resolved: list[str] | None = None
    if _find_uv() is not None:
        try:
            resolved = _resolve_with_uv(
                filtered,
                py_version,
                platform_tags,
                pypi_index,
                extra_index_urls=extra_index_urls,
                find_links=find_links,
            )
        except (DependencyError, subprocess.CalledProcessError) as e:
            _logger.warning("uv 解析失败，回退到 pip 完整解析: %s", e)

    if resolved is not None:
        # uv 解析成功：用 ThreadPoolExecutor 并行 pip download --no-deps 下载
        # I/O 密集网络下载，并行可显著提速（尤其多个独立 wheel，无需等待串行队列）
        _logger.info("并行下载 %d 个已解析依赖（最多 %d 并发）", len(resolved), _PARALLEL_DOWNLOAD_WORKERS)
        return _download_resolved_parallel(
            resolved,
            base_args,
            extra_args,
            py,
            pypi_index,
            cache_dir,
            extra_index_urls=extra_index_urls,
            find_links=find_links,
        )

    # uv 不可用或解析失败：回退到 pip 完整解析+下载
    try:
        result = _run_pip(
            [*base_args, "-i", pypi_index, *extra_args, *filtered],
            f"pip download {len(filtered)} 个依赖",
            stream=True,
        )
        assert result is not None  # suppress_error=False，不会返回 None
        return result
    except DependencyError as e:
        # sdist 回退：解析无 wheel 的包，用 pip wheel 从 sdist 构建后重试
        _handle_sdist_fallback(e, py, pypi_index, cache_dir, extra_index_urls=extra_index_urls, find_links=find_links)
        result = _run_pip(
            [*base_args, "-i", pypi_index, *extra_args, *filtered],
            f"pip download 重试 {len(filtered)} 个依赖",
            stream=True,
        )
        assert result is not None  # suppress_error=False，不会返回 None
        return result


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
) -> subprocess.CompletedProcess[str]:
    """并行下载 uv 解析出的精确版本 wheel.

    用 :class:`~concurrent.futures.ThreadPoolExecutor` 并发调用
    ``pip download --no-deps <pkg>==<ver>``，I/O 密集网络下载场景下
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
    """
    # 单包场景直接串行，避免线程池开销，但仍走 sdist 回退
    if len(resolved) == 1:
        try:
            return _download_one_resolved(resolved[0], base_args, extra_args, pypi_index, with_index=False)
        except subprocess.CalledProcessError as e:
            _logger.warning("单包下载失败，尝试 sdist 回退: %s", resolved[0])
            fallback_err = DependencyError(f"依赖下载失败:\n{e.stderr}")
            _handle_sdist_fallback(
                fallback_err, py, pypi_index, cache_dir, extra_index_urls=extra_index_urls, find_links=find_links
            )
            return _download_one_resolved(resolved[0], base_args, extra_args, pypi_index, with_index=True)

    workers = min(_PARALLEL_DOWNLOAD_WORKERS, len(resolved))
    succeeded: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    failed: list[tuple[str, subprocess.CalledProcessError]] = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wheel-dl") as executor:
        future_to_req = {
            executor.submit(_download_one_resolved, req, base_args, extra_args, pypi_index, with_index=False): req
            for req in resolved
        }
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


def _download_one_resolved(
    req: str,
    base_args: list[str],
    extra_args: list[str],
    pypi_index: str,
    *,
    with_index: bool,
) -> subprocess.CompletedProcess[str]:
    """下载单个已解析 wheel（``pip download --no-deps <req>``）.

    用 ``subprocess.run`` 捕获 stdout/stderr，不流式输出（并行模式多进程
    stderr 交错混乱，单包模式量小无需进度条）。

    Args:
        req: 精确版本需求字符串（如 ``numpy==1.24.0``）。
        base_args: pip download 基础参数（不含 ``-i index`` 与包名）。
        extra_args: 私有包源参数（``--extra-index-url``/``--find-links`` 展开）。
        pypi_index: PyPI 索引 URL，``with_index=True`` 时附加 ``-i <pypi_index>``。
        with_index: True 时附加 ``-i``（sdist 回退重试场景，需从网络下载其他包）。
    """
    if with_index:
        cmd = [*base_args, "--no-deps", "-i", pypi_index, *extra_args, req]
    else:
        cmd = [*base_args, "--no-deps", *extra_args, req]
    try:
        return subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 pip: {cmd[0]}") from e
    except subprocess.CalledProcessError:
        # 重新抛出原异常，保留 stderr 供 sdist 回退解析
        raise


def _merge_parallel_results(
    results: Iterable[tuple[str, subprocess.CompletedProcess[str]]],
) -> subprocess.CompletedProcess[str]:
    """合并并行下载结果：拼接 stdout 供 :func:`_parse_pip_download_wheels` 解析.

    stderr 不合并（并行时各进程 stderr 独立，合并无意义），返回空字符串。
    """
    stdout_parts = [r.stdout for _, r in results if r.stdout]
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="\n".join(stdout_parts), stderr="")
