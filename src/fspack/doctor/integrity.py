"""归档与二进制文件完整性检测.

从 :mod:`fspack.doctor.envs` 拆出的底层检测原语（原 iter-148 多 cache 类型
扫描器的公共依赖），供 :mod:`fspack.doctor.cache_health` 各扫描器复用：

- :func:`_is_zip_intact` —— zip 快检（中心目录）/ 全量 CRC 两级校验
- :func:`_is_tar_intact` —— tar.gz 成员表读取校验
- :func:`_is_pe_file` —— PE（MZ 头）识别，用于 loader exe 缓存
- :func:`_try_unlink` / :func:`_file_size` —— best-effort 删除与安全取大小

三态约定：``True`` 完整 / ``False`` 损坏 / ``None`` IO 异常无法判定
（杀软占用/文件锁/权限），调用方对 ``None`` 不计损坏也不删除。
"""

from __future__ import annotations

import gzip
import logging
import tarfile
import zipfile
from pathlib import Path

_logger = logging.getLogger(__name__)

__all__ = [
    "_file_size",
    "_is_pe_file",
    "_is_tar_intact",
    "_is_zip_intact",
    "_try_unlink",
]

# PE 文件 MZ 头（DOS header magic）：用于识别"非空但损坏"的 exe/loader 缓存
_PE_MZ_MAGIC = b"MZ"


def _try_unlink(path: Path) -> None:
    """best-effort 删除文件，OSError 仅告警不抛（扫描器与清理器共用）."""
    try:
        path.unlink()
    except OSError as e:
        _logger.warning("删除文件失败: %s: %s", path, e)


def _file_size(path: Path) -> int:
    """安全取文件大小，OSError 返回 0（与 orphan_size_bytes 累加逻辑兼容）."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _is_zip_intact(path: Path, *, full: bool = False) -> bool | None:
    """检查 zip 文件完整性，``full`` 控制快检/全量校验两级深度.

    - 快检（``full=False``，默认）：仅验证 ``ZipFile`` 能打开并读取中心目录
      （``namelist()`` 触发解析），不逐项读取数据。中心目录位于文件尾部，
      可发现截断/垃圾数据等绝大多数损坏，且无需读取全文件（数百 MB 的
      embed zip 全量 CRC 校验耗时可达秒级，``fsp cache status`` 默认快检）
    - 全量（``full=True``）：调 ``testzip()`` 逐文件读取并校验 CRC，可发现
      中心目录完好但数据区损坏的文件；``fsp cache status --verify`` 启用

    ``zipfile.BadZipFile``/``KeyError``/CRC 校验失败视为损坏返回 ``False``。

    ``OSError``（杀软占用/文件锁/权限）无法判定完整性，返回 ``None``：
    调用方不应据此删除文件（与缓存扫描既有 "OSError 不计损坏"策略一致）。

    :param path: zip 文件路径
    :param full: True 时全量 CRC 校验（testzip），False 时仅快检中心目录
    :return: True 完整 / False 损坏 / None IO 异常无法判定
    """
    try:
        with zipfile.ZipFile(path) as zf:
            if full:
                return zf.testzip() is None
            zf.namelist()
            return True
    except (zipfile.BadZipFile, KeyError):
        return False
    except OSError:
        return None


def _is_tar_intact(path: Path) -> bool | None:
    """检查 tar.gz 文件完整性：``tarfile.open`` 能否正常打开并读取成员表.

    ``tarfile.TarError``/``EOFError``/``gzip.BadGzipFile``（内容损坏，注意其
    为 ``OSError`` 子类需先匹配）视为损坏返回 ``False``；``OSError``（杀软
    占用/文件锁/权限）无法判定返回 ``None``，调用方不应据此删除文件。

    :return: True 完整 / False 损坏 / None IO 异常无法判定
    """
    try:
        with tarfile.open(path, "r:gz") as tf:
            # getmembers 触发实际读取（仅读 header），不需要 extractall
            tf.getmembers()
            return True
    except (tarfile.TarError, EOFError, gzip.BadGzipFile):
        return False
    except OSError:
        return None


def _is_pe_file(path: Path) -> bool | None:
    """检查文件是否为合法 PE（Windows 可执行）：MZ 头 + 非空.

    loader exe 缓存为 mingw/gcc 编译产物，文件头应为 ``MZ``（DOS header magic）。
    0 字节文件或缺少 MZ 头视为损坏（如磁盘写满导致截断、缓存写入被中断）。

    ``OSError``（杀软占用/文件锁/权限）无法判定，返回 ``None``，调用方
    不应据此删除文件。

    :return: True 合法 PE / False 损坏 / None IO 异常无法判定
    """
    try:
        with path.open("rb") as f:
            head = f.read(2)
    except OSError:
        return None
    return head == _PE_MZ_MAGIC
