"""console 彩色输出测试."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from fspack import logutil
from fspack.console import StageLogHandler, console


def test_step_prints_title() -> None:
    """step 打印步骤标题，含 > 标记."""
    with console.rich.capture() as capture:
        console.step("解析项目")
    out = capture.get()
    assert "解析项目" in out
    assert ">" in out


def test_logger_message_brackets_not_swallowed() -> None:
    """阶段级 handler 不做 markup 解析：``[project.scripts]`` 等方括号按字面输出."""
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    console.setup_logging()
    try:
        with console.rich.capture() as capture:
            logging.getLogger("fspack.test.markup").info("[project.scripts] 已解析")
        out = capture.get()
    finally:
        root.handlers.clear()
        root.handlers.extend(previous_handlers)
        root.setLevel(previous_level)
    assert "[project.scripts] 已解析" in out


def test_tool_output_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """外部工具原始输出按整行、带 [tool] 前缀进入 DEBUG."""
    with caplog.at_level(logging.DEBUG, logger="fspack.console"):
        console.tool_output("nuitka", "gcc -c a.c\nINFO: 编译中\n".encode())
    assert "[nuitka] gcc -c a.c" in caplog.text
    assert "[nuitka] INFO: 编译中" in caplog.text


def test_tool_output_dropped_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """默认 INFO 级别下工具输出不进入终端日志（只在 -v/日志文件可见）."""
    with caplog.at_level(logging.INFO, logger="fspack.console"):
        console.tool_output("pip", b"Downloading numpy.whl\n")
    assert "Downloading numpy.whl" not in caplog.text


def test_stage_header_and_finish_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """交互式终端：阶段标题 + 缩进完成行（耗时与备注）."""
    from fspack.console import ConsoleUI

    monkeypatch.setattr(ConsoleUI, "is_interactive", property(lambda self: True))
    ui = ConsoleUI()
    with ui.rich.capture() as capture:
        ui.stage_header(3, "分析依赖")
        ui.stage_finish(3, "分析依赖", "0.48s", "缓存命中")
    out = capture.get()
    assert "[3] 分析依赖" in out
    assert "ok" in out
    assert "0.48s" in out
    assert "缓存命中" in out


def test_stage_non_tty_emits_start_and_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 TTY：开始行 + 完成行各一条，带 [i] 前缀（CI 可 grep，长阶段不静默）."""
    from fspack.console import CICompat, ConsoleUI

    monkeypatch.setattr(CICompat, "is_github_actions", staticmethod(lambda: False))
    monkeypatch.setattr(ConsoleUI, "is_interactive", property(lambda self: False))
    ui = ConsoleUI()
    with ui.rich.capture() as capture:
        ui.stage_header(3, "分析依赖")
        ui.stage_finish(3, "分析依赖", "0.48s", "缓存命中")
    out = capture.get()
    assert "[3] 分析依赖 ..." in out
    assert "[3] 分析依赖 ... ok 0.48s (缓存命中)" in out


def test_stage_github_actions_uses_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub Actions：阶段开始/结束输出 ::group:: / ::endgroup:: 折叠."""
    from fspack.console import CICompat, ConsoleUI

    monkeypatch.setattr(CICompat, "is_github_actions", staticmethod(lambda: True))
    monkeypatch.setattr(ConsoleUI, "is_interactive", property(lambda self: False))
    ui = ConsoleUI()
    with ui.rich.capture() as capture:
        ui.stage_header(2, "下载依赖")
        ui.stage_finish(2, "下载依赖", "3.10s")
    out = capture.get()
    assert "::group::[2] 下载依赖" in out
    assert "::endgroup::" in out


def test_result_line_only_when_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """最终结果行只在非交互场景输出，交互终端沿用 success/error."""
    from fspack.console import CICompat, ConsoleUI

    monkeypatch.setattr(CICompat, "is_github_actions", staticmethod(lambda: False))
    monkeypatch.setattr(ConsoleUI, "is_interactive", property(lambda self: False))
    ui = ConsoleUI()
    with ui.rich.capture() as capture:
        ui.result(True, "total=95.2s artifacts=2")
        ui.result(False, "依赖下载失败")
    out = capture.get()
    assert "RESULT ok total=95.2s artifacts=2" in out
    assert "RESULT fail 依赖下载失败" in out

    monkeypatch.setattr(ConsoleUI, "is_interactive", property(lambda self: True))
    interactive = ConsoleUI()
    with interactive.rich.capture() as capture:
        interactive.result(True, "ignored")
    assert capture.get() == ""


def test_stage_finish_non_tty_failure_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 TTY 失败阶段输出 FAIL，便于 CI 直接判定."""
    from fspack.console import ConsoleUI

    monkeypatch.setattr(ConsoleUI, "is_interactive", property(lambda self: False))
    ui = ConsoleUI()
    with ui.rich.capture() as capture:
        ui.stage_finish(9, "Nuitka 编译", "42.10s", failed=True)
    out = capture.get()
    assert "[9] Nuitka 编译 ... FAIL 42.10s" in out


def test_stage_header_escapes_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """阶段名中的方括号按字面显示，不被 rich 当作 markup 吞掉."""
    from fspack.console import ConsoleUI

    monkeypatch.setattr(ConsoleUI, "is_interactive", property(lambda self: True))
    ui = ConsoleUI()
    with ui.rich.capture() as capture:
        ui.stage_header(1, "[project.scripts] 校验")
    assert "[project.scripts] 校验" in capture.get()


def test_success_prints_check() -> None:
    """success 打印成功消息，含 √ 标记."""
    with console.rich.capture() as capture:
        console.success("构建完成")
    out = capture.get()
    assert "构建完成" in out
    assert "√" in out


def test_warn_prints_warning() -> None:
    """warn 打印警告消息，含 ! 标记."""
    with console.rich.capture() as capture:
        console.warn("注意")
    out = capture.get()
    assert "注意" in out
    assert "!" in out


def test_error_prints_cross() -> None:
    """error 打印错误消息，含 × 标记."""
    with console.rich.capture() as capture:
        console.error("失败")
    out = capture.get()
    assert "失败" in out
    assert "×" in out


def test_error_message_brackets_not_swallowed() -> None:
    """error 消息体中的方括号原样显示，不被 rich 当作 markup 标签吞掉.

    回归：``[tool.fspack.entries] 已移除支持，请改用 [project.scripts]``
    的迁移提示曾因 markup 解析渲染为 "已移除支持，请改用 声明入口"。
    """
    msg = "[tool.fspack.entries] 已移除支持，请改用 [project.scripts] 声明入口"
    with console.rich.capture() as capture:
        console.error(msg)
    out = capture.get()
    assert "[tool.fspack.entries]" in out
    assert "[project.scripts]" in out


def test_warn_and_success_message_brackets_not_swallowed() -> None:
    """warn/success 消息体同样转义，方括号原样显示."""
    for method in (console.warn, console.success):
        with console.rich.capture() as capture:
            method("[tool.fspack] 配置")
        assert "[tool.fspack]" in capture.get()


def test_setup_logging_configures_root() -> None:
    """setup_logging 配置 root logger 的 level 与阶段级 handler."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        console.setup_logging(verbose=True)
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler, StageLogHandler)
        assert handler.show_time is True

        console.setup_logging(verbose=False)
        assert root.level == logging.INFO
        assert isinstance(root.handlers[0], StageLogHandler)
        assert root.handlers[0].show_time is False
    finally:
        root.setLevel(original_level)
        root.handlers = original_handlers


def _record(level: int, message: str) -> logging.LogRecord:
    """构造一条日志记录（handler 渲染单测用）."""
    return logging.LogRecord("fspack.test", level, __file__, 1, message, None, None)


def test_stage_log_handler_prefix_is_compact() -> None:
    """INFO 前缀只有 6 列，正文固定从第 7 列（0 基 6）起，默认不带时间戳."""
    from rich.console import Console

    from fspack.console import StageLogHandler

    handler = StageLogHandler(Console(width=120, force_terminal=False))
    rendered = handler.render(_record(logging.INFO, "项目: demo 0.1.0")).plain
    assert rendered.startswith("INFO  项目")
    assert rendered.index("项目") == 6
    # 时间戳默认不出现（只在 -v 补 HH:MM:SS）
    assert ":" not in rendered[:6]


def test_stage_log_handler_verbose_prefix_has_clock() -> None:
    """-v 时前缀补 HH:MM:SS，正文仍从固定列开始（与默认观感一致的对齐）."""
    from rich.console import Console

    from fspack.console import StageLogHandler

    handler = StageLogHandler(Console(width=120, force_terminal=False), show_time=True)
    rendered = handler.render(_record(logging.INFO, "下载完成")).plain
    assert rendered[2] == ":" and rendered[5] == ":"
    assert rendered.index("下载完成") == 15


def test_stage_log_level_labels_distinct_without_color() -> None:
    """级别靠文本标记区分：INFO / WARN / ERROR 前缀互不相同且等宽."""
    from rich.console import Console

    from fspack.console import StageLogHandler

    handler = StageLogHandler(Console(width=80, force_terminal=False))
    heads = {
        level: handler.render(_record(level, "x")).plain.split(" ")[0]
        for level in (logging.INFO, logging.WARNING, logging.ERROR)
    }
    assert heads == {logging.INFO: "INFO", logging.WARNING: "WARN", logging.ERROR: "ERROR"}
    widths = {logutil.display_width(handler.render(_record(level, "x")).plain[:6]) for level in heads}
    assert widths == {6}


def test_stage_log_handler_wraps_path_by_directory_segments() -> None:
    """窄终端下长路径按目录段折行：续行对齐正文列，文件名不劈开."""
    from rich.console import Console

    from fspack.console import StageLogHandler

    handler = StageLogHandler(Console(width=40, force_terminal=False))
    path = r"C:\Users\zhou\AppData\Local\spam\eggs\win7-compat-report.txt"
    lines = handler.render(_record(logging.INFO, f"Win7 兼容报告已写入: {path}")).plain.split("\n")
    assert len(lines) > 1
    # 每行不超过终端宽度，续行与正文首列（第 7 列）严格对齐
    assert all(logutil.display_width(line) <= 40 for line in lines)
    assert all(line.startswith("      ") and not line[6:].startswith(" ") for line in lines[1:])
    # 非末行都在目录分隔符或空格后断开，末行保留完整文件名
    assert all(line.rstrip().endswith(("\\", "/", " ")) for line in lines[:-1])
    assert lines[-1].endswith("win7-compat-report.txt")


def test_stage_log_handler_plain_text_when_not_terminal() -> None:
    """非终端（重定向/CI 日志）不输出 ANSI，级别仍以文本表达."""
    from io import StringIO

    from rich.console import Console

    from fspack.console import StageLogHandler

    stream = StringIO()
    handler = StageLogHandler(Console(file=stream, width=80, force_terminal=False))
    handler.emit(_record(logging.ERROR, "构建失败"))
    out = stream.getvalue()
    assert "ERROR 构建失败" in out
    assert "\x1b[" not in out


def _spy_console_init() -> tuple[dict[str, object], Callable[..., object]]:
    """构造 Console.__init__ 的 spy，返回 (captured_kwargs, real_init)."""
    from rich.console import Console

    captured: dict[str, object] = {}
    real_init = Console.__init__

    def spy_init(self: Any, *args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_init(self, *args, **kwargs)

    return captured, spy_init


def test_make_console_disables_legacy_windows_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI 环境下创建 Console 时强制 legacy_windows=False.

    避免在 GitHub Actions Windows runner 上 rich 误用 LegacyWindowsTerm，
    导致 RichHandler emit 时 ``SetConsoleTextAttribute`` 在重定向 stdout
    上失败。
    """
    from rich.console import Console

    from fspack.console import CICompat

    captured, spy_init = _spy_console_init()
    monkeypatch.setattr(Console, "__init__", spy_init)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    CICompat.make_console()
    assert captured.get("legacy_windows") is False


def test_make_console_keeps_auto_detection_outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 CI 现代控制台下保持 legacy_windows=None，交由 rich 自动检测."""
    from rich.console import Console

    from fspack.console import CICompat

    captured, spy_init = _spy_console_init()
    monkeypatch.setattr(Console, "__init__", spy_init)
    for name in ("CI", "GITHUB_ACTIONS", "BUILD_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(CICompat, "is_legacy_windows_console", staticmethod(lambda *_a, **_k: False))
    CICompat.make_console()
    assert captured.get("legacy_windows") is None
    assert captured.get("file") is None


class _TtyFakeStream:
    """模拟带 isatty 开关的 stdout."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def _patch_detect_legacy_windows(monkeypatch: pytest.MonkeyPatch, result: bool) -> None:
    """patch rich.console.detect_legacy_windows（被测代码函数内延迟导入）."""
    import rich.console

    monkeypatch.setattr(rich.console, "detect_legacy_windows", lambda: result)


def test_is_legacy_windows_console_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 平台直接返回 False（不触碰 stdout/detect）."""
    import sys

    from fspack.console import CICompat

    monkeypatch.setattr(sys, "platform", "linux")
    assert CICompat.is_legacy_windows_console() is False


def test_is_legacy_windows_console_requires_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout 非 tty（重定向/pytest capture）时返回 False，即使控制台无 VT."""
    import sys

    from fspack.console import CICompat

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", _TtyFakeStream(tty=False))
    _patch_detect_legacy_windows(monkeypatch, True)
    assert CICompat.is_legacy_windows_console() is False


def test_is_legacy_windows_console_tty_without_vt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows + 真实 tty + 无 VT（Win7 conhost）：返回 True."""
    import sys

    from fspack.console import CICompat

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", _TtyFakeStream(tty=True))
    _patch_detect_legacy_windows(monkeypatch, True)
    assert CICompat.is_legacy_windows_console() is True


def test_is_legacy_windows_console_tty_with_vt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows + 真实 tty + VT 已开启（Win10+）：返回 False."""
    import sys

    from fspack.console import CICompat

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", _TtyFakeStream(tty=True))
    _patch_detect_legacy_windows(monkeypatch, False)
    assert CICompat.is_legacy_windows_console() is False


def test_is_legacy_windows_console_detect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """rich 探测抛异常时按非 legacy 处理（保持现代渲染路径）."""
    import sys

    from fspack.console import CICompat

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", _TtyFakeStream(tty=True))

    def _boom() -> bool:
        raise OSError("probe failed")

    import rich.console

    monkeypatch.setattr(rich.console, "detect_legacy_windows", _boom)
    assert CICompat.is_legacy_windows_console() is False


def test_make_console_legacy_uses_ascii_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """legacy 控制台：legacy_windows=True 且 file 为报告 ASCII 编码的代理."""
    import sys

    from rich.console import Console

    from fspack.console import CICompat, _AsciiEncodingStream

    captured, spy_init = _spy_console_init()
    monkeypatch.setattr(Console, "__init__", spy_init)
    for name in ("CI", "GITHUB_ACTIONS", "BUILD_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(CICompat, "is_legacy_windows_console", staticmethod(lambda *_a, **_k: True))
    fake_stdout = _TtyFakeStream(tty=True)
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    CICompat.make_console()

    assert captured.get("legacy_windows") is True
    proxy = captured.get("file")
    assert isinstance(proxy, _AsciiEncodingStream)
    assert proxy.encoding == "ascii", "代理须向 rich 报告 ASCII 以启用 ascii_only"
    assert proxy._stream is fake_stdout, "代理须包装当前 sys.stdout"
    assert proxy.write == fake_stdout.write, "write 须委托原流"


def test_make_console_ci_overrides_legacy_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI + legacy 控制台：CI 优先（legacy_windows=False、无代理，强制 ANSI）."""
    from rich.console import Console

    from fspack.console import CICompat

    captured, spy_init = _spy_console_init()
    monkeypatch.setattr(Console, "__init__", spy_init)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(CICompat, "is_legacy_windows_console", staticmethod(lambda *_a, **_k: True))
    CICompat.make_console()
    assert captured.get("legacy_windows") is False
    assert captured.get("file") is None


def test_console_ui_ascii_marks_on_legacy_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """legacy 控制台下 success/error 用 ASCII v/x 替代 Ambiguous 宽度的 √/×."""
    from fspack.console import CICompat, ConsoleUI

    monkeypatch.setattr(CICompat, "is_legacy_windows_console", staticmethod(lambda *_a, **_k: True))
    ui = ConsoleUI()
    with ui.rich.capture() as capture:
        ui.success("构建完成")
        ui.error("失败")
    out = capture.get()
    assert "构建完成" in out
    assert "失败" in out
    assert "v" in out
    assert "x" in out
    assert "√" not in out
    assert "×" not in out


def test_console_ui_unicode_marks_on_modern_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """现代控制台保持 √/× 标记."""
    from fspack.console import CICompat, ConsoleUI

    monkeypatch.setattr(CICompat, "is_legacy_windows_console", staticmethod(lambda *_a, **_k: False))
    ui = ConsoleUI()
    with ui.rich.capture() as capture:
        ui.success("构建完成")
        ui.error("失败")
    out = capture.get()
    assert "√" in out
    assert "×" in out


class _FakeStream:
    """模拟 stdout/stderr，记录 reconfigure 调用."""

    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding
        self.reconfigured: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.reconfigured.append(kwargs)


def test_ensure_utf8_stdio_reconfigures_non_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 UTF-8 编码的 stdout/stderr 应被重配置为 UTF-8."""
    import sys

    from fspack.console import CICompat

    fake_out = _FakeStream("cp1252")
    fake_err = _FakeStream("cp936")
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    CICompat.ensure_utf8_stdio()
    assert fake_out.reconfigured == [{"encoding": "utf-8"}]
    assert fake_err.reconfigured == [{"encoding": "utf-8"}]


def test_ensure_utf8_stdio_skips_already_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """已经是 UTF-8 编码的流不应被重配置."""
    import sys

    from fspack.console import CICompat

    fake_out = _FakeStream("utf-8")
    fake_err = _FakeStream("UTF-8")
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    CICompat.ensure_utf8_stdio()
    assert fake_out.reconfigured == []
    assert fake_err.reconfigured == []


def test_ensure_utf8_stdio_handles_no_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    """流无 reconfigure 方法时不应崩溃（如已关闭的流或自定义对象）."""
    import sys

    from fspack.console import CICompat

    class _NoReconfigure:
        encoding = "cp1252"

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(sys, "stderr", None)
    CICompat.ensure_utf8_stdio()  # 不应抛异常


def test_ensure_utf8_stdio_handles_reconfigure_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """reconfigure 抛 OSError/ValueError 时应静默忽略（流已关闭等场景）."""
    import sys

    from fspack.console import CICompat

    class _FailingStream:
        encoding = "cp1252"

        def reconfigure(self, **kwargs: str) -> None:
            raise OSError("流已关闭")

    monkeypatch.setattr(sys, "stdout", _FailingStream())
    monkeypatch.setattr(sys, "stderr", _FailingStream())
    CICompat.ensure_utf8_stdio()  # 不应抛异常


def test_get_theme_returns_expected_styles() -> None:
    """get_theme 返回含 info/warning/error/success/step 样式的 Theme."""
    from rich.theme import Theme

    from fspack.console import CICompat

    theme = CICompat.get_theme()
    assert isinstance(theme, Theme)
    # 验证所有自定义样式键存在
    styles = theme.styles
    assert "info" in styles
    assert "warning" in styles
    assert "error" in styles
    assert "success" in styles
    assert "step" in styles


def test_console_ui_delegates_to_ci_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    """ConsoleUI.__init__ 通过 CICompat.make_console 创建底层 Console."""
    from rich.console import Console

    from fspack.console import CICompat, ConsoleUI

    captured: dict[str, object] = {}
    real_make = CICompat.make_console

    def spy_make(cls: Any) -> Console:
        result = real_make.__func__(cls)  # type: ignore[attr-defined]
        captured["called"] = True
        captured["result"] = result
        return result

    monkeypatch.setattr(CICompat, "make_console", classmethod(spy_make))
    ui = ConsoleUI()
    assert captured.get("called") is True
    assert ui.rich is captured.get("result")
