"""console 彩色输出测试。."""

from __future__ import annotations

import logging

from fspack.console import console, error, setup_logging, step, success, warn


def test_step_prints_title() -> None:
    """step 打印步骤标题，含 > 标记。."""
    with console.capture() as capture:
        step("解析项目")
    out = capture.get()
    assert "解析项目" in out
    assert ">" in out


def test_success_prints_check() -> None:
    """success 打印成功消息，含 √ 标记。."""
    with console.capture() as capture:
        success("构建完成")
    out = capture.get()
    assert "构建完成" in out
    assert "√" in out


def test_warn_prints_warning() -> None:
    """warn 打印警告消息，含 ! 标记。."""
    with console.capture() as capture:
        warn("注意")
    out = capture.get()
    assert "注意" in out
    assert "!" in out


def test_error_prints_cross() -> None:
    """error 打印错误消息，含 × 标记。."""
    with console.capture() as capture:
        error("失败")
    out = capture.get()
    assert "失败" in out
    assert "×" in out


def test_setup_logging_configures_root() -> None:
    """setup_logging 配置 root logger 的 level 与 handler。."""
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    try:
        setup_logging(verbose=True)
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert root.handlers[0].__class__.__name__ == "RichHandler"

        setup_logging(verbose=False)
        assert root.level == logging.INFO
    finally:
        root.setLevel(original_level)
        root.handlers = original_handlers
