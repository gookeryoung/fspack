"""Runtime 归档安全解压：条目安全校验 + 损坏归档清理.

拆自 :mod:`fspack.packaging.runtime`，提供 :func:`extract_zip_safe` /
:func:`extract_tar_safe` 两个通用安全解压函数。EmbedRuntime / StandaloneRuntime
子类 ``extract_archive`` 直接委托给这两个函数，消除条目校验重复代码。
"""

from __future__ import annotations

import logging
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

from fspack.exceptions import EmbedError

_logger = logging.getLogger(__name__)


def _safe_unlink_archive(archive_path: Path, label: str) -> None:
    """删除损坏的归档文件，OSError 仅告警不抛."""
    try:
        archive_path.unlink()
    except OSError as e:
        _logger.warning("删除损坏的 %s 失败: %s: %s", label, archive_path, e)


def _validate_tar_member(member: tarfile.TarInfo) -> None:
    """PEP 706 ``data`` filter 等价检查（用于 Python 3.11 及以下手动实现）.

    拒绝：绝对路径（Unix ``/`` 或 Windows 盘符 ``C:``）、路径穿越（``..`` 段）、
    设备文件（字符/块设备）。

    对于符号链接/硬链接：仅拒绝 ``linkname`` 为绝对路径、Windows 盘符或解析后
    逃逸 tarball 根目录的链接，允许相对路径且不穿越的安全链接（与 PEP 706
    ``data`` filter 行为一致）。python-build-standalone 官方 tarball 含两类
    合法相对链接：

    1. ``python/bin/2to3 -> python3.11``（同目录别名，不含 ``..``）；
    2. ``python/share/terminfo/1/1178 -> ../a/adm1178``（ncurses terminfo 数据库
       按字母分目录组织，跨目录别名通过 ``..`` 指向父目录的兄弟子目录，
       解析后为 ``python/share/terminfo/a/adm1178`` 仍在 tarball 根内）。

    正确判断穿越：将 ``linkname`` 与 ``member.name`` 所在目录拼接后逐段规范化，
    若 ``stack`` 为空时仍遇 ``..`` 即真正逃逸根目录；而非简单禁止 ``..`` 段
    （会误拒 terminfo 别名）。
    """
    name = member.name.replace("\\", "/")
    if name.startswith("/"):
        raise EmbedError(f"python-build-standalone tarball 含绝对路径条目: {member.name}")
    if len(name) >= 2 and name[1] == ":":
        raise EmbedError(f"python-build-standalone tarball 含盘符条目: {member.name}")
    if ".." in name.split("/"):
        raise EmbedError(f"python-build-standalone tarball 含路径穿越条目: {member.name}")
    if member.issym() or member.islnk():
        linkname = member.linkname.replace("\\", "/")
        if linkname.startswith("/"):
            raise EmbedError(f"python-build-standalone tarball 含绝对路径链接: {member.name} -> {member.linkname}")
        if len(linkname) >= 2 and linkname[1] == ":":
            raise EmbedError(f"python-build-standalone tarball 含盘符链接: {member.name} -> {member.linkname}")
        base = name.rsplit("/", 1)[0] if "/" in name else ""
        combined = f"{base}/{linkname}" if base else linkname
        stack: list[str] = []
        for seg in combined.split("/"):
            if seg in {"", "."}:
                continue
            if seg == "..":
                if not stack:
                    raise EmbedError(
                        f"python-build-standalone tarball 含路径穿越链接: {member.name} -> {member.linkname}"
                    )
                stack.pop()
            else:
                stack.append(seg)
    if member.isdev():
        raise EmbedError(f"python-build-standalone tarball 含设备文件条目: {member.name}")


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    """校验 zip 条目路径安全：拒绝绝对路径、路径穿越（``..``）、符号链接."""
    name = info.filename.replace("\\", "/")
    if name.startswith("/"):
        raise EmbedError(f"embed zip 含绝对路径条目: {info.filename}")
    if len(name) >= 2 and name[1] == ":":
        raise EmbedError(f"embed zip 含盘符条目: {info.filename}")
    if ".." in name.split("/"):
        raise EmbedError(f"embed zip 含路径穿越条目: {info.filename}")
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise EmbedError(f"embed zip 含符号链接条目: {info.filename}")


def extract_zip_safe(archive_path: Path, runtime_dir: Path, label: str) -> None:
    """zip 安全解压：对每个条目先执行 :func:`_validate_zip_member` 再 extractall.

    损坏 zip（BadZipFile）或条目校验失败（EmbedError）时删除归档避免缓存污染。
    """
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                _validate_zip_member(info)
            zf.extractall(runtime_dir)
    except zipfile.BadZipFile as e:
        _safe_unlink_archive(archive_path, label)
        raise EmbedError(f"{label} 损坏: {archive_path}") from e
    except EmbedError:
        _safe_unlink_archive(archive_path, label)
        raise


def extract_tar_safe(archive_path: Path, runtime_dir: Path, label: str) -> None:
    """tar.gz 安全解压：先逐条目预检，再 extractall（3.12+ 传 filter="data"）.

    预检（:func:`_validate_tar_member`）在**所有** Python 版本执行，不能只在
    低版本预检而 3.12+ 依赖 PEP 706 ``data`` filter：Python 3.13 实测 data
    filter 会把绝对路径（``/etc/passwd``）与 Windows 盘符路径（``C:evil.txt``）
    **静默规范化**为相对路径解压而非拒绝（安全缺口），且其异常统一为
    TarError 子类无区分度（被本函数转为笼统「损坏」消息）。预检保证恶意
    条目按类别拒绝并给出具体 EmbedError 消息；3.12+ extractall 仍传
    ``filter="data"`` 作双重防护（预检通过后 data filter 不会再拒绝任何条目，
    仅防御预检遗漏的边角情况如危险权限位）。

    TarError / OSError / EOFError 或预检失败（EmbedError）时删除归档避免缓存污染。

    ``EOFError`` 单独列出：截断的 tar.gz 在 ``getmembers``/``extractall`` 读到
    gzip 流尾时抛 ``EOFError``（"Compressed file ended before the end-of-stream
    marker was reached"），它既非 ``TarError`` 也非 ``OSError``，漏捕会让损坏
    归档留在缓存且以原始 traceback 崩溃 CLI（下载中断产生半成品文件的典型症状）。
    """
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf.getmembers():
                _validate_tar_member(member)
            if sys.version_info >= (3, 12):
                tf.extractall(runtime_dir, filter="data")
            else:  # pragma: no cover - 测试环境 3.13，低版本分支不可达
                tf.extractall(runtime_dir)
    except (tarfile.TarError, OSError, EOFError) as e:
        _safe_unlink_archive(archive_path, label)
        raise EmbedError(f"{label} 损坏: {archive_path}") from e
    except EmbedError:
        _safe_unlink_archive(archive_path, label)
        raise
