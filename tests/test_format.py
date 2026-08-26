"""``fspack.format`` 字节数格式化测试：二进制/十进制两套互斥契约."""

from __future__ import annotations

import pytest

from fspack.format import format_bytes_dec, format_size_bin


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (1024 * 1024 * 1024, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1.0 PiB"),
    ],
)
def test_format_size_bin(size: int, expected: str) -> None:
    """二进制单位带空格格式化（doctor 契约）."""
    assert format_size_bin(size) == expected


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (1024 * 1024, "1.0MB"),
        (1024 * 1024 * 1024, "1.00GB"),
    ],
)
def test_format_bytes_dec(n: int, expected: str) -> None:
    """十进制风格无空格格式化（progress/size_report 契约）."""
    assert format_bytes_dec(n) == expected
