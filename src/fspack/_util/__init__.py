"""跨模块公用工具子包.

收敛此前散落在顶层与各子包的同类实现，消除重复：

- :mod:`fspack._util.format` — 字节数格式化（两套互斥风格，不合并）。
- :mod:`fspack._util.fsutil` — 目录大小计算、原子写文本、安全删除。
- :mod:`fspack._util.jsoncache` — JSON 缓存读取骨架（解析 + 校验 + 损坏处理）。

各消费方经 re-export 保留原导入路径与私有名，行为不变。
"""

from __future__ import annotations

from fspack._util.format import format_bytes_dec, format_size_bin
from fspack._util.fsutil import (
    atomic_write_text,
    dir_size_with_count,
    safe_unlink,
    scandir_dir_size,
    scandir_tree,
    walk_dir_size,
)
from fspack._util.jsoncache import load_json_dict

__all__ = [
    "atomic_write_text",
    "dir_size_with_count",
    "format_bytes_dec",
    "format_size_bin",
    "load_json_dict",
    "safe_unlink",
    "scandir_dir_size",
    "scandir_tree",
    "walk_dir_size",
]
