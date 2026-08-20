"""Wheel 下载入口与缓存调度：pip 解释器查找、subprocess 包装、wheel 文件名解析.

从 :mod:`fspack.packaging.wheels` facade 拆分而来，封装 wheel 下载入口与 pip 调用
基础设施。依赖 :mod:`fspack.packaging.wheels.markers` 做 ``python_version`` 标记
预过滤，依赖 :mod:`fspack.packaging.wheels.cache` 做依赖解析缓存，
:mod:`fspack.packaging.wheels.resolver` 做在线解析编排（其下
:mod:`fspack.packaging.wheels.uv_bridge` 做 uv 依赖图解析、
:mod:`fspack.packaging.wheels.parallel` 做单包下载与并行编排），
:mod:`fspack.packaging.wheels.sdist` 做 sdist 回退构建。

核心流程：

1. ``download_wheels`` 入口：预过滤标记 → 查缓存 → 构造 ``DownloadContext`` →
   调 ``_run_pip_download`` → 解析结果
2. ``_run_pip``/``_stream_subprocess``：subprocess 包装，被 resolver/parallel/sdist 复用
3. ``_find_pip_python``：查找能跑 pip 的 python 解释器

显式 ``import`` 标准库模块（``os``/``re``/``subprocess``/``sys``）
是为了兼容测试中的 ``monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", ...)``
等 patch 路径——patch 设置的是模块对象的属性，因标准库模块为单例，全局生效，
对 :mod:`fspack.packaging.wheels.resolver`、:mod:`fspack.packaging.wheels.parallel`
与 :mod:`fspack.packaging.wheels.sdist` 内的调用同样有效。

从 :mod:`fspack.packaging.wheels.resolver`、:mod:`fspack.packaging.wheels.parallel`、
:mod:`fspack.packaging.wheels.uv_bridge` 与 :mod:`fspack.packaging.wheels.sdist`
re-export 函数，保持 ``from fspack.packaging.wheels.downloader import X`` 路径兼容
（``wheels`` facade 与部分测试仍通过本模块访问这些函数）。
"""

from __future__ import annotations

import functools
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from fspack.config.versions import _split_t_suffix
from fspack.exceptions import DependencyError
from fspack.packaging.wheels.cache import _deps_cache_key, _load_deps_cache, _save_deps_cache
from fspack.packaging.wheels.markers import _filter_by_python_version
from fspack.packaging.wheels.parallel import (  # noqa: F401
    _download_one_resolved,
    _download_one_with_uv,
    _download_resolved_parallel,
    _merge_parallel_results,
)
from fspack.packaging.wheels.resolver import (
    DownloadContext,
    _download_online,  # noqa: F401
    _run_pip_download,
)
from fspack.packaging.wheels.uv_bridge import (  # noqa: F401
    _UV_DOWNLOAD_WHEEL_RE,
    _UV_RESOLVED_LINE_RE,
    _convert_uv_output_to_pip_format,
    _find_uv,
    _resolve_with_uv,
    _uv_python_platform,
    _uv_supports_download,
)

if TYPE_CHECKING:
    # StageRecorder 仅用于类型注解（``from __future__ import annotations`` 使
    # 注解不在运行时求值），顶部不导入 fspack.progress 避免连锁触发
    # rich.progress/rich.table 加载（省 ~12ms）。spinner 在 _run_pip 函数内
    # 延迟导入（实际执行 pip 命令时才加载）。
    from fspack.progress import StageRecorder
from fspack.packaging.wheels.sdist import (
    _MISSING_PKG_RE,  # noqa: F401
    _build_sdist_wheels,  # noqa: F401
    _handle_sdist_fallback,  # noqa: F401
    _parse_missing_packages,  # noqa: F401
)

__all__ = ["download_wheels"]

_logger = logging.getLogger(__name__)

# Windows 系统标准命名为 python.exe；Microsoft Store 版本另提供 python3.exe stub。
# Linux/macOS 用 python3，回退 python。
_PIP_PYTHON_NAMES: tuple[str, ...] = ("python.exe", "python3.exe") if sys.platform == "win32" else ("python3", "python")

# 匹配 pip download stdout 中的 "Saved <path>.whl" 和 "File was already downloaded <path>.whl"
_PIP_WHEEL_LINE_RE = re.compile(r"(?:Saved|File was already downloaded)\s+(.+\.whl)", re.IGNORECASE)

# stderr 累积上限：pip/uv 正常输出 < 1MB，sdist 构建输出 1-3MB，4MB 足以容纳
# 正常输出用于错误诊断。超过上限后停止累积（继续写 sys.stderr 实时显示），
# 避免长输出场景（如失控的 sdist 构建日志）导致内存膨胀。
_STDERR_ACCUM_LIMIT = 4 * 1024 * 1024

# pip 探测超时（秒）：候选解释器 ``python -m pip --version`` 卡死（如网络盘/
# 损坏的解释器）时中断并继续下一个候选，避免拖慢构建
_PIP_PROBE_TIMEOUT = 15


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
    require_hashes: bool = False,
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

    ``require_hashes`` 为 True 时强制依赖哈希校验（iter-103）：仅在线模式生效，
    强制走 ``uv pip compile --generate-hashes`` 路径生成带哈希的 requirements.txt，
    再 ``pip download --require-hashes -r requirements.txt`` 校验下载。
    缓存命中（deps_cache 或 --no-index 解析成功）时跳过校验（缓存目录 wheel 已首次
    校验）；uv 不可用时降级为 warning 不校验（避免阻塞构建）。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    filtered = _prefilter_by_python_version(packages, py_version)
    if not filtered:
        return []

    # 尝试读取依赖解析缓存，命中则跳过 pip 调用
    # require_hashes=True 时缓存命中仍跳过校验：缓存目录 wheel 已首次校验过哈希
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
    ctx = DownloadContext(
        py=py,
        py_version=py_version,
        platform_tags=tuple(platform_tags),
        pypi_index=pypi_index,
        cache_dir=cache_dir,
        base_args=base_args,
        extra_index_urls=tuple(extra_index_urls),
        find_links=tuple(find_links),
    )

    _logger.info("下载依赖 wheel: %s（镜像 %s）", " ".join(filtered), pypi_index)
    before = {f.name for f in cache_dir.glob("*.whl")}

    try:
        result = _run_pip_download(filtered, ctx, require_hashes=require_hashes)
    except DependencyError:
        # pip download 失败时清理本次部分下载的 .whl：pip 可能已下载部分 wheel
        # 才失败，残留的半成品 wheel 会被下次构建的 --no-index 离线解析错误命中，
        # 导致依赖缺失。清理后下次构建重新下载完整依赖。
        _cleanup_partial_wheels(cache_dir, before)
        raise

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


def _cleanup_partial_wheels(cache_dir: Path, before: set[str]) -> None:
    """清理本次下载产生的部分 wheel 文件.

    pip download 失败时可能已下载部分 wheel 才中断，残留的半成品 wheel 会被
    下次构建的 ``--no-index`` 离线解析错误命中（认为依赖已就绪但实际不完整），
    导致依赖缺失。删除本次新增的 wheel（``cache_dir`` 中存在但不在 ``before``
    集合中的），保留下载前已存在的 wheel（其他项目的依赖缓存）。

    Args:
        cache_dir: wheel 缓存目录.
        before: 下载前 ``cache_dir`` 中已有的 wheel 文件名集合.
    """
    partial = [f for f in cache_dir.glob("*.whl") if f.name not in before]
    if not partial:
        return
    for whl in partial:
        try:
            whl.unlink()
        except OSError as e:
            _logger.warning("清理部分下载的 wheel 失败 %s: %s", whl, e)
    _logger.warning("pip download 失败，已清理 %d 个部分下载的 wheel", len(partial))


def _build_pip_download_args(
    py: str,
    py_version: str,
    platform_tags: Sequence[str],
    cache_dir: Path,
) -> list[str]:
    """构造 ``pip download`` 基础参数（不含 ``-i index`` 与包名）.

    自由线程版本（PEP 703/779，``py_version`` 末尾 ``t`` 后缀）：
    - ``--python-version`` 传纯数字 ``3.13``（pip 不识别 ``t`` 后缀，
      ``--python-version 3.13t`` 报 "each version part must be an integer"）
    - ``--abi`` 传 ``cp313t``（free-threaded wheel abi tag，与标准版 cp313 不互通；
      pip 按 ``cp313-cp313t-<platform>`` 组合兼容 tag，正确命中 freethreaded wheel）
    """
    base, is_t = _split_t_suffix(py_version)
    major, minor = base.split(".")[:2]
    py_ver_arg = f"{major}.{minor}"
    abi_arg = f"cp{major}{minor}{'t' if is_t else ''}"
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
        py_ver_arg,
        "--abi",
        abi_arg,
        "--implementation",
        "cp",
        "--only-binary=:all:",
    ]


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


@functools.lru_cache(maxsize=1)
def _find_pip_python() -> str:
    """找一个能跑 ``python -m pip`` 的解释器。

    优先当前 venv（``sys.executable``），无 pip 时遍历 ``PATH`` 找系统 python
    （跳过 venv 所在目录，因为 ``shutil.which`` 在 venv 激活时只返回 venv python）。
    候选名按平台：Windows 为 ``python.exe``/``python3.exe``，其他为 ``python3``/``python``。
    ``pip download`` 的 ``--python-version``/``--abi``/``--implementation`` 参数
    支持跨版本下载，跑 pip 的 python 版本无需匹配目标版本。

    uv 管理的 venv 默认不含 pip（用 Rust 实现的 ``uv pip``），需回退系统 python。

    每个候选解释器的 ``--version`` 探测加 ``timeout=_PIP_PROBE_TIMEOUT``（15s），
    候选卡死（网络盘/损坏解释器）时抛 :class:`subprocess.TimeoutExpired` 中断，
    继续探测下一个候选。

    成功结果经 :func:`functools.lru_cache` 缓存（函数无参数，可直接哈希）：
    同进程内多次构建共享一次探测结果，避免逐包下载反复 spawn 子进程探测。
    失败抛 :class:`DependencyError` 不缓存，下次调用重新探测（环境可能已修复）。
    测试需调用 ``_find_pip_python.cache_clear()`` 清理跨测试缓存污染。
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
                [py, "-m", "pip", "--version"],
                check=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PIP_PROBE_TIMEOUT,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            continue
        return py
    raise DependencyError("未找到可用的 pip，请在当前 venv 执行 `uv pip install pip`，或在系统安装 python3-pip 包")


class _DownloadMonitor:
    """监控 cache_dir ``.whl`` 文件总大小变化，用 rich.progress 显示实时下载速度.

    pip download 在 ``stderr=PIPE`` 下检测到非 tty 不输出进度条，无法通过
    流式输出获取下载进度。本类每 0.5 秒采样 cache_dir 中所有 ``.whl`` 文件
    （含 pip 临时文件 ``tmpXXXXXX.whl``）总大小，计算增量与瞬时速度，用
    :class:`rich.progress.Progress` 显示已下载字节数与速度（``DownloadColumn``
    + ``TransferSpeedColumn``）。

    下载完成后调用 :meth:`stop` 停止监控线程并清除进度条（``transient=True``）。

    监控基于文件系统大小增量，不依赖 pip 输出，跨平台兼容。仅适用于单包
    下载场景（``stream=True`` 路径）；并行模式多进程交错无法区分单包进度，
    不启用监控（靠 :func:`_log_download_event` 事件日志反馈）。
    """

    _SAMPLE_INTERVAL = 0.5  # 采样间隔（秒），平衡刷新流畅度与 stat 开销

    def __init__(self, cache_dir: Path, label: str) -> None:
        """初始化监控器.

        Args:
            cache_dir: wheel 缓存目录（pip ``-d`` 参数指向的目录）。
            label: 进度条显示的标签（通常是 ``req`` 如 ``numpy==1.24.0``）。
        """
        import threading

        from rich.progress import (
            DownloadColumn,
            Progress,
            SpinnerColumn,
            TextColumn,
            TransferSpeedColumn,
        )

        from fspack.console import console

        self._cache_dir = cache_dir
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="dl-monitor")
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console.rich,
            transient=True,
        )
        self._task_id = self._progress.add_task(label, total=None)

    def start(self) -> None:
        """启动监控线程与进度条."""
        self._progress.start()
        self._thread.start()

    def stop(self) -> None:
        """停止监控线程并清除进度条."""
        self._stop.set()
        self._thread.join(timeout=2)
        self._progress.stop()

    def _run(self) -> None:
        import time

        last_size = self._dir_whl_size()
        last_time = time.monotonic()

        while not self._stop.wait(self._SAMPLE_INTERVAL):
            current = self._dir_whl_size()
            now = time.monotonic()
            elapsed = now - last_time
            if elapsed > 0 and current > last_size:
                delta = current - last_size
                speed = delta / elapsed
                self._progress.update(self._task_id, advance=delta, speed=speed)
            last_size = current
            last_time = now

    def _dir_whl_size(self) -> int:
        """获取 cache_dir 中所有 ``.whl`` 文件总大小.

        用 :func:`os.scandir` 代替 :meth:`Path.iterdir`，避免 Path 对象构造
        开销（scandir 返回 ``DirEntry`` 直接调 ``stat()`` 缓存）。
        """
        import contextlib

        total = 0
        with contextlib.suppress(OSError), os.scandir(self._cache_dir) as entries:
            for entry in entries:
                if entry.name.endswith(".whl"):
                    with contextlib.suppress(OSError):
                        total += entry.stat().st_size
        return total


def _stream_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """运行命令，实时流式输出 stderr 到终端，捕获 stdout 和 stderr。

    用 ``Popen`` + 守护线程通过 ``os.read`` 读取 stderr 文件描述符字节块并实时
    写入 ``sys.stderr``，支持 pip 进度条的 ``\\r`` 回车更新。stdout 始终捕获
    用于解析 wheel 列表。stderr 同时累积，供失败时构造 ``CalledProcessError``。

    使用 ``os.read`` 而非 ``BufferedReader.read1``：前者直接读 fd，不依赖
    ``Popen`` 的缓冲层（``bufsize=0`` 时 stderr 是 ``FileIO`` 无 ``read1`` 方法）。

    **内存保护**：stderr 累积上限 :data:`_STDERR_ACCUM_LIMIT`（4MB），超过后
    停止累积（继续写 ``sys.stderr`` 实时显示），避免失控的 sdist 构建日志
    （可达数十 MB）导致内存膨胀。错误诊断只需末尾数 KB 即可定位根因。

    调用方应在调用前停止 spinner（避免 ``\\r`` 与 pip 进度条冲突），并在调用后
    恢复 spinner 或继续后续日志输出。

    .. note::
        pip 在 ``stderr=PIPE`` 下检测到非 tty 不输出进度条，本函数的流式输出
        实际只传递 pip 的非进度条输出（如 ``Downloading X.whl`` 行）。实时下载
        速度由调用方通过 :class:`_DownloadMonitor` 监控 cache_dir 文件大小变化
        显示（见 :func:`fspack.packaging.wheels.parallel._download_one_resolved`）。
    """
    # 延迟导入 threading：保持模块顶部零 stdlib 副作用约定（与 net.py/runtime.py
    # 等热路径模块一致）。site.py 启动期已加载 threading，此处为 dict 查询，无实际开销。
    import threading

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_chunks: list[bytes] = []
    stderr_total = 0

    def _drain_stderr() -> None:
        nonlocal stderr_total
        assert process.stderr is not None
        fd = process.stderr.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            # 累积上限保护：超过 _STDERR_ACCUM_LIMIT 后仅写终端不再累积，
            # 避免长输出场景内存膨胀。错误诊断只需末尾片段即可定位根因。
            if stderr_total < _STDERR_ACCUM_LIMIT:
                stderr_chunks.append(chunk)
                stderr_total += len(chunk)
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
    # 延迟导入：spinner 触发 fspack.progress 加载（含 rich.progress ~12ms）。
    # 仅在实际执行 pip 命令时加载，避免 import fspack.builder 热路径触发。
    from fspack.progress import spinner

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
