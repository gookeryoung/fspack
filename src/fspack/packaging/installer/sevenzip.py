"""跨平台 7z 便携包生成（调用系统 7-Zip 命令行，零 Python 依赖）.

与 zip 便携包同构的编排（可选 build → 校验可执行文件 → staging → 压缩），
压缩交由系统已安装的 7-Zip 命令行完成，不引入 py7zr 等第三方依赖，避免
项目膨胀。命令行参数取最高压缩与多线程：

- ``-mx=9``：超高压缩（LZMA2，64MB 字典）
- ``-mmt=on``：多线程压缩（LZMA2 分块并行，尽可能利用全部核心）

可执行文件定位顺序（:func:`_find_7z`）：PATH 中的 ``7z``/``7za``/``7zr``
（POSIX 另含官方新版命名 ``7zz``）→ Windows 默认安装目录
（``%ProgramFiles%\\7-Zip\\7z.exe``，7-Zip 安装器默认不写 PATH）。
未找到时抛 :class:`InstallerError` 并给出各平台安装建议。

依赖 :mod:`fspack.packaging.installer.dist_prep` 提供 staging 准备
（``_prepare_staging`` 与 tar.gz/zip 共用同一 ``<base>`` staging 目录，
多格式场景复用消除重复 copytree）。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from fspack.config import ProjectInfo
from fspack.console import console
from fspack.exceptions import InstallerError
from fspack.packaging.installer.base import _run_stage, _run_tool
from fspack.packaging.installer.dist_prep import (
    _check_exe,
    _prepare_dist,
    _prepare_staging,
    _release_base,
)
from fspack.packaging.installer.request import ReleaseRequest
from fspack.packaging.installer.zip import _zip_platform_suffix
from fspack.platform import Platform
from fspack.progress import BuildTracker

__all__ = ["_find_7z", "_make_7z", "build_sevenzip"]

_logger = logging.getLogger("fspack.packaging.installer")

# PATH 中依次探测的 7-Zip 可执行文件名；7zz 为 Linux/macOS 官方新版命名
_PATH_CANDIDATES: tuple[str, ...] = ("7z", "7za", "7zr") if sys.platform == "win32" else ("7z", "7zz", "7za", "7zr")

# 未找到 7-Zip 时的安装建议（各平台）
_NOT_FOUND_MSG = (
    "未找到 7-Zip 命令行工具（已探测 PATH 与默认安装目录），7z 格式需要系统 7-Zip："
    "Windows 从 https://www.7-zip.org/ 安装（默认目录无需加 PATH 即可探测）；"
    "Linux 安装 p7zip-full（如 sudo apt install -y p7zip-full）或 7zip；"
    "macOS 安装 sevenzip（brew install sevenzip）或 p7zip"
)


def _find_7z() -> str | None:
    """定位系统 7-Zip 可执行文件，返回其路径（未找到返回 ``None``）.

    探测顺序：PATH 中的 ``7z``/``7za``/``7zr``（POSIX 另含 ``7zz``）→
    Windows 默认安装目录 ``%ProgramFiles%\\7-Zip\\7z.exe``（7-Zip 安装器
    默认不写 PATH，须回退目录探测）。

    :return: 可执行文件路径字符串，未安装返回 ``None``
    """
    for name in _PATH_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "win32":
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            program_files = os.environ.get(var)
            if not program_files:
                continue
            candidate = Path(program_files) / "7-Zip" / "7z.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def build_sevenzip(
    req: ReleaseRequest,
    *,
    target: Platform = Platform.WINDOWS,
    keep_staging: bool = False,
    reuse_staging: bool = False,
) -> Path:
    """编排：可选 build → 校验可执行文件 → 打包 7z 便携包，返回 7z 路径.

    7z 跨平台解压即用（需 7-Zip），压缩率高于 zip（LZMA2 超高压缩）。
    文件名 ``<name>-<version>-<platform>-slim.7z``，内顶层目录同名，解压后
    不污染当前目录。staging 与 zip/tar.gz 共用 ``<base>`` 目录名，多格式
    场景经 ``keep_staging``/``reuse_staging`` 复用（语义与 :func:`build_zip`
    一致）。
    """
    own_tracker = req.tracker is None
    tk = req.tracker or BuildTracker(title="打包阶段汇总")
    dist, info = _prepare_dist(req, target)
    _check_exe(dist, info, target)
    release = dist / "release"
    seven_name = f"{_release_base(info, _zip_platform_suffix(target))}.7z"
    result = _run_stage(
        tk,
        "生成 7z 便携包",
        lambda: _make_7z(dist, info, release, target, keep_staging=keep_staging, reuse_staging=reuse_staging),
        detail=seven_name,
    )
    console.success(f"7z 便携包已生成: {result}")
    if own_tracker:
        console.rich.print(tk.summary())
    return result


def _make_7z(  # noqa: PLR0913
    dist_dir: Path,
    info: ProjectInfo,
    release_dir: Path,
    target: Platform,
    *,
    keep_staging: bool = False,
    reuse_staging: bool = False,
) -> Path:
    """打包 dist 为 7z 便携包，返回 7z 路径.

    顶层目录 ``<name>-<version>-<py_tag>-<platform>-slim``，与 zip 便携包
    同名（多格式场景共享 staging）。压缩参数 ``-mx=9 -mmt=on``：超高压缩 +
    LZMA2 多线程并行。staging 准备复用 :func:`_prepare_staging`（排除
    ``release/`` 与构建中间文件）。

    Args:
        dist_dir: 待打包的 dist 目录
        info: 项目元信息（命名用）
        release_dir: 归档输出目录（同时用作 staging 父目录）
        target: 目标平台（文件名平台后缀）
        keep_staging: 打包后保留 staging 目录（供后续格式复用）
        reuse_staging: 复用已存在的 staging 目录（跳过 copytree）

    Returns:
        生成的 7z 文件路径

    Raises:
        InstallerError: 系统 7-Zip 未安装，或压缩命令执行失败
    """
    exe = _find_7z()
    if exe is None:
        raise InstallerError(_NOT_FOUND_MSG)

    base = _release_base(info, _zip_platform_suffix(target))
    staging = _prepare_staging(dist_dir, release_dir, base, reuse_staging=reuse_staging)
    archive_path = release_dir / f"{base}.7z"
    # cwd=release_dir + 相对路径 base：归档内顶层目录为 <base>/...，与 zip 一致；
    # -t7z 显式 7z 格式，-mx=9 超高压缩，-mmt=on LZMA2 多线程并行，-y 免交互
    _run_tool(
        [exe, "a", "-t7z", "-mx=9", "-mmt=on", "-y", str(archive_path), base],
        not_found_msg=_NOT_FOUND_MSG,
        fail_prefix="7z 打包失败",
        cwd=release_dir,
        produces=archive_path,
    )
    if not keep_staging:
        shutil.rmtree(staging)
    _logger.info("已生成 7z 便携包: %s", archive_path)
    return archive_path
