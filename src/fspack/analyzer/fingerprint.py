"""源码指纹与路径排除规则.

:mod:`fspack.analyzer` 子包的文件系统遍历模块，专注于"源码文件系统遍历"——
递归扫描 ``.py`` 文件计算指纹、判断路径是否位于构建产物目录（AST 解析见
:mod:`fspack.analyzer.ast_scan`）。

公开 API：

- :func:`source_fingerprint`：BLAKE2b 源码指纹（用于依赖分析缓存键）
- :func:`_is_excluded_name`：判断目录名是否应被排除（精确名 + ``.venv`` 前缀 + egg-info 后缀）
- :func:`_is_excluded`：判断路径是否位于构建产物目录（AST 解析见
  :mod:`fspack.analyzer.ast_scan`）
- :data:`_EXCLUDED_DIRS`：始终排除的目录名集合
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Iterator

__all__ = [
    "_EXCLUDED_DIRS",
    "_is_excluded",
    "_is_excluded_name",
    "cached_source_fingerprint",
    "clear_fingerprint_cache",
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


def _is_excluded_name(name: str) -> bool:
    """判断目录名是否应被排除：精确名匹配 ``_EXCLUDED_DIRS``、``.venv`` 前缀或 ``.egg-info`` 后缀.

    ``.venv`` 用前缀匹配而非精确匹配：多版本 venv 命名惯例为 ``.venv38``/
    ``.venv310`` 等（同项目并存多个 Python 版本的兼容线），精确匹配会漏排，
    导致 venv 内数千个第三方 ``.py`` 被误扫——既拖慢依赖分析与指纹计算，
    又可能把 venv 内部依赖误报为项目依赖（甚至触发并行解析阈值）。
    供 :func:`_is_excluded` 与 :mod:`fspack.analyzer.analysis` 的 scandir
    剪枝共用，保证指纹与分析的排除口径一致。
    """
    return name in _EXCLUDED_DIRS or name.startswith(".venv") or name.endswith(".egg-info")


def _is_excluded(path: Path, src_dir: Path, data_dirs: tuple[Path, ...] = ()) -> bool:
    """判断文件是否位于构建产物或缓存目录下，应跳过扫描.

    适用于 .py 与 .qml 文件：检查路径的目录前缀是否应被排除
    （:func:`_is_excluded_name`：精确名 + ``.venv`` 前缀 + egg-info 后缀），
    或位于 ``data_dirs`` 数据资源目录树内（data-dirs 内的 .py 是模板/
    前端产物等数据资源，不应被 AST 扫描误判为项目依赖）。
    """
    parts = path.relative_to(src_dir).parts[:-1]
    if any(_is_excluded_name(part) for part in parts):
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
    """计算源码指纹用于依赖分析缓存键（无缓存，每次全量扫描）。

    遍历 ``src_dir`` 下所有不被排除的 ``.py`` 与 ``.qml`` 文件（与
    :func:`fspack.analyzer.analyze_dependencies` 的分析范围一致），以
    ``相对路径|mtime_ns|size`` 拼接后求 BLAKE2b（digest_size=32，hex 64
    字符，与原 SHA-256 输出长度一致）。QML 同样参与指纹——QML 修改会改变
    依赖产物（Qt 子模块保留集合），须触发 deps 缓存失效，否则产物静默缺
    DLL。排除逻辑（``_EXCLUDED_DIRS`` + ``data_dirs``）亦与分析一致，
    保证指纹只反映被分析的源码变化。

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

    需要构建级复用时请用 :func:`cached_source_fingerprint`（stamp 键计算等
    同一构建内多次调用同一目录的场景）。
    """
    resolved_data_dirs = tuple((src_dir / Path(rel)).resolve() for rel in data_dirs)
    h = hashlib.blake2b(digest_size=32)
    for rel, mtime_ns, size in _iter_py_entries(src_dir, src_dir, resolved_data_dirs):
        h.update(f"{rel}|{mtime_ns}|{size}\n".encode())
    return h.hexdigest()


@lru_cache(maxsize=4)
def cached_source_fingerprint(src_dir: Path, data_dirs: tuple[str, ...] = ()) -> str:
    """带构建级缓存的 :func:`source_fingerprint`：同键目录树只扫描一次.

    同一次构建中 Nuitka stamp（:meth:`NuitkaCompile._stamp_key`）与 pyc stamp
    （:func:`fspack.packaging.pyc.stamp._pyc_stamp_key`）会对同一 ``dist/src``
    各算一次全树指纹，缓存命中场景（stamp 命中早退、dist/src 未被修改）下
    第二次直接复用，省一次全树扫描。

    **失效约定**（防脏缓存优先于收益）：构建流程会修改 ``dist/src`` 树
    （Nuitka 编译删 .py、pyc 剥离删 .py），修改后必须调用
    :func:`clear_fingerprint_cache` 失效缓存：

    - :func:`fspack.packaging.pipeline.deps_stage._analyze_dependencies` 入口
      （每次构建开始，保证跨构建失效）
    - :meth:`NuitkaCompile.compile_with_stamp` 编译写 stamp 后
      （Nuitka 编译已删除 dist/src 下 .py）

    :func:`source_fingerprint` 本身保持无缓存（测试与基准测试直接调用，
    语义为"始终反映当前目录树状态"）。
    """
    return source_fingerprint(src_dir, data_dirs)


def clear_fingerprint_cache() -> None:
    """清空 :func:`cached_source_fingerprint` 的构建级指纹缓存.

    在构建入口与"构建流程修改了指纹计算目录树"的节点调用，保证缓存值
    始终与磁盘状态一致（宁可放弃缓存收益也不能返回脏指纹）。
    """
    cached_source_fingerprint.cache_clear()


def _iter_py_entries(current: Path, root: Path, data_dirs: tuple[Path, ...] = ()) -> Iterator[tuple[str, int, int]]:
    """递归遍历 ``.py`` 与 ``.qml`` 文件，返回 ``(相对路径, mtime_ns, size)`` 三元组。

    后缀范围与 :func:`fspack.analyzer.analyze_dependencies` 的分析范围一致
    （QML 修改须触发指纹变化），data-dirs 排除逻辑亦一致。

    :func:`os.scandir` 返回的 :class:`os.DirEntry` 对象缓存了目录枚举时的
    stat 信息，``entry.stat(follow_symlinks=False)`` 直接复用缓存避免独立
    stat 调用。剪枝排除 ``_EXCLUDED_DIRS`` 与 ``*.egg-info`` 目录，以及
    ``data_dirs`` 数据资源目录树（含 data-dir 自身）。

    ``data_dirs`` 判断用预计算的相对 parts 前缀纯字符串比较，消除逐条目
    ``Path.resolve()`` 系统调用（Windows ~20-50µs/次）；不在 ``root`` 树内
    的 data-dir 直接丢弃——原逐条目 ``resolve`` + ``relative_to`` 同样
    永不匹配，行为等价。

    条目按名称排序（含子目录），保证遍历顺序跨平台确定性——``os.walk``
    不保证目录遍历顺序，导致旧实现在不同文件系统上指纹不一致。
    """
    root_resolved = root.resolve()
    prefixes: list[tuple[str, ...]] = []
    for dp in data_dirs:
        try:
            prefixes.append(dp.relative_to(root_resolved).parts)
        except ValueError:
            continue
    yield from _iter_entries_tree(current, (), tuple(prefixes))


def _iter_entries_tree(
    current: Path,
    rel_parts: tuple[str, ...],
    data_dir_prefixes: tuple[tuple[str, ...], ...],
) -> Iterator[tuple[str, int, int]]:
    """``_iter_py_entries`` 的递归主体：携带相对 parts 做 data-dirs 前缀剪枝.

    ``rel_parts`` 为 ``current`` 相对遍历根的路径组件（递归时元组拼接），
    同时用于产出相对路径（``"/".join``），避免每条目 ``relative_to``。
    """
    for entry in sorted(os.scandir(current), key=lambda e: e.name):
        entry_rel = (*rel_parts, entry.name)
        if entry.is_dir(follow_symlinks=False):
            if _is_excluded_name(entry.name):
                continue
            # data-dirs 剪枝：整个目录树不遍历
            if data_dir_prefixes and any(entry_rel[: len(p)] == p for p in data_dir_prefixes):
                continue
            yield from _iter_entries_tree(Path(entry.path), entry_rel, data_dir_prefixes)
        elif entry.is_file(follow_symlinks=False) and entry.name.endswith((".py", ".qml")):
            # data-dirs 内的单文件也排除（防御性，剪枝应已跳过整个目录）
            if data_dir_prefixes and any(entry_rel[: len(p)] == p for p in data_dir_prefixes):
                continue
            st = entry.stat(follow_symlinks=False)
            yield ("/".join(entry_rel), st.st_mtime_ns, st.st_size)
