"""``fsp doctor`` 环境信息检查.

检查 Python 版本/路径、目标平台、fspack 版本、镜像源配置、缓存目录大小，
返回 :class:`fspack.doctor.models.CheckResult`。同时提供 :func:`_dir_size`
与 :func:`_format_size` 工具函数，供 :mod:`fspack.doctor.templates`/
:mod:`fspack.doctor.bench` 复用（递归目录大小与人类可读字节数格式化）。

缓存健康扫描/清理引擎已拆分至 :mod:`fspack.doctor.cache_health`（多 cache
类型扫描器与分发器）、归档完整性检测原语拆分至 :mod:`fspack.doctor.integrity`；
:func:`_check_cache_integrity` 作为诊断项仍留在本模块，底层委托
:func:`fspack.doctor.cache_health._scan_cache_health`。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from fspack import __version__
from fspack._util.format import format_size_bin
from fspack._util.fsutil import walk_dir_size
from fspack.doctor.cache_health import _scan_cache_health
from fspack.doctor.models import CheckResult, CheckStatus

if TYPE_CHECKING:
    from fspack.platform import Platform

_logger = logging.getLogger(__name__)

__all__ = [
    "_check_cache_dir",
    "_check_cache_integrity",
    "_check_fspack_version",
    "_check_mirror_config",
    "_check_platform_info",
    "_check_python",
    "_dir_size",
    "_format_size",
]


def _check_python() -> CheckResult:
    """检查当前 Python 解释器版本与路径."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return CheckResult(
        name="Python",
        status=CheckStatus.OK,
        detail=f"{version} ({sys.executable})",
    )


def _check_platform_info(platform: Platform) -> CheckResult:
    """检查目标平台标识."""
    return CheckResult(
        name="平台",
        status=CheckStatus.OK,
        detail=platform.value,
    )


def _check_fspack_version() -> CheckResult:
    """检查 fspack 自身版本."""
    return CheckResult(
        name="fspack",
        status=CheckStatus.OK,
        detail=__version__,
    )


def _check_mirror_config(default_mirror: str, mirrors: Mapping[str, object]) -> CheckResult:
    """检查镜像源配置：默认镜像名 + 可用镜像列表."""
    available = ", ".join(mirrors.keys())
    detail = f"默认={default_mirror}；可用={available}"
    return CheckResult(
        name="镜像源",
        status=CheckStatus.OK,
        detail=detail,
    )


def _check_cache_dir(cache_root: Path) -> CheckResult:
    """检查缓存目录：路径 + 总大小（递归扫描）.

    目录不存在视为 OK（首次使用尚未下载缓存），大小显示 0 B。
    """
    if not cache_root.exists():
        return CheckResult(
            name="缓存目录",
            status=CheckStatus.OK,
            detail=f"{cache_root}（尚未创建）",
        )
    try:
        size_bytes = _dir_size(cache_root)
    except OSError as exc:
        return CheckResult(
            name="缓存目录",
            status=CheckStatus.WARN,
            detail=f"{cache_root}",
            suggestion=f"扫描缓存目录失败: {exc}（不影响打包，仅诊断信息缺失）",
        )
    return CheckResult(
        name="缓存目录",
        status=CheckStatus.OK,
        detail=f"{cache_root}（{_format_size(size_bytes)}）",
    )


def _dir_size(path: Path) -> int:
    """递归计算目录总字节数（不含符号链接循环）.

    实现搬迁至 :func:`fspack._util.fsutil.walk_dir_size`，此处保留同名薄封装
    维持 ``fspack.doctor.envs._dir_size`` 引用兼容。
    """
    return walk_dir_size(path)


def _format_size(size_bytes: int) -> str:
    """字节数格式化为人类可读（如 ``"123.4 MiB"``）.

    实现搬迁至 :func:`fspack._util.format.format_size_bin`，此处保留同名薄封装
    维持 ``fspack.doctor.envs._format_size`` 引用兼容。
    """
    return format_size_bin(size_bytes)


def _check_cache_integrity(cache_dir: Path) -> CheckResult:
    """扫描 wheel 缓存目录，报告损坏/stale/orphan 概要（只读诊断，不删除文件）.

    iter-128 引入：``fsp doctor --check-cache`` 调用。
    iter-139 扩展：复用 :func:`fspack.doctor.cache_health._scan_cache_health`
    的扫描结果，详情中追加 stale deps（引用缺失 wheel）与 orphan wheels
    （未被任何 deps 引用）计数。

    诊断阶段不删除任何文件（损坏/stale/orphan 均仅报告），统一提示用户用
    ``fsp cache clean`` 清理，避免只读诊断产生删除副作用。

    Args:
        cache_dir: wheel 缓存目录（通常是 :func:`fspack.config.cache.wheel_cache_dir`）。

    :return: OK（无任何问题）/ WARN（有损坏/stale/orphan）
    """
    report = _scan_cache_health(cache_dir)

    if not cache_dir.is_dir():
        return CheckResult(
            name="缓存完整性",
            status=CheckStatus.OK,
            detail=f"{cache_dir}（缓存目录不存在）",
        )

    if report.total_deps_files == 0 and report.total_wheels == 0:
        return CheckResult(
            name="缓存完整性",
            status=CheckStatus.OK,
            detail="无依赖解析缓存文件与 wheel 文件",
        )

    parts: list[str] = []
    if report.total_deps_files > 0:
        valid_count = report.total_deps_files - len(report.corrupt_deps_files) - len(report.stale_deps_files)
        parts.append(f"{report.total_deps_files} 个 deps 缓存（{valid_count} 有效")
        if report.corrupt_deps_files:
            parts[-1] += f"，{len(report.corrupt_deps_files)} 损坏"
        if report.stale_deps_files:
            parts[-1] += f"，{len(report.stale_deps_files)} stale 引用缺失 wheel"
        parts[-1] += "）"
    if report.total_wheels > 0:
        parts.append(f"{report.total_wheels} 个 wheel")
        if report.orphan_wheels:
            parts[-1] += f"（{len(report.orphan_wheels)} 孤儿，{_format_size(report.orphan_size_bytes)}）"

    detail = "扫描 " + "，".join(parts)

    if not report.has_issues:
        return CheckResult(name="缓存完整性", status=CheckStatus.OK, detail=detail)

    suggestion_parts: list[str] = []
    if report.corrupt_deps_files:
        suggestion_parts.append(f"{len(report.corrupt_deps_files)} 个损坏 deps 待清理")
    if report.stale_deps_files or report.orphan_wheels:
        suggestion_parts.append(
            f"运行 `fsp cache clean` 清理 {len(report.stale_deps_files)} stale deps + {len(report.orphan_wheels)} 孤儿 wheel"
        )
    return CheckResult(
        name="缓存完整性",
        status=CheckStatus.WARN,
        detail=detail,
        suggestion="；".join(suggestion_parts),
    )
