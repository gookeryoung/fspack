"""``fsp doctor --bench`` 基准历史持久化与横向对比.

将 :class:`fspack.doctor.models.TemplateBuildResult` 列表序列化为 JSON
保存到 ``.benchmarks/doctor/{group}/{timestamp}.json``，按机器 + Python 版本
分组（``{System}-CPython-{major}.{minor}-{bits}bit-doctor``）。下次运行
``--bench`` 时自动加载上一次历史并打印横向对比表（构建耗时/启动耗时/产物
大小变化），变慢/变大红色、变快/变小绿色、持平灰色。

机器代号用 ``platform.node()`` 的 MD5 前 8 位，匿名化不可逆推真实机器名。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import struct
import sys
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fspack._util.fsutil import atomic_write_text
from fspack.console import console
from fspack.doctor.envs import _format_size
from fspack.doctor.models import TemplateBuildResult, TemplateRunResult

__all__ = [
    "_bench_history_group_dir",
    "_collect_machine_info",
    "_deserialize_bench_results",
    "_format_bench_delta",
    "_load_previous_bench_history",
    "_machine_id",
    "_print_bench_comparison",
    "_save_and_compare_bench",
    "_save_bench_history",
    "_serialize_bench_results",
]


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


def _bench_history_group_dir(base: Path) -> Path:
    """返回当前机器与 Python 版本对应的基准历史分组目录.

    按 ``{System}-CPython-{major}.{minor}-{bits}bit-doctor`` 分组，与
    pytest-benchmark 的 ``{System}-CPython-{ver}-{bits}bit`` 目录区分
    （``-doctor`` 后缀），避免互相干扰。

    :param base: 基准根目录（如 ``<project>/.benchmarks``）
    :return: 分组目录路径（如 ``<base>/Windows-CPython-3.11-64bit-doctor``）
    """
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    bits = struct.calcsize("P") * 8
    return base / f"{platform.system()}-CPython-{py_ver}-{bits}bit-doctor"


def _serialize_bench_results(results: list[TemplateBuildResult]) -> dict[str, Any]:
    """序列化构建结果为可 JSON 持久化的字典.

    :return: 含 ``timestamp``/``machine``/``results`` 三段的字典，
        ``results`` 每项含 ``template_id``/``success``/``duration_sec``/
        ``dist_size``/``entry_count``/``error``/``run_success``/``run_exit_code``/
        ``run_duration_sec``（应用调用响应速度）。
    """
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "machine": _collect_machine_info(),
        "results": [
            {
                "template_id": r.template_id,
                "success": r.success,
                "duration_sec": r.duration_sec,
                "dist_size": r.dist_size,
                "entry_count": r.entry_count,
                "error": r.error,
                "run_success": r.run_result.success if r.run_result else None,
                "run_exit_code": r.run_result.exit_code if r.run_result else None,
                "run_duration_sec": r.run_result.duration_sec if r.run_result else None,
            }
            for r in results
        ],
    }


def _deserialize_bench_results(data: dict[str, Any]) -> tuple[list[TemplateBuildResult], str]:
    """从 JSON 字典反序列化构建结果.

    :param data: :func:`_serialize_bench_results` 产出的字典
    :return: ``(results, timestamp)``，``timestamp`` 为 ISO 格式时间字符串
    """
    results: list[TemplateBuildResult] = []
    for item in data.get("results", []):
        run_result: TemplateRunResult | None = None
        if item.get("run_success") is not None:
            run_result = TemplateRunResult(
                success=item["run_success"],
                timed_out=False,
                exit_code=item.get("run_exit_code", -1),
                duration_sec=item.get("run_duration_sec", 0.0) or 0.0,
            )
        results.append(
            TemplateBuildResult(
                template_id=item["template_id"],
                success=item["success"],
                duration_sec=item["duration_sec"],
                dist_size=item.get("dist_size", 0),
                entry_count=item.get("entry_count", 0),
                error=item.get("error", ""),
                run_result=run_result,
            )
        )
    return results, data.get("timestamp", "")


def _save_bench_history(results: list[TemplateBuildResult], dir: Path) -> Path:
    """保存基准结果到历史目录，返回保存的文件路径.

    文件名 ``{YYYYMMDDTHHMMSS}.json``，按时间戳排序。``dir`` 不存在时自动创建。
    """
    dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    data = _serialize_bench_results(results)
    path = dir / f"{timestamp}.json"
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    return path


def _load_previous_bench_history(
    dir: Path, *, exclude: Path | None = None
) -> tuple[list[TemplateBuildResult], str] | None:
    """加载上一次基准结果.

    按文件名降序扫描 ``dir`` 下 ``*.json``，返回第一个有效的历史文件。
    ``exclude`` 指定的文件跳过（用于排除刚保存的当前结果）。

    :return: ``(results, timestamp)`` 或 ``None``（无历史或全部损坏）
    """
    if not dir.is_dir():
        return None
    for f in sorted(dir.glob("*.json"), reverse=True):
        if exclude and f.samefile(exclude):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue  # pragma: no cover - 跳过损坏/非 UTF-8 文件
        try:
            return _deserialize_bench_results(data)
        except (KeyError, TypeError, ValueError):
            continue  # pragma: no cover - 跳过格式错误文件
    return None


def _format_bench_delta(
    current: float,
    previous: float,
    fmt_abs: Callable[[float], str],
) -> str:
    """格式化基准变化值为 rich 标记字符串.

    :param current: 当前值
    :param previous: 上次值
    :param fmt_abs: 绝对值格式化函数（如 ``lambda v: f"{v:.1f}s"`` 或 ``_format_size``）
    :return: rich 标记字符串——变慢红色、变快绿色、持平灰色、无历史灰色 ``--``

    持平阈值：耗时 ``0.05s`` 以内、大小 ``10B`` 以内视为 ``=``（由调用方确保
    ``fmt_abs`` 的精度隐含的阈值合理，本函数统一用 ``abs(delta) < 0.01`` 数值阈值
    避免浮点噪声，大小类调用方传入字节值时 ``0.01`` 远小于 10B 仍有效）。
    """
    if previous <= 0:
        return "[dim]--[/dim]"
    delta = current - previous
    if abs(delta) < 0.01:
        return "[dim]=[/dim]"
    pct = delta / previous * 100
    sign = "+" if delta > 0 else "-"
    color = "red" if delta > 0 else "green"
    return f"[{color}]{sign}{fmt_abs(abs(delta))} ({sign}{abs(pct):.1f}%)[/{color}]"


def _print_bench_comparison(
    current: list[TemplateBuildResult],
    previous: list[TemplateBuildResult],
    prev_label: str,
) -> None:
    """打印当前基准与历史基准的横向对比表.

    仅对比构建成功的模板，按 ``template_id`` 匹配。构建耗时变化、启动耗时变化
    （应用调用响应速度）与产物大小变化以 rich 标记着色：变慢/变大红色、
    变快/变小绿色、持平灰色、无历史灰色 ``--``。

    :param current: 当前基准结果
    :param previous: 上次基准结果
    :param prev_label: 上次基准的时间标签（用于标题显示）
    """
    from rich.table import Table

    prev_by_id = {r.template_id: r for r in previous if r.success}
    ok_current = [r for r in current if r.success]
    if not ok_current:
        return

    console.rich.print()
    console.step(f"性能对比（与 {prev_label} 基准）")

    table = Table(title="横向对比", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("模板", style="cyan")
    table.add_column("本次构建", justify="right")
    table.add_column("上次构建", justify="right")
    table.add_column("构建变化", justify="right")
    table.add_column("本次启动", justify="right")
    table.add_column("上次启动", justify="right")
    table.add_column("启动变化", justify="right")
    table.add_column("本次大小", justify="right")
    table.add_column("大小变化", justify="right")

    for i, r in enumerate(ok_current, 1):
        prev = prev_by_id.get(r.template_id)
        cur_run = r.run_result.duration_sec if r.run_result else 0.0
        if prev:
            prev_time_str = f"{prev.duration_sec:.1f}s"
            time_delta = _format_bench_delta(r.duration_sec, prev.duration_sec, lambda v: f"{v:.1f}s")
            prev_run = prev.run_result.duration_sec if prev.run_result else 0.0
            prev_run_str = f"{prev_run:.2f}s" if prev_run > 0 else "-"
            run_delta = _format_bench_delta(cur_run, prev_run, lambda v: f"{v:.2f}s")
            size_delta = _format_bench_delta(r.dist_size, prev.dist_size, lambda v: _format_size(int(v)))
        else:
            prev_time_str = "-"
            time_delta = "[dim]--[/dim]"
            prev_run_str = "-"
            run_delta = "[dim]--[/dim]"
            size_delta = "[dim]--[/dim]"

        table.add_row(
            str(i),
            r.template_id,
            f"{r.duration_sec:.1f}s",
            prev_time_str,
            time_delta,
            f"{cur_run:.2f}s" if cur_run > 0 else "-",
            prev_run_str,
            run_delta,
            _format_size(r.dist_size) if r.dist_size else "-",
            size_delta,
        )

    console.rich.print(table)


def _save_and_compare_bench(results: list[TemplateBuildResult]) -> None:
    """保存当前基准并与历史横向对比.

    1. 加载上一次历史基准（保存当前结果之前加载，避免把当前当作历史）。
    2. 保存当前结果到 ``.benchmarks/doctor/{group}/{timestamp}.json``。
    3. 如有历史，打印横向对比表；无历史则提示本次为首次基准。

    :param results: 本次基准构建结果
    """
    base = Path.cwd() / ".benchmarks"
    group_dir = _bench_history_group_dir(base)

    # 先加载历史（保存当前结果之前），避免把当前结果当作历史
    previous = _load_previous_bench_history(group_dir)

    # 保存当前结果
    saved_path = _save_bench_history(results, group_dir)
    console.rich.print(f"\n[dim]基准已保存: {saved_path.name}[/dim]")

    # 打印对比
    if previous:
        prev_results, prev_ts = previous
        _print_bench_comparison(results, prev_results, prev_ts)
    else:
        console.rich.print("\n[dim]无历史基准，本次结果将作为首次基准[/dim]")
