"""构建性能日志：落盘、加载与历史对比.

``fsp b --profile`` 构建结束后将 :class:`ProfileReport` 连同元数据写入
JSON 日志（默认 ``<项目>/.benchmarks/fsp-b-<时间戳>.json``，``--profile-out``
可指定目录或文件），``--profile-compare`` 与最近一次或指定基准日志对比，
渲染差异表格定位性能回归。

日志 JSON 结构（schema ``fspack/build-profile/1``）::

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

公共 API：

- :func:`save_profile_report` — 写入性能日志（目录自动命名 / 文件直写）
- :func:`load_profile_log` — 读取日志为 dict
- :func:`find_latest_log` — 目录内最新日志（排除指定文件）
- :func:`print_profile_compare` — 渲染本次与基准的差异对比表
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # ProfileReport 仅用于类型注解（from __future__ import annotations 使
    # 注解不在运行时求值），顶部不导入 fspack.packaging.profile 避免连锁
    # 触发 fspack.console/rich 加载。
    from fspack.packaging.profile import ProfileReport

__all__ = [
    "ProfileLogMeta",
    "find_latest_log",
    "load_profile_log",
    "print_profile_compare",
    "save_profile_report",
]

# 日志 schema 版本：结构变更时递增，加载侧按版本校验兼容性
PROFILE_LOG_SCHEMA = "fspack/build-profile/1"
# 默认日志目录名（项目根下，与 pytest-benchmark 共存，文件名前缀区分）
DEFAULT_LOG_DIR = ".benchmarks"
# 日志文件名前缀与通配（fsp-b-YYYYMMDD-HHMMSS.json）
_LOG_PREFIX = "fsp-b-"
_LOG_GLOB = "fsp-b-*.json"
# 阶段差异显著阈值：绝对差超 50ms 且相对差超 10% 才列入对比表，
# 其余折叠为计数行，避免噪声淹没真实回归
_STAGE_MIN_DELTA = 0.05
_STAGE_MIN_PCT = 10.0


@dataclass(frozen=True)
class ProfileLogMeta:
    """性能日志元数据：项目 / 解释器 / 平台，用于对比时的环境差异提示."""

    name: str
    version: str
    python: str
    platform: str


def _auto_name(directory: Path) -> Path:
    """在目录内生成不冲突的日志文件路径（``fsp-b-<时间戳>.json``）.

    同秒内多次构建自动追加 ``-2``/``-3`` 序号，避免覆盖既有日志。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"{_LOG_PREFIX}{stamp}.json"
    seq = 2
    while candidate.exists():
        candidate = directory / f"{_LOG_PREFIX}{stamp}-{seq}.json"
        seq += 1
    return candidate


def save_profile_report(report: ProfileReport, out: Path, meta: ProfileLogMeta) -> Path:
    """写入性能日志 JSON，返回实际写入路径.

    :param report: 构建耗时报告
    :param out: 输出路径——``.json`` 后缀按文件直写（父目录自动创建）；
        其余（目录或不存在路径）按目录处理，自动命名写入
    :param meta: 项目/解释器/平台元数据
    :return: 实际写入的文件路径
    """
    path = out if out.suffix == ".json" else _auto_name(out)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_profile_log(path: Path) -> dict[str, Any]:
    """读取性能日志 JSON 为 dict，文件不存在/畸形/schema 不符时抛 :class:`ValueError`.

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
    if schema != PROFILE_LOG_SCHEMA:
        raise ValueError(f"性能日志 schema 不受支持（{schema}，期望 {PROFILE_LOG_SCHEMA}）: {path}")
    return data


def find_latest_log(directory: Path, exclude: Path | None = None) -> Path | None:
    """返回目录内最新的性能日志（排除 ``exclude``），无则返回 ``None``.

    文件名 ``fsp-b-YYYYMMDD-HHMMSS[-N].json`` 的字典序即时间序，直接按名
    排序取最大，无需读文件内容。
    """
    if not directory.is_dir():
        return None
    logs = [p for p in directory.glob(_LOG_GLOB) if exclude is None or p.resolve() != exclude.resolve()]
    return max(logs, key=lambda p: p.name) if logs else None


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


def print_profile_compare(current: dict[str, Any], baseline: dict[str, Any], baseline_path: Path) -> None:
    """渲染本次与基准性能日志的差异对比表到控制台.

    表格分两段：总览（墙钟/CPU/内存，差异带符号百分比与箭头着色）与
    阶段明细（仅差异显著项：绝对差 > 50ms 且相对差 > 10%；新增/移除
    阶段单列；其余折叠为一行计数）。环境不一致（项目版本/Python/平台
    不同）时在表尾注明，提示对比结论需谨慎。
    """
    # 延迟导入：rich 渲染链与 console 仅在真正输出对比表时加载
    from rich.table import Table
    from rich.text import Text

    from fspack.console import console
    from fspack.progress import fmt_bytes

    # 环境差异提示：项目版本/解释器/平台任一不同即标注
    notes: list[str] = []
    cur_proj, base_proj = current.get("project", {}), baseline.get("project", {})
    if cur_proj.get("version") != base_proj.get("version"):
        notes.append(f"项目版本 {base_proj.get('version', '?')} → {cur_proj.get('version', '?')}")
    if current.get("python") != baseline.get("python"):
        notes.append(f"Python {baseline.get('python', '?')} → {current.get('python', '?')}")
    if current.get("platform") != baseline.get("platform"):
        notes.append(f"平台 {baseline.get('platform', '?')} → {current.get('platform', '?')}")

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
    _overview_row("CPU 时间", "cpu_time", _fmt_seconds)
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
        if abs(delta) > _STAGE_MIN_DELTA and abs(pct) > _STAGE_MIN_PCT:
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
