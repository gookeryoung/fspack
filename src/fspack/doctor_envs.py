"""``fsp doctor`` 环境信息检查.

检查 Python 版本/路径、目标平台、fspack 版本、镜像源配置、缓存目录大小，
返回 :class:`fspack.doctor_models.CheckResult`。同时提供 :func:`_dir_size`
与 :func:`_format_size` 工具函数，供 :mod:`fspack.doctor_templates`/
:mod:`fspack.doctor_bench` 复用（递归目录大小与人类可读字节数格式化）。

iter-139 扩展：:func:`_scan_cache_health` 全面扫描 wheel 缓存目录健康状态
（损坏/stale/orphan），:func:`_clean_cache_issues` 提供清理能力，供
``fsp cache status``/``fsp cache clean`` 子命令复用。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from fspack import __version__
from fspack.doctor_models import CacheHealthReport, CheckResult, CheckStatus

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
    "_clean_cache_issues",
    "_dir_size",
    "_format_size",
    "_scan_cache_health",
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
    """递归计算目录总字节数（不含符号链接循环）."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _format_size(size_bytes: int) -> str:
    """字节数格式化为人类可读（如 ``"123.4 MiB"``）."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ("KiB", "MiB", "GiB", "TiB")
    size = float(size_bytes) / 1024
    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def _check_cache_integrity(cache_dir: Path) -> CheckResult:
    """扫描 wheel 缓存目录，报告损坏/stale/orphan 概要并删除损坏文件.

    iter-128 引入：``fsp doctor --check-cache`` 调用。
    iter-139 扩展：复用 :func:`_scan_cache_health` 的扫描结果，详情中追加
    stale deps（引用缺失 wheel）与 orphan wheels（未被任何 deps 引用）计数。

    损坏文件（JSON 解析失败/结构非法）自动删除（与
    :func:`fspack.packaging.wheels.cache._load_deps_cache` 行为一致）；
    stale deps 与 orphan wheels 不在诊断阶段删除，仅提示用户用
    ``fsp cache clean`` 清理。

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
            parts[-1] += f"，{len(report.corrupt_deps_files)} 损坏已删除"
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
        suggestion_parts.append("损坏 deps 已自动删除")
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


def _scan_cache_health(cache_dir: Path) -> CacheHealthReport:
    """扫描 wheel 缓存目录健康状态，返回 :class:`CacheHealthReport`.

    iter-139 引入：``fsp doctor --check-cache``/``fsp cache status``/``fsp cache clean``
    共用的扫描入口，避免重复扫描。

    扫描规则：

    - ``.deps-*.json`` 文件：JSON 结构校验（根对象 dict、wheels 字段 list）。
      损坏文件立即删除（best-effort，删除失败不影响扫描继续），记录到
      ``corrupt_deps_files``。
    - 有效 deps 文件中 ``wheels`` 列表指向的 wheel 文件名聚合为 ``referenced`` 集合。
      若引用的 wheel 不在 cache_dir 中，该 deps 文件记入 ``stale_deps_files``，
      缺失的 wheel 名记入 ``missing_wheels``（不删除 deps 文件，由 ``fsp cache clean`` 处理）。
    - cache_dir 下的 ``*.whl`` 文件聚合为 ``existing`` 集合，未出现在任何 deps
      引用集合中的记入 ``orphan_wheels``，并累加 ``orphan_size_bytes``。

    ``OSError``（权限/磁盘 I/O）不计为损坏也不删除：可能是瞬时问题，与
    :func:`fspack.packaging.wheels.cache._load_deps_cache` 行为一致。

    Args:
        cache_dir: wheel 缓存目录。

    :return: :class:`CacheHealthReport`，cache_dir 不存在时返回空报告
        （total_deps_files/total_wheels 均为 0）。
    """
    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir)

    cache_files = sorted(cache_dir.glob(".deps-*.json"))
    corrupt_names: list[str] = []
    stale_names: list[str] = []
    missing_wheels: list[str] = []
    referenced: set[str] = set()

    for f in cache_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"根对象不是 dict: {type(data).__name__}")
            names = data.get("wheels", [])
            if not isinstance(names, list):
                raise ValueError(f"wheels 字段不是 list: {type(names).__name__}")
        except (json.JSONDecodeError, ValueError):
            corrupt_names.append(f.name)
            with contextlib.suppress(OSError):
                # 删除失败不阻断扫描，仍计入 corrupt 计数让用户知晓
                f.unlink()
            continue
        except OSError:
            # 文件系统层错误（权限/磁盘 I/O）：不计为损坏，跳过
            # （与 _load_deps_cache 一致：OSError 可能是瞬时问题，不删除）
            continue

        # 有效 deps 文件：检查引用的 wheel 是否存在
        wheel_names = [n for n in names if isinstance(n, str)]
        referenced.update(wheel_names)
        missing = [n for n in wheel_names if not (cache_dir / n).is_file()]
        if missing:
            stale_names.append(f.name)
            missing_wheels.extend(missing)

    # 枚举现有 wheel 文件（仅顶层目录，与 _save_deps_cache 写入位置一致）
    existing_wheels = sorted(p.name for p in cache_dir.glob("*.whl"))
    existing_set = set(existing_wheels)
    orphan_names = sorted(existing_set - referenced)
    orphan_size = 0
    for name in orphan_names:
        try:
            orphan_size += (cache_dir / name).stat().st_size
        except OSError:
            # 文件在枚举后被删除（竞态）：不计入体积但仍视为孤儿
            continue

    return CacheHealthReport(
        cache_dir=cache_dir,
        total_deps_files=len(cache_files),
        corrupt_deps_files=tuple(corrupt_names),
        stale_deps_files=tuple(stale_names),
        missing_wheels=tuple(dict.fromkeys(missing_wheels)),
        orphan_wheels=tuple(orphan_names),
        total_wheels=len(existing_wheels),
        orphan_size_bytes=orphan_size,
    )


def _clean_cache_issues(cache_dir: Path, *, dry_run: bool = False) -> CacheHealthReport:
    """清理 wheel 缓存目录中的 stale deps 与 orphan wheels，返回清理后的扫描报告.

    iter-139 引入：``fsp cache clean`` 调用。

    清理规则：

    - 重新扫描（确保使用最新状态，避免清理期间被外部修改的文件误删）。
    - 删除 ``stale_deps_files``（引用缺失 wheel 的 ``.deps-*.json``）：deps 文件
      指向的 wheel 已不在 cache_dir，下次构建会重新解析依赖，删除安全。
    - 删除 ``orphan_wheels``（未被任何 deps 引用的 ``*.whl``）：可能来自历史
      项目已删除/依赖变更。``dry_run=True`` 时仅扫描不删除，输出待删除列表。

    损坏的 ``.deps-*.json`` 在 :func:`_scan_cache_health` 阶段已删除，本函数
    返回的报告中 ``corrupt_deps_files`` 通常为空（除非扫描后又新增损坏文件，
    极罕见，仍按报告原样返回）。

    删除失败 best-effort：单个文件 ``OSError`` 不阻断其他文件清理，仅 warning 日志。
    仍返回扫描报告（用户可看到实际删除了哪些、哪些失败）。

    Args:
        cache_dir: wheel 缓存目录。
        :param dry_run: True 时仅扫描不删除，用于 ``fsp cache clean --dry-run`` 预览。

    :return: 清理前的 :class:`CacheHealthReport`（含本次扫描发现的所有问题）。
        调用方可基于 ``corrupt_deps_files``/``stale_deps_files``/``orphan_wheels``
        字段统计本次清理量。
    """
    report = _scan_cache_health(cache_dir)

    if dry_run or not report.has_issues:
        return report

    # 删除 stale deps 文件（引用缺失 wheel，下次构建重新解析）
    for name in report.stale_deps_files:
        target = cache_dir / name
        try:
            target.unlink()
        except OSError as e:
            _logger.warning("清理 stale deps 文件失败: %s: %s", target, e)

    # 删除 orphan wheel 文件（未被任何 deps 引用）
    for name in report.orphan_wheels:
        target = cache_dir / name
        try:
            target.unlink()
        except OSError as e:
            _logger.warning("清理孤儿 wheel 文件失败: %s: %s", target, e)

    return report
