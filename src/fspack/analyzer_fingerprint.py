"""源码指纹与路径排除规则.

提取自 :mod:`fspack.analyzer`，按职责拆分。本模块专注于"源码文件系统遍历"——
递归扫描 ``.py`` 文件计算指纹、判断路径是否位于构建产物目录。

公开 API：

- :func:`source_fingerprint`：BLAKE2b 源码指纹（用于依赖分析缓存键）
- :func:`_is_excluded`：判断路径是否位于构建产物/缓存目录
- :data:`_EXCLUDED_DIRS`：始终排除的目录名集合
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterator

__all__ = [
    "_EXCLUDED_DIRS",
    "_is_excluded",
    "source_fingerprint",
]

_EXCLUDED_DIRS = frozenset(
    {
        "dist",
        "build",
        ".git",
        "__pycache__",
        ".venv",
        ".tox",
        ".fspack",
        ".trae",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".pyrefly_cache",
        ".uv-cache",
        "htmlcov",
        "node_modules",
        # 开发期目录：非运行时代码，扫描会导致误报依赖
        "examples",
        "tests",
        "docs",
        "templates",
    }
)


def _is_excluded(path: Path, src_dir: Path, data_dirs: tuple[Path, ...] = ()) -> bool:
    """判断文件是否位于构建产物或缓存目录下，应跳过扫描.

    适用于 .py 与 .qml 文件：检查路径的目录前缀是否在
    :data:`_EXCLUDED_DIRS` 中、为 ``.egg-info`` 后缀，或位于 ``data_dirs``
    数据资源目录树内（data-dirs 内的 .py 是模板/前端产物等数据资源，
    不应被 AST 扫描误判为项目依赖）。
    """
    parts = path.relative_to(src_dir).parts[:-1]
    if any(part in _EXCLUDED_DIRS or part.endswith(".egg-info") for part in parts):
        return True
    # data-dirs 内的 .py 是数据资源（模板/前端产物），不扫描
    return bool(data_dirs) and _is_in_data_dirs(path, data_dirs)


def _is_in_data_dirs(path: Path, data_dirs: tuple[Path, ...]) -> bool:
    """判断 ``path`` 是否位于任一 ``data_dirs`` 目录树内（含 data-dir 自身）.

    用 ``relative_to`` + ``ValueError`` 兼容 Python 3.8（无 ``Path.is_relative_to``）。
    ``data_dirs`` 非空时调用方已保证。
    """
    for d in data_dirs:
        try:
            path.relative_to(d)
            return True
        except ValueError:
            continue
    return False


def source_fingerprint(src_dir: Path, data_dirs: tuple[str, ...] = ()) -> str:
    """计算源码指纹用于依赖分析缓存键。

    遍历 ``src_dir`` 下所有不被排除的 ``.py`` 文件，以 ``相对路径|mtime_ns|size``
    拼接后求 BLAKE2b（digest_size=32，hex 64 字符，与原 SHA-256 输出长度一致）。
    与 :func:`fspack.analyzer.analyze_dependencies` 使用相同的排除逻辑
    （``_EXCLUDED_DIRS`` + ``data_dirs``），保证指纹只反映被分析的源码变化。

    ``data_dirs`` 为 ``[tool.fspack] data-dirs`` 配置的数据资源目录树（相对
    ``src_dir`` 的 POSIX 路径，如 ``src/fspack/assets/templates``），其下 ``.py``
    是模板/前端产物等数据资源，不应参与指纹计算（与 AST 扫描一致排除）。

    用 :func:`os.scandir` 递归遍历，利用 :meth:`os.DirEntry.stat` 缓存目录
    枚举时的 stat 信息（Windows ``WIN32_FIND_DATA`` / Linux ``d_ino``），
    避免对每个文件单独 ``stat`` 系统调用。同时按名称排序目录条目（含子目录），
    保证跨平台/文件系统的指纹确定性（``os.walk`` 不保证目录遍历顺序）。

    用 :func:`hashlib.blake2b` 替代 :func:`hashlib.sha256`：BLAKE2b 在 CPython
    实现中略快（约 10-20%），且 ``digest_size=32`` 输出 64 hex 字符与
    SHA-256 长度一致，缓存键文件名兼容。BLAKE2b 抗碰撞性足够用于缓存键场景。
    """
    resolved_data_dirs = tuple((src_dir / Path(rel)).resolve() for rel in data_dirs)
    h = hashlib.blake2b(digest_size=32)
    for rel, mtime_ns, size in _iter_py_entries(src_dir, src_dir, resolved_data_dirs):
        h.update(f"{rel}|{mtime_ns}|{size}\n".encode())
    return h.hexdigest()


def _iter_py_entries(current: Path, root: Path, data_dirs: tuple[Path, ...] = ()) -> Iterator[tuple[str, int, int]]:
    """递归遍历 ``.py`` 文件，返回 ``(相对路径, mtime_ns, size)`` 三元组。

    :func:`os.scandir` 返回的 :class:`os.DirEntry` 对象缓存了目录枚举时的
    stat 信息，``entry.stat(follow_symlinks=False)`` 直接复用缓存避免独立
    stat 调用。剪枝排除 ``_EXCLUDED_DIRS`` 与 ``*.egg-info`` 目录，以及
    ``data_dirs`` 数据资源目录树（含 data-dir 自身）。

    条目按名称排序（含子目录），保证遍历顺序跨平台确定性——``os.walk``
    不保证目录遍历顺序，导致旧实现在不同文件系统上指纹不一致。
    """
    for entry in sorted(os.scandir(current), key=lambda e: e.name):
        entry_path = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            if entry.name in _EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                continue
            # data-dirs 剪枝：整个目录树不遍历
            if data_dirs and _is_in_data_dirs(entry_path.resolve(), data_dirs):
                continue
            yield from _iter_py_entries(entry_path, root, data_dirs)
        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".py"):
            # data-dirs 内的单文件也排除（防御性，剪枝应已跳过整个目录）
            if data_dirs and _is_in_data_dirs(entry_path.resolve(), data_dirs):
                continue
            rel = entry_path.relative_to(root).as_posix()
            st = entry.stat(follow_symlinks=False)
            yield (rel, st.st_mtime_ns, st.st_size)
