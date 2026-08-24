"""``fsp r --profile`` 启动耗时剖析：采集与汇总.

流式读取子进程 stderr，采集四类打点标记并解析为启动耗时汇总表：

- ``[fspack loader] <阶段> 耗时 <ms>ms``：C loader 各阶段耗时，
  由 ``FSPACK_LOADER_VERBOSE=1`` 激活（三平台 loader 已内置打点）。
- ``[fspack timing] <label> @<累计ms>ms``：入口包装器各阶段累计时刻，
  由 ``FSPACK_TIMING=1`` 激活（新构建的 dist wrapper 已内置打点；
  旧 dist 无此类行，汇总缺 wrapper 段并提示重新构建）。
- ``[fspack timing-gap] <label> <ms>ms``：跨打点体系缝隙实测（Windows
  新 dist）：loader 在 Py_Main 调用前写 QPC 锚点环境变量，wrapper 首语句
  用同源 perf_counter 相减，得 C 层初始化缝隙（py_init）。
- ``[fspack timing] gui_ready @<累计ms>ms``：GUI 界面就绪里程碑（wrapper
  事件循环自终止钩子输出，Qt/tkinter 首帧上屏后），GUI 应用"进入界面后
  自行终止"的启动终点。
- ``import time: <self> | <cumulative> | <缩进><模块名>``：CPython 原生
  ``-X importtime`` 逐模块导入耗时，由 ``PYTHONPROFILEIMPORTTIME=1`` 激活。

非标记行（程序自身的 stderr 输出）原样透传；标记行与 importtime 原始行
不透传，由退出后打印的对齐汇总表替代（阶段/名称/耗时/占比/条形图五列，
CJK 全宽字符按 2 列对齐，条形图用 █▓░ 按占比填充；"用户入口执行"段按
``entry_start`` 打点分界细分为导入合计与其余执行，stderr 行序即时间序；
末端"未细分"行解释未被各阶段覆盖的 wall time 去向——父进程侧记录首/末
行 stderr 到达时刻，把 gap 实测拆分为进程创建与映像加载（头部）、进程
收尾（尾部）与其余盲区（Py_Main C 层初始化等无打点段）三行）。
stdout/stdin 继承父进程，交互与正常输出不受影响。
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PROFILE_ENV", "ProfileSample", "run_with_profile"]

# 激活三个数据源所需注入的环境变量（loader 打点 / wrapper 打点 / importtime）
PROFILE_ENV = {"FSPACK_LOADER_VERBOSE": "1", "FSPACK_TIMING": "1", "PYTHONPROFILEIMPORTTIME": "1"}


@dataclass
class ProfileSample:
    """一次运行的剖析采集结果（``run_with_profile`` 流式收集的原始数据）.

    - ``loader_stages``：``(阶段名, 耗时ms, 补充说明)`` 三元组列表
    - ``timing_stages``：wrapper 打点 ``{label: 累计时刻ms}``
    - ``gap_stages``：跨打点体系缝隙实测 ``{label: 缝隙ms}``
      （Windows 新 dist 的 ``py_init``：Py_Main C 层初始化）
    - ``import_lines``/``post_entry_lines``：``entry_start`` 分界前后的
      importtime 原始行（stderr 同管道行序即时间序）
    - ``first_line_ms``/``last_line_ms``：首/末行 stderr 到达时刻（自
      父进程 t_start 的偏移 ms，逐行 readline 实测），用于父进程侧
      细分"未细分"段的头部（进程创建与映像加载）与尾部（进程收尾）；
      子进程无任何 stderr 输出时为 None
    - ``timed_out``：是否因超时被强制终止（GUI 框架不在 wrapper 自终止
      钩子支持清单或程序长跑），此时 wall_ms 与各阶段数据不完整
    """

    wall_ms: float = 0.0
    returncode: int = 0
    timed_out: bool = False
    loader_stages: list[tuple[str, float, str]] = field(default_factory=list)
    timing_stages: dict[str, float] = field(default_factory=dict)
    gap_stages: dict[str, float] = field(default_factory=dict)
    import_lines: list[str] = field(default_factory=list)
    post_entry_lines: list[str] = field(default_factory=list)
    first_line_ms: float | None = None
    last_line_ms: float | None = None


_LOADER_PREFIX = "[fspack loader]"
_TIMING_PREFIX = "[fspack timing]"
_TIMING_GAP_PREFIX = "[fspack timing-gap]"
_IMPORTTIME_PREFIX = "import time:"
# loader 打点行：[fspack loader] <阶段> 耗时 <ms>ms[（补充说明）]
_LOADER_RE = re.compile(r"\[fspack loader\]\s*(.+?)\s*耗时\s*([0-9.]+)ms(.*)")
# wrapper 打点行：[fspack timing] <label> @<累计ms>ms
_TIMING_RE = re.compile(r"\[fspack timing\]\s*(\S+)\s*@\s*([0-9.]+)ms")
# 跨打点缝隙行：[fspack timing-gap] <label> <ms>ms
_TIMING_GAP_RE = re.compile(r"\[fspack timing-gap\]\s*(\S+)\s*([0-9.]+)ms")
# importtime 名字字段每级缩进 2 空格，深度 = 缩进空格数 // 2
_NAME_INDENT = 2
# 顶层导入与入口导入的展示条数上限
_TOP_ROOTS = 8
_TOP_ENTRY = 5
_TOP_SELF = 10
# 低于该阈值（ms）的条目在汇总中不展示，避免噪音
_MIN_DISPLAY_MS = 0.1
# 汇总表列宽（按终端显示宽度计，CJK 全宽字符占 2 列）
_TAG_W = 10
_LABEL_W = 34
_MS_W = 9
_PCT_W = 7
# 占比条形图格数（█ 实心 + ▓ 半格 + ░ 空槽，置于行尾不参与列对齐）
_BAR_W = 12
# 汇总表总列宽 = 2 + TAG + LABEL + MS + 2 + PCT + 2 + BAR
_SEP_LEN = 2 + _TAG_W + _LABEL_W + _MS_W + 2 + _PCT_W + 2 + _BAR_W
# "未细分"行的显示阈值：gap 超过 100ms 且占总时长 5% 以上，或占比超
# 30%（快程序的外部开销占比天然高）时显示，避免控制噪声的同时保留
# 对"时间去哪了"的自解释能力
_GAP_MIN_MS = 100.0
_GAP_MIN_RATIO = 0.05
_GAP_HIGH_RATIO = 0.30
# 单次运行超时兜底（秒）：GUI 应用由 wrapper 的事件循环自终止钩子
# （FSPACK_TIMING=1 时注入，见 packaging/entry.py）在"进入界面"后自然
# 退出；未知框架钩子未命中时进程长跑，由该超时强制终止防止剖析永久挂起
_TIMEOUT_S = 300.0


def _consume_marker(line: str, sample: ProfileSample, entry_started: bool) -> bool | None:
    """解析一行 stderr 打点标记，写入 ``sample``；返回是否已消费.

    - importtime 行按 ``entry_started`` 分界归前/后两段；
    - timing 行触发 ``entry_start`` 时返回 True（调用方置位 entry_started）；
    - 非标记行（或无法解析的 loader 错误行）返回 None，由调用方透传。
    """
    if line.startswith(_IMPORTTIME_PREFIX):
        if entry_started:
            sample.post_entry_lines.append(line)
        else:
            sample.import_lines.append(line)
        return False
    if line.startswith(_LOADER_PREFIX):
        m = _LOADER_RE.match(line)
        if m is not None:
            sample.loader_stages.append((m.group(1), float(m.group(2)), m.group(3)))
            return False
    elif line.startswith(_TIMING_GAP_PREFIX):
        m = _TIMING_GAP_RE.match(line)
        if m is not None:
            sample.gap_stages[m.group(1)] = float(m.group(2))
            return False
    elif line.startswith(_TIMING_PREFIX):
        m = _TIMING_RE.match(line)
        if m is not None:
            sample.timing_stages[m.group(1)] = float(m.group(2))
            return m.group(1) == "entry_start"
    return None


def _terminate_on_timeout(proc: subprocess.Popen[bytes], sample: ProfileSample) -> None:
    """watchdog 回调：标记超时并强制终止子进程（管道写端随进程关闭，读循环经 EOF 退出）."""
    sample.timed_out = True
    proc.terminate()


def _run_once(cmd: list[str], env: dict[str, str] | None, timeout: float) -> ProfileSample:
    """运行一次目标程序并采集启动耗时打点，返回剖析样本.

    ``timeout`` 秒后进程仍未退出（GUI 框架不在 wrapper 自终止钩子支持
    清单或程序长跑）时强制终止，样本标记 ``timed_out=True``。
    """
    t_start = time.perf_counter()
    proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE)
    sample = ProfileSample()
    watchdog = threading.Timer(timeout, _terminate_on_timeout, args=(proc, sample))
    watchdog.daemon = True
    watchdog.start()
    entry_started = False
    assert proc.stderr is not None
    # readline 逐行读取（不用缓冲迭代器）：迭代器可能一次预读多行，行到达
    # 时刻会失真为批量到达；readline 每行一次系统调用，首/末行时间戳真实
    # 反映子进程写出时刻 + 管道传播延迟，用于细分"未细分"段的首尾两端
    while True:
        raw = proc.stderr.readline()
        if not raw:
            break
        line_ms = (time.perf_counter() - t_start) * 1000.0
        if sample.first_line_ms is None:
            sample.first_line_ms = line_ms
        sample.last_line_ms = line_ms
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        consumed = _consume_marker(line, sample, entry_started)
        if consumed is not None:
            entry_started = entry_started or consumed
            continue
        # 非标记行（程序自身输出/无法解析的 loader 错误行）原样透传
        sys.stderr.write(line + "\n")
    sys.stderr.flush()
    proc.stderr.close()
    sample.returncode = proc.wait()
    watchdog.cancel()
    sample.wall_ms = (time.perf_counter() - t_start) * 1000.0
    return sample


def _median_sample(samples: list[ProfileSample]) -> ProfileSample:
    """返回 wall_ms 处于中位数的样本（偶数取 lower median，实际样本而非均值）.

    汇总表展示中位数样本：中位数对单次抖动（杀软扫描/磁盘冷读/调度）
    天然免疫，是最能代表典型启动路径的一次运行。
    """
    return sorted(samples, key=lambda s: s.wall_ms)[(len(samples) - 1) // 2]


def _print_repeat_stats(samples: list[ProfileSample]) -> None:
    """打印多次运行统计块：中位数/最小/最大/均值/标准差（pytest-benchmark 风格）."""
    walls = sorted(s.wall_ms for s in samples)
    n = len(walls)
    median = walls[(n - 1) // 2]
    mean = sum(walls) / n
    std = (sum((w - mean) ** 2 for w in walls) / n) ** 0.5
    print(
        f"[fspack] {n} 次运行统计：中位数 {median:.0f}ms，最小 {walls[0]:.0f}ms，"
        f"最大 {walls[-1]:.0f}ms，均值 {mean:.0f}ms，标准差 {std:.1f}ms（汇总表取中位数样本）"
    )


def run_with_profile(
    cmd: list[str],
    env: dict[str, str] | None = None,
    on_summary: Callable[[dict[str, Any]], None] | None = None,
    repeat: int = 1,
    timeout: float = _TIMEOUT_S,
) -> int:
    """运行目标程序并采集启动耗时打点，子进程退出后打印汇总.

    stdout/stdin 继承父进程（交互与正常输出不受影响）；stderr 经管道流式
    读取：非标记行原样透传，标记行与 importtime 行由汇总替代。

    importtime 行按 ``entry_start`` 打点分界为两段：之前的归 wrapper
    （解释器初始化 + wrapper 顶层导入），之后的归用户入口执行期间的导入
    （stderr 同管道行序即时间序，入口细分由 :func:`_print_summary` 完成）。

    ``repeat > 1`` 时多次运行取统计（pytest-benchmark 风格）：每次结束
    打印进度行，全部结束后打印中位数/最小/最大/均值/标准差统计块，汇总
    表取中位数样本（对单次抖动免疫），``runs_ms``/``repeat`` 进入剖析
    数据供落盘；``repeat=1`` 时行为与单次运行完全一致（无统计块）。

    GUI 应用由 wrapper 的事件循环自终止钩子（``FSPACK_TIMING=1`` 时注入，
    见 :mod:`fspack.packaging.entry`）在"进入界面"后自然退出；未知框架
    钩子未命中时由 ``timeout`` 秒超时兜底强制终止（样本标记 ``timed_out``，
    汇总表标注数据不完整）。

    ``on_summary`` 非空时在打印汇总后回调结构化剖析数据（键见
    :func:`_print_summary` 返回值），供调用方落盘性能日志（毫秒单位）。
    返回中位数样本的退出码。
    """
    samples: list[ProfileSample] = []
    for i in range(max(repeat, 1)):
        s = _run_once(cmd, env, timeout)
        samples.append(s)
        if repeat > 1:
            note = "（超时被终止）" if s.timed_out else ""
            print(f"[fspack] 运行 {i + 1}/{repeat}: {s.wall_ms:.0f}ms{note}")
    if len(samples) > 1:
        _print_repeat_stats(samples)
    sample = _median_sample(samples)
    data = _print_summary(sample)
    if len(samples) > 1:
        data["runs_ms"] = [s.wall_ms for s in samples]
        data["repeat"] = len(samples)
    if on_summary is not None:
        on_summary(data)
    return sample.returncode


def _parse_import_lines(
    lines: list[str], anchor: str | None = "runpy"
) -> tuple[float, list[tuple[str, float]], list[tuple[str, float]]]:
    """解析 importtime 行，返回 (解释器初始化耗时, 顶层导入列表, 模块自身耗时列表).

    - 解释器初始化耗时（约）：wrapper 首次 import ``runpy`` 之前的全部
      depth-0 根导入 cumulative 之和（encodings/site 等解释器启动导入）。
    - 顶层导入列表：``runpy`` 及其后的 depth-0 根导入（wrapper 自身与用户
      代码的顶层导入），值为 cumulative 毫秒。
    - 模块自身耗时列表：全部模块按 self 毫秒降序的前 :data:`_TOP_SELF` 条。

    行格式 ``import time: <self_us> | <cum_us> | <缩进><name>``，无法解析的
    行（表头/畸形行）跳过。锚点用 ``runpy``：wrapper 顶层必导入 runpy 且
    早于一切用户代码，而 os/sys/time 在解释器启动期已缓存不产生 importtime
    行；找不到时（理论上仅极旧版 wrapper）全部根导入计入解释器初始化段。
    ``anchor=None`` 跳过锚点分界（入口执行期间的导入段用：全部根导入
    归顶层导入列表，解释器初始化为 0）。
    """
    roots: list[tuple[str, float]] = []
    self_items: list[tuple[str, float]] = []
    for line in lines:
        parts = line[len(_IMPORTTIME_PREFIX) :].split("|")
        if len(parts) != 3:
            continue
        try:
            self_ms = int(parts[0].strip()) / 1000.0
            cum_ms = int(parts[1].strip()) / 1000.0
        except ValueError:
            continue
        name_field = parts[2]
        name = name_field.strip()
        if not name:
            continue
        self_items.append((name, self_ms))
        depth = (len(name_field) - len(name_field.lstrip(" "))) // _NAME_INDENT
        if depth == 0:
            roots.append((name, cum_ms))
    if anchor is None:
        return 0.0, roots, sorted(self_items, key=lambda x: -x[1])[:_TOP_SELF]
    anchor_idx = next((i for i, (n, _) in enumerate(roots) if n == anchor), None)
    if anchor_idx is None:
        return sum(c for _, c in roots), [], sorted(self_items, key=lambda x: -x[1])[:_TOP_SELF]
    interp_ms = sum(c for _, c in roots[:anchor_idx])
    return interp_ms, roots[anchor_idx:], sorted(self_items, key=lambda x: -x[1])[:_TOP_SELF]


def _disp_width(text: str) -> int:
    """返回终端显示宽度（CJK 全宽/宽字符按 2 列计，其余按 1 列）."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格到指定列宽（超宽原样返回，不截断）."""
    return text + " " * max(0, width - _disp_width(text))


def _rpad(text: str, width: int) -> str:
    """按显示宽度左侧补空格右对齐到指定列宽（超宽原样返回）."""
    return " " * max(0, width - _disp_width(text)) + text


def _fmt_ms(ms: float) -> str:
    """格式化毫秒值（一位小数）."""
    return f"{ms:.1f}ms"


def _fmt_pct(ms: float, wall_ms: float) -> str:
    """格式化占总时长的百分比（wall time 非正时为空串）."""
    if wall_ms <= 0:
        return ""
    return f"{ms / wall_ms * 100:.1f}%"


def _fmt_bar(ms: float, wall_ms: float) -> str:
    """返回 :data:`_BAR_W` 格占比条形图：``█`` 实心格 + ``▓`` 半格 + ``░`` 空槽.

    条形图置于行尾，不参与列对齐（块字符在不同终端的宽度渲染存在歧义，
    行尾错位不可见）。占比映射：每格 = 100/``_BAR_W``，余数 ≥ 0.5 格补
    ``▓``（半亮）表示"该格未满"。GBK 兼容（█▓░ 均在 GBK 区）。
    """
    if wall_ms <= 0:
        return "░" * _BAR_W
    units = min(ms / wall_ms, 1.0) * _BAR_W
    full = int(units)
    bar = "█" * min(full, _BAR_W)
    if len(bar) < _BAR_W and units - full >= 0.5:
        bar += "▓"
    return bar + "░" * (_BAR_W - len(bar))


def _row(tag: str, label: str, ms_text: str = "", pct_text: str = "", bar: str | None = None) -> None:
    """打印一行汇总：标签列 + 名称列 + 耗时列 + 占比列（可选条形图列）."""
    line = "  " + _pad(tag, _TAG_W) + _pad(label, _LABEL_W) + _rpad(ms_text, _MS_W) + "  " + _rpad(pct_text, _PCT_W)
    if bar is not None:
        line += "  " + bar
    print(line)


def _sub_row(name: str, ms: float, wall_ms: float, indent: int = 0) -> None:
    """打印子项行：名称列位置缩进对齐（``indent`` 额外缩进展示层级），含条形图."""
    print(
        "  "
        + " " * (_TAG_W + indent)
        + _pad(name, _LABEL_W - indent)
        + _rpad(_fmt_ms(ms), _MS_W)
        + "  "
        + _rpad(_fmt_pct(ms, wall_ms), _PCT_W)
        + "  "
        + _fmt_bar(ms, wall_ms)
    )


def _print_import_sections(
    interp_ms: float,
    user_roots: list[tuple[str, float]],
    self_top: list[tuple[str, float]],
    wall_ms: float,
) -> None:
    """打印 import 段：解释器初始化、顶层导入子项、模块自身耗时子项."""
    if interp_ms > 0:
        _row(
            "[import]",
            "解释器初始化(约)",
            _fmt_ms(interp_ms),
            _fmt_pct(interp_ms, wall_ms),
            _fmt_bar(interp_ms, wall_ms),
        )
    if user_roots:
        shown_roots = [r for r in sorted(user_roots, key=lambda x: -x[1])[:_TOP_ROOTS] if r[1] >= _MIN_DISPLAY_MS]
        if shown_roots:
            _row("[import]", f"顶层导入 top{len(shown_roots)}")
            for name, ms in shown_roots:
                _sub_row(name, ms, wall_ms)
    if self_top:
        shown_self = [r for r in self_top if r[1] >= _MIN_DISPLAY_MS]
        if shown_self:
            _row("[import]", f"模块自身耗时 top{len(shown_self)}")
            for name, ms in shown_self:
                _sub_row(name, ms, wall_ms)


def _print_entry_breakdown(
    exec_ms: float,
    post_roots: list[tuple[str, float]],
    wall_ms: float,
) -> None:
    """打印用户入口执行子段：导入合计（top 明细）与其余执行.

    ``post_roots`` 为 ``entry_start`` 打点之后出现的 depth-0 根导入
    （stderr 同管道行序即时间序），其 cumulative 之和即入口执行期间的
    导入耗时（根导入串行无重叠）；``exec_ms - 导入合计`` 为纯执行耗时
    （含 ``main()`` 逻辑与动态延迟导入的执行部分）。两段分开指导优化：
    导入大头 → 延迟导入；执行大头 → 优化入口逻辑。
    """
    import_total = sum(ms for _, ms in post_roots)
    rest_ms = max(exec_ms - import_total, 0.0)
    shown = [r for r in sorted(post_roots, key=lambda x: -x[1])[:_TOP_ENTRY] if r[1] >= _MIN_DISPLAY_MS]
    if shown:
        # 小计行展示全部根导入之和（含未列出的），子行仅 top 明细
        _sub_row(f"导入合计 top{len(shown)}", import_total, wall_ms)
        for name, ms in shown:
            _sub_row(name, ms, wall_ms, indent=2)
    if rest_ms >= _MIN_DISPLAY_MS or not shown:
        _sub_row("其余执行", rest_ms, wall_ms)


def _print_unaccounted(
    sample: ProfileSample,
    loader_total: float,
    interp_ms: float,
    py_init_ms: float,
) -> dict[str, float] | None:
    """打印"未细分"行：wall 减去已归因阶段（loader 总耗时 + C 层初始化
    实测 + 解释器初始化 + wrapper 末次打点累计值）。

    - gap ≥ 100ms 且占 5% 以上，或占比超 30%（快程序的外部开销占比天然
      高）时展示。
    - 有首/末行 stderr 到达时刻（父进程实测）时拆为三行：进程创建与映像
      加载（头部，扣除首 loader 阶段耗时避免与已归因段重叠）、进程收尾
      （尾部）、其余盲区（gap 减去首尾，负值归零——首尾含管道延迟等
      噪声，三行之和略超 gap 可接受）。
    - 无首/末行数据（构造调用/子进程无 stderr 输出）回退单行归因：
      旧 dist 无 wrapper 打点 → 提示重新构建；缺末次打点 → 入口未完成。
    返回拆分字典（无拆分时 None）。
    """
    wall_ms = sample.wall_ms
    timing_stages = sample.timing_stages
    first_line_ms = sample.first_line_ms
    last_line_ms = sample.last_line_ms
    # 首 loader 阶段（read_entry）耗时已单独归因，从头部实测中扣除
    first_loader_ms = sample.loader_stages[0][1] if sample.loader_stages else 0.0
    env_ready = timing_stages.get("env_ready")
    entry_done = timing_stages.get("entry_done")
    gap = wall_ms - (loader_total + py_init_ms + interp_ms + max(entry_done or 0.0, env_ready or 0.0))
    high_ratio = gap >= wall_ms * _GAP_HIGH_RATIO
    if not high_ratio and (gap < _GAP_MIN_MS or gap < wall_ms * _GAP_MIN_RATIO):
        return None
    if first_line_ms is not None and last_line_ms is not None:
        head = max(first_line_ms - first_loader_ms, 0.0)
        tail = max(wall_ms - last_line_ms, 0.0)
        blind = max(gap - head - tail, 0.0)
        _row("[未细分]", "进程创建与映像加载(约)", f"~{head:.0f}ms", _fmt_pct(head, wall_ms), _fmt_bar(head, wall_ms))
        print("  " + _pad("", _TAG_W) + "子进程创建/杀软扫描/映像加载/管道延迟")
        _row("[未细分]", "进程收尾(约)", f"~{tail:.0f}ms", _fmt_pct(tail, wall_ms), _fmt_bar(tail, wall_ms))
        print("  " + _pad("", _TAG_W) + "解释器退出/管道 EOF/父进程调度")
        if blind >= 1.0:
            _row("[未细分]", "其余盲区(约)", f"~{blind:.0f}ms", _fmt_pct(blind, wall_ms), _fmt_bar(blind, wall_ms))
            print("  " + _pad("", _TAG_W) + "残余无打点段与测量噪声（管道延迟/父进程调度等）")
        if env_ready is None:
            print("  " + _pad("", _TAG_W) + "旧 dist 无 wrapper 打点：重新 fsp b 后可细分环境准备与入口执行")
        elif entry_done is None:
            print("  " + _pad("", _TAG_W) + "入口未完成打点：os._exit 或异常退出，入口内耗时未归因")
        return {"head_ms": head, "tail_ms": tail, "blind_ms": blind}
    if env_ready is None:
        label, reason = "旧 dist 无 wrapper 打点", "重新 fsp b 后可细分环境准备与入口执行"
    elif entry_done is None:
        label, reason = "入口执行未完成打点", "os._exit 或异常退出，入口内耗时未归因"
    elif gap < _GAP_MIN_MS:
        label, reason = "进程创建与收尾(约)", "子进程创建/杀毒扫描/解释器退出等外部开销"
    else:
        label, reason = "进程收尾", "解释器退出与资源清理"
    _row("[未细分]", label, f"~{gap:.0f}ms", _fmt_pct(gap, wall_ms), _fmt_bar(gap, wall_ms))
    print("  " + _pad("", _TAG_W) + reason)
    return None


def _print_loader_section(sample: ProfileSample, wall_ms: float) -> tuple[float, float]:
    """打印 loader 各阶段与 C 层初始化行，返回 ``(loader 总耗时, py_init 毫秒)``.

    loader 总耗时取各阶段打点的最大值：阶段打点口径为相邻累计时刻差，
    相加会重复计入重叠段，取 max 是保守估计。
    """
    loader_total = 0.0
    for stage, ms, suffix in sample.loader_stages:
        loader_total = max(loader_total, ms)
        _row("[loader]", f"{stage}{suffix}", _fmt_ms(ms), _fmt_pct(ms, wall_ms), _fmt_bar(ms, wall_ms))
    py_init_ms = sample.gap_stages.get("py_init") or 0.0
    if py_init_ms > 0:
        _row(
            "[py-init]",
            "C 层初始化(实测)",
            _fmt_ms(py_init_ms),
            _fmt_pct(py_init_ms, wall_ms),
            _fmt_bar(py_init_ms, wall_ms),
        )
    return loader_total, py_init_ms


def _print_wrapper_section(sample: ProfileSample, wall_ms: float) -> tuple[float | None, float | None]:
    """打印 wrapper 里程碑行（环境准备/界面就绪），返回 ``(entry_start, entry_done)``.

    ``gui_ready`` 为 wrapper GUI 自终止钩子输出的累计时刻（Qt
    ``QApplication.exec``/tkinter ``Tk.mainloop`` 进入前、首帧已上屏），
    是 GUI 应用"进入界面"的启动终点；CLI 应用与旧 dist 无此行。
    """
    timing_stages = sample.timing_stages
    env_ready = timing_stages.get("env_ready")
    gui_ready = timing_stages.get("gui_ready")
    if env_ready is not None:
        _row("[wrapper]", "环境准备", _fmt_ms(env_ready), _fmt_pct(env_ready, wall_ms), _fmt_bar(env_ready, wall_ms))
    if gui_ready is not None:
        _row("[gui]", "界面就绪(实测)", _fmt_ms(gui_ready), _fmt_pct(gui_ready, wall_ms), _fmt_bar(gui_ready, wall_ms))
    return timing_stages.get("entry_start"), timing_stages.get("entry_done")


def _print_summary(sample: ProfileSample) -> dict[str, Any]:
    """打印启动耗时汇总表：loader → 里程碑（环境准备/界面就绪）→ 解释器初始化 → import 细分 → 入口执行（导入/执行细分）→ 未细分.

    ``sample.post_entry_lines`` 为 ``entry_start`` 打点之后的 importtime 行
    （入口执行期间的导入），有 ``entry_start`` 打点时用于细分"用户入口
    执行"段；缺失（旧 dist）时入口执行整段展示，行为与旧版一致。

    返回结构化剖析数据（毫秒单位），供落盘性能日志（``fsp r --profile``）::

        {
          "wall_ms": 总墙钟毫秒, "returncode": 退出码,
          "stages": [("loader 各阶段/环境准备/界面就绪/解释器初始化(约)/用户入口执行", ms), ...],
          "top_imports": [("wrapper 顶层根导入名", cumulative_ms), ...],
          "entry_imports": [("入口执行期间根导入名", cumulative_ms), ...],
          "top_self": [("模块名", self_ms), ...],
          "gap_breakdown": {"head_ms": 进程创建与映像加载, "tail_ms": 进程收尾,
                            "blind_ms": 其余盲区},  # 有首/末行实测时才有
          "runs_ms": [各次运行 wall 毫秒], "repeat": 次数,  # repeat > 1 时才有
        }

    ``stages`` 仅含主阶段（子项与"未细分"归因不稳定，不参与对比）；
    ``gap_breakdown`` 含管道延迟等噪声同样不参与对比，仅供逐次诊断；
    导入列表存全量根导入（打印截 top，落盘全量供后续分析）。
    """
    wall_ms = sample.wall_ms
    timing_stages = sample.timing_stages
    import_lines = sample.import_lines
    post_entry_lines: list[str] = sample.post_entry_lines
    # 防御：无 entry_start 打点却有分界数据（理论不可达），并回主列表
    if timing_stages.get("entry_start") is None and post_entry_lines:
        import_lines = [*import_lines, *post_entry_lines]
        post_entry_lines = []
    print(f"[fspack] 启动耗时剖析（总 {wall_ms:.0f}ms，退出码 {sample.returncode}）")
    if sample.timed_out:
        print("  （进程超时被强制终止，数据不完整：GUI 框架不在自终止钩子支持清单或程序长跑）")
    print("─" * _SEP_LEN)
    interp_ms = 0.0
    user_roots: list[tuple[str, float]] = []
    self_top: list[tuple[str, float]] = []
    if import_lines:
        interp_ms, user_roots, self_top = _parse_import_lines(import_lines)
    post_roots: list[tuple[str, float]] = []
    if post_entry_lines:
        # 入口执行期间的导入段无 runpy 锚点（runpy 在 entry_start 前已导入），
        # 跳过分界：全部根导入即入口导入列表
        _, post_roots, post_self = _parse_import_lines(post_entry_lines, anchor=None)
        # 模块自身耗时 top 合并两段（各自 top 的并集再取全局 top）
        self_top = sorted([*self_top, *post_self], key=lambda x: -x[1])[:_TOP_SELF]
    loader_total, py_init_ms = _print_loader_section(sample, wall_ms)
    entry_start, entry_done = _print_wrapper_section(sample, wall_ms)
    _print_import_sections(interp_ms, user_roots, self_top, wall_ms)
    if entry_start is not None and entry_done is not None:
        exec_ms = entry_done - entry_start
        _row("[wrapper]", "用户入口执行", _fmt_ms(exec_ms), _fmt_pct(exec_ms, wall_ms), _fmt_bar(exec_ms, wall_ms))
        if exec_ms >= _MIN_DISPLAY_MS:
            _print_entry_breakdown(exec_ms, post_roots, wall_ms)
    elif entry_start is not None:
        _row("[wrapper]", "用户入口执行", "未返回")
    gap_breakdown = _print_unaccounted(sample, loader_total, interp_ms, py_init_ms)
    # 落盘数据：主阶段用 loader 打点原文阶段名（suffix 为补充说明，跨次
    # 运行不稳定，不并入名字）
    stages = _build_stage_list(sample, py_init_ms, interp_ms, entry_start, entry_done)
    data: dict[str, Any] = {
        "wall_ms": wall_ms,
        "returncode": sample.returncode,
        "stages": stages,
        "top_imports": list(user_roots),
        "entry_imports": list(post_roots),
        "top_self": list(self_top),
    }
    if gap_breakdown is not None:
        data["gap_breakdown"] = gap_breakdown
    return data


def _build_stage_list(
    sample: ProfileSample,
    py_init_ms: float,
    interp_ms: float,
    entry_start: float | None,
    entry_done: float | None,
) -> list[tuple[str, float]]:
    """构造落盘主阶段列表：loader 各阶段 → C 层初始化 → 环境准备 → 解释器初始化 → 界面就绪 → 用户入口执行."""
    timing_stages = sample.timing_stages
    env_ready = timing_stages.get("env_ready")
    gui_ready = timing_stages.get("gui_ready")
    stages: list[tuple[str, float]] = [(stage, ms) for stage, ms, _ in sample.loader_stages]
    if py_init_ms > 0:
        stages.append(("C 层初始化(实测)", py_init_ms))
    if env_ready is not None:
        stages.append(("环境准备", env_ready))
    if interp_ms > 0:
        stages.append(("解释器初始化(约)", interp_ms))
    if gui_ready is not None:
        stages.append(("界面就绪", gui_ready))
    if entry_start is not None and entry_done is not None:
        stages.append(("用户入口执行", entry_done - entry_start))
    return stages
