"""单包 wheel 下载原语（pip/uv 双实现）与 ThreadPoolExecutor 并行编排.

从 :mod:`fspack.packaging.wheels.resolver` 拆分而来。单包下载原语由并行
编排调度：``ctx.uv_path`` 非 None 时优先 ``uv pip download --no-deps``
（快 2-5x，无 Python 解释器启动开销 + Rust HTTP 客户端），单包 uv 下载
失败自动回退 ``pip download --no-deps``。

失败处理：并行下载失败的包（pip/uv 非零退出或 pip 消失转的
:class:`~fspack.exceptions.DependencyError`）合并 stderr 后走 sdist 回退
（:func:`fspack.packaging.wheels.sdist._handle_sdist_fallback`），构建
成功后仅重试失败的包，最终合并所有 stdout 返回。

依赖方向：:mod:`fspack.packaging.wheels.uv_bridge`（输出格式转换/平台映射）
与 :mod:`fspack.packaging.wheels.sdist`（回退构建）为顶层导入；
:mod:`fspack.packaging.wheels.downloader` 的流式输出与下载监控
（``_stream_subprocess``/``_DownloadMonitor``）为惰性导入（downloader
顶层导入 resolver、resolver 顶层导入本模块，顶层导入会成环）。

共享参数经 :class:`~fspack.packaging.wheels.resolver.DownloadContext`
传递（uv 路径、索引、缓存目录、私有包源等），函数签名不超过 5 个参数。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.config.versions import _split_t_suffix
from fspack.exceptions import DependencyError
from fspack.packaging.wheels.sdist import _handle_sdist_fallback
from fspack.packaging.wheels.uv_bridge import (
    _UV_DOWNLOAD_WHEEL_RE,
    _convert_uv_output_to_pip_format,
    _uv_python_platform,
)

if TYPE_CHECKING:
    # DownloadContext 仅用于类型注解（from __future__ import annotations 使注解
    # 不在运行时求值）。顶层导入 resolver 会形成循环：resolver 顶层导入本模块。
    from fspack.packaging.wheels.resolver import DownloadContext

__all__ = [
    "_download_one_resolved",
    "_download_one_with_uv",
    "_download_resolved_parallel",
    "_log_download_event",
    "_merge_parallel_results",
]

_logger = logging.getLogger(__name__)

# 并行下载线程数上限：I/O 密集网络下载，8 个并发平衡 PyPI 限流与吞吐量
# 单个 wheel 下载耗时差异大（几 KB 元数据 vs 数百 MB 二进制），线程池自动调度
_PARALLEL_DOWNLOAD_WORKERS = 8

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


def _download_one_with_uv(
    req: str, ctx: DownloadContext, *, with_index: bool = True
) -> subprocess.CompletedProcess[str]:
    """用 ``uv pip download --no-deps`` 下载单个已解析 wheel.

    uv 比 pip 快 2-5x：无 Python 解释器启动开销（~150ms/次）+ Rust HTTP
    客户端（reqwest 并发连接）。单包场景下 uv 启动 ~10ms vs pip ~150ms，
    50 包并行场景下总启动开销从 ~7.5s 降至 ~0.5s。

    uv 输出 ``Downloaded <name>.whl``/``Cached <name>.whl`` 格式，通过
    :func:`fspack.packaging.wheels.uv_bridge._convert_uv_output_to_pip_format`
    转换为 ``Saved <name>.whl`` 格式，兼容下游 :func:`_parse_pip_download_wheels`
    解析。

    Args:
        req: 精确版本需求字符串（如 ``numpy==1.24.0``）。
        ctx: 下载上下文。``uv_path`` 为 uv 可执行路径（None 时抛
            :class:`DependencyError`，正常由并行编排在调用前判断非 None）；
            ``cache_dir`` 为 uv ``-d`` 目标目录；``extra_args`` 展开私有包源；
            ``py_version``/``platform_tags`` 用于 ``--python-version``/
            ``--python-platform`` 跨版本跨平台解析。
        with_index: True 时附加 ``--index-url <ctx.pypi_index>``，使用用户
            配置的镜像源。并行下载路径与 sdist 回退重试均传 ``True``：
            前者需用配置镜像而非 uv 默认 pypi.org（国内访问慢/超时），
            后者需从网络下载其他包。

    Raises:
        subprocess.CalledProcessError: uv 非零退出时抛出，由调用方捕获后回退 pip。
        DependencyError: ``ctx.uv_path`` 未设置或 uv 消失（FileNotFoundError 转换）。
    """
    if ctx.uv_path is None:
        raise DependencyError("ctx.uv_path 未设置，无法用 uv 下载")
    # 自由线程版本（py_version 末尾 't' 后缀）：uv --python-version 不识别 t 后缀
    # （报 "found t, which is not part of a valid version"），剥离后传纯数字 3.13。
    # freethreaded wheel（cp313t abi）的实际选择由回退的 pip download --abi cp313t 完成。
    if ctx.py_version:
        base, _ = _split_t_suffix(ctx.py_version)
        major, minor = base.split(".")[:2]
        py_ver_arg = f"{major}.{minor}"
    else:
        major = minor = ""
        py_ver_arg = ""
    py_platform = _uv_python_platform(ctx.platform_tags)
    cmd: list[str] = [
        ctx.uv_path,
        "pip",
        "download",
        "--no-deps",
        "-d",
        str(ctx.cache_dir),
        "--find-links",
        str(ctx.cache_dir),
    ]
    if major and minor:
        cmd.extend(["--python-version", py_ver_arg])
    cmd.extend(["--python-platform", py_platform])
    if with_index:
        cmd.extend(["--index-url", ctx.pypi_index])
    cmd.extend(ctx.extra_args)
    cmd.append(req)
    _logger.info("uv 下载 %s（镜像 %s）", req, ctx.pypi_index if with_index else "默认")
    start = time.perf_counter()
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 uv: {ctx.uv_path}") from e
    # uv 输出转换为 pip 兼容的 "Saved <name>.whl" 格式
    pip_stdout = _convert_uv_output_to_pip_format(result.stdout + "\n" + result.stderr)
    # 用原始 uv 输出（含 "Downloaded X.whl" 行）让 _log_download_event 走 uv fallback
    # 路径：pip_stdout 转换后是 "Saved X.whl"（仅文件名），is_file() 会失败；
    # uv 原始输出 "Downloaded X.whl" 也是文件名，但拼接 cache_dir 后能定位文件取大小
    _log_download_event(req, result.stdout, result.stderr, time.perf_counter() - start, ctx.cache_dir)
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=pip_stdout, stderr=result.stderr)


def _download_one_resolved(
    req: str,
    ctx: DownloadContext,
    *,
    with_index: bool = True,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """下载单个已解析 wheel（``pip download --no-deps <req>``）.

    ``stream=False``（默认，并行模式）用 ``subprocess.run`` 捕获 stdout/stderr，
    不流式输出（多进程 stderr 交错混乱）。``stream=True``（单包模式）用
    :func:`fspack.packaging.wheels.downloader._stream_subprocess` 实时流式输出
    pip 的非进度条输出（如 ``Downloading X.whl`` 行）。

    pip 在 ``stderr=PIPE`` 下检测到非 tty **不输出进度条**，流式输出无法获取
    实时下载速度。``stream=True`` 时启动
    :class:`fspack.packaging.wheels.downloader._DownloadMonitor` 监控
    ``ctx.cache_dir`` 中 ``.whl`` 文件（含 pip 临时文件 ``tmpXXXXXX.whl``）
    总大小变化，用 rich.progress 显示实时下载字节数与速度。

    下载完成通过 :func:`_log_download_event` 打印事件日志（含 req、文件
    大小、耗时），``stream=True`` 时进度条与事件日志配合提供完整下载反馈。

    Args:
        req: 精确版本需求字符串（如 ``numpy==1.24.0``）。
        ctx: 下载上下文。``base_args`` 为 pip download 基础参数（不含
            ``-i index`` 与包名）；``extra_args`` 展开私有包源；``pypi_index``
            为 ``with_index=True`` 时的镜像源；``cache_dir`` 供
            ``stream=True`` 时的下载速度监控。
        with_index: True 时附加 ``-i <ctx.pypi_index>``，使用用户配置的镜像源。
            并行下载路径与 sdist 回退重试均传 ``True``：前者需用配置镜像
            而非 pip 默认 pypi.org（国内访问慢/超时），后者需从网络下载
            其他包。``--find-links <cache_dir>`` 仍优先检查本地缓存。
        stream: True 时流式输出 pip 输出到终端（单包场景），False 时静默
            捕获（并行场景避免多进程 stderr 交错）。
    """
    if with_index:
        cmd = [*ctx.base_args, "--no-deps", "-i", ctx.pypi_index, *ctx.extra_args, req]
    else:
        cmd = [*ctx.base_args, "--no-deps", *ctx.extra_args, req]
    _logger.info("pip 下载 %s（镜像 %s）", req, ctx.pypi_index if with_index else "默认")
    start = time.perf_counter()
    try:
        if stream:
            # 单包场景：流式输出 pip 输出 + 监控 cache_dir 文件大小变化显示实时下载速度。
            # 通过模块属性访问 _stream_subprocess/_DownloadMonitor，便于测试 monkeypatch。
            from fspack.packaging.wheels import downloader as _dl

            monitor = _dl._DownloadMonitor(ctx.cache_dir, req)
            try:
                monitor.start()
                result = _dl._stream_subprocess(cmd)
            finally:
                monitor.stop()
        else:
            result = subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise DependencyError(f"未找到 pip: {cmd[0]}") from e
    except subprocess.CalledProcessError:
        # 重新抛出原异常，保留 stderr 供 sdist 回退解析
        raise
    _log_download_event(req, result.stdout, result.stderr, time.perf_counter() - start)
    return result


def _download_resolved_parallel(resolved: list[str], ctx: DownloadContext) -> subprocess.CompletedProcess[str]:
    """并行下载 uv 解析出的精确版本 wheel.

    用 :class:`~concurrent.futures.ThreadPoolExecutor` 并发调用 ``uv pip
    download --no-deps``（``ctx.uv_path`` 非 None 时）或 ``pip download
    --no-deps``（uv 不可用或单包 uv 下载失败回退），I/O 密集网络下载场景下
    相比串行 ``-r requirements.txt`` 显著提速。

    失败处理：单个包下载失败时收集其异常。若全部成功则合并 stdout 返回；
    若有失败则尝试 sdist 回退（合并失败包的 stderr 提取缺失包名），构建后
    仅重试失败的包，最终合并所有 stdout 返回。

    Args:
        resolved: uv 解析出的精确版本需求列表（如 ``["numpy==1.24.0", ...]``）。
        ctx: 下载上下文。``uv_path`` 非 None 时优先 uv 下载（编排层已按
            ``_uv_supports_download`` 能力探测结果决定是否置 None）。
    """

    def _download_worker(req: str, *, stream: bool = False) -> subprocess.CompletedProcess[str]:
        """单包下载 worker：优先 uv，失败回退 pip.

        ``with_index=True`` 始终附加 ``-i``/``--index-url <pypi_index>``：并行下载
        路径已在在线模式（``--no-index`` 离线解析失败后回退），需用用户配置的镜像源
        而非 pip/uv 默认的 pypi.org（国内访问慢/超时）。``--find-links <cache_dir>``
        仍优先检查本地缓存，命中时 pip/uv 不会访问网络。

        ``stream`` 仅透传给 pip 路径（uv 下载快且无进度条，无需流式）。``stream=True``
        时 :func:`_download_one_resolved` 启动 :class:`_DownloadMonitor` 显示实时
        下载速度。
        """
        if ctx.uv_path is not None:
            try:
                return _download_one_with_uv(req, ctx, with_index=True)
            except subprocess.CalledProcessError as uv_err:
                _logger.info("uv 下载 %s 失败，回退到 pip: %s", req, (uv_err.stderr or "").strip()[:200])
        return _download_one_resolved(req, ctx, with_index=True, stream=stream)

    # 单包场景直接串行，避免线程池开销，但仍走 sdist 回退
    # stream=True 流式输出 pip 输出 + _DownloadMonitor 监控 cache_dir 显示实时下载速度
    if len(resolved) == 1:
        try:
            return _download_worker(resolved[0], stream=True)
        except subprocess.CalledProcessError as e:
            _logger.warning("单包下载失败，尝试 sdist 回退: %s", resolved[0])
            fallback_err = DependencyError(f"依赖下载失败:\n{e.stderr}")
            _handle_sdist_fallback(
                fallback_err,
                ctx.py,
                ctx.pypi_index,
                ctx.cache_dir,
                extra_index_urls=ctx.extra_index_urls,
                find_links=ctx.find_links,
            )
            try:
                return _download_one_resolved(resolved[0], ctx, with_index=True, stream=True)
            except subprocess.CalledProcessError as retry_err:
                # sdist 回退后重试仍失败：转 DependencyError（含 stderr），与
                # _run_pip 的异常约定一致，避免裸 CalledProcessError 逃逸到 CLI
                raise DependencyError(f"依赖下载失败:\n{retry_err.stderr}") from retry_err

    workers = min(_PARALLEL_DOWNLOAD_WORKERS, len(resolved))
    succeeded: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    # worker 内可能抛 CalledProcessError（pip/uv 非零退出）或 DependencyError
    # （pip/uv 消失，_download_one_with_uv/_download_one_resolved 转换），均需
    # 收集进 failed 走 sdist 回退，不能逃逸跳过回退
    failed: list[tuple[str, Exception]] = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wheel-dl") as executor:
        future_to_req = {executor.submit(_download_worker, req): req for req in resolved}
        for future in as_completed(future_to_req):
            req = future_to_req[future]
            try:
                result = future.result()
                succeeded.append((req, result))
            except (subprocess.CalledProcessError, DependencyError) as e:
                failed.append((req, e))

    if not failed:
        return _merge_parallel_results(succeeded)

    # 有失败包：sdist 回退（合并所有失败包的 stderr 解析 missing 包名）
    # 注意：并行模式下每个包的 stderr 独立捕获，必须合并才能解析出所有 sdist-only 包
    # （如 win-unicode-console==0.5 无 wheel，--only-binary=:all: 失败）
    _logger.warning("并行下载 %d 个失败，尝试 sdist 回退: %s", len(failed), [r for r, _ in failed])
    # CalledProcessError 取 stderr；DependencyError 无 stderr 属性，取 str(e) 参与合并
    combined_stderr = "\n".join(
        (e.stderr or "") if isinstance(e, subprocess.CalledProcessError) else str(e) for _, e in failed
    )
    fallback_err = DependencyError(f"依赖下载失败:\n{combined_stderr}")
    _handle_sdist_fallback(
        fallback_err,
        ctx.py,
        ctx.pypi_index,
        ctx.cache_dir,
        extra_index_urls=ctx.extra_index_urls,
        find_links=ctx.find_links,
    )
    # sdist 构建后重试失败的包（带 -i index，因 sdist 构建的 wheel 在本地缓存）
    # 重试仍失败时转 DependencyError（含 stderr），避免裸 CalledProcessError 逃逸
    retry_results: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    for req, _ in failed:
        try:
            result = _download_one_resolved(req, ctx, with_index=True)
        except subprocess.CalledProcessError as retry_err:
            raise DependencyError(f"依赖下载失败:\n{retry_err.stderr}") from retry_err
        retry_results.append((req, result))
    return _merge_parallel_results([*succeeded, *retry_results])


def _merge_parallel_results(
    results: Iterable[tuple[str, subprocess.CompletedProcess[str]]],
) -> subprocess.CompletedProcess[str]:
    """合并并行下载结果：拼接 stdout 供 :func:`_parse_pip_download_wheels` 解析.

    stderr 不合并（并行时各进程 stderr 独立，合并无意义），返回空字符串。
    """
    stdout_parts = [r.stdout for _, r in results if r.stdout]
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="\n".join(stdout_parts), stderr="")
