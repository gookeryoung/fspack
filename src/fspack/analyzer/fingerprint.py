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
from collections.abc import Iterator
from functools import lru_cache
from operator import attrgetter
from pathlib import Path
from typing import Any, Protocol


class _HashLike(Protocol):
    """具有 ``update`` 方法的类哈希对象协议（BLAKE2b/SHA256 等）."""

    def update(self, data: Any, /) -> None: ...


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

    用 :func:`os.scandir` 递归遍历（depth-first，保证确定性），利用
    :meth:`os.DirEntry.stat` 缓存目录枚举时的 stat 信息，避免对每个文件
    单独 ``stat`` 系统调用。条目按名称排序（``attrgetter("name")`` 代替
    lambda 微优化），保证跨平台/文件系统的指纹确定性。

    **性能优化点**（相对原版递归实现约 15% 提速，从 cProfile 实测：
    函数调用次数 119K → 64K）：

    - 纯字符串路径：递归时直接传 ``entry.path``（str）而非构造
      ``Path(entry.path)``，消除每层 Path 对象开销。
    - f-string 前缀拼接：``f"{prefix}/{name}"`` 替代 ``(*rel_parts, name)``
      元组拼接 + ``"/".join``，省掉中间元组对象。
    - 字符串前缀剪枝：data_dirs 用 ``entry_rel == p or entry_rel.startswith(p + "/")``
      替代元组切片 ``entry_rel[:len(p)] == p``。

    用 :func:`hashlib.blake2b` 替代 :func:`hashlib.sha256`：BLAKE2b 在 CPython
    实现中略快（约 10-20%），且 ``digest_size=32`` 输出 64 hex 字符与
    SHA-256 长度一致，缓存键文件名兼容。BLAKE2b 抗碰撞性足够用于缓存键场景。

    需要构建级复用时请用 :func:`cached_source_fingerprint`（stamp 键计算等
    同一构建内多次调用同一目录的场景）。
    """
    h = hashlib.blake2b(digest_size=32)
    data_prefixes = _compute_data_prefixes(src_dir, data_dirs) if data_dirs else ()
    _walk_recursive(h, str(src_dir), "", data_prefixes)
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


def _compute_data_prefixes(src_dir: Path, data_dirs: tuple[str, ...]) -> tuple[str, ...]:
    """把 ``data_dirs`` 相对路径列表预计算成字符串前缀集合.

    每个前缀是 ``"src/fspack/assets/templates"`` 形式，depth-first 递归遍历时
    用 ``entry_rel == p or entry_rel.startswith(p + "/")`` 做目录树剪枝——与
    原实现的 ``entry_rel[:len(p)] == p`` 元组切片语义完全等价，但纯字符串
    操作更快（省掉每层元组创建/切片）。

    不在 ``src_dir`` 树内的 data-dir 被丢弃（``relative_to`` 抛 ValueError），
    行为与原实现一致。
    """
    root_resolved = src_dir.resolve()
    prefix_list: list[str] = []
    for rel in data_dirs:
        dp = (src_dir / Path(rel)).resolve()
        try:
            rel_parts = dp.relative_to(root_resolved).parts
        except ValueError:
            continue
        if rel_parts:
            prefix_list.append("/".join(rel_parts))
    return tuple(prefix_list)


def _walk_recursive(
    h: _HashLike,
    current_str: str,
    prefix: str,
    data_prefixes: tuple[str, ...],
) -> None:
    """depth-first 递归遍历，直接向哈希器写入 ``(rel|mtime|size)\\n`` 条目.

    纯字符串路径版本：递归时传 ``entry.path``（str），相对路径拼接用
    ``f"{prefix}/{name}"``，省掉原版 ``Path(entry.path)`` 构造与
    ``(*rel_parts, name)`` 元组拼接 + ``"/".join``。条目按名称排序
    （``attrgetter("name")`` 替代 lambda），保证跨平台确定性。
    """
    for entry in sorted(os.scandir(current_str), key=attrgetter("name")):
        name = entry.name
        entry_rel = f"{prefix}/{name}" if prefix else name
        if entry.is_dir(follow_symlinks=False):
            if _is_excluded_name(name):
                continue
            if data_prefixes and _is_under_data_dir(entry_rel, data_prefixes):
                continue
            _walk_recursive(h, entry.path, entry_rel, data_prefixes)
        elif entry.is_file(follow_symlinks=False) and name.endswith((".py", ".qml")):
            if data_prefixes and _is_under_data_dir(entry_rel, data_prefixes):
                continue
            st = entry.stat(follow_symlinks=False)
            h.update(f"{entry_rel}|{st.st_mtime_ns}|{st.st_size}\n".encode())


def _is_under_data_dir(entry_rel: str, prefixes: tuple[str, ...]) -> bool:
    """判断 ``entry_rel`` 是否落在任一 data-dir 前缀下（含 data-dir 自身）.

    ``entry_rel`` 与 ``prefixes`` 都是 ``"a/b/c"`` 形式的 POSIX 路径字符串。
    """
    return any(entry_rel == p or entry_rel.startswith(p + "/") for p in prefixes)


def _iter_py_entries(
    current: Path,
    root: Path,
    data_dirs: tuple[Path, ...] = (),
) -> Iterator[tuple[str, int, int]]:
    """递归遍历 ``.py`` 与 ``.qml`` 文件，返回 ``(相对路径, mtime_ns, size)`` 三元组.

    遍历逻辑委托给 :func:`_iter_recursive`，与 :func:`source_fingerprint`
    共用一套 depth-first 递归结构，保持迭代器接口以便测试和增量调用方复用。
    参数 ``current``/``root``/``data_dirs`` 语义不变——``data_dirs`` 为已
    resolve 的 Path 元组（与历史签名兼容）。
    """
    if data_dirs:
        root_resolved = root.resolve()
        prefix_list: list[str] = []
        for dp in data_dirs:
            try:
                rel_parts = dp.relative_to(root_resolved).parts
            except ValueError:
                continue
            if rel_parts:
                prefix_list.append("/".join(rel_parts))
        prefixes = tuple(prefix_list)
    else:
        prefixes = ()
    yield from _iter_recursive(str(current), "", prefixes)


def _iter_recursive(
    current_str: str,
    prefix: str,
    data_prefixes: tuple[str, ...],
) -> Iterator[tuple[str, int, int]]:
    """depth-first 递归迭代器版本，yield ``(rel_path, mtime_ns, size)``."""
    for entry in sorted(os.scandir(current_str), key=attrgetter("name")):
        name = entry.name
        entry_rel = f"{prefix}/{name}" if prefix else name
        if entry.is_dir(follow_symlinks=False):
            if _is_excluded_name(name):
                continue
            if data_prefixes and _is_under_data_dir(entry_rel, data_prefixes):
                continue
            yield from _iter_recursive(entry.path, entry_rel, data_prefixes)
        elif entry.is_file(follow_symlinks=False) and name.endswith((".py", ".qml")):
            if data_prefixes and _is_under_data_dir(entry_rel, data_prefixes):
                continue
            st = entry.stat(follow_symlinks=False)
            yield (entry_rel, st.st_mtime_ns, st.st_size)
