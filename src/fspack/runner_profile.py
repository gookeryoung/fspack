"""``fsp r --profile`` 启动耗时剖析：采集与汇总.

流式读取子进程 stderr，采集三类打点标记并解析为启动耗时汇总：

- ``[fspack loader] <阶段> 耗时 <ms>ms``：C loader 各阶段耗时，
  由 ``FSPACK_LOADER_VERBOSE=1`` 激活（三平台 loader 已内置打点）。
- ``[fspack timing] <label> @<累计ms>ms``：入口包装器各阶段累计时刻，
  由 ``FSPACK_TIMING=1`` 激活（新构建的 dist wrapper 已内置打点；
  旧 dist 无此类行，汇总缺 wrapper 段）。
- ``import time: <self> | <cumulative> | <缩进><模块名>``：CPython 原生
  ``-X importtime`` 逐模块导入耗时，由 ``PYTHONPROFILEIMPORTTIME=1`` 激活。

非标记行（程序自身的 stderr 输出）原样透传；标记行与 importtime 原始行
不透传，由退出后打印的汇总替代，避免刷屏。stdout/stdin 继承父进程，
交互与正常输出不受影响。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

__all__ = ["PROFILE_ENV", "run_with_profile"]

# 激活三个数据源所需注入的环境变量（loader 打点 / wrapper 打点 / importtime）
PROFILE_ENV = {"FSPACK_LOADER_VERBOSE": "1", "FSPACK_TIMING": "1", "PYTHONPROFILEIMPORTTIME": "1"}

_LOADER_PREFIX = "[fspack loader]"
_TIMING_PREFIX = "[fspack timing]"
_IMPORTTIME_PREFIX = "import time:"
# loader 打点行：[fspack loader] <阶段> 耗时 <ms>ms[（补充说明）]；
# 展示时保留去前缀原文（阶段名与"耗时"连写场景如"loader 总耗时"不可拆分重组）
_LOADER_RE = re.compile(r"\[fspack loader\]\s*(.+?)\s*耗时\s*([0-9.]+)ms(.*)")
# wrapper 打点行：[fspack timing] <label> @<累计ms>ms
_TIMING_RE = re.compile(r"\[fspack timing\]\s*(\S+)\s*@\s*([0-9.]+)ms")
# importtime 名字字段每级缩进 2 空格，深度 = 缩进空格数 // 2
_NAME_INDENT = 2
# 汇总中顶层导入与模块自身耗时的展示条数上限
_TOP_ROOTS = 8
_TOP_SELF = 10
# 低于该阈值（ms）的条目在汇总中不展示，避免噪音
_MIN_DISPLAY_MS = 0.1


def run_with_profile(cmd: list[str], env: dict[str, str] | None = None) -> int:
    """运行目标程序并采集启动耗时打点，子进程退出后打印汇总.

    stdout/stdin 继承父进程（交互与正常输出不受影响）；stderr 经管道流式
    读取：非标记行原样透传，标记行与 importtime 行由汇总替代。
    返回子进程退出码。
    """
    t_start = time.perf_counter()
    proc = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE)
    loader_stages: list[str] = []
    timing_stages: dict[str, float] = {}
    import_lines: list[str] = []
    assert proc.stderr is not None
    for raw in proc.stderr:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith(_IMPORTTIME_PREFIX):
            import_lines.append(line)
            continue
        if line.startswith(_LOADER_PREFIX) and _LOADER_RE.match(line) is not None:
            loader_stages.append(line[len(_LOADER_PREFIX) :].strip())
            continue
        if line.startswith(_TIMING_PREFIX):
            m = _TIMING_RE.match(line)
            if m is not None:
                timing_stages[m.group(1)] = float(m.group(2))
                continue
        # 非标记行（程序自身输出/无法解析的 loader 错误行）原样透传
        sys.stderr.write(line + "\n")
    sys.stderr.flush()
    proc.stderr.close()
    returncode = proc.wait()
    wall_ms = (time.perf_counter() - t_start) * 1000.0
    _print_summary(wall_ms, returncode, loader_stages, timing_stages, import_lines)
    return returncode


def _parse_import_lines(lines: list[str]) -> tuple[float, list[tuple[str, float]], list[tuple[str, float]]]:
    """解析 importtime 行，返回 (解释器初始化耗时, 顶层导入列表, 模块自身耗时列表).

    - 解释器初始化耗时（约）：wrapper 首次 import ``glob`` 之前的全部
      depth-0 根导入 cumulative 之和（encodings/site 等解释器启动导入）。
    - 顶层导入列表：``glob`` 及其后的 depth-0 根导入（wrapper 自身与用户
      代码的顶层导入），值为 cumulative 毫秒。
    - 模块自身耗时列表：全部模块按 self 毫秒降序的前 :data:`_TOP_SELF` 条。

    行格式 ``import time: <self_us> | <cum_us> | <缩进><name>``，无法解析的
    行（表头/畸形行）跳过。未找到 ``glob``（理论上仅旧版 wrapper）时全部
    根导入计入解释器初始化段。
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
    glob_idx = next((i for i, (n, _) in enumerate(roots) if n == "glob"), None)
    if glob_idx is None:
        return sum(c for _, c in roots), [], sorted(self_items, key=lambda x: -x[1])[:_TOP_SELF]
    interp_ms = sum(c for _, c in roots[:glob_idx])
    return interp_ms, roots[glob_idx:], sorted(self_items, key=lambda x: -x[1])[:_TOP_SELF]


def _print_summary(
    wall_ms: float,
    returncode: int,
    loader_stages: list[str],
    timing_stages: dict[str, float],
    import_lines: list[str],
) -> None:
    """打印启动耗时汇总：loader → wrapper 环境准备 → 解释器初始化 → import 细分 → 用户入口执行."""
    print(f"[fspack] 启动耗时剖析（总 wall time {wall_ms:.0f}ms，退出码 {returncode}）")
    for text in loader_stages:
        print(f"  [loader]   {text}")
    env_ready = timing_stages.get("env_ready")
    entry_start = timing_stages.get("entry_start")
    entry_done = timing_stages.get("entry_done")
    if env_ready is not None:
        print(f"  [wrapper]  环境准备  {env_ready:.1f}ms  （site-packages/Qt/lazy hooks/web 补丁）")
    if import_lines:
        interp_ms, user_roots, self_top = _parse_import_lines(import_lines)
        print(f"  [import]   解释器初始化(约)  {interp_ms:.1f}ms  （encodings/site 根导入累计）")
        roots_str = " | ".join(
            f"{n} {c:.1f}ms" for n, c in sorted(user_roots, key=lambda x: -x[1])[:_TOP_ROOTS] if c >= _MIN_DISPLAY_MS
        )
        if roots_str:
            print(f"  [import]   顶层导入:  {roots_str}")
        top_str = " | ".join(f"{n} {s:.1f}ms" for n, s in self_top if s >= _MIN_DISPLAY_MS)
        if top_str:
            print(f"  [import]   模块自身耗时 top{_TOP_SELF}:  {top_str}")
    if entry_start is not None and entry_done is not None:
        print(f"  [wrapper]  用户入口执行  {entry_done - entry_start:.1f}ms  （runpy 开始 → 入口返回）")
    elif entry_start is not None:
        print("  [wrapper]  用户入口执行  未返回（入口未完成或 os._exit 退出）")
