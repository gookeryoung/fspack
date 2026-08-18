"""在线依赖解析编排：离线优先、uv 解析优先、哈希校验链与回退调度.

原 816 行模块按职责拆分为三部分，本模块保留编排职责与共享参数包：

- :mod:`fspack.packaging.wheels.uv_bridge`：uv CLI 集成（检测/平台映射/
  能力探测/依赖图解析/输出格式转换）
- :mod:`fspack.packaging.wheels.parallel`：单包下载原语（pip/uv）+ 并行编排
- 本模块：下载流程编排与 :class:`DownloadContext` 参数包

核心流程：

1. ``_run_pip_download``：先用 ``--no-index`` 离线解析（搜索 cache_dir 与
   用户 find-links），失败回退到 ``_download_online``
2. ``_download_online``：优先 ``uv pip compile`` 解析依赖图（PubGrub）后
   并行下载；uv 不可用/解析失败回退 ``pip download`` 完整解析（含 sdist 回退）
3. ``_download_with_hashes``：``--generate-hashes`` + ``pip download
   --require-hashes`` 哈希校验链

依赖 :mod:`fspack.packaging.wheels.downloader` 提供 ``_run_pip``（惰性导入
打破循环：downloader 顶层导入本模块，本模块不能顶层导入 downloader）。
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from fspack.config import is_offline
from fspack.exceptions import DependencyError
from fspack.packaging.wheels.parallel import _PARALLEL_DOWNLOAD_WORKERS, _download_resolved_parallel
from fspack.packaging.wheels.sdist import _handle_sdist_fallback
from fspack.packaging.wheels.uv_bridge import (
    _extract_resolved_lines,
    _find_uv,
    _resolve_with_uv,
    _uv_supports_download,
)

__all__ = [
    "DownloadContext",
    "_download_online",
    "_download_with_hashes",
    "_run_pip_download",
]

_logger = logging.getLogger(__name__)


@dataclass
class DownloadContext:
    """单次在线下载任务的共享参数包：跨编排/解析/下载/sdist 回退复用.

    将 pip 解释器、目标 Python 版本与平台、索引与私有包源等 9 个原本在
    各下载函数间逐层透传的参数收敛为一个对象，消除 PLR0913（参数过多）豁免。

    ``uv_path`` 由编排层 :func:`_download_online` 调用 ``_find_uv()`` 探测后
    填充（探测一次，uv 解析、能力检测与并行下载共享），创建时通常不指定；
    uv 不支持 ``pip download`` 子命令或不可用时置回 ``None`` 让并行下载走
    pip 路径。
    """

    py: str
    """pip 解释器路径（pip download / pip wheel 命令的解释器）。"""

    py_version: str
    """目标 Python 版本（如 ``3.11.9``），uv ``--python-version`` 用。"""

    platform_tags: Sequence[str]
    """目标平台标签列表（如 ``("win_amd64",)``）。"""

    pypi_index: str
    """PyPI 索引 URL（用户配置的镜像源）。"""

    cache_dir: Path
    """wheel 缓存目录（``~/.fspack/cache/wheels/``）。"""

    base_args: list[str]
    """pip download 基础参数（不含 ``-i index`` 与包名）。"""

    extra_index_urls: Sequence[str] = ()
    """额外索引 URL 列表（私有 PyPI）。"""

    find_links: Sequence[str] = ()
    """本地 wheel 目录列表（``--find-links``）。"""

    uv_path: str | None = None
    """uv 可执行路径，None 表示 uv 不可用（走 pip 路径）。"""

    @property
    def extra_args(self) -> list[str]:
        """展开私有包源为命令行片段（``--extra-index-url``/``--find-links`` 成对）."""
        args: list[str] = []
        for url in self.extra_index_urls:
            args.extend(["--extra-index-url", url])
        for link in self.find_links:
            args.extend(["--find-links", link])
        return args

    @property
    def user_find_links_args(self) -> list[str]:
        """仅展开用户 find-links（离线 ``--no-index`` 解析用，不含额外索引）."""
        args: list[str] = []
        for link in self.find_links:
            args.extend(["--find-links", link])
        return args


def _run_pip_download(
    filtered: list[str],
    ctx: DownloadContext,
    *,
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

    # 先用 --no-index 从本地缓存 + 用户 find-links 解析（离线模式），命中则跳过网络查询
    result = _run_pip(
        [*ctx.base_args, *ctx.user_find_links_args, "--no-index", *filtered],
        f"检查缓存 {len(filtered)} 个依赖",
        suppress_error=True,
    )
    if result is None:
        if is_offline():
            searched = [str(ctx.cache_dir), *ctx.find_links]
            raise DependencyError(
                f"离线模式下依赖缓存未命中: {', '.join(filtered)}，"
                f"已搜索路径: {'; '.join(searched)}。"
                f"请预先下载 wheel 放入上述路径之一，或通过 --find-links 指定本地 wheel 目录，"
                f"或取消 FSPACK_OFFLINE 环境变量"
            )
        _logger.info("缓存解析失败，回退到在线解析下载")
        return _download_online(filtered, ctx, require_hashes=require_hashes)
    _logger.info("缓存解析成功，跳过网络查询")
    return result


def _download_online(
    filtered: list[str],
    ctx: DownloadContext,
    *,
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

    uv 路径在本函数顶部探测一次（``_find_uv``），写入 ``ctx.uv_path`` 共享给
    require_hashes 检查、uv 解析与 ``_download_resolved_parallel`` 的 uv 下载
    路径，避免重复 ``shutil.which`` 调用。``_uv_supports_download`` 也调一次，
    uv 不支持时将 ``ctx.uv_path`` 置回 ``None`` 让并行下载全走 pip。
    """
    # 惰性导入打破循环依赖：downloader 顶层导入本模块，本模块不能顶层导入 downloader
    from fspack.packaging.wheels.downloader import _run_pip

    # 共享 uv 路径检测：require_hashes 检查、uv 解析、uv 下载共用一次 _find_uv()
    ctx.uv_path = _find_uv()
    # 检测 uv 是否支持 pip download 子命令（0.1.9+ 移除），结果决定并行下载走 uv 还是 pip
    uv_can_download = _uv_supports_download(ctx.uv_path)

    # require_hashes=True：强制走 uv --generate-hashes 路径
    if require_hashes:
        if ctx.uv_path is None:
            _logger.warning("require_hashes=True 但 uv 不可用，降级为不校验哈希")
        else:
            return _download_with_hashes(filtered, ctx)

    # 尝试用 uv 解析依赖图（不带哈希）
    resolved: list[str] | None = None
    if ctx.uv_path is not None:
        try:
            uv_output = _resolve_with_uv(ctx, filtered)
            resolved = _extract_resolved_lines(uv_output)
            if not resolved:
                # 空列表视为解析失败：若继续走并行下载，min(8, 0) 会让
                # ThreadPoolExecutor(max_workers=0) 抛 ValueError 且跳过 pip 回退
                _logger.warning("uv 解析输出无有效 name==version 行，回退到 pip 完整解析")
                resolved = None
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
            ctx.pypi_index,
        )
        if not uv_can_download:
            # uv 不支持 pip download 子命令：置回 None 让并行下载走 pip 路径
            ctx.uv_path = None
        return _download_resolved_parallel(resolved, ctx)

    # uv 不可用或解析失败：回退到 pip 完整解析+下载
    try:
        result = _run_pip(
            [*ctx.base_args, "-i", ctx.pypi_index, *ctx.extra_args, *filtered],
            f"pip download {len(filtered)} 个依赖（镜像 {ctx.pypi_index}）",
            stream=True,
        )
        assert result is not None  # suppress_error=False，不会返回 None
        return result
    except DependencyError as e:
        # sdist 回退：解析无 wheel 的包，用 pip wheel 从 sdist 构建后重试
        _handle_sdist_fallback(
            e, ctx.py, ctx.pypi_index, ctx.cache_dir, extra_index_urls=ctx.extra_index_urls, find_links=ctx.find_links
        )
        result = _run_pip(
            [*ctx.base_args, "-i", ctx.pypi_index, *ctx.extra_args, *filtered],
            f"pip download 重试 {len(filtered)} 个依赖（镜像 {ctx.pypi_index}）",
            stream=True,
        )
        assert result is not None  # suppress_error=False，不会返回 None
        return result


def _download_with_hashes(filtered: list[str], ctx: DownloadContext) -> subprocess.CompletedProcess[str]:
    """``require_hashes=True`` 路径：uv 生成带哈希 requirements + pip download 校验.

    用 ``uv pip compile --generate-hashes`` 生成包含所有依赖哈希的 requirements.txt，
    写入临时文件后用 ``pip download --require-hashes -r <tmp>`` 一次性下载校验。
    无法并行（pip ``--require-hashes`` 要求所有包在同一 requirements 文件中）。

    临时 requirements.txt 在 ``ctx.cache_dir`` 下（避免 tempfile 目录权限问题），
    下载完成后删除。
    """
    import contextlib
    import tempfile

    from fspack.packaging.wheels.downloader import _run_pip

    # uv pip compile --generate-hashes 输出带哈希的 requirements 文本
    requirements_text = _resolve_with_uv(ctx, filtered, generate_hashes=True)

    # 写入临时 requirements.txt（cache_dir 下，便于 pip --find-links <cache_dir> 复用）
    with tempfile.NamedTemporaryFile(
        mode="w", suffix="-requirements.txt", dir=str(ctx.cache_dir), delete=False, encoding="utf-8"
    ) as f:
        f.write(requirements_text)
        req_path = f.name
    try:
        cmd = [
            *ctx.base_args,
            *ctx.extra_args,
            "--require-hashes",
            "-r",
            req_path,
        ]
        _logger.info(
            "pip download --require-hashes -r %s（%d 个依赖，镜像 %s）", req_path, len(filtered), ctx.pypi_index
        )
        result = _run_pip(
            cmd, f"pip download --require-hashes {len(filtered)} 个依赖（镜像 {ctx.pypi_index}）", stream=True
        )
        assert result is not None  # suppress_error=False
        return result
    finally:
        with contextlib.suppress(OSError):
            Path(req_path).unlink()
