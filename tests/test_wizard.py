"""wizard 交互选择组件测试.

覆盖 :func:`fspack.wizard.select_item` 的导航/确认/取消行为与
``_read_key_windows``/``_read_key_posix`` 的按键归一化（用假模块注入
sys.modules，两个平台分支在任何系统上都可测）。
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from fspack import wizard

# ---- select_item 交互行为 ----


def _fake_read_key(monkeypatch: pytest.MonkeyPatch, keys: list[str]) -> None:
    """将 wizard._read_key 替换为按序弹出按键事件的桩（耗尽后 StopIteration 报错）."""
    seq = iter(keys)
    monkeypatch.setattr(wizard, "_read_key", lambda: next(seq))


def test_select_item_enter_immediately_returns_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """直接按 Enter → 返回第 0 项索引."""
    _fake_read_key(monkeypatch, ["enter"])
    assert wizard.select_item("选择", ["a", "b", "c"]) == 0


def test_select_item_navigation_down_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """down×2 + up → 返回第 1 项."""
    _fake_read_key(monkeypatch, ["down", "down", "up", "enter"])
    assert wizard.select_item("选择", ["a", "b", "c"]) == 1


def test_select_item_wrap_around_from_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """首项按 up → 循环滚动到最后项."""
    _fake_read_key(monkeypatch, ["up", "enter"])
    assert wizard.select_item("选择", ["a", "b", "c"]) == 2


def test_select_item_wrap_around_from_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """末项按 down → 循环滚动回第 0 项."""
    _fake_read_key(monkeypatch, ["up", "down", "enter"])
    assert wizard.select_item("选择", ["a", "b", "c"]) == 0


def test_select_item_esc_raises_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Esc → 抛 KeyboardInterrupt（调用方与 Ctrl+C 同路径处理）."""
    _fake_read_key(monkeypatch, ["esc"])
    with pytest.raises(KeyboardInterrupt):
        wizard.select_item("选择", ["a", "b"])


def test_select_item_q_raises_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """q 的归一化事件 cancel → 抛 KeyboardInterrupt."""
    _fake_read_key(monkeypatch, ["cancel"])
    with pytest.raises(KeyboardInterrupt):
        wizard.select_item("选择", ["a", "b"])


def test_select_item_ignores_other_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """未知按键（other）不移动高亮、不退出，直到 Enter 确认."""
    _fake_read_key(monkeypatch, ["other", "other", "enter"])
    assert wizard.select_item("选择", ["a", "b"]) == 0


def test_select_item_empty_items_raises() -> None:
    """空选项列表 → ValueError."""
    with pytest.raises(ValueError, match="选项列表不能为空"):
        wizard.select_item("选择", [])


def test_select_item_renders_prompt_and_items(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """渲染帧包含标题、步数、选项与按键提示（锁定 UX 契约）."""
    _fake_read_key(monkeypatch, ["enter"])
    idx = wizard.select_item("选择模板", ["helloworld — Hello World", "args — 参数"], step=(1, 2))
    captured = capsys.readouterr()
    assert idx == 0
    assert "选择模板" in captured.out
    assert "helloworld — Hello World" in captured.out
    assert "args — 参数" in captured.out
    assert "Enter 确认" in captured.out


def test_select_item_detail_follows_highlight(monkeypatch: pytest.MonkeyPatch) -> None:
    """detail 回调随高亮项更新：down 后详情换为第 1 项的文本."""
    _fake_read_key(monkeypatch, ["down", "enter"])
    idx = wizard.select_item("选择", ["a", "b"], detail=lambda i: f"详情-{i}")
    assert idx == 1


def test_select_item_ctrl_c_during_read_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """读键阶段 Ctrl+C（KeyboardInterrupt）→ 透传抛出（终端属性由底层恢复）."""

    def ctrl_c() -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard, "_read_key", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        wizard.select_item("选择", ["a", "b"])


# ---- _build_frame 渲染 ----


def test_build_frame_detail_empty_not_shown() -> None:
    """detail 回调返回空串 → 帧中不出现空详情区（仅有按键提示行）."""
    frame = wizard._build_frame("选择", ["a", "b"], 0, detail=lambda i: "", step=None)
    assert "选择" in frame.plain
    assert "详情" not in frame.plain


def test_build_frame_step_and_highlight() -> None:
    """步数前缀 [1/2] 与高亮标记 > 均渲染进帧."""
    frame = wizard._build_frame("选择", ["a", "b"], 1, detail=None, step=(1, 2))
    assert "[1/2] 选择" in frame.plain
    assert "> b" in frame.plain
    assert "  a" in frame.plain


# ---- 按键归一化 ----


def test_normalize_plain_key_mapping() -> None:
    """普通字符 → 事件名映射（enter/esc/j/k/q/other）."""
    assert wizard._normalize_plain_key("\r") == "enter"
    assert wizard._normalize_plain_key("\n") == "enter"
    assert wizard._normalize_plain_key("\x1b") == "esc"
    assert wizard._normalize_plain_key("j") == "down"
    assert wizard._normalize_plain_key("k") == "up"
    assert wizard._normalize_plain_key("q") == "cancel"
    assert wizard._normalize_plain_key("x") == "other"


def test_normalize_plain_key_ctrl_c_raises() -> None:
    """Windows Ctrl+C 字符 \\x03 → KeyboardInterrupt."""
    with pytest.raises(KeyboardInterrupt):
        wizard._normalize_plain_key("\x03")


# ---- _read_key_windows（假 msvcrt 注入 sys.modules）----


class _FakeMsvcrt:
    """msvcrt 桩：按序返回 getwch 结果."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)

    def getwch(self) -> str:
        return self._keys.pop(0)


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (["\x00", "H"], "up"),  # 上方向键（\x00 前缀）
        (["\xe0", "P"], "down"),  # 下方向键（\xe0 前缀）
        (["\x00", "M"], "other"),  # 右方向键：未识别的扩展码
        (["\r"], "enter"),
        (["j"], "down"),
        (["k"], "up"),
        (["q"], "cancel"),
        (["x"], "other"),
    ],
)
def test_read_key_windows_normalization(monkeypatch: pytest.MonkeyPatch, keys: list[str], expected: str) -> None:
    """Windows 按键归一化：特殊键前缀 + 扩展码 / 普通字符."""
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(keys))
    assert wizard._read_key_windows() == expected


def test_read_key_windows_ctrl_c_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows Ctrl+C（\x03）→ KeyboardInterrupt."""
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(["\x03"]))
    with pytest.raises(KeyboardInterrupt):
        wizard._read_key_windows()


def test_read_key_dispatch_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """_read_key 在 win32 平台分发到 Windows 实现."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(["\r"]))
    assert wizard._read_key() == "enter"


# ---- _read_key_posix（假 termios/tty/select/stdin 注入）----


class _FakeTermios:
    """termios 桩：记录 tcsetattr 恢复的属性，验证终端状态还原."""

    TCSADRAIN = 0

    def __init__(self) -> None:
        self.restored: list[object] = []

    def tcgetattr(self, fd: int) -> str:
        return "old-attrs"

    def tcsetattr(self, fd: int, when: int, attrs: object) -> None:
        self.restored.append(attrs)


class _FakeTty:
    """tty 桩：setcbreak 空实现."""

    @staticmethod
    def setcbreak(fd: int) -> None:
        pass


class _FakeSelect:
    """select 桩：readable 控制转义序列判断（模拟短超时内是否有后续字节）."""

    def __init__(self, readable: bool) -> None:
        self._readable = readable

    # 参数签名对齐 select.select，rlist 含 sys.stdin
    def select(
        self, rlist: list[Any], wlist: list[Any], xlist: list[Any], timeout: float
    ) -> tuple[list[Any], list[Any], list[Any]]:
        return ([sys.stdin], [], []) if self._readable else ([], [], [])


class _FakeStdin:
    """stdin 桩：按序返回 read 结果（每个 read(n) 弹出一个脚本块）."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = list(chunks)

    def fileno(self) -> int:
        return 0

    def read(self, n: int) -> str:
        return self._chunks.pop(0)


def _patch_posix_env(monkeypatch: pytest.MonkeyPatch, chunks: list[str], readable: bool) -> _FakeTermios:
    """注入 POSIX 读键环境（假 stdin/termios/tty/select），返回 termios 桩."""
    monkeypatch.setattr(sys, "stdin", _FakeStdin(chunks))
    termios = _FakeTermios()
    monkeypatch.setitem(sys.modules, "termios", termios)
    monkeypatch.setitem(sys.modules, "tty", _FakeTty())
    monkeypatch.setitem(sys.modules, "select", _FakeSelect(readable))
    return termios


@pytest.mark.parametrize(
    ("chunks", "readable", "expected"),
    [
        (["\x1b", "[A"], True, "up"),  # 上方向键转义序列
        (["\x1b", "[B"], True, "down"),  # 下方向键转义序列
        (["\x1b", "[C"], True, "other"),  # 未识别的转义序列
        (["\x1b"], False, "esc"),  # 裸 Esc（短超时无后续字节）
        (["\r"], False, "enter"),  # 普通字符不触发 select
        (["j"], False, "down"),
        (["k"], False, "up"),
        (["q"], False, "cancel"),
        (["x"], False, "other"),
    ],
)
def test_read_key_posix_normalization(
    monkeypatch: pytest.MonkeyPatch, chunks: list[str], readable: bool, expected: str
) -> None:
    """POSIX 按键归一化：转义序列 / 裸 Esc / 普通字符."""
    termios = _patch_posix_env(monkeypatch, chunks, readable)
    assert wizard._read_key_posix() == expected
    # 终端属性读取后原样恢复
    assert termios.restored == ["old-attrs"]


def test_read_key_posix_terminal_restored_on_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    """读键阶段 Ctrl+C（read 抛中断）→ 终端属性仍被恢复."""
    monkeypatch.setitem(sys.modules, "termios", _FakeTermios())
    monkeypatch.setitem(sys.modules, "tty", _FakeTty())
    monkeypatch.setitem(sys.modules, "select", _FakeSelect(False))

    class _InterruptStdin:
        def fileno(self) -> int:
            return 0

        def read(self, n: int) -> str:
            raise KeyboardInterrupt

    monkeypatch.setattr(sys, "stdin", _InterruptStdin())
    termios = sys.modules["termios"]
    with pytest.raises(KeyboardInterrupt):
        wizard._read_key_posix()
    assert termios.restored == ["old-attrs"]


def test_read_key_dispatch_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """_read_key 在非 win32 平台分发到 POSIX 实现."""
    monkeypatch.setattr(sys, "platform", "linux")
    _patch_posix_env(monkeypatch, ["\r"], False)
    assert wizard._read_key() == "enter"
