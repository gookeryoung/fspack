"""二进制依赖分析子包：扫描 dist/.dll/.so/.dylib/.pyd 依赖树，剥离无引用文件.

``__init__.py`` 为 facade，子模块：

- :mod:`~fspack.packaging.dep_analyzer.common`：数据模型 + 常量 + 扫描/入口识别/依赖名解析
- :mod:`~fspack.packaging.dep_analyzer.pe`：PE 导入表解析（纯 Python，无 pefile 依赖）
- :mod:`~fspack.packaging.dep_analyzer.elf`：ELF 依赖（objdump -p NEEDED 条目）
- :mod:`~fspack.packaging.dep_analyzer.macho`：Mach-O 依赖（otool -L）
- :mod:`~fspack.packaging.dep_analyzer.graph`：依赖图构建 + 入口 BFS 可达分析 + 未引用剥离

测试 patch 点（``monkeypatch.setattr("fspack.packaging.dep_analyzer.<name>", ...)``）：

- ``subprocess.run``（test_dep_analyzer L328/L337/L405）
- ``_parse_pe_imports``（test_dep_analyzer L523/L579/L868/L977）
"""

from __future__ import annotations

import subprocess  # noqa: F401  — 测试 patch dep_analyzer.subprocess.run 用（elf/macho 经 _D 延迟取本模块属性）

from .common import (  # noqa: F401 — facade re-export（测试与跨模块引用兼容面）
    _BINARY_EXTS,
    _PARALLEL_THRESHOLD,
    BinaryInfo,
    DepGraph,
    _collect_loader_entries,
    _dep_basename,
    _detect_platform_from_path,
    _identify_entries,
    _is_system_dep,
    _iter_binary_files,
    _parse_deps_parallel,
)
from .elf import _parse_objdump_deps  # noqa: F401 — facade re-export
from .graph import (
    _parse_dependencies,
    analyze_binary_dependencies,
    find_unused_binaries,
    strip_unused_binaries,
)
from .macho import _parse_otool_deps  # noqa: F401 — facade re-export
from .pe import (  # facade re-export（__all__ 成员，_parse_pe_imports 为测试 patch 点）
    _parse_pe_imports,
    _read_ascii_string,
)

__all__ = [
    "BinaryInfo",
    "DepGraph",
    "_collect_loader_entries",
    "_parse_dependencies",
    "_parse_pe_imports",
    "_read_ascii_string",
    "analyze_binary_dependencies",
    "find_unused_binaries",
    "strip_unused_binaries",
]
