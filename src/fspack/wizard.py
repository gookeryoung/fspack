"""交互式向导选择：方向键导航的列表选择组件.

供 ``fsp init`` 等命令做向导式多步选择（如先选项目类型、再选具体模板），
替代数字编号菜单。TTY 环境下用 ↑/↓（或 j/k）移动、Enter 确认、Esc/q 取消；
非 TTY 环境的回退策略由调用方决定（本模块不感知业务默认值）。

平台适配：

- Windows：``msvcrt.getwch`` 读键（无回显），方向键等特殊键以
  ``\\x00``/``\\xe0`` 前缀 + 扩展码两个 ``getwch`` 调用返回
- POSIX：``termios`` cbreak 模式逐字节读取（无回显、无行缓冲），
  用 ``select`` 短超时区分裸 Esc 与方向键转义序列（``\\x1b[A``/``\\x1b[B``）

渲染用 :class:`rich.live.Live` 原地刷新（legacy Windows 控制台由 rich
自动适配 win32 API），高亮项的详情文本（``detail`` 回调）随选择动态更新。

公共 API：

- :func:`select_item` — 交互式单选（返回选中索引）
"""

from __future__ import annotations

import sys
from typing import Callable, Final, Sequence

from rich.live import Live
from rich.text import Text

from fspack.console import console

__all__ = ["select_item"]

# 归一化按键事件名
KEY_UP: Final = "up"
KEY_DOWN: Final = "down"
KEY_ENTER: Final = "enter"
KEY_ESC: Final = "esc"
KEY_CANCEL: Final = "cancel"

# Windows 扩展键码 → 归一化事件（getwch 返回 \x00/\xe0 前缀后的第二字节）
_WIN_SPECIAL_KEYS: Final[dict[str, str]] = {"H": KEY_UP, "P": KEY_DOWN}

# 底部按键提示
_KEY_HINT: Final = "↑/↓ 移动 · Enter 确认 · Esc 取消"


def _normalize_plain_key(ch: str) -> str:
    """将普通字符按键归一化为事件名.

    :param ch: 单个字符（``\\r``/``\\n``/字母等）
    :return: 事件名（``enter``/``esc``/``cancel``/``up``/``down``/``other``）
    :raises KeyboardInterrupt: Windows 下 Ctrl+C 以 ``\\x03`` 字符返回（getwch
        不触发 SIGINT），归一化时转换为中断
    """
    if ch in ("\r", "\n"):
        return KEY_ENTER
    if ch == "\x1b":
        return KEY_ESC
    if ch == "\x03":
        # Windows getwch 不触发 SIGINT，Ctrl+C 以 \x03 字符返回
        raise KeyboardInterrupt
    if ch == "j":
        return KEY_DOWN
    if ch == "k":
        return KEY_UP
    if ch == "q":
        return KEY_CANCEL
    return "other"


def _read_key_windows() -> str:
    """Windows 平台读取一个按键并归一化为事件名.

    用 :func:`msvcrt.getwch` 读键（无回显）；方向键等特殊键以
    ``\\x00``/``\\xe0`` 前缀 + 扩展码两个 ``getwch`` 调用返回。

    :return: 归一化按键事件名
    :raises KeyboardInterrupt: 用户按 Ctrl+C
    """
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        # 特殊键前缀：再读一个扩展码字节
        return _WIN_SPECIAL_KEYS.get(msvcrt.getwch(), "other")
    return _normalize_plain_key(ch)


def _read_key_posix() -> str:
    """POSIX 平台读取一个按键并归一化为事件名.

    ``termios`` cbreak 模式逐字节读取（无回显、无行缓冲）。方向键等转义
    序列以 ``\\x1b`` 开头：用 :func:`select.select` 短超时判断后续字节
    是否到达——有则读取并识别（``\\x1b[A`` 上/``\\x1b[B`` 下），无则视为
    裸 Esc（用户取消）。cbreak 保留 ISIG，Ctrl+C 直接触发 SIGINT。

    :return: 归一化按键事件名
    :raises KeyboardInterrupt: 用户按 Ctrl+C
    """
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    # termios/tty 为 POSIX 专属模块，Windows 桩缺这些属性；pyrefly 误报加规则码忽略
    old_attrs = termios.tcgetattr(fd)  # type: ignore[missing-attribute]
    try:
        tty.setcbreak(fd)  # type: ignore[missing-attribute]
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # 短超时内无后续字节 → 裸 Esc；有 → 转义序列（方向键等）
            if select.select([sys.stdin], [], [], 0.05)[0]:
                seq = sys.stdin.read(2)
                if seq == "[A":
                    return KEY_UP
                if seq == "[B":
                    return KEY_DOWN
                return "other"
            return KEY_ESC
        return _normalize_plain_key(ch)
    finally:
        # 无论正常返回还是中断，都恢复终端原属性
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)  # type: ignore[missing-attribute]


def _read_key() -> str:
    """读取一个按键并归一化为事件名（按当前平台分发）.

    :return: 归一化按键事件名
    :raises KeyboardInterrupt: 用户按 Ctrl+C
    """
    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix()


def _build_frame(
    prompt: str,
    items: Sequence[str],
    idx: int,
    *,
    detail: Callable[[int], str] | None,
    step: tuple[int, int] | None,
) -> Text:
    """构造选择列表的当前帧.

    :param prompt: 标题文案
    :param items: 选项文本列表
    :param idx: 当前高亮项索引
    :param detail: 高亮项详情回调（返回空串则不显示详情区）
    :param step: 向导步数 ``(当前步, 总步数)``，``None`` 不显示步数
    :return: rich Text 帧（标题、选项列表、详情区、按键提示）
    """
    frame = Text()
    title = prompt if step is None else f"[{step[0]}/{step[1]}] {prompt}"
    frame.append(f"? {title}\n", style="bold blue")
    for i, item in enumerate(items):
        if i == idx:
            frame.append(f"> {item}\n", style="bold cyan")
        else:
            frame.append(f"  {item}\n", style="")
    if detail is not None:
        detail_text = detail(idx)
        if detail_text:
            frame.append("\n")
            frame.append(f"{detail_text}\n", style="dim")
    frame.append(f"\n{_KEY_HINT}", style="dim")
    return frame


def select_item(
    prompt: str,
    items: Sequence[str],
    *,
    detail: Callable[[int], str] | None = None,
    step: tuple[int, int] | None = None,
) -> int:
    """TTY 交互式单选：↑/↓（j/k）移动，Enter 确认，Esc/q 取消.

    用 :class:`rich.live.Live` 原地刷新列表帧，高亮项随按键移动（首尾
    循环滚动）；确认后保留最终帧（高亮停留在选中项上），取消抛
    :class:`KeyboardInterrupt` 交由调用方统一处理（与 Ctrl+C 同路径）。

    :param prompt: 标题文案（如 ``选择项目类型``）
    :param items: 选项文本列表（至少一项）
    :param detail: 可选回调，入参为当前高亮索引，返回详情文本（显示在
        列表下方，随高亮动态更新；返回空串不显示详情区）
    :param step: 向导步数 ``(当前步, 总步数)``，``None`` 不显示步数
    :return: 选中项的索引
    :raises ValueError: ``items`` 为空
    :raises KeyboardInterrupt: 用户按 Esc/q 或 Ctrl+C 取消
    """
    if not items:
        raise ValueError("选项列表不能为空")

    idx = 0
    with Live(
        _build_frame(prompt, items, idx, detail=detail, step=step),
        console=console.rich,
        auto_refresh=False,
        vertical_overflow="visible",
    ) as live:
        while True:
            key = _read_key()
            if key == KEY_UP:
                idx = (idx - 1) % len(items)
            elif key == KEY_DOWN:
                idx = (idx + 1) % len(items)
            elif key == KEY_ENTER:
                break
            elif key in (KEY_ESC, KEY_CANCEL):
                raise KeyboardInterrupt
            # 其余按键（other/左右方向等）忽略，仅刷新帧
            live.update(_build_frame(prompt, items, idx, detail=detail, step=step), refresh=True)
    return idx
