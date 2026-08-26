"""``fsp doctor --bench`` 基准剖析日志：聚合落盘与历史对比.

与 ``fsp b -P`` / ``fsp r -P`` 对齐：``fsp d --bench -P`` 将一次基准运行
聚合为单个剖析日志（schema ``fspack/doctor-bench-profile/1``）写入
``<当前目录>/.benchmarks/fsp-d-<时间戳>.json``——``stages`` 为各模板构建
耗时（附产物大小/入口数/启动耗时扩展字段，对比渲染只读 ``elapsed``，
扩展字段供事后分析），``wall_time`` 为本次基准总墙钟；失败构建单列
``failures`` 字段（中断样本不是有效性能数据，不混入阶段统计）。

``-PC`` 复用 :func:`fspack.packaging.profile_log.compare_with_baseline`
与历史 ``fsp-d-*`` 日志对比（不带值趋势表 / ``last`` / 近 N 次 / 基准
路径），趋势表按模板阶段中位数定位异常模板。

匿名机器信息（``platform.node()`` 的 MD5 前 8 位）写入日志 ``machine``
字段：多台机器的历史落在同一目录时可事后区分，且不可逆推真实机器名。
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import struct
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fspack import __version__
from fspack.console import console
from fspack.doctor.models import TemplateBuildResult
from fspack.packaging.profile_log import (
    DOCTOR_LOG_KIND,
    DOCTOR_LOG_PREFIX,
    DOCTOR_PROFILE_LOG_SCHEMA,
    ProfileOptions,
)

__all__ = [
    "_bench_profile_log_data",
    "_collect_machine_info",
    "_machine_id",
    "_save_and_compare_bench",
]

_logger = logging.getLogger(__name__)


def _machine_id() -> str:
    """生成匿名机器代号，基于机器名哈希前 8 位，不可逆推真实机器名.

    用 ``platform.node()`` 的 MD5 前缀作为确定性编码，同一机器每次运行
    返回相同值，便于历史对比时确认同一机器。``platform.node()`` 为空时
    回退到 ``uuid.getnode()``（MAC 地址哈希化），避免空值导致碰撞。
    """
    raw = platform.node() or str(uuid.getnode())
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def _collect_machine_info() -> dict[str, Any]:
    """收集机器性能配置信息（匿名化），便于分析性能基线.

    :return: 含 ``node_id``（匿名编码）、``system``、``python_version``、
        ``cpu``（brand/count/arch/bits）的字典，不含个人隐私信息。
    """
    return {
        "node_id": _machine_id(),
        "system": platform.system(),
        "python_version": sys.version.split()[0],
        "cpu": {
            "brand": platform.processor(),
            "count": os.cpu_count() or 0,
            "arch": platform.machine(),
            "bits": struct.calcsize("P") * 8,
        },
    }


def _bench_profile_log_data(results: list[TemplateBuildResult], wall_time: float) -> dict[str, Any]:
    """将一次基准运行聚合为剖析日志 dict（schema ``fspack/doctor-bench-profile/1``）.

    ``stages`` 仅含构建成功的模板（失败构建的耗时是中断样本，混入会污染
    趋势中位数；单列 ``failures`` 字段记录模板与错误）。环境字段沿用
    ``profile_log`` 体系约定：``project.version`` 为 fspack 版本——跨版本
    历史在对比时会被标注环境不一致且不参与统计基准。

    :param results: 本次基准全部模板构建结果
    :param wall_time: 本次基准总墙钟耗时（秒，含运行验证）
    :return: 可 JSON 持久化的日志 dict
    """
    stages: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for r in results:
        if not r.success:
            failures.append({"template_id": r.template_id, "error": r.error})
            continue
        stages.append(
            {
                "name": r.template_id,
                "elapsed": round(r.duration_sec, 4),
                "dist_size": r.dist_size,
                "entry_count": r.entry_count,
                "run_duration_sec": round(r.run_result.duration_sec, 4) if r.run_result else None,
            }
        )
    log: dict[str, Any] = {
        "schema": DOCTOR_PROFILE_LOG_SCHEMA,
        "created": datetime.now().isoformat(timespec="seconds"),
        "project": {"name": "doctor-bench", "version": __version__},
        "python": sys.version.split()[0],
        "platform": platform.system().lower(),
        "machine": _collect_machine_info(),
        "wall_time": round(wall_time, 4),
        "stages": stages,
    }
    if failures:
        log["failures"] = failures
    return log


def _save_and_compare_bench(results: list[TemplateBuildResult], wall_time: float, opts: ProfileOptions) -> None:
    """落盘聚合基准剖析日志并按需渲染历史对比（``-P``/``-PO``/``-PC``）.

    与构建侧 ``_save_and_compare_profile``、运行侧 ``_save_and_compare_run_profile``
    对称：落盘默认目录 ``<当前目录>/.benchmarks/``（前缀 ``fsp-d-`` 与构建
    ``fsp-b-``、启动剖析 ``fsp-r-`` 区分）；对比逻辑复用
    :func:`fspack.packaging.profile_log.compare_with_baseline`（日志类别
    :data:`DOCTOR_LOG_KIND`：前缀过滤 + 50ms 阶段显著阈值 + 中文文案标签）。

    :param results: 本次基准全部模板构建结果
    :param wall_time: 本次基准总墙钟耗时（秒）
    :param opts: 剖析选项（``out`` 输出路径 / ``compare`` 对比目标）
    """
    # 延迟导入：profile_log 的对比渲染链触发 rich 加载，仅在 -P 基准时执行
    from fspack.packaging.profile_log import DEFAULT_LOG_DIR, compare_with_baseline, save_profile_log

    default_dir = Path.cwd() / DEFAULT_LOG_DIR
    data = _bench_profile_log_data(results, wall_time)
    log_path = save_profile_log(data, opts.out or default_dir, prefix=DOCTOR_LOG_PREFIX)
    _logger.info("基准剖析日志已写入: %s", log_path)
    console.rich.print(f"[dim]基准剖析日志已保存: {log_path.name}[/dim]")
    compare_with_baseline(log_path, default_dir, opts.compare, kind=DOCTOR_LOG_KIND)
