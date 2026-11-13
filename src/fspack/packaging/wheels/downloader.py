"""Wheel 下载入口与编排：缓存调度、pip 调用与结果解析的组合.

本模块是 wheel 下载的编排层，职责已按四块拆分到独立模块：

- :mod:`fspack.packaging.wheels.pip_args`：``pip download`` 参数构造
- :mod:`fspack.packaging.wheels.process`：解释器查找、subprocess 执行、下载监控
- :mod:`fspack.packaging.wheels.output`：stdout/目录扫描解析与版本消解
- :mod:`fspack.packaging.wheels.cache`：依赖解析缓存与缓存目录维护

编排流程：预过滤标记 → 查缓存 → 构造 ``DownloadContext`` → 调
:func:`fspack.packaging.wheels.resolver._run_pip_download` → 解析结果。

本模块同时 re-export 上述模块与 :mod:`fspack.packaging.wheels.resolver`、
:mod:`fspack.packaging.wheels.parallel`、:mod:`fspack.packaging.wheels.uv_bridge`、
:mod:`fspack.packaging.wheels.sdist` 的实现函数，保持
``from fspack.packaging.wheels.downloader import X`` 路径兼容（``wheels``
facade 与部分测试仍通过本模块访问这些函数）。

显式 ``import`` 标准库模块（``os``/``re``/``subprocess``/``sys``）是为了兼容
测试中的 ``monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", ...)``
等 patch 路径——patch 设置的是模块对象的属性，因标准库模块为单例，全局生效，
对各实现模块内的调用同样有效。
"""

from __future__ import annotations

import logging
import os  # noqa: F401 - 兼容 fspack.packaging.wheels.os.* patch 路径
import re  # noqa: F401 - 兼容 fspack.packaging.wheels.re.* patch 路径
import subprocess  # noqa: F401 - 兼容 fspack.packaging.wheels.subprocess.run patch 路径
import sys  # noqa: F401 - 兼容 fspack.packaging.wheels.sys.executable patch 路径
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.exceptions import DependencyError
from fspack.packaging.wheels.cache import (
    _cleanup_partial_wheels,
    _deps_cache_key,
    _load_deps_cache,
    _record_wheel_stage,
    _save_deps_cache,
)
from fspack.packaging.wheels.markers import _filter_by_python_version
from fspack.packaging.wheels.parallel import (  # noqa: F401
    _download_one_resolved,
    _download_one_with_uv,
    _download_resolved_parallel,
    _merge_parallel_results,
)
from fspack.packaging.wheels.pip_args import _build_pip_download_args
from fspack.packaging.wheels.process import (  # noqa: F401
    _PIP_PROBE_TIMEOUT,
    _PIP_PYTHON_NAMES,
    _DownloadMonitor,
    _find_pip_python,
    _run_pip,
    _stream_subprocess,
)
from fspack.packaging.wheels.resolver import (
    DownloadContext,
    _download_online,  # noqa: F401
    _run_pip_download,
)

if TYPE_CHECKING:
    # StageRecorder 仅用于类型注解（``from __future__ import annotations`` 使
    # 注解不在运行时求值），顶部不导入 fspack.progress 避免连锁触发
    # rich.progress/rich.table 加载（省 ~12ms）。
    from fspack.metrics import StageRecorder
from fspack.packaging.wheels.output import (  # noqa: F401
    _PIP_SUCCESS_LINE_RE,
    _PIP_WHEEL_LINE_RE,
    _dedup_wheels_by_pkg,
    _parse_pip_download_wheels,
    _parse_wheel_names,
    _select_coherent_wheels,
    _validate_declared_constraints,
)
from fspack.packaging.wheels.sdist import (
    _MISSING_PKG_RE,  # noqa: F401
    _build_sdist_wheels,  # noqa: F401
    _handle_sdist_fallback,  # noqa: F401
    _parse_missing_packages,  # noqa: F401
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

__all__ = ["download_wheels"]

_logger = logging.getLogger(__name__)


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

    预过滤环境标记：``pip download --python-version`` 只用运行 pip 的解释器
    求值 marker，需在调用前按目标 Python 版本与目标平台（``platform_tags``）
    完整求值（如目标 3.8 时跳过 ``PySide6>=6.5.0; python_version >= '3.11'``，
    Linux 目标跳过 ``pywin32; sys_platform == 'win32'``）。

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
    静默跳过校验。uv 不可用时抛 :class:`DependencyError`（fail-fast），不再静默降级。

    ``pypi_index`` 防御性清理：strip 首尾空白与 markdown 反引号/引号包裹
    （从文档复制 URL 常带入 `` `url` `` 形式，pip/uv 不识别反引号导致
    "Invalid URL" 或 DNS 解析失败）。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    pypi_index = pypi_index.strip(" \t\r\n`'\"")

    filtered = _prefilter_by_python_version(packages, py_version, platform_tags)
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
        _logger.debug("依赖解析缓存命中，跳过 pip 调用")
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

    _logger.debug("下载依赖 wheel: %s（镜像 %s）", " ".join(filtered), pypi_index)
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


def _prefilter_by_python_version(
    packages: tuple[str, ...] | list[str],
    py_version: str,
    platform_tags: Sequence[str] = (),
) -> list[str]:
    """按目标版本与目标平台的 marker 求值结果过滤依赖，返回保留的包列表."""
    filtered = _filter_by_python_version(packages, py_version, platform_tags)
    if len(filtered) < len(packages):
        _logger.info("按 python_version 标记过滤: 保留 %d，跳过 %d 个", len(filtered), len(packages) - len(filtered))
    if not filtered:
        _logger.info("所有依赖被 python_version 标记过滤，跳过下载")
    return filtered
