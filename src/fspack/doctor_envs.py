"""``fsp doctor`` 环境信息检查.

检查 Python 版本/路径、目标平台、fspack 版本、镜像源配置、缓存目录大小，
返回 :class:`fspack.doctor_models.CheckResult`。同时提供 :func:`_dir_size`
与 :func:`_format_size` 工具函数，供 :mod:`fspack.doctor_templates`/
:mod:`fspack.doctor_bench` 复用（递归目录大小与人类可读字节数格式化）。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from fspack import __version__
from fspack.doctor_models import CheckResult, CheckStatus

if TYPE_CHECKING:
    from fspack.platform import Platform

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
    """扫描 wheel 缓存目录的依赖解析缓存文件，报告损坏数量并删除损坏文件.

    iter-128 引入：``fsp doctor --check-cache`` 调用。

    遍历 ``.deps-*.json`` 文件，逐个校验 JSON 结构（根对象是 dict、wheels 字段是
    list）。损坏文件自动删除（与 :func:`fspack.packaging.wheels.cache._load_deps_cache`
    行为一致），避免下次构建重复告警。

    注意：只校验缓存文件本身的结构完整性，不检查 wheel 文件是否存在（wheel 缺失
    属于正常缓存失效，会在构建时重新解析，不算损坏）。

    Args:
        cache_dir: wheel 缓存目录（通常是 :func:`fspack.config.cache.wheel_cache_dir`）。

    :return: OK（无损坏或无缓存文件）/ WARN（有损坏文件已删除）
    """
    if not cache_dir.is_dir():
        return CheckResult(
            name="缓存完整性",
            status=CheckStatus.OK,
            detail=f"{cache_dir}（缓存目录不存在）",
        )

    cache_files = sorted(cache_dir.glob(".deps-*.json"))
    if not cache_files:
        return CheckResult(
            name="缓存完整性",
            status=CheckStatus.OK,
            detail="无依赖解析缓存文件",
        )

    corrupt_names: list[str] = []
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
                # 删除失败不阻断诊断，仍计入 corrupt 计数让用户知晓
                f.unlink()
        except OSError:
            # 文件系统层错误（权限/磁盘 I/O）：不计为损坏，跳过
            # （与 _load_deps_cache 一致：OSError 可能是瞬时问题，不删除）
            pass

    valid_count = len(cache_files) - len(corrupt_names)
    if not corrupt_names:
        return CheckResult(
            name="缓存完整性",
            status=CheckStatus.OK,
            detail=f"扫描 {len(cache_files)} 个缓存文件，全部有效",
        )
    # 列出前 3 个损坏文件名，避免详情过长
    preview = ", ".join(corrupt_names[:3])
    if len(corrupt_names) > 3:
        preview += f" 等 {len(corrupt_names)} 个"
    return CheckResult(
        name="缓存完整性",
        status=CheckStatus.WARN,
        detail=f"扫描 {len(cache_files)} 个缓存文件，{valid_count} 有效，{len(corrupt_names)} 损坏已删除（{preview}）",
        suggestion="损坏的缓存文件已自动删除，下次构建将重新解析依赖",
    )
