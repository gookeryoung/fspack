"""源码与目录同步：copy_source、增量同步、目录大小、site-packages 指纹.

本模块从 :mod:`fspack.builder` 抽离，仅含源码同步与目录度量辅助函数，
无外部 API 依赖。``builder.py`` 通过 re-export 保持公开 API 不变。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Iterator

_logger = logging.getLogger(__name__)

# dist/src 仅保留应用运行所需源码与资源，剥离所有开发期文件。
# 向后兼容策略：未在下方显式列出的文件默认保留，避免误删项目特有运行时资源。
# LICENSE 不排除：分发产物保留许可证文件满足 MIT/GPL 等开源协议「随附 LICENSE」要求。
_EXCLUDE = shutil.ignore_patterns(
    # 构建产物与 Python 缓存
    "dist",
    "build",
    "__pycache__",
    "*.egg-info",
    "*.pyc",
    "*.pyo",
    # 虚拟环境、测试与覆盖率
    ".venv",
    ".tox",
    ".pytest_cache",
    "htmlcov",
    ".coverage",
    ".coverage.*",
    "coverage.xml",
    "tests",
    # 工具缓存
    ".ruff_cache",
    ".pyrefly_cache",
    ".mypy_cache",
    ".uv-cache",
    # 版本控制
    ".git",
    ".gitignore",
    ".gitattributes",
    # IDE 与编辑器
    ".idea",
    ".vscode",
    "*.code-workspace",
    # fspack 自身目录
    ".fspack",
    ".trae",
    # 凭证与敏感信息（rule-11 安全要求：.env 须排除避免泄漏到 dist）
    ".env",
    ".env.*",
    # Python 项目元数据（打包阶段已解析完毕，运行时不再需要）
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    "requirements*.txt",
    # 工具链配置文件（rule-11 独立配置文件，仅开发期使用）
    "ruff.toml",
    ".ruff.toml",
    "pyrefly.toml",
    "pytest.ini",
    "tox.ini",
    ".bumpversion.toml",
    ".pre-commit-config.yaml",
    ".coveragerc",
    ".readthedocs.yaml",
    "Makefile",
    ".copier-answers.yml",
    # CI/CD
    ".github",
    # 文档（应用运行时不需要）
    "*.md",
    "*.rst",
    "docs",
)


def copy_source(project_dir: Path, src_dst: Path, extra_excludes: tuple[str, ...] = ()) -> None:
    """将项目源码同步到 dist/src，剥离开发期文件.

    保留应用运行所需源码与资源（``.py``/数据文件/``LICENSE`` 等），
    排除构建产物、缓存、虚拟环境、工具配置、项目元数据（
    ``pyproject.toml``/``.python-version``/``uv.lock`` 等）、
    凭证（``.env``）、文档（``*.md``/``*.rst``/``docs``）与测试代码（``tests``）。
    详见 ``_EXCLUDE`` 模式列表。

    ``extra_excludes`` 为 ``[tool.fspack] exclude`` 配置的额外排除模式，
    合并到内置 ``_EXCLUDE`` 中（如排除 ``examples`` 目录）。

    增量同步：``src_dst`` 已存在时保留 ``__pycache__`` 目录以复用 ``.pyc`` 缓存，
    仅删除源码中已不存在的文件、覆盖复制新增/改动的文件（``copy2`` 保留 mtime）。
    """
    ignore_fn = _merge_excludes(_EXCLUDE, extra_excludes) if extra_excludes else _EXCLUDE
    if src_dst.exists():
        _sync_tree(project_dir, src_dst, ignore_fn)
    else:
        shutil.copytree(project_dir, src_dst, ignore=ignore_fn)


def _merge_excludes(base: Callable[..., set[str]], extra: tuple[str, ...]) -> Callable[..., set[str]]:
    """合并内置排除函数与配置额外排除模式.

    返回的函数对同一 ``(directory, names)`` 取两者排除集的并集。
    """
    extra_fn = shutil.ignore_patterns(*extra)

    def combined(directory: str, names: list[str]) -> set[str]:
        return base(directory, names) | extra_fn(directory, names)

    return combined


def _sync_tree(src: Path, dst: Path, ignore_fn: Callable[..., set[str]]) -> None:
    """增量同步 src 到 dst，保留 dst 中的 ``__pycache__`` 以复用 .pyc 缓存.

    1. 删除 dst 中 src 没有的文件/目录（``__pycache__`` 除外）；
    2. 复制 src 中的文件——mtime_ns + size 相同时跳过 ``copy2``（避免重复磁盘写），
       否则用 ``copy2`` 覆盖（保留 mtime 供 compileall 增量判断）。

    用 :func:`os.scandir` 替代 :meth:`Path.iterdir`：``DirEntry.stat`` 复用
    目录枚举时的 stat 缓存，避免对每个文件单独 stat 系统调用。增量同步场景
    下需对比 src 与 dst 的 mtime_ns/size，DirEntry 缓存可减半 stat 调用次数。
    """
    src_names: list[str] = []
    src_entries: dict[str, os.DirEntry[str]] = {}
    try:
        with os.scandir(src) as it:
            for entry in it:
                src_names.append(entry.name)
                src_entries[entry.name] = entry
    except OSError:
        return
    ignored = ignore_fn(str(src), src_names) if ignore_fn else set()
    keep = set(src_names) - ignored

    for item in dst.iterdir():
        if item.name == "__pycache__":
            continue
        if item.name not in keep:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

    for name in keep:
        _sync_entry(src_entries[name], src / name, dst / name, ignore_fn)


def _sync_entry(
    src_entry: os.DirEntry[str],
    src_item: Path,
    dst_item: Path,
    ignore_fn: Callable[..., set[str]],
) -> None:
    """同步单个 ``src_entry`` 到 ``dst_item``（从 :func:`_sync_tree` 拆分，降低分支数）.

    - 目录：递归 :func:`_sync_tree`
    - 已存在文件：mtime_ns + size 相同跳过，否则 ``copy2`` 覆盖
    - 不存在文件：直接 ``copy2``

    ``DirEntry.stat(follow_symlinks=False)`` 复用枚举缓存，避免独立 stat 调用。
    """
    try:
        is_dir = src_entry.is_dir(follow_symlinks=False)
    except OSError:
        return
    if is_dir:
        dst_item.mkdir(exist_ok=True)
        _sync_tree(src_item, dst_item, ignore_fn)
        return
    if not dst_item.is_file():
        shutil.copy2(src_item, dst_item)
        return
    # mtime_ns + size 相同视为未改动，跳过 copy2 避免不必要的磁盘写
    try:
        src_st = src_entry.stat(follow_symlinks=False)
    except OSError:
        return
    try:
        dst_st = dst_item.stat()
    except OSError:
        shutil.copy2(src_item, dst_item)
        return
    if src_st.st_mtime_ns == dst_st.st_mtime_ns and src_st.st_size == dst_st.st_size:
        return
    shutil.copy2(src_item, dst_item)


def _dir_size(path: Path) -> int:
    """递归计算目录总字节数（文件大小累加，不含目录元数据）.

    用 :func:`os.scandir` 替代 :meth:`Path.rglob`：``DirEntry.stat`` 复用
    枚举时的 stat 缓存（Windows ``WIN32_FIND_DATA`` / Linux ``d_ino``），
    避免对每个文件单独 stat 系统调用。大目录（PySide6 site-packages 数千文件）
    下显著减少系统调用次数。
    """
    total = 0
    for entry in _scandir_tree(path):
        try:
            total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            # 文件被并发删除或权限问题：跳过，不阻断精简流程
            continue
    return total


def _scandir_tree(root: Path) -> Iterator[os.DirEntry[str]]:
    """递归遍历 ``root``，yield 所有文件 ``DirEntry``（不含目录自身）.

    用 ``os.scandir`` 替代 ``Path.rglob("*")``：DirEntry 缓存 stat 信息，
    ``is_file(follow_symlinks=False)`` 复用缓存避免独立 stat 调用。
    遇到权限/不存在等 OSError 静默跳过（与 rglob 行为一致）。
    """
    try:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from _scandir_tree(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield entry
        except OSError:
            continue


def _site_packages_fingerprint(sp: Path) -> str:
    """site-packages 指纹：``dist-info`` 目录名排序后哈希，快速检测依赖变化.

    用 :meth:`Path.glob` 直接匹配 ``*.dist-info``，避免 ``iterdir`` 遍历
    site-packages 中数千个文件（如 PySide2）时的 stat 开销。
    """
    if not sp.is_dir():
        return ""
    h = hashlib.sha256()
    for d in sorted(sp.glob("*.dist-info")):
        h.update(d.name.encode())
    return h.hexdigest()
