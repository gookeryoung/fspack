"""AST 依赖分析：扫描 import，分类标准库/本地/第三方.

facade 模块：编排 :mod:`fspack.analyzer_ast`（AST 解析）与
:mod:`fspack.analyzer_fingerprint`（源码指纹）完成依赖分析。本模块保留
:func:`analyze_dependencies` 的并行调度逻辑与本地包识别。

同时扫描 QML 文件（``.qml``）中的 ``import QtXxx`` 语句，将 QML 运行时
依赖映射为 Qt 子模块名（如 ``QtQuick`` → ``Quick``），补充 AST 静态分析
无法发现的 QML 运行时依赖——QML 引擎加载 ``qml/QtQuick.2/qtquick2plugin.dll``
时依赖 ``Qt5Quick.dll``，但 Python 入口仅 ``import PySide2.QtQml`` 不会
触发 ``Quick`` 子模块保留，导致 DLL 缺失。
"""

from __future__ import annotations

import ast
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from fspack.analyzer_ast import (
    _QT_PYTHON_PACKAGES,
    _STDLIB,
    STDLIB_FALLBACK,
    _qml_module_to_qt_sub,
    collect_imports,
    collect_imports_and_submodules,
    collect_submodule_imports,
    parse_qml_imports,
)
from fspack.analyzer_fingerprint import (
    _is_excluded,
    source_fingerprint,
)
from fspack.config import DependencyReport

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


def _local_packages(src_dir: Path, project_name: str) -> set[str]:
    """识别项目本地包/模块名（顶层 .py 与含 __init__.py 的目录）.

    用 :func:`os.scandir` 替代 :meth:`Path.iterdir`，避免 ``Path`` 包装
    开销与重复 stat 调用：``DirEntry.is_file``/``is_dir`` 复用枚举时的 stat
    缓存（Windows ``WIN32_FIND_DATA`` / Linux ``d_ino``）。
    """
    local: set[str] = {project_name}
    for entry in os.scandir(src_dir):
        name = entry.name
        if entry.is_file() and name.endswith(".py"):
            local.add(name[:-3])
        elif entry.is_dir() and (src_dir / name / "__init__.py").is_file():
            local.add(name)
    return local


def analyze_dependencies(src_dir: Path, project_name: str, declared: tuple[str, ...]) -> DependencyReport:
    """扫描 src_dir 下所有 .py 与 .qml，分类 import 为标准库/本地/第三方。

    自动排除 dist/build/.venv 等构建产物与缓存目录，避免扫描到已解包的
    embed python 或 python-build-standalone 标准库源码导致误报依赖。

    文件数超过 :data:`_PARALLEL_THRESHOLD` 时使用 :class:`ProcessPoolExecutor`
    并行解析（CPU 密集 ``ast.parse``），大项目显著提速。小项目走串行路径
    避免进程池启动开销（Windows spawn 约 100-200ms，需足够工作量摊销）。

    QML 文件（``.qml``）中的 ``import QtXxx`` 语句会被解析并映射为 Qt 子模块名
    （如 ``QtQuick`` → ``Quick``），加入对应 Qt 绑定包（PySide2/PySide6/PyQt5/PyQt6）
    的子模块集合——QML 引擎加载插件时依赖 ``Qt5Quick.dll`` 等 C 层 DLL，但 Python
    入口仅 ``import PySide2.QtQml`` 不会触发 ``Quick`` 子模块保留，AST 无法发现
    此运行时依赖。
    """
    py_files: list[Path] = [py for py in src_dir.rglob("*.py") if not _is_excluded(py, src_dir)]

    all_imports: list[str] = []
    all_submodules: dict[str, set[str]] = {}

    if len(py_files) >= _PARALLEL_THRESHOLD:
        _parse_parallel(py_files, all_imports, all_submodules)
    else:
        _parse_serial(py_files, all_imports, all_submodules)

    # 扫描 QML 文件提取 QtQuick 等 QML 运行时依赖（AST 无法发现）
    # 仅当项目 import 了 Qt 绑定包时才扫描，避免非 Qt 项目无谓 I/O
    imported_qt_pkgs = _QT_PYTHON_PACKAGES & set(all_imports)
    if imported_qt_pkgs:
        qml_files: list[Path] = [qml for qml in src_dir.rglob("*.qml") if not _is_excluded(qml, src_dir)]
        qml_qt_subs: set[str] = set()
        for qml_file in qml_files:
            qml_qt_subs.update(parse_qml_imports(qml_file))
        if qml_qt_subs:
            for qt_pkg in imported_qt_pkgs:
                all_submodules.setdefault(qt_pkg, set()).update(qml_qt_subs)

    local = _local_packages(src_dir, project_name)
    stdlib: list[str] = []
    third: list[str] = []
    local_imports: list[str] = []
    seen: set[str] = set()
    for imp in all_imports:
        if imp in seen:
            continue
        seen.add(imp)
        if imp in local:
            local_imports.append(imp)
        elif imp in _STDLIB:
            stdlib.append(imp)
        else:
            third.append(imp)
    ast_submodules = {
        pkg: frozenset(subs) for pkg, subs in all_submodules.items() if pkg not in local and pkg not in _STDLIB
    }
    return DependencyReport(
        declared=declared,
        ast_third_party=tuple(third),
        ast_stdlib=tuple(stdlib),
        ast_local=tuple(local_imports),
        ast_submodules=ast_submodules,
    )


# 并行解析阈值：低于此文件数走串行，避免进程池启动开销
# Windows spawn 启动 ~100-200ms，需足够工作量摊销；Linux fork 较快可更低
_PARALLEL_THRESHOLD = 200


def _parse_file_worker(py: str) -> tuple[list[str], dict[str, frozenset[str]]]:
    """进程池 worker：解析单个 .py 文件返回 ``(顶层导入, 子模块字典)``。

    错误文件返回空结果 ``([], {})``。模块级函数确保可 pickle 跨进程传递；
    接收 ``str`` 路径（比 ``Path`` 序列化更轻量）。

    用 :meth:`Path.read_bytes` + :func:`ast.parse(bytes)`，避免 Python 层
    decode 中间步骤（详见 :func:`_parse_serial`）。
    """
    try:
        tree = ast.parse(Path(py).read_bytes())
    except (SyntaxError, OSError):
        return [], {}
    return collect_imports_and_submodules(tree)


def _parse_serial(py_files: list[Path], all_imports: list[str], all_submodules: dict[str, set[str]]) -> None:
    """串行解析所有 .py 文件，结果合并到 ``all_imports`` / ``all_submodules``.

    用 :meth:`Path.read_bytes` + :func:`ast.parse(bytes)`，避免 Python 层
    ``decode("utf-8")`` 中间步骤——``ast.parse`` 内部用 C 实现解码，比
    显式 ``str.decode`` 快约 5-10%。基线测试 50 文件场景下可见微收益。
    """
    for py in py_files:
        try:
            tree = ast.parse(py.read_bytes())
        except (SyntaxError, OSError):
            continue
        tops, subs = collect_imports_and_submodules(tree)
        all_imports.extend(tops)
        for pkg, sub_set in subs.items():
            all_submodules.setdefault(pkg, set()).update(sub_set)


def _parse_parallel(py_files: list[Path], all_imports: list[str], all_submodules: dict[str, set[str]]) -> None:
    """进程池并行解析 .py 文件（CPU 密集 ``ast.parse``）。

    ``chunksize`` 按 CPU 核心数与文件数自适应，减少 IPC 调度开销。
    """
    cpu_count = os.cpu_count() or 4
    chunksize = max(1, len(py_files) // (cpu_count * 4))
    with ProcessPoolExecutor(max_workers=cpu_count) as pool:
        for tops, subs in pool.map(_parse_file_worker, [str(p) for p in py_files], chunksize=chunksize):
            all_imports.extend(tops)
            for pkg, sub_set in subs.items():
                all_submodules.setdefault(pkg, set()).update(sub_set)
