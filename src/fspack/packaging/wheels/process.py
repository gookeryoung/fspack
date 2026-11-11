"""pip 进程执行：解释器查找、subprocess 包装与下载进度监控.

从 :mod:`fspack.packaging.wheels.downloader` 拆分而来（进程执行块）。
:func:`_run_pip` / :func:`_stream_subprocess` 被 :mod:`resolver`、
:mod:`parallel` 与 :mod:`sdist` 复用；:class:`_DownloadMonitor` 供单包流式
下载显示实时速度。
"""

from __future__ import annotations

import functools
import logging
import os
import subprocess
import sys
from pathlib import Path

from fspack.exceptions import DependencyError

__all__ = [
    "_DownloadMonitor",
    "_find_pip_python",
    "_run_pip",
    "_stream_subprocess",
]

_logger = logging.getLogger(__name__)

# Windows 系统标准命名为 python.exe；Microsoft Store 版本另提供 python3.exe stub。
# Linux/macOS 用 python3，回退 python。
_PIP_PYTHON_NAMES: tuple[str, ...] = ("python.exe", "python3.exe") if sys.platform == "win32" else ("python3", "python")

# stderr 累积上限：pip/uv 正常输出 < 1MB，sdist 构建输出 1-3MB，4MB 足以容纳
# 正常输出用于错误诊断。超过上限后停止累积（继续写 sys.stderr 实时显示），
# 避免长输出场景（如失控的 sdist 构建日志）导致内存膨胀。
_STDERR_ACCUM_LIMIT = 4 * 1024 * 1024

# pip 探测超时（秒）：候选解释器 ``python -m pip --version`` 卡死（如网络盘/
# 损坏的解释器）时中断并继续下一个候选，避免拖慢构建
_PIP_PROBE_TIMEOUT = 15


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
