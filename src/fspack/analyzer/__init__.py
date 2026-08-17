"""AST 依赖分析：扫描 import，分类标准库/本地/第三方.

facade 子包：re-export :mod:`fspack.analyzer.ast_scan`（AST 解析）、
:mod:`fspack.analyzer.analysis`（依赖分析编排与并行调度）、
:mod:`fspack.analyzer.fingerprint`（源码指纹）的公开接口。

业务实现（``analyze_dependencies`` 编排、串行/并行解析、进程池 worker、
QML 扫描）位于 :mod:`fspack.analyzer.analysis`；worker 函数
（``_parse_file_worker``/``_init_parse_worker``）在此 re-export 保持
``from fspack.analyzer import ...`` 引用路径兼容——进程池 pickle 按定义
模块 ``fspack.analyzer.analysis`` 定位函数，不受 facade re-export 影响。
拦截 ``analyze_dependencies`` 内部调用的 monkeypatch 请 patch
``fspack.analyzer.analysis.<name>``（定义所在模块）。
"""

from __future__ import annotations

from fspack.analyzer.analysis import (  # noqa: F401 — facade re-export（测试/内部引用兼容面）
    _PARALLEL_THRESHOLD,
    _PARSE_TOTAL_TIMEOUT,
    _WORKER_STATE,
    _format_ast_errors,
    _init_parse_worker,
    _iter_src_files_by_ext,
    _local_packages,
    _parse_file_worker,
    _parse_parallel,
    _parse_serial,
    analyze_dependencies,
)
from fspack.analyzer.ast_scan import (
    STDLIB_FALLBACK,
    _qml_module_to_qt_sub,
    collect_imports,
    collect_imports_and_submodules,
    collect_submodule_imports,
    parse_qml_imports,
)
from fspack.analyzer.fingerprint import source_fingerprint

__all__ = [
    "STDLIB_FALLBACK",
    "_qml_module_to_qt_sub",
    "analyze_dependencies",
    "collect_imports",
    "collect_imports_and_submodules",
    "collect_submodule_imports",
    "parse_qml_imports",
    "source_fingerprint",
]
