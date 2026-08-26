"""性能日志：落盘、加载与历史对比（构建剖析与启动剖析共用）.

``fsp b --profile`` 构建结束后将 :class:`ProfileReport` 连同元数据写入
JSON 日志（默认 ``<项目>/.benchmarks/fsp-b-<时间戳>.json``，``--profile-out``
可指定目录或文件），``--profile-compare`` 与历史日志对比：不带值输出历次
趋势表（近 :data:`_TREND_LIMIT` 次历史 + 本次，统计基准为环境一致历史的
中位数，抗单次抖动），``last`` 与最近一次对比，正整数取近 N 次，或指定
基准 JSON 文件路径两两对比。

``fsp r --profile`` 启动剖析同样落盘（``fsp-r-<时间戳>.json``，schema
``fspack/run-profile/1``，由 :func:`save_profile_log` 写入完整 dict），
与构建日志同目录共存、按前缀区分，对比渲染共用同一张差异表。

``fsp d --bench -P`` 基准剖析把一次 doctor 基准运行聚合为单个日志
（``fsp-d-<时间戳>.json``，schema ``fspack/doctor-bench-profile/1``，
``stages`` 为各模板构建耗时），与前两类日志同目录共存，``-PC`` 对比
同样走 :func:`compare_with_baseline`（日志类别 :data:`DOCTOR_LOG_KIND`）。

构建日志 JSON 结构（schema ``fspack/build-profile/1``）::

    {
      "schema": "fspack/build-profile/1",
      "created": "2026-08-24T16:30:05",
      "project": {"name": "app", "version": "1.0.0"},
      "python": "3.13.14",
      "platform": "windows",
      "wall_time": 3.08,
      "cpu_time": 1.62,
      "memory_peak": 95420416,
      "cpu_ratio": 0.528,
      "stages": [{"name": "...", "elapsed": 0.5, ...}]
    }

启动剖析日志（schema ``fspack/run-profile/1``）结构相同，差异为：无
``cpu_time``/``memory_peak``（对比表总览自适应跳过），另有 ``entry``/
``debug``/``returncode`` 字段（入口名/运行模式/退出码，对比时环境
不一致会注明），``stages`` 为启动阶段（loader 各阶段/环境准备/解释器
初始化/用户入口执行）。

公共 API：

- :class:`ProfileOptions` — 剖析开关与日志输出/对比选项（build/run 共用）
- :func:`save_profile_report` — 写入构建性能日志（目录自动命名 / 文件直写）
- :func:`save_profile_log` — 写入完整日志 dict（启动剖析用，前缀区分）
- :func:`load_profile_log` — 读取日志为 dict
- :func:`find_latest_log` — 目录内最新日志（排除指定文件，按前缀过滤）
- :func:`find_recent_logs` — 目录内最近 N 条日志（时间升序，趋势表数据源）
- :func:`print_profile_compare` — 渲染本次与基准的差异对比表
- :func:`print_profile_trend` — 渲染历次趋势表（明细 + 中位数统计 + 阶段偏离）
- :func:`compare_with_baseline` — 解析对比目标（趋势/``last``/路径）并渲染（两侧共用）
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # ProfileReport 仅用于类型注解（from __future__ import annotations 使
    # 注解不在运行时求值），顶部不导入 fspack.packaging.profile 避免连锁
    # 触发 fspack.console/rich 加载。
    from fspack.packaging.profile import ProfileReport

__all__ = [
    "BUILD_LOG_KIND",
    "DEFAULT_LOG_DIR",
    "DOCTOR_LOG_GLOB",
    "DOCTOR_LOG_KIND",
    "DOCTOR_LOG_PREFIX",
    "DOCTOR_PROFILE_LOG_SCHEMA",
    "PROFILE_LOG_SCHEMA",
    "RUN_LOG_GLOB",
    "RUN_LOG_KIND",
    "RUN_LOG_PREFIX",
    "RUN_PROFILE_LOG_SCHEMA",
    "LogKind",
    "ProfileLogMeta",
    "ProfileOptions",
    "compare_with_baseline",
    "find_latest_log",
    "find_recent_logs",
    "load_profile_log",
    "print_profile_compare",
    "print_profile_trend",
    "save_profile_log",
    "save_profile_report",
]

_logger = logging.getLogger(__name__)

# 日志 schema 版本：结构变更时递增，加载侧按版本校验兼容性
PROFILE_LOG_SCHEMA = "fspack/build-profile/1"
RUN_PROFILE_LOG_SCHEMA = "fspack/run-profile/1"
DOCTOR_PROFILE_LOG_SCHEMA = "fspack/doctor-bench-profile/1"
# 加载侧接受的 schema 集合（对比前另行校验双方 schema 一致，防跨类型对比）
_KNOWN_SCHEMAS = frozenset({PROFILE_LOG_SCHEMA, RUN_PROFILE_LOG_SCHEMA, DOCTOR_PROFILE_LOG_SCHEMA})
# 默认日志目录名（项目根下，与 pytest-benchmark 共存，文件名前缀区分）
DEFAULT_LOG_DIR = ".benchmarks"
# 日志文件名前缀与通配（fsp-b-/fsp-r-/fsp-d-YYYYMMDD-HHMMSS.json，构建/
# 启动剖析/doctor 基准剖析共存）
_LOG_PREFIX = "fsp-b-"
_LOG_GLOB = "fsp-b-*.json"
RUN_LOG_PREFIX = "fsp-r-"
RUN_LOG_GLOB = "fsp-r-*.json"
DOCTOR_LOG_PREFIX = "fsp-d-"
DOCTOR_LOG_GLOB = "fsp-d-*.json"
# 阶段差异显著阈值：绝对差超 50ms 且相对差超 10% 才列入对比表，
# 其余折叠为计数行，避免噪声淹没真实回归
_STAGE_MIN_DELTA = 0.05
_STAGE_MIN_PCT = 10.0
# 启动剖析侧的阶段显著阈值：总时长常为几十毫秒，阈值比构建侧小一个量级
_RUN_STAGE_MIN_DELTA = 0.005
# 趋势表默认展示的历史条数（--profile-compare 不带值 / 传 trend 时）
_TREND_LIMIT = 15


@dataclass(frozen=True)
class ProfileLogMeta:
    """性能日志元数据：项目 / 解释器 / 平台，用于对比时的环境差异提示."""

    name: str
    version: str
    python: str
    platform: str


@dataclass(frozen=True)
class ProfileOptions:
    """性能剖析选项（``--profile``/``--profile-out``/``--profile-compare``）.

    build 与 run 两侧共用：``enabled`` 对应 ``--profile`` 开关；``out`` 为
    ``.json`` 文件时直写、目录时自动命名、``None`` 落默认目录；``compare``
    为 ``"trend"`` 时渲染历次趋势表（默认近 :data:`_TREND_LIMIT` 次）、
    正整数取近 N 次、``"last"`` 与最近一次对比，其他值按基准文件路径。
    ``repeat``（仅 run 侧生效）为多次运行统计次数（pytest-benchmark 风格，
    汇总取中位数样本）。frozen 不可变，默认值可安全共享。
    """

    enabled: bool = False
    out: Path | None = None
    compare: str | None = None
    repeat: int = 1


def _auto_name(directory: Path, prefix: str = _LOG_PREFIX) -> Path:
    """在目录内生成不冲突的日志文件路径（``<prefix><时间戳>.json``）.

    同秒内多次构建自动追加 ``-2``/``-3`` 序号，避免覆盖既有日志。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"{prefix}{stamp}.json"
    seq = 2
    while candidate.exists():
        candidate = directory / f"{prefix}{stamp}-{seq}.json"
        seq += 1
    return candidate


def save_profile_log(data: dict[str, Any], out: Path, prefix: str = _LOG_PREFIX) -> Path:
    """写入完整日志 dict 为 JSON，返回实际写入路径.

    :param data: 完整日志数据（调用方负责 schema/元数据/阶段字段）
    :param out: 输出路径——``.json`` 后缀按文件直写（父目录自动创建）；
        其余（目录或不存在路径）按目录处理，自动命名写入
    :param prefix: 目录模式下自动命名的文件名前缀（构建 ``fsp-b-`` /
        启动剖析 ``fsp-r-``）
    :return: 实际写入的文件路径
    """
    path = out if out.suffix == ".json" else _auto_name(out, prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_profile_report(report: ProfileReport, out: Path, meta: ProfileLogMeta) -> Path:
    """写入构建性能日志 JSON，返回实际写入路径.

    :param report: 构建耗时报告
    :param out: 输出路径——``.json`` 后缀按文件直写（父目录自动创建）；
        其余（目录或不存在路径）按目录处理，自动命名写入
    :param meta: 项目/解释器/平台元数据
    :return: 实际写入的文件路径
    """
    data: dict[str, Any] = {
        "schema": PROFILE_LOG_SCHEMA,
        "created": datetime.now().isoformat(timespec="seconds"),
        "project": {"name": meta.name, "version": meta.version},
        "python": meta.python,
        "platform": meta.platform,
        "wall_time": round(report.wall_time, 4),
        "cpu_time": round(report.cpu_time, 4),
        "memory_peak": report.memory_peak,
        "cpu_ratio": round(report.cpu_ratio, 4),
        "stages": [
            {
                "name": s.name,
                "elapsed": round(s.elapsed, 4),
                "bytes_downloaded": s.bytes_downloaded,
                "bytes_saved": s.bytes_saved,
                "cache_hit": s.cache_hit,
                "items": s.items,
                "skipped": s.skipped,
                "detail": s.detail,
            }
            for s in report.stages
        ],
    }
    return save_profile_log(data, out)


def load_profile_log(path: Path) -> dict[str, Any]:
    """读取性能日志 JSON 为 dict，文件不存在/畸形/schema 不符时抛 :class:`ValueError`.

    接受构建（``fspack/build-profile/1``）与启动剖析（``fspack/run-profile/1``）
    两种 schema；对比渲染前由 :func:`print_profile_compare` 另行校验双方
    schema 一致，防止跨类型对比产生无意义的全量新增/移除。

    :param path: 日志文件路径
    :raises ValueError: 文件不存在 / JSON 畸形 / schema 版本不识别
    """
    if not path.is_file():
        raise ValueError(f"性能日志不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"性能日志不是合法 JSON: {path} ({exc})") from exc
    schema = data.get("schema")
    if schema not in _KNOWN_SCHEMAS:
        raise ValueError(f"性能日志 schema 不受支持（{schema}，期望 {'/'.join(sorted(_KNOWN_SCHEMAS))}）: {path}")
    return data


def find_latest_log(directory: Path, exclude: Path | None = None, pattern: str = _LOG_GLOB) -> Path | None:
    """返回目录内最新的性能日志（排除 ``exclude``），无则返回 ``None``.

    文件名 ``<前缀>YYYYMMDD-HHMMSS[-N].json`` 的字典序即时间序，直接按名
    排序取最大，无需读文件内容。``pattern`` 按前缀过滤（构建 ``fsp-b-*`` /
    启动剖析 ``fsp-r-*``），两类日志同目录共存互不干扰。
    """
    if not directory.is_dir():
        return None
    logs = [p for p in directory.glob(pattern) if exclude is None or p.resolve() != exclude.resolve()]
    return max(logs, key=lambda p: p.name) if logs else None


def find_recent_logs(
    directory: Path,
    exclude: Path | None = None,
    pattern: str = _LOG_GLOB,
    limit: int = _TREND_LIMIT,
) -> list[Path]:
    """返回目录内最近的性能日志路径列表（时间升序，旧→新），趋势表数据源.

    与 :func:`find_latest_log` 同样的文件名字典序假设，排序后截取最近
    ``limit`` 条（不足则全部）。``limit <= 0`` 时不截断（测试与全量
    分析场景）。排除 ``exclude``（本次刚写入的日志，不作为历史）。
    """
    if not directory.is_dir():
        return []
    logs = [p for p in directory.glob(pattern) if exclude is None or p.resolve() != exclude.resolve()]
    logs.sort(key=lambda p: p.name)
    return logs[-limit:] if limit > 0 else logs


@dataclass(frozen=True)
class LogKind:
    """同类日志的对比配置：``last`` 候选通配 / 阶段显著阈值 / 警告文案标签.

    构建与启动剖析两类日志同目录共存，对比时按 :attr:`pattern` 前缀过滤
    同类日志；启动剖析总时长常为几十毫秒，显著阈值比构建侧小一个量级。
    """

    pattern: str
    stage_min_delta: float
    label: str


# 默认值是 frozen 不可变单例，可安全共享（B008/RUF009 豁免动机）
_DEFAULT_PROFILE = ProfileOptions()
# 构建性能日志类别（fsp-b-*.json，阶段显著阈值 50ms）
BUILD_LOG_KIND = LogKind(pattern=_LOG_GLOB, stage_min_delta=_STAGE_MIN_DELTA, label="性能")
# 启动剖析日志类别（fsp-r-*.json，阶段显著阈值 5ms）
RUN_LOG_KIND = LogKind(pattern=RUN_LOG_GLOB, stage_min_delta=_RUN_STAGE_MIN_DELTA, label="启动剖析")
# doctor 基准剖析日志类别（fsp-d-*.json，阶段=各模板构建耗时，秒级，
# 显著阈值与构建侧一致 50ms）
DOCTOR_LOG_KIND = LogKind(pattern=DOCTOR_LOG_GLOB, stage_min_delta=_STAGE_MIN_DELTA, label="基准剖析")


def compare_with_baseline(
    log_path: Path,
    default_dir: Path,
    compare: str | None,
    kind: LogKind = BUILD_LOG_KIND,
) -> None:
    """解析对比目标并与本次渲染（构建与启动剖析共用）.

    ``compare`` 四种语义：

    - ``"trend"``（``--profile-compare`` 不带值的哨兵）：渲染历次趋势表
      （近 :data:`_TREND_LIMIT` 次历史 + 本次 + 中位数统计 + 阶段偏离）
    - 正整数字符串（如 ``"5"``）：同趋势表，历史取近 N 次
    - ``"last"``：与最近一次同类日志两两对比（差异表）
    - 其他值：按基准文件路径加载并两两对比

    未指定时直接返回。日志缺失/畸形/schema 与本次不一致时警告并跳过
    对比，不中断构建或运行结果。

    :param log_path: 本次刚写入的日志路径（排除在历史候选外）
    :param default_dir: 默认日志目录（``<项目>/.benchmarks``）
    :param compare: ``--profile-compare`` 值（``"trend"``/``"last"``/数字/路径/``None``）
    :param kind: 日志类别（同类过滤通配 + 阶段显著阈值 + 警告文案标签）
    """
    if not compare:
        return
    if compare == "trend" or compare.isdigit():
        _print_trend_from_dir(log_path, default_dir, int(compare) if compare.isdigit() else _TREND_LIMIT, kind)
        return
    if compare == "last":
        baseline_path = find_latest_log(default_dir, exclude=log_path, pattern=kind.pattern)
        if baseline_path is None:
            _logger.warning("未找到可对比的历史%s日志（%s）", kind.label, default_dir)
            return
    else:
        baseline_path = Path(compare)
    try:
        baseline = load_profile_log(baseline_path)
        current = load_profile_log(log_path)
        print_profile_compare(current, baseline, baseline_path, stage_min_delta=kind.stage_min_delta)
    except ValueError as exc:
        _logger.warning("加载基准%s日志失败，跳过对比: %s", kind.label, exc)


def _print_trend_from_dir(log_path: Path, default_dir: Path, limit: int, kind: LogKind) -> None:
    """从默认目录收集历史日志并渲染趋势表（``compare_with_baseline`` 的趋势分支）.

    历史日志逐个加载：畸形/schema 与本次不一致的跳过（warning），全部
    不可用时跳过趋势对比，不中断构建或运行结果。
    """
    paths = find_recent_logs(default_dir, exclude=log_path, pattern=kind.pattern, limit=limit)
    if not paths:
        _logger.warning("未找到可对比的历史%s日志（%s）", kind.label, default_dir)
        return
    try:
        current = load_profile_log(log_path)
    except ValueError as exc:
        _logger.warning("加载本次%s日志失败，跳过对比: %s", kind.label, exc)
        return
    history: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        try:
            data = load_profile_log(path)
        except ValueError as exc:
            _logger.warning("跳过无法读取的历史%s日志: %s", kind.label, exc)
            continue
        if data.get("schema") != current.get("schema"):
            _logger.warning("跳过类型不一致的历史%s日志: %s", kind.label, path.name)
            continue
        history.append((path, data))
    if not history:
        _logger.warning("历史%s日志均不可用，跳过趋势对比", kind.label)
        return
    print_profile_trend(current, history, stage_min_delta=kind.stage_min_delta, label=kind.label)


def _fmt_seconds(s: float) -> str:
    """格式化耗时为人类可读字符串（ms/s/m），与 profile.py 展示一致."""
    if s < 1:
        return f"{s * 1000:.0f}ms"
    if s < 60:
        return f"{s:.2f}s"
    return f"{int(s // 60)}m{s % 60:.1f}s"


def _fmt_signed(fmt: Callable[[float], str], delta: float) -> str:
    """带符号格式化差值：正数 ``+0.13s``，负数 ``-0.13s``，零 ``0``."""
    if delta == 0:
        return "0"
    sign = "+" if delta > 0 else "-"
    return f"{sign}{fmt(abs(delta))}"


def _fmt_ago(iso_created: str) -> str:
    """格式化基准日志创建时间为相对时长（如 ``3 小时前``），解析失败返回空串."""
    try:
        created = datetime.fromisoformat(iso_created)
    except ValueError:
        return ""
    seconds = (datetime.now() - created).total_seconds()
    if seconds < 0:
        return ""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def _delta_cell(cur: float, base: float, fmt: Callable[[float], str]) -> Any:
    """构造差异单元格（rich Text）：带符号差值 + 百分比 + 回归/改善箭头.

    回归（耗时/内存增加）红色 ``▲``，改善绿色 ``▼``，持平暗色 ``＝``；
    基准为 0 时只显示差值（百分比无意义）。
    """
    from rich.text import Text

    delta = cur - base
    if delta > 0:
        arrow, style = "▲", "red"
    elif delta < 0:
        arrow, style = "▼", "green"
    else:
        arrow, style = "＝", "dim"
    if base > 0:
        pct = delta / base * 100
        return Text(f"{_fmt_signed(fmt, delta)} {pct:+.1f}% {arrow}", style=style)
    return Text(f"{_fmt_signed(fmt, delta)} {arrow}", style=style)


def _env_notes(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """收集双方环境差异提示（项目版本/Python/平台/入口/调试模式）.

    任一维度不一致即生成一条说明，对比结论需谨慎；双方均无该字段
    （如构建日志无 entry/debug）时不提示。
    """
    notes: list[str] = []
    cur_proj, base_proj = current.get("project", {}), baseline.get("project", {})
    if cur_proj.get("version") != base_proj.get("version"):
        notes.append(f"项目版本 {base_proj.get('version', '?')} → {cur_proj.get('version', '?')}")
    if current.get("python") != baseline.get("python"):
        notes.append(f"Python {baseline.get('python', '?')} → {current.get('python', '?')}")
    if current.get("platform") != baseline.get("platform"):
        notes.append(f"平台 {baseline.get('platform', '?')} → {current.get('platform', '?')}")
    # 启动剖析特有：入口名与 --debug 模式（debug 下无 loader 段，阶段构成不同）
    if current.get("entry") != baseline.get("entry"):
        notes.append(f"入口 {baseline.get('entry', '?')} → {current.get('entry', '?')}")
    if bool(current.get("debug")) != bool(baseline.get("debug")):
        notes.append(
            f"调试模式 {'开' if current.get('debug') else '关'}（基准为 {'开' if baseline.get('debug') else '关'}）"
        )
    return notes


def print_profile_compare(
    current: dict[str, Any],
    baseline: dict[str, Any],
    baseline_path: Path,
    *,
    stage_min_delta: float = _STAGE_MIN_DELTA,
) -> None:
    """渲染本次与基准性能日志的差异对比表到控制台.

    表格分两段：总览（墙钟恒显；CPU/内存仅当日志含该字段——构建剖析有、
    启动剖析无）与阶段明细（仅差异显著项：绝对差超 ``stage_min_delta``
    且相对差超 10%；新增/移除阶段单列；其余折叠为一行计数）。环境不一致
    （项目版本/Python/平台/入口/调试模式不同）时在表尾注明，提示对比
    结论需谨慎。

    :raises ValueError: 双方 schema 不一致（构建日志与启动剖析日志不可比）
    """
    # 延迟导入：rich 渲染链与 console 仅在真正输出对比表时加载
    from rich.table import Table
    from rich.text import Text

    from fspack.console import console
    from fspack.progress import fmt_bytes

    if current.get("schema") != baseline.get("schema"):
        raise ValueError(
            f"对比双方日志类型不一致（{current.get('schema')} vs {baseline.get('schema')}）: {baseline_path}"
        )

    # 环境差异提示：项目版本/解释器/平台/入口/调试模式任一不同即标注
    notes = _env_notes(current, baseline)

    ago = _fmt_ago(str(baseline.get("created", "")))
    subtitle = f"基准: {baseline_path.name}" + (f"（{ago}）" if ago else "")
    table = Table(title="性能对比", title_style="bold magenta", caption="；".join(notes) or None)
    table.caption_style = "dim yellow"
    table.add_column("指标", style="bold cyan", no_wrap=True)
    table.add_column("本次", justify="right")
    table.add_column("基准", justify="right")
    table.add_column("差异", justify="right")

    def _overview_row(label: str, key: str, fmt: Callable[[float], str]) -> None:
        cur, base = float(current.get(key, 0)), float(baseline.get(key, 0))
        table.add_row(label, fmt(cur), fmt(base), _delta_cell(cur, base, fmt))

    _overview_row("墙钟时间", "wall_time", _fmt_seconds)
    if "cpu_time" in current or "cpu_time" in baseline:
        _overview_row("CPU 时间", "cpu_time", _fmt_seconds)
    if "memory_peak" in current or "memory_peak" in baseline:
        _overview_row("内存峰值", "memory_peak", lambda v: fmt_bytes(int(v)))

    # 阶段差异：按名字对齐，显著项按 |差值| 降序列出（上限 8 行）
    cur_stages = {s["name"]: float(s.get("elapsed", 0)) for s in current.get("stages", [])}
    base_stages = {s["name"]: float(s.get("elapsed", 0)) for s in baseline.get("stages", [])}
    significant: list[tuple[str, float, float]] = []
    common = [n for n in cur_stages if n in base_stages]
    for name in common:
        cur, base = cur_stages[name], base_stages[name]
        delta = cur - base
        pct = delta / base * 100 if base > 0 else 0.0
        if abs(delta) > stage_min_delta and abs(pct) > _STAGE_MIN_PCT:
            significant.append((name, cur, base))
    significant.sort(key=lambda x: -abs(x[1] - x[2]))
    added = [n for n in cur_stages if n not in base_stages]
    removed = [n for n in base_stages if n not in cur_stages]
    quiet = len(common) - len(significant)

    if significant or added or removed or quiet:
        table.add_section()
    for name, cur, base in significant[:8]:
        table.add_row(name, _fmt_seconds(cur), _fmt_seconds(base), _delta_cell(cur, base, _fmt_seconds))
    for name in added[:4]:
        table.add_row(name, _fmt_seconds(cur_stages[name]), "-", Text("新增", style="yellow"))
    for name in removed[:4]:
        table.add_row(name, "-", _fmt_seconds(base_stages[name]), Text("移除", style="dim"))
    if quiet > 0:
        table.add_row(f"其余 {quiet} 个阶段", "", "", Text("差异不显著", style="dim"))
    console.rich.print(subtitle)
    console.rich.print(table)


def _fmt_trend_time(iso_created: str) -> str:
    """格式化日志创建时间为 ``MM-DD HH:MM:SS``（同年省略年份），解析失败返回空串."""
    try:
        created = datetime.fromisoformat(iso_created)
    except ValueError:
        return ""
    return created.strftime("%m-%d %H:%M:%S")


def _env_matches(current: dict[str, Any], baseline: dict[str, Any]) -> bool:
    """判定两份日志环境是否一致（复用 :func:`_env_notes` 的全部维度）."""
    return not _env_notes(current, baseline)


def print_profile_trend(
    current: dict[str, Any],
    history: list[tuple[Path, dict[str, Any]]],
    *,
    stage_min_delta: float = _STAGE_MIN_DELTA,
    label: str = "性能",
) -> None:
    """渲染历次趋势表：明细 + 本次 + 中位数统计 + 阶段显著偏离（构建与启动剖析共用）.

    两张表：

    1. 趋势表：每行一次历史运行（时间升序，按日期直观看走势），末尾
       ``本次``/``中位数``/``本次 vs 中位数`` 三行——中位数基准抗单次
       抖动（缓存冷热/网络波动），比"与最近一次对比"更能回答"本次是否
       异常"。统计基准仅用环境一致（项目版本/Python/平台/入口/调试模式
       相同）的历史行，不一致行以 ``*`` 前缀 + 暗色展示且不参与统计；
       无一致行时退化为全部历史并在表尾注明。
    2. 阶段偏离表：本次各阶段 vs 环境一致历史的阶段中位数，仅列显著项
       （绝对差超 ``stage_min_delta`` 且相对差超 10%，按差值降序上限 8 行）
       ；新增/移除阶段单列（上限 4）。无显著偏离时不渲染。

    :param current: 本次刚写入的日志 dict
    :param history: 历史日志 ``(路径, dict)`` 列表（时间升序，不含本次）
    :param stage_min_delta: 阶段显著阈值（秒），构建与启动剖析量级不同
    :param label: 日志类别标签（"性能"/"启动剖析"），用于表标题
    """
    # 延迟导入：rich 渲染链与 console 仅在真正输出趋势表时加载
    from rich.table import Table
    from rich.text import Text

    from fspack.console import console
    from fspack.progress import fmt_bytes

    matched = [(p, d) for p, d in history if _env_matches(current, d)]
    mismatched_rows = [(p, d) for p, d in history if not _env_matches(current, d)]
    # 统计基准优先用环境一致行；全部不一致时退化为全部（表尾注明）
    stats_source = matched if matched else history
    n_stats = len(stats_source)

    show_cpu = "cpu_time" in current or any("cpu_time" in d for _, d in history)
    show_mem = "memory_peak" in current or any("memory_peak" in d for _, d in history)

    table = Table(title=f"{label}趋势（历史 {len(history)} 次）", title_style="bold magenta")
    table.add_column("时间", style="cyan", no_wrap=True)
    table.add_column("墙钟时间", justify="right")
    if show_cpu:
        table.add_column("CPU 时间", justify="right")
    if show_mem:
        table.add_column("内存峰值", justify="right")

    def _history_row(path: Path, data: dict[str, Any], env_ok: bool) -> None:
        time_str = ("  " if env_ok else "* ") + (_fmt_trend_time(str(data.get("created", ""))) or path.stem)
        cells: list[Any] = [
            Text(time_str, style="" if env_ok else "dim"),
            _fmt_seconds(float(data.get("wall_time", 0))),
        ]
        if show_cpu:
            cells.append(_fmt_seconds(float(data.get("cpu_time", 0))))
        if show_mem:
            cells.append(fmt_bytes(int(data.get("memory_peak", 0))))
        table.add_row(*cells)

    for path, data in matched:
        _history_row(path, data, env_ok=True)
    for path, data in mismatched_rows:
        _history_row(path, data, env_ok=False)

    # 统计三行：本次 / 中位数 / 本次 vs 中位数
    cur_wall = float(current.get("wall_time", 0))
    med_wall = median(float(d.get("wall_time", 0)) for _, d in stats_source)
    cur_cells: list[Any] = [Text("本次", style="bold"), _fmt_seconds(cur_wall)]
    stat_cells: list[Any] = [f"中位数(n={n_stats})", _fmt_seconds(med_wall)]
    delta_cells: list[Any] = ["本次 vs 中位数", _delta_cell(cur_wall, med_wall, _fmt_seconds)]
    if show_cpu:
        cur_cpu = float(current.get("cpu_time", 0))
        med_cpu = median(float(d.get("cpu_time", 0)) for _, d in stats_source)
        cur_cells.append(_fmt_seconds(cur_cpu))
        stat_cells.append(_fmt_seconds(med_cpu))
        delta_cells.append(_delta_cell(cur_cpu, med_cpu, _fmt_seconds))
    if show_mem:
        cur_mem = int(current.get("memory_peak", 0))
        med_mem = median(int(d.get("memory_peak", 0)) for _, d in stats_source)
        cur_cells.append(fmt_bytes(cur_mem))
        stat_cells.append(fmt_bytes(int(med_mem)))
        delta_cells.append(_delta_cell(cur_mem, med_mem, lambda v: fmt_bytes(int(v))))
    table.add_section()
    table.add_row(*cur_cells)
    table.add_row(*stat_cells)
    table.add_row(*delta_cells)

    notes: list[str] = []
    if mismatched_rows:
        notes.append(f"* 环境不一致 {len(mismatched_rows)} 次（Python/平台/版本/入口不同），不参与统计基准")
    if not matched and history:
        notes.append("无环境一致历史，统计基准退化为全部历史")
    table.caption = "；".join(notes) or None
    table.caption_style = "dim yellow"
    console.rich.print(table)
    _print_stage_deviation(current, stats_source, stage_min_delta, label)


def _print_stage_deviation(
    current: dict[str, Any],
    stats_source: list[tuple[Path, dict[str, Any]]],
    stage_min_delta: float,
    label: str,
) -> None:
    """渲染阶段偏离表：本次各阶段 vs 统计基准的阶段中位数，仅列显著项.

    显著判定与 :func:`print_profile_compare` 一致（绝对差超
    ``stage_min_delta`` 且相对差超 10%），按差值降序上限 8 行；新增/
    移除阶段单列（上限 4）。无显著偏离时不渲染。
    """
    from rich.table import Table
    from rich.text import Text

    from fspack.console import console

    per_stage: dict[str, list[float]] = {}
    for _, data in stats_source:
        for stage in data.get("stages", []):
            per_stage.setdefault(str(stage.get("name")), []).append(float(stage.get("elapsed", 0)))
    cur_stages = {str(s.get("name")): float(s.get("elapsed", 0)) for s in current.get("stages", [])}
    significant: list[tuple[str, float, float]] = []
    for name, cur in cur_stages.items():
        samples = per_stage.get(name)
        if not samples:
            continue
        med = median(samples)
        delta = cur - med
        pct = delta / med * 100 if med > 0 else 0.0
        if abs(delta) > stage_min_delta and abs(pct) > _STAGE_MIN_PCT:
            significant.append((name, cur, med))
    significant.sort(key=lambda x: -abs(x[1] - x[2]))
    added = [n for n in cur_stages if n not in per_stage]
    removed = [n for n in per_stage if n not in cur_stages]
    if not (significant or added or removed):
        return
    deviation = Table(title=f"{label}阶段偏离（vs 中位数）", title_style="bold magenta")
    deviation.add_column("阶段", style="bold cyan", no_wrap=True)
    deviation.add_column("本次", justify="right")
    deviation.add_column("中位数", justify="right")
    deviation.add_column("差异", justify="right")
    for name, cur, med in significant[:8]:
        deviation.add_row(name, _fmt_seconds(cur), _fmt_seconds(med), _delta_cell(cur, med, _fmt_seconds))
    for name in added[:4]:
        deviation.add_row(name, _fmt_seconds(cur_stages[name]), "-", Text("新增", style="yellow"))
    for name in removed[:4]:
        deviation.add_row(name, "-", _fmt_seconds(median(per_stage[name])), Text("移除", style="dim"))
    console.rich.print(deviation)
