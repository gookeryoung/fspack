"""console 彩色输出测试."""

from __future__ import annotations

import logging

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


def _spy_console_init() -> tuple[dict[str, object], object]:
    """构造 Console.__init__ 的 spy，返回 (captured_kwargs, real_init)."""
    import fspack.console as mod

    captured: dict[str, object] = {}
    real_init = mod.Console.__init__

    def spy_init(self: object, *args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_init(self, *args, **kwargs)  # type: ignore[arg-type]

    return captured, spy_init


def test_make_console_disables_legacy_windows_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI 环境下创建 Console 时强制 legacy_windows=False.

    避免在 GitHub Actions Windows runner 上 rich 误用 LegacyWindowsTerm，
    导致 RichHandler emit 时 ``SetConsoleTextAttribute`` 在重定向 stdout
    上失败。
    """
    import fspack.console as mod

    captured, spy_init = _spy_console_init()
    monkeypatch.setattr(mod.Console, "__init__", spy_init)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    mod._make_console()
    assert captured.get("legacy_windows") is False


def test_make_console_keeps_auto_detection_outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 CI 环境下保持 legacy_windows=None，交由 rich 自动检测."""
    import fspack.console as mod

    captured, spy_init = _spy_console_init()
    monkeypatch.setattr(mod.Console, "__init__", spy_init)
    for name in ("CI", "GITHUB_ACTIONS", "BUILD_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    mod._make_console()
    assert captured.get("legacy_windows") is None
