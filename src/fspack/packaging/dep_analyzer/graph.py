"""二进制依赖图构建与未引用剥离：BFS 可达分析 + unused 判定 + strip.

从 :mod:`fspack.packaging.dep_analyzer` facade 迁入的业务实现（facade 仅保留
re-export）。``analyze_binary_dependencies`` 扫描二进制构建依赖图，
``find_unused_binaries`` 从入口 BFS 求可达集合，``strip_unused_binaries``
删除不可达文件。``_parse_dependencies`` 经 :func:`_S` 延迟从 facade 取
``_parse_pe_imports`` 当前值，保持测试 patch ``dep_analyzer._parse_pe_imports``
语义不变。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fspack.platform import Platform

from .common import (
    _BINARY_EXTS,
    _PARALLEL_THRESHOLD,
    BinaryInfo,
    DepGraph,
    _dep_basename,
    _detect_platform_from_path,
    _identify_entries,
    _is_system_dep,
    _iter_binary_files,
    _parse_deps_parallel,
)
from .elf import _parse_objdump_deps
from .macho import _parse_otool_deps
from .pe import _parse_pe_imports

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 自身属性延迟 dispatch：测试 patch dep_analyzer._parse_pe_imports 时 _parse_dependencies
# 需在调用时通过 getattr(facade 模块) 取当前值，才能感知 patch。
# ---------------------------------------------------------------------------
_self_mod_holder: list[Any] = [None]


def _S(attr_name: str, fallback: Any) -> Any:
    """从 ``fspack.packaging.dep_analyzer`` facade 模块按名取属性（延迟解析）."""
    mod = _self_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging import dep_analyzer as _self_mod

            mod = _self_mod
            _self_mod_holder[0] = mod
        except ImportError:
            return fallback
    return getattr(mod, attr_name, fallback)


def _parse_dependencies(path: Path, target: Platform) -> list[str] | None:
    """按平台分发依赖解析.

    ``_parse_pe_imports`` 通过 :func:`_S` 延迟从 facade 取值：测试 patch
    ``dep_analyzer._parse_pe_imports`` 后此处会感知。
    """
    if target is Platform.WINDOWS:
        parse_pe_dispatch = _S("_parse_pe_imports", _parse_pe_imports)
        return parse_pe_dispatch(path)
    if target is Platform.MACOS:
        return _parse_otool_deps(path)
    return _parse_objdump_deps(path)


def analyze_binary_dependencies(
    dist_dir: Path,
    target: Platform,
    *,
    runtime_dir: Path | None = None,
) -> DepGraph:
    """扫描 ``dist_dir`` 下所有二进制文件，构建依赖图.

    工具缺失（objdump/otool 未安装）时返回空图，不抛异常。
    二进制数 ≥ :data:`_PARALLEL_THRESHOLD` 时并行解析。
    """
    dist_dir = Path(dist_dir).resolve()
    runtime_dir = Path(runtime_dir).resolve() if runtime_dir else dist_dir / "runtime"

    graph = DepGraph()
    exts = _BINARY_EXTS[target]

    paths = _iter_binary_files(dist_dir, exts)
    if not paths:
        return graph

    if len(paths) >= _PARALLEL_THRESHOLD:
        results = _parse_deps_parallel(_parse_dependencies, paths, target)
    else:
        results = [_parse_dependencies(p, target) for p in paths]

    for path, deps in zip(paths, results):
        if deps is None:
            continue
        graph.binaries[path] = BinaryInfo(path=path, deps=tuple(deps))

    if not graph.binaries:
        return graph

    graph.entries = _identify_entries(dist_dir, runtime_dir, target, graph.binaries)

    dist_basenames = {p.name.lower() for p in graph.binaries}
    for info in graph.binaries.values():
        for dep in info.deps:
            dep_basename = _dep_basename(dep, target)
            if dep_basename and dep_basename.lower() not in dist_basenames and not _is_system_dep(dep, target):
                graph.unresolved.append(dep_basename)

    return graph


def find_unused_binaries(graph: DepGraph) -> list[Path]:
    """从入口 BFS 可达集合，返回不可达的二进制路径列表."""
    if not graph.binaries or not graph.entries:
        return []

    by_basename: dict[str, Path] = {}
    for path in graph.binaries:
        by_basename[path.name.lower()] = path

    visited: set[Path] = set()
    queue: list[Path] = [p for p in graph.entries if p in graph.binaries]

    while queue:
        current = queue.pop()
        if current in visited:
            continue
        visited.add(current)
        info = graph.binaries.get(current)
        if info is None:  # pragma: no cover
            continue
        for dep in info.deps:
            dep_basename = _dep_basename(dep, _detect_platform_from_path(current))
            if not dep_basename:
                continue
            dep_path = by_basename.get(dep_basename.lower())
            if dep_path is not None and dep_path not in visited:
                queue.append(dep_path)

    return [p for p in graph.binaries if p not in visited]


def strip_unused_binaries(unused: list[Path]) -> int:
    """删除未引用二进制文件，返回节省字节数."""
    saved = 0
    for path in unused:
        try:
            size = path.stat().st_size
            path.unlink()
            saved += size
            _logger.info("依赖分析剥离: %s (%d bytes)", path.name, size)
        except OSError as e:
            _logger.warning("依赖分析剥离失败: %s (%s)", path, e)
    return saved
