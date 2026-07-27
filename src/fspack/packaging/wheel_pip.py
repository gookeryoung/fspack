"""Wheel 下载与依赖解析核心：pip/uv 调用、sdist 回退、流式输出。

从 :mod:`fspack.packaging.wheels` 拆分而来，封装所有与 pip/uv 子进程交互的
逻辑。依赖 :mod:`fspack.packaging.wheel_markers` 做 ``python_version`` 标记
预过滤，依赖 :mod:`fspack.packaging.wheel_cache` 做依赖解析缓存。

核心流程：

1. ``download_wheels`` 入口：预过滤标记 → 查缓存 → 调 pip download → 解析结果
2. ``_run_pip_download``：先用 ``--no-index`` 离线解析，失败回退到 ``_download_online``
3. ``_download_online``：优先用 ``uv pip compile`` 解析依赖图（PubGrub 算法），
   再用 ``pip download --no-deps`` 逐个下载；uv 不可用回退到 ``pip download`` 完整解析
4. sdist 回退：``--only-binary=:all:`` 无法下载无 wheel 的包时，用 ``pip wheel --no-deps``
   从 sdist 构建纯 Python wheel 后重试
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

from fspack.exceptions import DependencyError
from fspack.packaging.wheel_cache import _deps_cache_key, _load_deps_cache, _save_deps_cache
from fspack.packaging.wheel_markers import _filter_by_python_version
from fspack.progress import StageRecorder, spinner

__all__ = ["download_wheels"]

_logger = logging.getLogger(__name__)

# 并行下载线程数上限：I/O 密集网络下载，8 个并发平衡 PyPI 限流与吞吐量
# 单个 wheel 下载耗时差异大（几 KB 元数据 vs 数百 MB 二进制），线程池自动调度
_PARALLEL_DOWNLOAD_WORKERS = 8

# Windows 系统标准命名为 python.exe；Microsoft Store 版本另提供 python3.exe stub。
# Linux/macOS 用 python3，回退 python。
_PIP_PYTHON_NAMES: tuple[str, ...] = ("python.exe", "python3.exe") if sys.platform == "win32" else ("python3", "python")

# uv pip compile 输出中匹配 ``name==version`` 的行（忽略注释/空行）
_UV_RESOLVED_LINE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+*-]+)")

# 匹配 pip download stderr 中的 "Could not find a version that satisfies the requirement <pkg>"
_MISSING_PKG_RE = re.compile(r"Could not find a version that satisfies the requirement (.+?) \(from versions:")

# 匹配 pip download stdout 中的 "Saved <path>.whl" 和 "File was already downloaded <path>.whl"
_PIP_WHEEL_LINE_RE = re.compile(r"(?:Saved|File was already downloaded)\s+(.+\.whl)", re.IGNORECASE)


def download_wheels(  # noqa: PLR0913
    packages: tuple[str, ...] | list[str],
    py_version: str,
    pypi_index: str,
    cache_dir: Path,
    platform_tags: Sequence[str] = ("win_amd64",),
    *,
    stage: StageRecorder | None = None,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
) -> list[Path]:
    """用 dev python 的 pip 下载指定平台 wheel 到 cache_dir，返回本次依赖的 wheel 路径列表。

    优先用 ``--no-index --find-links cache_dir`` 从本地缓存解析依赖，命中则完全跳过
    网络查询；缓存不完整或条件依赖未满足（如 pypdf 的 ``typing_extensions`` marker）
    时回退到带 ``-i index`` 的完整下载。

    预过滤 ``python_version`` 环境标记：``pip download --python-version`` 不评估
    命令行参数中的 marker，需在调用前按目标 Python 版本过滤（如目标 3.8 时跳过
    ``PySide6>=6.5.0; python_version >= '3.11'``）。

    sdist 回退：``--only-binary=:all:`` 无法下载无 wheel 的包（如 odfpy 仅有 sdist），
    回退到 ``pip wheel --no-deps`` 从 sdist 构建纯 Python wheel 后重试。

    ``platform_tags`` 为 pip ``--platform`` 标签列表，可重复指定以匹配多个
    平台标签（如 Linux 同时匹配 manylinux2014 与 manylinux_2_28）。

    ``cache_dir`` 为 fspack wheel 缓存目录（``~/.fspack/cache/wheels/``），持久化
    保存已下载的 wheel。pip 自动跳过已存在的 wheel（"File was already downloaded"），
    仅下载缺失项（"Saved"）。解析 stdout 获取本次所有 wheel 路径（含传递依赖），
    供 unpack_wheels 解压。

    自动选择能跑 pip 的 python 解释器：优先当前 venv，回退系统 python3
    （uv venv 默认不含 pip）。

    ``stage`` 用于回写缓存命中数、下载字节数与 wheel 数到 BuildTracker。

    ``extra_index_urls`` 为额外 PyPI 索引 URL 列表（私有 PyPI 服务器），
    透传给 pip/uv 的 ``--extra-index-url`` 参数。
    ``find_links`` 为本地/远程 wheel 目录列表，透传给 pip/uv 的 ``--find-links`` 参数。
    二者用于支持私有包下载，PyPI 仍由 ``pypi_index`` 指定。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    filtered = _prefilter_by_python_version(packages, py_version)
    if not filtered:
        return []

    # 尝试读取依赖解析缓存，命中则跳过 pip 调用
    deps_key = _deps_cache_key(filtered, py_version, platform_tags, extra_index_urls, find_links)
    cached_wheels = _load_deps_cache(cache_dir, deps_key)
    if cached_wheels is not None:
        _logger.info("依赖解析缓存命中，跳过 pip 调用")
        if stage is not None:
            stage.hit_cache(len(cached_wheels))
            stage.processed(len(cached_wheels))
            stage.set_detail(f"{len(cached_wheels)} wheels, 解析缓存命中")
        return cached_wheels

    py = _find_pip_python()
    base_args = _build_pip_download_args(py, py_version, platform_tags, cache_dir)

    _logger.info("下载依赖 wheel: %s", " ".join(filtered))
    before = {f.name for f in cache_dir.glob("*.whl")}

    result = _run_pip_download(
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

    wheel_names, used_fallback = _parse_wheel_names(result.stdout, cache_dir)
    wheels = [cache_dir / name for name in wheel_names if (cache_dir / name).is_file()]
    if wheels and not used_fallback:
        _save_deps_cache(cache_dir, deps_key, wheels)
    if stage is not None:
        _record_wheel_stage(stage, wheels, before)
    return wheels


def _prefilter_by_python_version(packages: tuple[str, ...] | list[str], py_version: str) -> list[str]:
    """按目标 Python 版本过滤 ``python_version`` 环境标记，返回保留的包列表."""
    filtered = _filter_by_python_version(packages, py_version)
    if len(filtered) < len(packages):
        _logger.info("按 python_version 标记过滤: 保留 %d，跳过 %d 个", len(filtered), len(packages) - len(filtered))
    if not filtered:
        _logger.info("所有依赖被 python_version 标记过滤，跳过下载")
    return filtered


def _build_pip_download_args(
    py: str,
    py_version: str,
    platform_tags: Sequence[str],
    cache_dir: Path,
) -> list[str]:
    """构造 ``pip download`` 基础参数（不含 ``-i index`` 与包名）."""
    major, minor = py_version.split(".")[:2]
    platform_args: list[str] = []
    for tag in platform_tags:
        platform_args.extend(["--platform", tag])
    return [
        py,
        "-m",
        "pip",
        "download",
        "-d",
        str(cache_dir),
        "--find-links",
        str(cache_dir),
        *platform_args,
        "--python-version",
        f"{major}.{minor}",
        "--abi",
        f"cp{major}{minor}",
        "--implementation",
        "cp",
        "--only-binary=:all:",
    ]


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
    """执行 pip download：先用 ``--no-index`` 离线解析，失败回退到在线解析下载."""
    # 先用 --no-index 从本地缓存解析（离线模式），命中则跳过网络查询；
    # 缓存不完整或条件依赖未满足时回退到在线解析+下载
    result = _run_pip([*base_args, "--no-index", *filtered], f"检查缓存 {len(filtered)} 个依赖", suppress_error=True)
    if result is None:
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


def _parse_wheel_names(stdout: str, cache_dir: Path) -> tuple[list[str], bool]:
    """解析 pip download stdout 获取 wheel 文件名列表.

    Returns:
        (wheel 文件名列表, 是否回退到目录扫描). 回退扫描时不可作为 deps_key 缓存，
        否则下次命中缓存会返回错误依赖列表（如 requests 命中却返回 pygame wheel）。

    """
    wheel_names = _parse_pip_download_wheels(stdout)
    if wheel_names:
        return wheel_names, False
    _logger.warning("pip download 输出解析失败，回退到目录扫描")
    # 目录扫描可能包含其他项目遗留的 wheel，不可作为本 deps_key 的缓存
    return sorted(f.name for f in cache_dir.glob("*.whl")), True


def _record_wheel_stage(stage: StageRecorder, wheels: list[Path], before: set[str]) -> None:
    """回写 wheel 下载阶段统计到 stage：新增字节数、缓存命中数、总数."""
    new_wheels = [w for w in wheels if w.name not in before]
    existing_wheels = [w for w in wheels if w.name in before]
    if new_wheels:
        stage.add_bytes(sum(w.stat().st_size for w in new_wheels))
    if existing_wheels:
        stage.hit_cache(len(existing_wheels))
    stage.processed(len(wheels))
    cache_status = "缓存命中" if not new_wheels else f"新增 {len(new_wheels)}"
    stage.set_detail(f"{len(wheels)} wheels, {cache_status}")


def _find_pip_python() -> str:
    """找一个能跑 ``python -m pip`` 的解释器。

    优先当前 venv（``sys.executable``），无 pip 时遍历 ``PATH`` 找系统 python
    （跳过 venv 所在目录，因为 ``shutil.which`` 在 venv 激活时只返回 venv python）。
    候选名按平台：Windows 为 ``python.exe``/``python3.exe``，其他为 ``python3``/``python``。
    ``pip download`` 的 ``--python-version``/``--abi``/``--implementation`` 参数
    支持跨版本下载，跑 pip 的 python 版本无需匹配目标版本。

    uv 管理的 venv 默认不含 pip（用 Rust 实现的 ``uv pip``），需回退系统 python。
    """
    candidates: list[str] = [sys.executable]
    venv_bin = Path(sys.executable).parent.resolve()
    seen: set[str] = {sys.executable}
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not path_dir:
            continue
        try:
            resolved_dir = Path(path_dir).resolve()
        except OSError:
            continue
        if resolved_dir == venv_bin:
            continue
        for name in _PIP_PYTHON_NAMES:
            candidate = resolved_dir / name
            if candidate.is_file() and str(candidate) not in seen:
                candidates.append(str(candidate))
                seen.add(str(candidate))
    for py in candidates:
        try:
            subprocess.run(
                [py, "-m", "pip", "--version"], check=True, capture_output=True, encoding="utf-8", errors="replace"
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        return py
    raise DependencyError("未找到可用的 pip，请在当前 venv 执行 `uv pip install pip`，或在系统安装 python3-pip 包")


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

    # 有失败包：sdist 回退（用首个失败的 stderr 解析 missing 包名）
    _logger.warning("并行下载 %d 个失败，尝试 sdist 回退: %s", len(failed), [r for r, _ in failed])
    first_err = failed[0][1]
    fallback_err = DependencyError(f"依赖下载失败:\n{first_err.stderr}")
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


def _parse_missing_packages(stderr: str) -> list[str]:
    """从 pip download stderr 解析找不到 wheel 的依赖列表。

    匹配 ``Could not find a version that satisfies the requirement <pkg>``，
    返回去重的依赖字符串列表（含版本 specifier，供 pip wheel 使用）。
    """
    seen: set[str] = set()
    result: list[str] = []
    for m in _MISSING_PKG_RE.finditer(stderr):
        req = m.group(1).strip()
        if req and req not in seen:
            seen.add(req)
            result.append(req)
    return result


def _handle_sdist_fallback(  # noqa: PLR0913
    e: DependencyError,
    py: str,
    pypi_index: str,
    cache_dir: Path,
    *,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
) -> list[str]:
    """处理 sdist 回退：解析缺失包并构建无 wheel 的包，返回缺失包列表。

    无缺失包时重新抛出原异常（无法用 sdist 回退解决）。调用方据此重试下载。

    ``extra_index_urls``/``find_links`` 透传给 ``pip wheel``，确保私有 PyPI 服务器
    或本地 wheel 目录中的 sdist 也能被构建为 wheel。
    """
    missing = _parse_missing_packages(str(e))
    if not missing:
        raise e from None
    _logger.info("尝试用 pip wheel 构建无 wheel 的包: %s", ", ".join(missing))
    _build_sdist_wheels(missing, py, pypi_index, cache_dir, extra_index_urls, find_links)
    return missing


def _build_sdist_wheels(  # noqa: PLR0913
    packages: list[str],
    py: str,
    pypi_index: str,
    cache_dir: Path,
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
) -> None:
    """用 ``pip wheel --no-deps`` 从 sdist 构建 wheel（纯 Python 包无 wheel 时回退）。

    ``pip download --only-binary=:all:`` 无法下载无 wheel 的包（如 odfpy 仅有 sdist）。
    ``pip wheel --no-deps`` 可从 sdist 构建纯 Python wheel（``py3-none-any``），
    构建产物放入 cache_dir 供后续 ``pip download --find-links`` 使用。

    构建失败仅 warning（可能是 C 扩展包无法在当前环境编译），
    不影响后续重试——重试失败时抛出原始下载错误。

    ``extra_index_urls``/``find_links`` 透传给 ``pip wheel``，确保私有包源中的
    sdist 也能被构建。
    """
    extra_args: list[str] = []
    for url in extra_index_urls:
        extra_args.extend(["--extra-index-url", url])
    for link in find_links:
        extra_args.extend(["--find-links", link])
    for pkg in packages:
        cmd = [py, "-m", "pip", "wheel", "--no-deps", "-w", str(cache_dir), "-i", pypi_index, *extra_args, pkg]
        try:
            _stream_subprocess(cmd)
        except subprocess.CalledProcessError as e:
            _logger.warning("pip wheel 构建失败 %s: %s", pkg, (e.stderr or "").strip())
        except FileNotFoundError as e:
            raise DependencyError(f"未找到 pip: {cmd[0]}") from e


def _stream_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """运行命令，实时流式输出 stderr 到终端，捕获 stdout 和 stderr。

    用 ``Popen`` + 守护线程通过 ``os.read`` 读取 stderr 文件描述符字节块并实时
    写入 ``sys.stderr``，支持 pip 进度条的 ``\\r`` 回车更新。stdout 始终捕获
    用于解析 wheel 列表。stderr 同时累积，供失败时构造 ``CalledProcessError``。

    使用 ``os.read`` 而非 ``BufferedReader.read1``：前者直接读 fd，不依赖
    ``Popen`` 的缓冲层（``bufsize=0`` 时 stderr 是 ``FileIO`` 无 ``read1`` 方法）。

    调用方应在调用前停止 spinner（避免 ``\\r`` 与 pip 进度条冲突），并在调用后
    恢复 spinner 或继续后续日志输出。
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        assert process.stderr is not None
        fd = process.stderr.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    thread = threading.Thread(target=_drain_stderr, daemon=True)
    thread.start()
    stdout_bytes = process.stdout.read() if process.stdout else b""
    returncode = process.wait()
    thread.join()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, stdout, stderr)
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def _run_pip(
    cmd: list[str],
    label: str,
    *,
    suppress_error: bool = False,
    stream: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    """运行 pip download 命令，返回执行结果。

    ``suppress_error=True`` 时 ``CalledProcessError`` 返回 None（用于 ``--no-index``
    回退路径，调用方据 None 回退到带 index 命令）；``suppress_error=False`` 时转为
    ``DependencyError`` 抛出（含 stderr）。``FileNotFoundError`` 总是转为
    ``DependencyError``（pip 消失）。

    ``stream=True`` 时停止 spinner，用 ``_stream_subprocess`` 实时流式输出 pip 的
    stderr 到终端（显示下载进度条），stdout 仍捕获用于解析 wheel 列表。适用于
    耗时的网络下载和 sdist 构建场景；快速本地缓存检查保持 ``stream=False`` 用 spinner。
    """
    try:
        if stream:
            return _stream_subprocess(cmd)
        with spinner(label):
            return subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 pip: {cmd[0]}") from e
    except subprocess.CalledProcessError as e:
        if suppress_error:
            _logger.info("pip 命令失败（将回退）: %s", (e.stderr or "").strip())
            return None
        raise DependencyError(f"依赖下载失败:\n{e.stderr}") from e


def _parse_pip_download_wheels(stdout: str) -> list[str]:
    """解析 pip download stdout，提取本次涉及的 wheel 文件名（含传递依赖）。

    匹配 ``Saved <path>.whl``（新下载）和 ``File was already downloaded <path>.whl``（已存在跳过）。
    返回 wheel 文件名列表（去重保序）。
    """
    names: list[str] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        m = _PIP_WHEEL_LINE_RE.search(line)
        if m:
            name = Path(m.group(1).strip()).name
            if name not in seen:
                names.append(name)
                seen.add(name)
    return names
