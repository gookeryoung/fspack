"""console 彩色输出测试."""

from __future__ import annotations

import logging
from typing import Any, Callable

import pytest

from fspack.console import console


def test_step_prints_title() -> None:
    """step 打印步骤标题，含 > 标记."""
    with console.rich.capture() as capture:
        console.step("解析项目")
    out = capture.get()
    assert "解析项目" in out
    assert ">" in out


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


def test_setup_logging_configures_root() -> None:
    """setup_logging 配置 root logger 的 level 与 handler."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        console.setup_logging(verbose=True)
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert root.handlers[0].__class__.__name__ == "RichHandler"

        console.setup_logging(verbose=False)
        assert root.level == logging.INFO
    finally:
        root.setLevel(original_level)
        root.handlers = original_handlers


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

    from fspack._compat import CICompat

    captured, spy_init = _spy_console_init()
    monkeypatch.setattr(Console, "__init__", spy_init)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    CICompat.make_console()
    assert captured.get("legacy_windows") is False


def test_make_console_keeps_auto_detection_outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 CI 环境下保持 legacy_windows=None，交由 rich 自动检测."""
    from rich.console import Console

    from fspack._compat import CICompat

    captured, spy_init = _spy_console_init()
    monkeypatch.setattr(Console, "__init__", spy_init)
    for name in ("CI", "GITHUB_ACTIONS", "BUILD_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    CICompat.make_console()
    assert captured.get("legacy_windows") is None


class _FakeStream:
    """模拟 stdout/stderr，记录 reconfigure 调用."""

    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding
        self.reconfigured: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.reconfigured.append(kwargs)


def test_ensure_utf8_stdio_reconfigures_non_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 UTF-8 编码的 stdout/stderr 应被重配置为 UTF-8."""
    from fspack._compat import CICompat, sys

    fake_out = _FakeStream("cp1252")
    fake_err = _FakeStream("cp936")
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    CICompat.ensure_utf8_stdio()
    assert fake_out.reconfigured == [{"encoding": "utf-8"}]
    assert fake_err.reconfigured == [{"encoding": "utf-8"}]


def test_ensure_utf8_stdio_skips_already_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """已经是 UTF-8 编码的流不应被重配置."""
    from fspack._compat import CICompat, sys

    fake_out = _FakeStream("utf-8")
    fake_err = _FakeStream("UTF-8")
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    CICompat.ensure_utf8_stdio()
    assert fake_out.reconfigured == []
    assert fake_err.reconfigured == []


def test_ensure_utf8_stdio_handles_no_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    """流无 reconfigure 方法时不应崩溃（如已关闭的流或自定义对象）."""
    from fspack._compat import CICompat, sys

    class _NoReconfigure:
        encoding = "cp1252"

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(sys, "stderr", None)
    CICompat.ensure_utf8_stdio()  # 不应抛异常


def test_ensure_utf8_stdio_handles_reconfigure_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """reconfigure 抛 OSError/ValueError 时应静默忽略（流已关闭等场景）."""
    from fspack._compat import CICompat, sys

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

    from fspack._compat import CICompat

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

    from fspack._compat import CICompat
    from fspack.console import ConsoleUI

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
