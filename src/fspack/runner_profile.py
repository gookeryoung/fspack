"""``fsp r --profile`` 启动耗时剖析：采集与汇总.

流式读取子进程 stderr，采集三类打点标记并解析为启动耗时汇总表：

- ``[fspack loader] <阶段> 耗时 <ms>ms``：C loader 各阶段耗时，
  由 ``FSPACK_LOADER_VERBOSE=1`` 激活（三平台 loader 已内置打点）。
- ``[fspack timing] <label> @<累计ms>ms``：入口包装器各阶段累计时刻，
  由 ``FSPACK_TIMING=1`` 激活（新构建的 dist wrapper 已内置打点；
  旧 dist 无此类行，汇总缺 wrapper 段并提示重新构建）。
- ``import time: <self> | <cumulative> | <缩进><模块名>``：CPython 原生
  ``-X importtime`` 逐模块导入耗时，由 ``PYTHONPROFILEIMPORTTIME=1`` 激活。

非标记行（程序自身的 stderr 输出）原样透传；标记行与 importtime 原始行
不透传，由退出后打印的对齐汇总表替代（阶段/名称/耗时/占比/条形图五列，
CJK 全宽字符按 2 列对齐，条形图用 █▓░ 按占比填充；"用户入口执行"段按
``entry_start`` 打点分界细分为导入合计与其余执行，stderr 行序即时间序；
末端"未细分"行解释未被各阶段覆盖的 wall time 去向）。
stdout/stdin 继承父进程，交互与正常输出不受影响。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import unicodedata

__all__ = ["PROFILE_ENV", "run_with_profile"]

# 激活三个数据源所需注入的环境变量（loader 打点 / wrapper 打点 / importtime）
PROFILE_ENV = {"FSPACK_LOADER_VERBOSE": "1", "FSPACK_TIMING": "1", "PYTHONPROFILEIMPORTTIME": "1"}

_LOADER_PREFIX = "[fspack loader]"
_TIMING_PREFIX = "[fspack timing]"
_IMPORTTIME_PREFIX = "import time:"
# loader 打点行：[fspack loader] <阶段> 耗时 <ms>ms[（补充说明）]
_LOADER_RE = re.compile(r"\[fspack loader\]\s*(.+?)\s*耗时\s*([0-9.]+)ms(.*)")
# wrapper 打点行：[fspack timing] <label> @<累计ms>ms
_TIMING_RE = re.compile(r"\[fspack timing\]\s*(\S+)\s*@\s*([0-9.]+)ms")
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


def run_with_profile(cmd: list[str], env: dict[str, str] | None = None) -> int:
    """运行目标程序并采集启动耗时打点，子进程退出后打印汇总.

    stdout/stdin 继承父进程（交互与正常输出不受影响）；stderr 经管道流式
    读取：非标记行原样透传，标记行与 importtime 行由汇总替代。

    importtime 行按 ``entry_start`` 打点分界为两段：之前的归 wrapper
    （解释器初始化 + wrapper 顶层导入），之后的归用户入口执行期间的导入
    （stderr 同管道行序即时间序，入口细分由 :func:`_print_summary` 完成）。
    返回子进程退出码。
    """
    t_start = time.perf_counter()
    proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE)
    loader_stages: list[tuple[str, float, str]] = []
    timing_stages: dict[str, float] = {}
    import_lines: list[str] = []
    post_entry_lines: list[str] = []
    entry_started = False
    assert proc.stderr is not None
    for raw in proc.stderr:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith(_IMPORTTIME_PREFIX):
            (post_entry_lines if entry_started else import_lines).append(line)
            continue
        if line.startswith(_LOADER_PREFIX):
            m = _LOADER_RE.match(line)
            if m is not None:
                loader_stages.append((m.group(1), float(m.group(2)), m.group(3)))
                continue
        if line.startswith(_TIMING_PREFIX):
            m = _TIMING_RE.match(line)
            if m is not None:
                timing_stages[m.group(1)] = float(m.group(2))
                if m.group(1) == "entry_start":
                    entry_started = True
                continue
        # 非标记行（程序自身输出/无法解析的 loader 错误行）原样透传
        sys.stderr.write(line + "\n")
    sys.stderr.flush()
    proc.stderr.close()
    returncode = proc.wait()
    wall_ms = (time.perf_counter() - t_start) * 1000.0
    _print_summary(wall_ms, returncode, loader_stages, timing_stages, import_lines, post_entry_lines)
    return returncode


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
    wall_ms: float,
    loader_total: float,
    interp_ms: float,
    timing_stages: dict[str, float],
) -> None:
    """打印"未细分"行：wall 减去已归因阶段（loader 总耗时 + 解释器初始化 +
    wrapper 末次打点累计值）。

    - gap ≥ 100ms 且占 5% 以上：按可用打点解释去向——旧 dist 无 wrapper
      打点 → 提示重新构建细分；缺末次打点 → 入口未完成；否则进程收尾。
    - gap < 100ms 但占比超 30%：快程序的外部开销占比天然高（子进程创建、
      杀毒扫描、解释器退出等不在任何打点段内），同样展示并注明。
    """
    env_ready = timing_stages.get("env_ready")
    entry_done = timing_stages.get("entry_done")
    gap = wall_ms - (loader_total + interp_ms + max(entry_done or 0.0, env_ready or 0.0))
    high_ratio = gap >= wall_ms * _GAP_HIGH_RATIO
    if not high_ratio and (gap < _GAP_MIN_MS or gap < wall_ms * _GAP_MIN_RATIO):
        return
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


def _print_summary(  # noqa: PLR0913
    wall_ms: float,
    returncode: int,
    loader_stages: list[tuple[str, float, str]],
    timing_stages: dict[str, float],
    import_lines: list[str],
    post_entry_lines: list[str] | None = None,
) -> None:
    """打印启动耗时汇总表：loader → 环境准备 → 解释器初始化 → import 细分 → 入口执行（导入/执行细分）→ 未细分.

    ``post_entry_lines`` 为 ``entry_start`` 打点之后的 importtime 行
    （入口执行期间的导入），有 ``entry_start`` 打点时用于细分"用户入口
    执行"段；缺失（旧 dist）时入口执行整段展示，行为与旧版一致。
    """
    if post_entry_lines is None:
        post_entry_lines = []
    entry_start = timing_stages.get("entry_start")
    # 防御：无 entry_start 打点却有分界数据（理论不可达），并回主列表
    if entry_start is None and post_entry_lines:
        import_lines = [*import_lines, *post_entry_lines]
        post_entry_lines = []
    print(f"[fspack] 启动耗时剖析（总 {wall_ms:.0f}ms，退出码 {returncode}）")
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
    loader_total = 0.0
    for stage, ms, suffix in loader_stages:
        loader_total = max(loader_total, ms)
        _row("[loader]", f"{stage}{suffix}", _fmt_ms(ms), _fmt_pct(ms, wall_ms), _fmt_bar(ms, wall_ms))
    env_ready = timing_stages.get("env_ready")
    entry_done = timing_stages.get("entry_done")
    if env_ready is not None:
        _row("[wrapper]", "环境准备", _fmt_ms(env_ready), _fmt_pct(env_ready, wall_ms), _fmt_bar(env_ready, wall_ms))
    _print_import_sections(interp_ms, user_roots, self_top, wall_ms)
    if entry_start is not None and entry_done is not None:
        exec_ms = entry_done - entry_start
        _row("[wrapper]", "用户入口执行", _fmt_ms(exec_ms), _fmt_pct(exec_ms, wall_ms), _fmt_bar(exec_ms, wall_ms))
        if exec_ms >= _MIN_DISPLAY_MS:
            _print_entry_breakdown(exec_ms, post_roots, wall_ms)
    elif entry_start is not None:
        _row("[wrapper]", "用户入口执行", "未返回")
    _print_unaccounted(wall_ms, loader_total, interp_ms, timing_stages)
