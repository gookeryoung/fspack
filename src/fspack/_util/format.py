"""字节数格式化工具.

提供两套互斥的字节数格式化风格，由不同调用场景的历史契约决定，**不可统一**：

- :func:`format_size_bin` — 二进制单位带空格（如 ``"123.4 MiB"``），
  ``fsp doctor`` 环境/缓存报告使用（``fspack.doctor.envs`` 复用）。
- :func:`format_bytes_dec` — 十进制风格无空格（如 ``"1.0KB"``/``"1.00GB"``），
  ``fsp`` 构建进度/体积报告使用（``fspack.progress``/``fspack.packaging.size_report`` 复用）。

两套输出格式被各自的测试锁死（``test_progress.py``/``test_doctor.py``），
合并会破坏对外契约，故仅做物理去重，保留两个独立函数。
"""

from __future__ import annotations

__all__ = ["format_bytes_dec", "format_size_bin"]


def format_size_bin(size_bytes: int) -> str:
    """字节数格式化为人类可读的二进制单位字符串（带空格，如 ``"123.4 MiB"``）.

    小于 1024 字节显示为 ``"<n> B"``，否则按 KiB/MiB/GiB/TiB/PiB 逐级换算，
    保留一位小数并以空格分隔数值与单位。

    :param size_bytes: 字节数
    :return: 人类可读字符串（如 ``"1.0 MiB"``）
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ("KiB", "MiB", "GiB", "TiB")
    size = float(size_bytes) / 1024
    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def format_bytes_dec(n: int) -> str:
    """字节数格式化为人类可读的十进制风格字符串（无空格，如 ``"1.0KB"``）.

    小于 1024 字节显示为 ``"<n>B"``，KB/MB 保留一位小数，GB 保留两位小数，
    数值与单位之间无空格。

    :param n: 字节数
    :return: 人类可读字符串（如 ``"1.0KB"``/``"1.00GB"``）
    """
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    return f"{n / 1024 / 1024 / 1024:.2f}GB"
