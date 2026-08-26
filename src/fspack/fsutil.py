"""文件系统与目录工具：目录大小计算、原子写入、安全删除.

收敛此前散落在多处的同类实现：

- 三份 ``_dir_size`` 副本（``doctor_envs``/``packaging.sync``/``packaging.size_report``），
  返回类型与遍历策略不同，故拆为三个命名函数而非合一：

  - :func:`walk_dir_size` — ``os.walk`` 遍历，返回总字节数（``int``）。
  - :func:`scandir_dir_size` — ``os.scandir`` 遍历（复用 stat 缓存，大目录更快），
    返回总字节数（``int``）；配套 :func:`scandir_tree` 递归生成器。
  - :func:`dir_size_with_count` — ``Path.rglob`` 遍历，返回 ``(总字节数, 文件数)``。

- :func:`atomic_write_text` — 原子写文本（先写临时文件再 rename），
  埋在 ``nuitka.compile`` 的私有实现上提。
- :func:`safe_unlink` — 删除文件，``OSError`` 仅告警不抛。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

__all__ = [
    "atomic_write_text",
    "dir_size_with_count",
    "rmtree_longpath",
    "safe_unlink",
    "scandir_dir_size",
    "scandir_tree",
    "walk_dir_size",
]

_logger = logging.getLogger(__name__)


def walk_dir_size(path: Path) -> int:
    """递归计算目录总字节数（``os.walk`` 遍历，不跟随符号链接）.

    对每个文件单独 ``stat``，文件被并发删除或权限问题时跳过。

    :param path: 目录路径
    :return: 目录下所有文件的总字节数
    """
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def scandir_tree(root: Path) -> Iterator[os.DirEntry[str]]:
    """递归遍历 ``root``，yield 所有文件 ``DirEntry``（不含目录自身）.

    用 ``os.scandir`` 替代 ``Path.rglob("*")``：``DirEntry`` 缓存 stat 信息，
    ``is_file(follow_symlinks=False)`` 复用缓存避免独立 stat 调用。
    遇到权限/不存在等 ``OSError`` 静默跳过（与 rglob 行为一致）。

    :param root: 遍历根目录
    :return: 文件 ``DirEntry`` 迭代器（按名称排序，深度优先）
    """
    try:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from scandir_tree(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield entry
        except OSError:
            continue


def scandir_dir_size(path: Path) -> int:
    """递归计算目录总字节数（``os.scandir`` 遍历，复用 stat 缓存）.

    用 :func:`scandir_tree` 枚举文件，``DirEntry.stat`` 复用枚举时的 stat 缓存
    （Windows ``WIN32_FIND_DATA`` / Linux ``d_ino``），避免对每个文件单独 stat
    系统调用。大目录（数千文件）下显著减少系统调用次数。

    :param path: 目录路径
    :return: 目录下所有文件的总字节数
    """
    total = 0
    for entry in scandir_tree(path):
        try:
            total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            # 文件被并发删除或权限问题：跳过，不阻断流程
            continue
    return total


def dir_size_with_count(path: Path) -> tuple[int, int]:
    """递归计算目录总字节数与文件数（``Path.rglob`` 遍历）.

    :param path: 目录路径
    :return: ``(总字节数, 文件数)``；``path`` 非目录时返回 ``(0, 0)``。
        文件被并发删除或权限问题时跳过，不阻断报告生成。
    """
    total = 0
    count = 0
    if not path.is_dir():
        return 0, 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count


def atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    """原子写入文本文件：先写临时文件再 rename，避免半写入文件被读取.

    用 ``tempfile.mkstemp`` 在目标目录创建临时文件（同目录保证 ``Path.replace``
    是原子操作：POSIX rename(2) 原子，Windows ReplaceFile 原子），写入完成后
    ``f.flush() + os.fsync()`` 强制落盘（防掉电/系统崩溃后留下空文件或截断文件），
    再 ``Path.replace`` 替换目标文件。任何失败（含 KeyboardInterrupt 等基础异常）
    都清理临时文件后重抛，``OSError`` 语义分支保持不变。

    :param target: 目标文件路径（父目录不存在时自动创建）
    :param content: 待写入文本内容
    :param encoding: 文本编码，默认 ``utf-8``
    :raises OSError: 写入或 rename 失败（临时文件已清理）
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=target.parent, prefix=".tmp_", suffix=target.suffix)
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(content)
            # 刷缓冲并 fsync 落盘：rename 原子性只保证"新旧文件二选一"，
            # 不保证数据已写回磁盘；掉电场景下未 fsync 的 rename 可能留下空文件
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(target)
    except BaseException:
        # 捕获 BaseException（含 KeyboardInterrupt/SystemExit）：任何退出路径都
        # 先清理临时文件再重抛，避免残留 .tmp_* 文件；OSError 语义与原先一致
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def safe_unlink(path: Path, *, logger: logging.Logger | None = None) -> None:
    """删除文件，``OSError`` 仅告警不抛（用于损坏文件/临时文件清理）.

    :param path: 待删除文件路径
    :param logger: 记录删除失败的日志器，``None`` 时用本模块日志器
    """
    try:
        path.unlink()
    except OSError as e:
        (logger or _logger).warning("删除文件失败: %s: %s", path, e)


def rmtree_longpath(path: Path) -> None:
    """递归删除目录树，Windows 下加 ``\\\\?\\`` 前缀规避 MAX_PATH 260 限制.

    node_modules/.pnpm 等深层目录的文件路径可超 260 字符，普通
    ``shutil.rmtree`` 的 ``os.scandir`` 无法枚举超长路径，抛
    ``FileNotFoundError(WinError 3)`` 中途残留。``\\\\?\\`` 前缀告知
    Win32 跳过路径规范化与长度检查，要求绝对路径，故先
    :meth:`Path.resolve`。非 Windows 平台退化为普通 ``shutil.rmtree``。

    :param path: 待删除目录
    :raises OSError: 删除失败
    """
    s = str(path.resolve())
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + s
    shutil.rmtree(s)
