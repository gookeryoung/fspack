"""NSIS 工具链管理：缓存识别、本地归档解压与按需下载.

:meth:`fspack.packaging.installer.nsis.compile_installer` 编译 ``.nsi``
安装包需要 makensis。此前仅依赖 PATH 中的系统安装（choco/apt），本模块
将 NSIS 纳入 fspack 缓存体系（``<cache_root>/nsis``，与 winlibs 工具链
同模式），支持识别用户手动放置的归档、按需下载与解压：

- **缓存命中**：``<cache_root>/nsis/<dir>/Bin/makensis.exe`` 已存在即就绪
  （``<dir>`` 为归档顶层目录名，官方 ``nsis-3.11`` 或 portable 变体
  ``nsis-3.11-portable``；根级 ``makensis.exe`` 是启动器，``Bin/`` 下
  才是真实编译器）
- **本地归档**：缓存目录下精确匹配锁定版本文件名的 ``.zip``/``.7z``
  归档（官方仅发布 ``nsis-3.11.zip``；portable ``.7z`` 为社区重打包
  变体，顶层目录多一段 ``-portable`` 后缀）。``.zip`` 优先（标准库
  :mod:`zipfile` 解压零依赖）；``.7z`` 需系统 7-Zip（复用
  :mod:`fspack.packaging.installer.sevenzip` 的探测逻辑）。用户归档
  解压后保留不删；损坏归档删除后回退（用户归档保留原则的例外——
  损坏文件无保留价值，与 winlibs 口径一致）
- **PATH**：系统已装 makensis（choco/apt）时直接使用，不强制下载
- **下载**：在线且以上均未命中时下载官方 zip 解压（tauri GitHub 镜像
  优先——国内可达性优于 SourceForge，两者内容一致）；下载归档解压后
  删除。官方无 ``.7z`` 发布，``.7z`` 仅作为本地归档识别格式

非 Windows 平台不做缓存管理（Linux 交叉打包沿用 PATH 中的 makensis）。
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from fspack.config.cache import is_offline, nsis_cache_dir
from fspack.exceptions import InstallerError
from fspack.packaging.installer.sevenzip import _find_7z

__all__ = [
    "NSIS_ARCHIVE_NAMES",
    "NSIS_VERSION",
    "ensure_nsis",
    "find_cached_makensis",
]

_logger = logging.getLogger("fspack.packaging.installer")

# 锁定的 NSIS 版本（makensis 3.x 脚本语法稳定，按当前官方稳定版锁定）
NSIS_VERSION = "3.11"

# 下载源（均为官方内容的 zip）：tauri GitHub 镜像优先（国内可达性好），
# SourceForge 官方兜底（prdownloads 会 302 到就近镜像站）
NSIS_URLS: tuple[str, ...] = (
    f"https://github.com/tauri-apps/binary-releases/releases/download/nsis-{NSIS_VERSION}/nsis-{NSIS_VERSION}.zip",
    f"https://prdownloads.sourceforge.net/nsis/nsis-{NSIS_VERSION}.zip",
)

# 本地归档识别名（精确匹配，.zip 优先——标准库解压无外部依赖）：
# 官方 nsis-3.11.zip 与社区 portable 重打包变体（.zip/.7z 双格式）。
# 版本不匹配的归档不识别，避免未知布局被误用
NSIS_ARCHIVE_NAMES: tuple[str, ...] = (
    f"nsis-{NSIS_VERSION}.zip",
    f"nsis-{NSIS_VERSION}-portable.zip",
    f"nsis-{NSIS_VERSION}.7z",
    f"nsis-{NSIS_VERSION}-portable.7z",
)

# 归档顶层目录名（= 归档文件名去扩展名），缓存命中检查按此拼接
# Bin/makensis.exe
_NSIS_DIR_NAMES: tuple[str, ...] = (f"nsis-{NSIS_VERSION}", f"nsis-{NSIS_VERSION}-portable")

# NSIS zip 下载超时（秒）：归档约 2.3MB，慢网络留足余量
_NSIS_DOWNLOAD_TIMEOUT = 300

# 7z 解压超时（秒）：约 3MB 归档解压秒级，杀软扫描下留余量
_NSIS_7Z_TIMEOUT = 120.0

# 离线报错中提示用户放置的归档名（锁定版本 + 双格式，含 portable 变体）
_NSIS_ARCHIVE_HINT = f"nsis-{NSIS_VERSION}[-portable].zip/.7z"

# 需要 7-Zip 却未安装时的安装建议（与 winlibs/sevenzip 的提示口径一致）
_SEVENZIP_REQUIRED_MSG = (
    "NSIS .7z 归档需系统 7-Zip 解压：Windows 从 https://www.7-zip.org/ 安装"
    "（默认目录无需加 PATH 即可探测）；Linux 安装 p7zip-full 或 7zip；"
    "macOS 安装 sevenzip（brew install sevenzip）"
)


def find_cached_makensis() -> Path | None:
    """返回缓存中已就绪的 makensis.exe 路径，未缓存返回 ``None``.

    按锁定版本的归档顶层目录名逐一检查 ``Bin/makensis.exe``（真实编译
    器；根级 ``makensis.exe`` 为启动器）。doctor 盘点与工具检查复用
    本函数作为只读探测入口（不触发下载）。
    """
    for dir_name in _NSIS_DIR_NAMES:
        exe = nsis_cache_dir() / dir_name / "Bin" / "makensis.exe"
        if exe.is_file():
            return exe
    return None


def _find_local_nsis_archive() -> Path | None:
    """在 NSIS 缓存目录递归查找锁定版本的本地归档（.zip 优先于 .7z）.

    精确匹配 :data:`NSIS_ARCHIVE_NAMES` 的文件名（缓存根与任意子目录），
    版本/变体不匹配的归档不识别。用户手动放置或上次下载中断的残留
    均可命中。
    """
    cache_dir = nsis_cache_dir()
    if not cache_dir.is_dir():
        return None
    for name in NSIS_ARCHIVE_NAMES:
        for archive_path in sorted(cache_dir.rglob(name)):
            if archive_path.is_file():
                return archive_path
    return None


def _extract_7z(archive: Path, dest: Path) -> None:
    """调用系统 7-Zip 解压 ``.7z`` 归档到 ``dest`` 目录.

    :raises InstallerError: 7-Zip 未安装、命令执行失败/超时、或归档损坏
        （非零退出码，含尾部错误输出便于定位）。
    """
    exe = _find_7z()
    if exe is None:
        raise InstallerError(f"系统未安装 7-Zip，无法解压 {archive}: {_SEVENZIP_REQUIRED_MSG}")
    try:
        result = subprocess.run(
            [exe, "x", "-y", f"-o{dest}", str(archive)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_NSIS_7Z_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise InstallerError(f"NSIS 7z 解压失败 {archive}: {e}") from e
    if result.returncode != 0:
        # 尾部输出截断到 300 字符（7z 错误详情在末尾，避免刷屏）
        tail = (result.stderr or result.stdout or "").strip()[-300:]
        raise InstallerError(f"NSIS 7z 解压失败 {archive}（退出码 {result.returncode}）: {tail}")


def _extract_nsis_archive(archive: Path) -> Path:
    """解压 NSIS 归档（.zip/.7z）到缓存目录，验证 makensis 就位并返回路径.

    归档顶层即 ``nsis-3.11[-portable]/`` 目录树，解压到缓存目录得到
    ``<cache_root>/nsis/<dir>/Bin/makensis.exe``。不删除归档（用户资产
    保留；下载临时文件的清理由调用方决定）。

    :raises InstallerError: 解压失败（I/O 错误、归档损坏、7-Zip 未安装）
        或解压后未找到 makensis。
    """
    cache_dir = nsis_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".7z":
        _extract_7z(archive, cache_dir)
    else:
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(cache_dir)
        except (OSError, zipfile.BadZipFile) as e:
            raise InstallerError(f"NSIS 归档解压失败 {archive}: {e}") from e
    makensis = find_cached_makensis()
    if makensis is None:
        raise InstallerError(f"NSIS 归档解压后未找到 makensis: {archive}")
    return makensis


def _download_and_extract_nsis() -> Path:
    """按 :data:`NSIS_URLS` 顺序下载 NSIS zip 归档并解压，返回 makensis 路径.

    下载源逐一尝试（镜像不可达/证书问题时回退下一源），全部失败抛
    :class:`InstallerError`。下载的归档解压完成后删除（成功与失败路径
    均清理；缓存按 ``Bin/makensis.exe`` 存在判定命中，无需保留归档）。

    :raises InstallerError: 所有下载源均失败、或下载后解压失败。
    """
    from fspack.packaging.net import Downloader

    cache_dir = nsis_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / f"nsis-{NSIS_VERSION}.zip"
    last_error: Exception | None = None
    for url in NSIS_URLS:
        try:
            _logger.info("下载 NSIS %s: %s", NSIS_VERSION, url)
            Downloader(timeout=_NSIS_DOWNLOAD_TIMEOUT).download(url, archive, label="NSIS")
            return _extract_nsis_archive(archive)
        except (OSError, InstallerError) as e:
            last_error = e
            _logger.warning("NSIS 下载源失败，回退下一源 %s: %s", url, e)
        finally:
            # 下载临时归档清理（删除失败如杀软占用不中断流程）
            with contextlib.suppress(OSError):
                archive.unlink(missing_ok=True)
    raise InstallerError(f"NSIS 下载失败（已尝试 {len(NSIS_URLS)} 个源）: {last_error}")


def ensure_nsis() -> str:
    """定位 makensis 可执行命令，必要时填充缓存，返回命令首参数.

    仅 Windows 做缓存管理，查找顺序：

    1. fspack 缓存 ``<cache_root>/nsis/<dir>/Bin/makensis.exe`` 已存在
       → 缓存命中
    2. 缓存目录下存在锁定版本的归档（``.zip``/``.7z``，用户手动放置或
       上次下载中断的残留）→ 解压替代下载（**归档保留不删**；纯本地
       操作，离线同样适用）。``.7z`` 需系统 7-Zip：未装时跳过本层
       回退后续路径（离线且无其他来源时 raise）。归档损坏时删除后
       回退后续路径
    3. PATH 中已有 makensis（系统安装，如 choco install nsis）→ 直接
       使用，不强制下载填充缓存
    4. 离线模式 → raise :class:`InstallerError`（fail-fast）
    5. 在线 → 下载官方 zip 解压（tauri GitHub 镜像优先，SourceForge
       兜底）

    非 Windows 平台（Linux 交叉打包）直接返回 ``"makensis"``
    （PATH 查找由 :func:`fspack.packaging.installer.base._run_tool`
    的 FileNotFoundError 处理兜底）。

    :return: makensis 命令（缓存命中为绝对路径字符串，PATH 为
        ``"makensis"``）。
    :raises InstallerError: 离线模式无缓存/归档/PATH 来源、本地 ``.7z``
        离线且未装 7-Zip、或下载解压失败。
    """
    if sys.platform != "win32":
        return "makensis"

    # 1. 缓存命中
    cached = find_cached_makensis()
    if cached is not None:
        _logger.info("NSIS 已就绪（缓存命中 %s）", cached)
        return str(cached)

    # 2. 本地归档解压：识别缓存目录下用户手动放置（或下载中断残留）的
    #    .zip/.7z 归档，解压替代下载；纯本地操作，离线模式同样适用
    local_archive = _find_local_nsis_archive()
    if local_archive is not None:
        if local_archive.suffix == ".7z" and _find_7z() is None:
            # .7z 需系统 7-Zip：跳过本层，回退 PATH/下载（离线且无
            # 其他来源时由后续 raise 给出完整提示）
            _logger.warning("未安装 7-Zip，本地 .7z 归档无法解压，回退后续来源: %s", local_archive)
        else:
            _logger.info("从本地归档解压 NSIS: %s", local_archive)
            try:
                return str(_extract_nsis_archive(local_archive))
            except InstallerError:
                # 归档损坏（如下载中断的半成品）：删除后回退后续路径；
                # PATH/下载仍可能就绪，不在此处终结
                _logger.warning("本地 NSIS 归档损坏，删除后回退后续来源: %s", local_archive)
                with contextlib.suppress(OSError):
                    local_archive.unlink()

    # 3. PATH 中已装 makensis（系统安装优先于下载，避免重复占用）
    if shutil.which("makensis") is not None:
        _logger.info("NSIS 使用 PATH 中的 makensis（系统安装）")
        return "makensis"

    # 4. 离线模式 fail-fast：无法下载 NSIS
    if is_offline():
        # .7z 归档因未装 7-Zip 被跳过时补充安装提示（否则用户困惑
        # "明明放了归档却报无可识别归档"）
        sevenzip_hint = (
            f"。检测到本地 .7z 归档但无法解压: {_SEVENZIP_REQUIRED_MSG}"
            if local_archive is not None and local_archive.suffix == ".7z" and _find_7z() is None
            else ""
        )
        raise InstallerError(
            f"离线模式下 NSIS 未就绪：无缓存、无可解压归档、PATH 无 makensis。"
            f"请将 NSIS 归档（{_NSIS_ARCHIVE_HINT}）放入 {nsis_cache_dir()}，"
            "或在联网机器执行一次安装包构建填充缓存后拷贝，"
            "或安装 NSIS 到 PATH（choco install nsis），或取消 FSPACK_OFFLINE 环境变量"
            f"{sevenzip_hint}"
        )

    # 5. 在线下载
    return str(_download_and_extract_nsis())
