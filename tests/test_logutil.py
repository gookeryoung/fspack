"""logutil：路径缩略与 verbose 开关测试.

覆盖默认输出「路径单行、CJK 宽度正确、保留文件名」与 ``-v``（完整路径）两种
形态，以及宽度预算的上下限收敛。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from fspack import logutil


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    logutil.reset()
    yield
    logutil.reset()


# ---- display_width ----


def test_display_width_ascii() -> None:
    assert logutil.display_width("abc") == 3


def test_display_width_cjk_is_two_columns() -> None:
    """全角中文按 2 列计（不按字符数 hard cut 的前提）."""
    assert logutil.display_width("中文") == 4
    assert logutil.display_width("a中b") == 4


# ---- truncate_middle ----


def test_truncate_middle_keeps_short_text() -> None:
    assert logutil.truncate_middle("abc", 10) == "abc"


def test_truncate_middle_keeps_filename_and_width() -> None:
    path = r"C:\Users\zhou\AppData\Local\Temp\fsp-demo\demo-req\dist\runtime\python314.dll"
    out = logutil.truncate_middle(path, 36)
    assert logutil.display_width(out) <= 36
    assert "…" in out
    assert out.endswith("python314.dll")  # 文件名完整保留
    assert out.startswith("C:\\")  # 头部保留盘符


def test_truncate_middle_cjk_not_split() -> None:
    """中文路径按显示列截断，结果宽度不超预算且字符完整."""
    text = r"C:\项目\中文目录\另一个很长的目录名\运行时\python314.dll"
    out = logutil.truncate_middle(text, 30)
    assert logutil.display_width(out) <= 30
    assert "…" in out
    assert "python314.dll" in out


def test_truncate_middle_zero_width_returns_empty() -> None:
    assert logutil.truncate_middle("abc", 0) == ""


def test_truncate_middle_narrower_than_ellipsis() -> None:
    """预算不足一个省略号时也返回不超过预算的内容."""
    out = logutil.truncate_middle("abcdef", 1)
    assert logutil.display_width(out) <= 1


# ---- short_path ----


def test_short_path_default_truncates() -> None:
    path = r"C:\Users\zhou\AppData\Local\Temp\demo\dist\runtime\python314.dll"
    out = logutil.short_path(path)
    assert out != path
    assert out.endswith("python314.dll")
    assert logutil.display_width(out) <= 36


def test_short_path_verbose_returns_full() -> None:
    path = r"C:\Users\zhou\AppData\Local\Temp\demo\dist\runtime\python314.dll"
    logutil.configure(verbose=True)
    assert logutil.is_verbose()
    assert logutil.short_path(path) == path


def test_configure_clamps_path_width() -> None:
    """宽度预算收敛到 [24, 72]，避免过窄不可读或过宽仍折行."""
    logutil.configure(verbose=False, path_width=1)
    assert logutil.short_path(r"C:\a\b\c\file.dll") != ""
    logutil.configure(verbose=False, path_width=500)
    long_path = "C:\\" + "\\".join(["segment"] * 30) + "\\python314.dll"
    assert logutil.display_width(logutil.short_path(long_path)) <= 72


def test_short_path_explicit_width() -> None:
    path = r"C:\Users\zhou\AppData\Local\Temp\demo\runtime\python314.dll"
    out = logutil.short_path(path, max_width=20)
    assert logutil.display_width(out) <= 20


def test_short_path_logs_full_path_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """截断掉的完整路径以 DEBUG 记录：``-v``/CI 日志文件仍能找回原值."""
    path = r"C:\Users\zhou\AppData\Local\Temp\demo\runtime\python314.dll"
    with caplog.at_level("DEBUG", logger="fspack.logutil"):
        logutil.short_path(path)
    assert any(path in record.getMessage() and record.levelname == "DEBUG" for record in caplog.records)


def test_short_path_verbose_does_not_log(caplog: pytest.LogCaptureFixture) -> None:
    """``-v`` 下输出本就完整，不再重复写 DEBUG 完整路径."""
    logutil.configure(verbose=True)
    with caplog.at_level("DEBUG", logger="fspack.logutil"):
        logutil.short_path(r"C:\Users\zhou\AppData\Local\Temp\demo\runtime\python314.dll")
    assert not caplog.records


# ---- wrap_message ----


def test_wrap_message_single_line_when_fits() -> None:
    assert logutil.wrap_message("短消息", 40) == ["短消息"]


def test_wrap_message_breaks_after_separators() -> None:
    """按目录分隔符折行：每行不超宽，非末行以分隔符结尾，不劈开文件名."""
    text = r"报告已写入: C:\Users\zhou\AppData\Local\spam\eggs\win7-compat-report.txt"
    lines = logutil.wrap_message(text, 30)
    assert len(lines) > 1
    assert all(logutil.display_width(line) <= 30 for line in lines)
    assert all(line.endswith(("\\", "/", " ")) for line in lines[:-1])
    assert lines[-1].endswith("win7-compat-report.txt")


def test_wrap_message_cjk_width_respected() -> None:
    """中文按 2 列计宽：不能按 len() 折出超宽行."""
    lines = logutil.wrap_message("这是一个没有空格的很长的中文日志消息用于折行验证", 20)
    assert all(logutil.display_width(line) <= 20 for line in lines)
    assert "".join(lines) == "这是一个没有空格的很长的中文日志消息用于折行验证"


def test_wrap_message_overlong_segment_truncated_with_middle_ellipsis() -> None:
    """单段超宽（超长文件名）退回中间省略，保留尾部文件名."""
    lines = logutil.wrap_message("C:\\" + "a" * 60 + "\\imported-module-name.pyd", 24)
    assert all(logutil.display_width(line) <= 24 for line in lines)
    assert lines[-1].endswith("imported-module-name.pyd")


def test_wrap_message_no_leading_spaces_on_continuation() -> None:
    """续行不以空格开头（缩进由调用方统一补齐，否则会双缩进错位）."""
    text = r"产物清单已生成: C:\Users\zhou\AppData\Local\Temp\demo-req\dist\manifest.json"
    lines = logutil.wrap_message(text, 26)
    assert all(not line.startswith(" ") for line in lines)
