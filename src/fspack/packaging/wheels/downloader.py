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
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

# 匹配 pip download 成功结尾行 "Successfully downloaded <name> <name> ..."。
# 这是 pip 最终解析集的权威信号：回溯解析会把被放弃的候选版本也打印进
# Saved/File was already downloaded 行，只有本行列出的包名才是最终选中集。
_PIP_SUCCESS_LINE_RE = re.compile(r"^Successfully downloaded (.+)$", re.MULTILINE | re.IGNORECASE)

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
    该模式下跳过依赖解析缓存的读取与写入——缓存只存 wheel 文件名，命中即等于
    静默跳过校验。uv 不可用时降级为 warning 不校验（避免阻塞构建）。

    ``pypi_index`` 防御性清理：strip 首尾空白与 markdown 反引号/引号包裹
    （从文档复制 URL 常带入 `` `url` `` 形式，pip/uv 不识别反引号导致
    "Invalid URL" 或 DNS 解析失败）。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    pypi_index = pypi_index.strip(" \t\r\n`'\"")

    filtered = _prefilter_by_python_version(packages, py_version)
    if not filtered:
        return []

    # 尝试读取依赖解析缓存，命中则跳过 pip 调用。
    # require_hashes=True 时不读也不写解析缓存：缓存只记录 wheel 文件名列表，
    # 无法证明这些 wheel 真的通过哈希校验，命中即等于静默跳过校验。
    deps_key = _deps_cache_key(
        filtered,
        py_version,
        platform_tags,
        extra_index_urls,
        find_links,
        pypi_index=pypi_index,
        require_hashes=require_hashes,
    )
    cached_wheels = None if require_hashes else _load_deps_cache(cache_dir, deps_key)
    if cached_wheels is not None:
        _logger.info("依赖解析缓存命中，跳过 pip 调用")
        # 历史缓存可能含 pip 回溯残留的同包多版本 wheel（旧版本 fspack 写入），
        # 命中后同样做冲突消解与顶层约束校验，自动修复污染缓存
        cached_names = _select_coherent_wheels([w.name for w in cached_wheels], cache_dir)
        _validate_declared_constraints(cached_names, filtered)
        cached_wheels = [cache_dir / name for name in cached_names]
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
    _validate_declared_constraints(wheel_names, filtered)
    wheels = [cache_dir / name for name in wheel_names if (cache_dir / name).is_file()]
    if wheels and not used_fallback and not require_hashes:
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

    回退到目录扫描时按 PEP 503 规范化包名分组，每包只保留**版本最高**的 wheel
    （用 ``packaging.version.Version`` 做 PEP 440 比较）。wheel 缓存目录是
    跨项目持久化的，不同项目可能曾下载过同包名的不同版本——直接 glob 全部返回
    会把旧版本误带入本次构建，导致 site-packages 里同时存在多个 dist-info。
    """
    wheel_names = _parse_pip_download_wheels(stdout)
    if wheel_names:
        return _select_coherent_wheels(wheel_names, cache_dir), False
    _logger.warning("pip download 输出解析失败，回退到目录扫描")
    # 目录扫描可能包含其他项目遗留的 wheel，不可作为本 deps_key 的缓存
    wheels = sorted(f.name for f in cache_dir.glob("*.whl"))
    # 按规范化包名分组，每包只保留版本最高者（防止缓存里同包多版本被误带入）
    return _select_coherent_wheels(_dedup_wheels_by_pkg(wheels), cache_dir), True


def _dedup_wheels_by_pkg(wheel_names: list[str]) -> list[str]:
    """wheel 文件名列表按包名去重，每包只保留版本最高的 wheel.

    用 ``packaging.version.Version`` 做 PEP 440 比较。pip 运行时 packaging 必然
    已安装（pip 的硬依赖），但 fspack 本身未声明为顶层依赖，故惰性导入。
    解析 wheel 文件名失败（非标准格式）时回退到原顺序保留全部。
    """
    from fspack.packaging.site_packages import normalize_pkg_name
    from fspack.slim.spec import WheelInfo

    # packaging 仅 pip 运行时可用，lazy import 避免 fspack 顶层依赖膨胀
    try:
        from packaging.version import Version as _V
    except ImportError:  # pragma: no cover - pip 必然可用
        _logger.warning("packaging 不可用，wheel 去重降级为原样返回")
        return wheel_names

    best: dict[str, tuple[_V, str]] = {}
    for name in wheel_names:
        info = WheelInfo.from_filename(name)
        if info is None:
            # 无法解析的 wheel 保持原样（best effort）
            continue
        norm = normalize_pkg_name(info.name)
        try:
            ver = _V(info.version)
        except Exception:  # pragma: no cover - 版本字符串异常
            continue
        cur = best.get(norm)
        if cur is None or ver > cur[0]:
            best[norm] = (ver, name)
    result = sorted(path for _, path in best.values())
    dropped = len(wheel_names) - len(result)
    if dropped:
        _logger.info("wheel 缓存按包名去重: %d → %d（剔除 %d 个旧版本）", len(wheel_names), len(result), dropped)
    return result


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

    pip 回溯解析会把被放弃的候选版本也打印成 Saved/File was already downloaded
    行（如 ``pydantic>=2.6.0,<2.15.0`` 先试 2.13.5 失败再回溯到 2.6.4，两行都在）。
    若 stdout 末尾有 ``Successfully downloaded <name> ...`` 行（最终解析集的权威
    信号），按规范化包名过滤，只保留最终选中的包；同包多版本残留交由
    :func:`_select_coherent_wheels` 做版本消解。
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
    success_match = _PIP_SUCCESS_LINE_RE.search(stdout)
    if success_match and names:
        from fspack.packaging.site_packages import normalize_pkg_name
        from fspack.slim.spec import WheelInfo

        final_pkgs = {normalize_pkg_name(n.strip()) for n in success_match.group(1).split()}
        filtered: list[str] = []
        for name in names:
            info = WheelInfo.from_filename(name)
            # 文件名解析失败的 wheel 无法判包名，保守保留
            if info is None or normalize_pkg_name(info.name) in final_pkgs:
                filtered.append(name)
        if len(filtered) < len(names):
            _logger.info("按 Successfully downloaded 最终包名过滤: %d → %d 个 wheel", len(names), len(filtered))
        return filtered
    return names


def _wheel_requires(wheel_path: Path) -> list[str]:
    """读取 wheel 包内 METADATA 的 ``Requires-Dist`` 原文列表；读取失败返回空列表.

    仅用于同包多版本冲突消解（:func:`_select_coherent_wheels`）与测试桩替换，
    不做任何解析——Requirement 解析由调用方按需进行并容忍失败。
    """
    import zipfile

    text = ""
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            for member in zf.namelist():
                if member.endswith(".dist-info/METADATA"):
                    text = zf.read(member).decode("utf-8", errors="replace")
                    break
    except (OSError, zipfile.BadZipFile):
        return []
    return [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("Requires-Dist:")]


def _parse_wheel_versions(
    groups: dict[str, list[str]],
) -> dict[str, dict[str, Any]] | None:
    """解析各分组候选 wheel 的版本号；任一组解析失败返回 ``None``.

    Args:
        groups: 规范化包名 → 候选 wheel 文件名列表（保序）.

    Returns:
        包名 → {wheel 文件名 → packaging Version}；``packaging`` 不可用或版本号异常时 ``None``.
    """
    from fspack.slim.spec import WheelInfo

    try:
        from packaging.version import Version
    except ImportError:  # pragma: no cover - pip 运行时必然可用
        return None
    versions: dict[str, dict[str, Version]] = {}
    for norm, cands in groups.items():
        vmap: dict[str, Version] = {}
        for fname in cands:
            info = WheelInfo.from_filename(fname)
            try:
                assert info is not None
                vmap[fname] = Version(info.version)
            except Exception:
                return None
        versions[norm] = vmap
    return versions


def _select_coherent_wheels(wheel_names: list[str], cache_dir: Path) -> list[str]:
    """同包多版本 wheel 冲突消解：选出一组相互满足依赖约束的版本.

    pip download 回溯解析会把被放弃的候选版本也打印进 Saved/File was already
    downloaded 行（如 pydantic 2.13.5 与 2.6.4 同时出现），若不去重，同包多个
    版本会被全部解压进 site-packages，import 命中哪个 dist-info 看目录序——
    pydantic 2.13.5 + pydantic-core 2.16.3 不配套导致打包产物无法启动的事故
    即源于此。

    消解策略：

    1. 按规范化包名分组，单候选组直接保留；
    2. 冲突组用 wheel METADATA 的 ``Requires-Dist``（仅无环境标记的依赖）做
       约束回溯：每个包选一个版本，使全部选中 wheel 的依赖约束互相满足。
       候选按 stdout 中出现顺序的**逆序**尝试——pip 回溯从新到旧，最终版本
       最后打印，优先命中 pip 的实际选择；
    3. 无一致解（packaging 不可用 / wheel 文件名或版本无法解析 / METADATA
       缺失导致约束无解）时回退保留每组最后一个 wheel（pip 最终选择的经验
       位置）。

    Args:
        wheel_names: wheel 文件名列表（stdout 解析或目录扫描结果）.
        cache_dir: wheel 缓存目录（读取 METADATA 用）.

    Returns:
        消解后的 wheel 文件名列表，保持原有相对顺序.
    """
    from fspack.packaging.site_packages import normalize_pkg_name
    from fspack.slim.spec import WheelInfo

    try:
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - pip 运行时必然可用
        _logger.warning("packaging 不可用，同包多版本消解降级为保留每组最后一个 wheel")
        return _keep_last_wheel_per_pkg(wheel_names)

    # 按规范化包名分组（保序）；文件名无法解析的 wheel 不参与分组，原样保留
    groups: dict[str, list[str]] = {}
    unparsed: list[str] = []
    for name in wheel_names:
        info = WheelInfo.from_filename(name)
        if info is None:
            unparsed.append(name)
            continue
        groups.setdefault(normalize_pkg_name(info.name), []).append(name)
    conflicts = {norm: cands for norm, cands in groups.items() if len(cands) > 1}
    if not conflicts:
        return wheel_names
    _logger.warning(
        "解析结果存在同包多版本 wheel（pip 回溯残留），启动约束消解: %s",
        dict(conflicts),
    )

    # 各 wheel 的无标记 Requires-Dist（带环境标记的依赖无法离线求值，跳过）
    requires: dict[str, list[Requirement]] = {}
    for fname in wheel_names:
        reqs: list[Requirement] = []
        for raw in _wheel_requires(cache_dir / fname):
            try:
                req = Requirement(raw)
            except Exception:  # pragma: no cover - 非法 Requires-Dist 行
                continue
            if req.marker is None:
                reqs.append(req)
        requires[fname] = reqs

    # 各候选 wheel 的版本号；解析失败（含 packaging 不可用）则回退保留最后一个
    versions = _parse_wheel_versions(groups)
    if versions is None:
        _logger.warning("wheel 版本号解析失败，同包多版本消解回退为保留每组最后一个 wheel")
        return _keep_last_wheel_per_pkg(wheel_names)

    def _consistent(assignment: dict[str, str]) -> bool:
        """assignment（包名 → wheel 文件名）中全部选中 wheel 的依赖约束互满足."""
        for fname in assignment.values():
            for req in requires.get(fname, ()):
                target = normalize_pkg_name(req.name)
                other = assignment.get(target)
                if other is None:
                    continue
                if req.specifier and not req.specifier.contains(versions[target][other], prereleases=True):
                    return False
        return True

    conflict_norms = [norm for norm in groups if norm in conflicts]
    assignment: dict[str, str] = {norm: cands[0] for norm, cands in groups.items() if norm not in conflicts}

    def _backtrack(i: int) -> bool:
        if i == len(conflict_norms):
            return _consistent(assignment)
        norm = conflict_norms[i]
        for fname in reversed(groups[norm]):  # 逆序尝试：优先命中 pip 最终选择（最后打印）
            assignment[norm] = fname
            if _backtrack(i + 1):
                return True
            del assignment[norm]
        return False

    if not _backtrack(0):
        _logger.warning("同包多版本约束消解无一致解，回退为保留每组最后一个 wheel")
        return _keep_last_wheel_per_pkg(wheel_names)
    selected = set(assignment.values())
    dropped = [name for name in wheel_names if name not in selected and name not in unparsed]
    _logger.info("同包多版本消解完成: 剔除 %s", dropped)
    return [name for name in wheel_names if name in selected or name in unparsed]


def _keep_last_wheel_per_pkg(wheel_names: list[str]) -> list[str]:
    """同包多版本回退策略：保留每组（按规范化包名）最后一个 wheel.

    pip 回溯从新到旧尝试，最终选中的版本在 stdout 中最后出现；文件名无法
    解析的 wheel 原样保留。
    """
    from fspack.packaging.site_packages import normalize_pkg_name
    from fspack.slim.spec import WheelInfo

    last_per_pkg: dict[str, str] = {}
    unparsed: list[str] = []
    for name in wheel_names:
        info = WheelInfo.from_filename(name)
        if info is None:
            unparsed.append(name)
        else:
            last_per_pkg[normalize_pkg_name(info.name)] = name
    keep = set(last_per_pkg.values()) | set(unparsed)
    return [name for name in wheel_names if name in keep]


def _validate_declared_constraints(wheel_names: list[str], requirements: Sequence[str]) -> None:
    """校验解析出的 wheel 版本满足项目顶层声明的版本约束.

    ``download_wheels`` 的硬门禁：任一顶层依赖（如 ``pydantic>=2.6.0,<2.15.0``）
    的选中 wheel 版本超出声明范围即抛 :class:`DependencyError`，禁止打包超出
    项目约定版本的库。仅校验顶层声明（传递依赖约束由解析器保证，另由
    :func:`_select_coherent_wheels` 的 METADATA 约束消解兜底）。

    Args:
        wheel_names: 选中的 wheel 文件名列表.
        requirements: 顶层依赖声明列表（已过 python_version 标记预过滤）.

    Raises:
        DependencyError: 选中 wheel 版本不满足对应顶层声明的 specifier.
    """
    from fspack.packaging.site_packages import normalize_pkg_name
    from fspack.slim.spec import WheelInfo

    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError:  # pragma: no cover - pip 运行时必然可用
        _logger.warning("packaging 不可用，跳过顶层版本约束校验")
        return

    selected: dict[str, str] = {}
    for name in wheel_names:
        info = WheelInfo.from_filename(name)
        if info is not None:
            selected.setdefault(normalize_pkg_name(info.name), name)
    for req_str in requirements:
        try:
            req = Requirement(req_str)
        except Exception:  # pragma: no cover - 非法依赖声明由 pip 报错
            continue
        if not req.specifier:
            continue
        fname = selected.get(normalize_pkg_name(req.name))
        if fname is None:
            continue
        info = WheelInfo.from_filename(fname)
        try:
            assert info is not None
            version = Version(info.version)
        except Exception:  # pragma: no cover - 版本号异常
            continue
        if not req.specifier.contains(version, prereleases=True):
            raise DependencyError(
                f"依赖解析结果超出项目版本约束: {fname} 不满足声明 {req_str}。"
                f"请检查 wheel 缓存（{normalize_pkg_name(req.name)} 相关 wheel）是否被其他项目污染，"
                f"或依赖声明是否存在环境标记分歧"
            )
