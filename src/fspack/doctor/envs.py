"""``fsp doctor`` 环境信息检查.

检查 Python 版本/路径、目标平台、fspack 版本、镜像源配置、缓存目录大小，
返回 :class:`fspack.doctor.models.CheckResult`。同时提供 :func:`_dir_size`
与 :func:`_format_size` 工具函数，供 :mod:`fspack.doctor.templates`/
:mod:`fspack.doctor.bench` 复用（递归目录大小与人类可读字节数格式化）。

iter-139 扩展：:func:`_scan_cache_health` 全面扫描 wheel 缓存目录健康状态
（损坏/stale/orphan），:func:`_clean_cache_issues` 提供清理能力，供
``fsp cache status``/``fsp cache clean`` 子命令复用。

iter-148 多 cache 类型扩展：新增 6 个扫描器覆盖 ``embed``/``standalone``/
``nuitka``/``loaders``/``ccache``/``tkinter`` 子目录的损坏与过期文件识别，
:func:`_scan_all_caches`/``_clean_all_caches`` 聚合分发，``fsp cache status``
默认扫描全部 cache 类型，``--target <name>`` 指定单类型，``--stale`` 启用
过期文件清理。
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from fspack import __version__
from fspack._util.format import format_size_bin
from fspack._util.fsutil import walk_dir_size
from fspack._util.jsoncache import load_json_dict
from fspack.doctor.models import CacheHealthReport, CheckResult, CheckStatus

if TYPE_CHECKING:
    from fspack.platform import Platform

_logger = logging.getLogger(__name__)

__all__ = [
    "_check_cache_dir",
    "_check_cache_integrity",
    "_check_fspack_version",
    "_check_mirror_config",
    "_check_platform_info",
    "_check_python",
    "_clean_all_caches",
    "_clean_cache_by_type",
    "_clean_cache_issues",
    "_dir_size",
    "_file_size",
    "_format_size",
    "_is_pe_file",
    "_is_tar_intact",
    "_is_zip_intact",
    "_scan_all_caches",
    "_scan_cache_by_type",
    "_scan_cache_health",
    "_scan_ccache_health",
    "_scan_embed_health",
    "_scan_loader_health",
    "_scan_nuitka_health",
    "_scan_standalone_health",
    "_scan_tkinter_health",
    "_try_unlink",
]


def _check_python() -> CheckResult:
    """检查当前 Python 解释器版本与路径."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return CheckResult(
        name="Python",
        status=CheckStatus.OK,
        detail=f"{version} ({sys.executable})",
    )


def _check_platform_info(platform: Platform) -> CheckResult:
    """检查目标平台标识."""
    return CheckResult(
        name="平台",
        status=CheckStatus.OK,
        detail=platform.value,
    )


def _check_fspack_version() -> CheckResult:
    """检查 fspack 自身版本."""
    return CheckResult(
        name="fspack",
        status=CheckStatus.OK,
        detail=__version__,
    )


def _check_mirror_config(default_mirror: str, mirrors: Mapping[str, object]) -> CheckResult:
    """检查镜像源配置：默认镜像名 + 可用镜像列表."""
    available = ", ".join(mirrors.keys())
    detail = f"默认={default_mirror}；可用={available}"
    return CheckResult(
        name="镜像源",
        status=CheckStatus.OK,
        detail=detail,
    )


def _check_cache_dir(cache_root: Path) -> CheckResult:
    """检查缓存目录：路径 + 总大小（递归扫描）.

    目录不存在视为 OK（首次使用尚未下载缓存），大小显示 0 B。
    """
    if not cache_root.exists():
        return CheckResult(
            name="缓存目录",
            status=CheckStatus.OK,
            detail=f"{cache_root}（尚未创建）",
        )
    try:
        size_bytes = _dir_size(cache_root)
    except OSError as exc:
        return CheckResult(
            name="缓存目录",
            status=CheckStatus.WARN,
            detail=f"{cache_root}",
            suggestion=f"扫描缓存目录失败: {exc}（不影响打包，仅诊断信息缺失）",
        )
    return CheckResult(
        name="缓存目录",
        status=CheckStatus.OK,
        detail=f"{cache_root}（{_format_size(size_bytes)}）",
    )


def _dir_size(path: Path) -> int:
    """递归计算目录总字节数（不含符号链接循环）.

    实现搬迁至 :func:`fspack._util.fsutil.walk_dir_size`，此处保留同名薄封装
    维持 ``fspack.doctor.envs._dir_size`` 引用兼容。
    """
    return walk_dir_size(path)


def _format_size(size_bytes: int) -> str:
    """字节数格式化为人类可读（如 ``"123.4 MiB"``）.

    实现搬迁至 :func:`fspack._util.format.format_size_bin`，此处保留同名薄封装
    维持 ``fspack.doctor.envs._format_size`` 引用兼容。
    """
    return format_size_bin(size_bytes)


def _check_cache_integrity(cache_dir: Path) -> CheckResult:
    """扫描 wheel 缓存目录，报告损坏/stale/orphan 概要并删除损坏文件.

    iter-128 引入：``fsp doctor --check-cache`` 调用。
    iter-139 扩展：复用 :func:`_scan_cache_health` 的扫描结果，详情中追加
    stale deps（引用缺失 wheel）与 orphan wheels（未被任何 deps 引用）计数。

    损坏文件（JSON 解析失败/结构非法）自动删除（与
    :func:`fspack.packaging.wheels.cache._load_deps_cache` 行为一致）；
    stale deps 与 orphan wheels 不在诊断阶段删除，仅提示用户用
    ``fsp cache clean`` 清理。

    Args:
        cache_dir: wheel 缓存目录（通常是 :func:`fspack.config.cache.wheel_cache_dir`）。

    :return: OK（无任何问题）/ WARN（有损坏/stale/orphan）
    """
    report = _scan_cache_health(cache_dir)

    if not cache_dir.is_dir():
        return CheckResult(
            name="缓存完整性",
            status=CheckStatus.OK,
            detail=f"{cache_dir}（缓存目录不存在）",
        )

    if report.total_deps_files == 0 and report.total_wheels == 0:
        return CheckResult(
            name="缓存完整性",
            status=CheckStatus.OK,
            detail="无依赖解析缓存文件与 wheel 文件",
        )

    parts: list[str] = []
    if report.total_deps_files > 0:
        valid_count = report.total_deps_files - len(report.corrupt_deps_files) - len(report.stale_deps_files)
        parts.append(f"{report.total_deps_files} 个 deps 缓存（{valid_count} 有效")
        if report.corrupt_deps_files:
            parts[-1] += f"，{len(report.corrupt_deps_files)} 损坏已删除"
        if report.stale_deps_files:
            parts[-1] += f"，{len(report.stale_deps_files)} stale 引用缺失 wheel"
        parts[-1] += "）"
    if report.total_wheels > 0:
        parts.append(f"{report.total_wheels} 个 wheel")
        if report.orphan_wheels:
            parts[-1] += f"（{len(report.orphan_wheels)} 孤儿，{_format_size(report.orphan_size_bytes)}）"

    detail = "扫描 " + "，".join(parts)

    if not report.has_issues:
        return CheckResult(name="缓存完整性", status=CheckStatus.OK, detail=detail)

    suggestion_parts: list[str] = []
    if report.corrupt_deps_files:
        suggestion_parts.append("损坏 deps 已自动删除")
    if report.stale_deps_files or report.orphan_wheels:
        suggestion_parts.append(
            f"运行 `fsp cache clean` 清理 {len(report.stale_deps_files)} stale deps + {len(report.orphan_wheels)} 孤儿 wheel"
        )
    return CheckResult(
        name="缓存完整性",
        status=CheckStatus.WARN,
        detail=detail,
        suggestion="；".join(suggestion_parts),
    )


def _scan_cache_health(cache_dir: Path) -> CacheHealthReport:
    """扫描 wheel 缓存目录健康状态，返回 :class:`CacheHealthReport`.

    iter-139 引入：``fsp doctor --check-cache``/``fsp cache status``/``fsp cache clean``
    共用的扫描入口，避免重复扫描。

    扫描规则：

    - ``.deps-*.json`` 文件：JSON 结构校验（根对象 dict、wheels 字段 list）。
      损坏文件立即删除（best-effort，删除失败不影响扫描继续），记录到
      ``corrupt_deps_files``。
    - 有效 deps 文件中 ``wheels`` 列表指向的 wheel 文件名聚合为 ``referenced`` 集合。
      若引用的 wheel 不在 cache_dir 中，该 deps 文件记入 ``stale_deps_files``，
      缺失的 wheel 名记入 ``missing_wheels``（不删除 deps 文件，由 ``fsp cache clean`` 处理）。
    - cache_dir 下的 ``*.whl`` 文件聚合为 ``existing`` 集合，未出现在任何 deps
      引用集合中的记入 ``orphan_wheels``，并累加 ``orphan_size_bytes``。

    ``OSError``（权限/磁盘 I/O）不计为损坏也不删除：可能是瞬时问题，与
    :func:`fspack.packaging.wheels.cache._load_deps_cache` 行为一致。

    Args:
        cache_dir: wheel 缓存目录。

    :return: :class:`CacheHealthReport`，cache_dir 不存在时返回空报告
        （total_deps_files/total_wheels 均为 0）。
    """
    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir)

    cache_files = sorted(cache_dir.glob(".deps-*.json"))
    corrupt_names: list[str] = []
    stale_names: list[str] = []
    missing_wheels: list[str] = []
    referenced: set[str] = set()

    for f in cache_files:
        data = load_json_dict(f)
        if data is None:
            # load_json_dict 对 JSON 非法/非 dict 根：损坏删除+返回 None；对 OSError：不删+返回 None
            if not f.exists():
                # 文件被删除 = 真正 JSON 损坏，计入 corrupt_names
                corrupt_names.append(f.name)
            # OSError 瞬时问题（仍存在）或缺文件：不计入 corrupt，跳过
            continue
        names = data.get("wheels", [])
        if not isinstance(names, list):
            # wheels 字段类型错误：属缓存损坏，按同策略删除并计入
            corrupt_names.append(f.name)
            with contextlib.suppress(OSError):
                f.unlink()
            continue

        # 有效 deps 文件：检查引用的 wheel 是否存在
        wheel_names = [n for n in names if isinstance(n, str)]
        referenced.update(wheel_names)
        missing = [n for n in wheel_names if not (cache_dir / n).is_file()]
        if missing:
            stale_names.append(f.name)
            missing_wheels.extend(missing)

    # 枚举现有 wheel 文件（仅顶层目录，与 _save_deps_cache 写入位置一致）
    existing_wheels = sorted(p.name for p in cache_dir.glob("*.whl"))
    existing_set = set(existing_wheels)
    orphan_names = sorted(existing_set - referenced)
    orphan_size = 0
    for name in orphan_names:
        try:
            orphan_size += (cache_dir / name).stat().st_size
        except OSError:
            # 文件在枚举后被删除（竞态）：不计入体积但仍视为孤儿
            continue

    return CacheHealthReport(
        cache_dir=cache_dir,
        total_deps_files=len(cache_files),
        corrupt_deps_files=tuple(corrupt_names),
        stale_deps_files=tuple(stale_names),
        missing_wheels=tuple(dict.fromkeys(missing_wheels)),
        orphan_wheels=tuple(orphan_names),
        total_wheels=len(existing_wheels),
        orphan_size_bytes=orphan_size,
    )


def _clean_cache_issues(cache_dir: Path, *, dry_run: bool = False) -> CacheHealthReport:
    """清理 wheel 缓存目录中的 stale deps 与 orphan wheels，返回清理后的扫描报告.

    iter-139 引入：``fsp cache clean`` 调用。

    清理规则：

    - 重新扫描（确保使用最新状态，避免清理期间被外部修改的文件误删）。
    - 删除 ``stale_deps_files``（引用缺失 wheel 的 ``.deps-*.json``）：deps 文件
      指向的 wheel 已不在 cache_dir，下次构建会重新解析依赖，删除安全。
    - 删除 ``orphan_wheels``（未被任何 deps 引用的 ``*.whl``）：可能来自历史
      项目已删除/依赖变更。``dry_run=True`` 时仅扫描不删除，输出待删除列表。

    损坏的 ``.deps-*.json`` 在 :func:`_scan_cache_health` 阶段已删除，本函数
    返回的报告中 ``corrupt_deps_files`` 通常为空（除非扫描后又新增损坏文件，
    极罕见，仍按报告原样返回）。

    删除失败 best-effort：单个文件 ``OSError`` 不阻断其他文件清理，仅 warning 日志。
    仍返回扫描报告（用户可看到实际删除了哪些、哪些失败）。

    Args:
        cache_dir: wheel 缓存目录。
        :param dry_run: True 时仅扫描不删除，用于 ``fsp cache clean --dry-run`` 预览。

    :return: 清理前的 :class:`CacheHealthReport`（含本次扫描发现的所有问题）。
        调用方可基于 ``corrupt_deps_files``/``stale_deps_files``/``orphan_wheels``
        字段统计本次清理量。
    """
    report = _scan_cache_health(cache_dir)

    if dry_run or not report.has_issues:
        return report

    # 删除 stale deps 文件（引用缺失 wheel，下次构建重新解析）
    for name in report.stale_deps_files:
        target = cache_dir / name
        try:
            target.unlink()
        except OSError as e:
            _logger.warning("清理 stale deps 文件失败: %s: %s", target, e)

    # 删除 orphan wheel 文件（未被任何 deps 引用）
    for name in report.orphan_wheels:
        target = cache_dir / name
        try:
            target.unlink()
        except OSError as e:
            _logger.warning("清理孤儿 wheel 文件失败: %s: %s", target, e)

    return report


# ---------------------------------------------------------------------------
# iter-148 多 cache 类型扫描器：embed / standalone / nuitka / loaders / ccache / tkinter
#
# 设计要点：
#
# - 统一返回 :class:`CacheHealthReport`，wheels 专用字段保留为默认空，
#   非 wheels cache 类型用通用字段 ``corrupt_files``/``stale_files``/``orphan_files``/
#   ``total_files``/``issues_size_bytes`` 描述文件级健康状态。
# - 损坏文件（zip/tar 结构非法、PE 头缺失、空文件）在扫描阶段 best-effort 删除
#   （与 wheels ``_scan_cache_health`` 行为一致）。
# - 过期文件（如版本不在 ``KNOWN_*_VERSIONS`` 的旧 embed zip）扫描期不删除，
#   由 ``_clean_*_issues(..., include_stale=True)`` 显式清理。
# - 非 wheels cache 类型无引用关系，不识别 orphan，``orphan_files`` 始终为空。
# ---------------------------------------------------------------------------


# embed zip 文件名：``python-<version>-embed-amd64.zip``
_EMBED_ZIP_RE = re.compile(r"^python-(\d+\.\d+\.\d+)-embed-amd64\.zip$")

# standalone tar.gz 文件名：``cpython-<version>+<tag>-<platform_triplet>-install_only.tar.gz``
# platform triplet 含多段 ``-``（如 ``x86_64-unknown-linux``/``x86_64-pc-windows-msvc``），
# 用 ``.+`` 贪婪匹配整个 triplet 后回溯定位 ``-install_only.tar.gz`` 后缀
_STANDALONE_TAR_RE = re.compile(r"^cpython-(\d+\.\d+\.\d+)\+[^-]+-.+-install_only\.tar\.gz$")

# tkinter zip 文件名：``tkinter-<version>.zip``
_TKINTER_ZIP_RE = re.compile(r"^tkinter-(\d+\.\d+\.\d+)\.zip$")

# nuitka 目录名：``<py_version>``（如 ``3.11.15``），与 ``_build_python_cache_dir`` 一致
_NUITKA_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

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


def _is_zip_intact(path: Path) -> bool:
    """检查 zip 文件完整性：``ZipFile`` 能否正常打开并读取中心目录.

    ``zipfile.BadZipFile``/``KeyError``/``OSError`` 视为损坏。打开后调
    ``testzip()`` 验证 CRC（小文件无影响，大文件耗时但准确）。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except (zipfile.BadZipFile, OSError, KeyError):
        return False


def _is_tar_intact(path: Path) -> bool:
    """检查 tar.gz 文件完整性：``tarfile.open`` 能否正常打开并读取成员表."""
    try:
        with tarfile.open(path, "r:gz") as tf:
            # getmembers 触发实际读取（仅读 header），不需要 extractall
            tf.getmembers()
            return True
    except (tarfile.TarError, OSError, EOFError):
        return False


def _is_pe_file(path: Path) -> bool:
    """检查文件是否为合法 PE（Windows 可执行）：MZ 头 + 非空.

    loader exe 缓存为 mingw/gcc 编译产物，文件头应为 ``MZ``（DOS header magic）。
    0 字节文件或缺少 MZ 头视为损坏（如磁盘写满导致截断、缓存写入被中断）。
    """
    try:
        with path.open("rb") as f:
            head = f.read(2)
        return head == _PE_MZ_MAGIC
    except OSError:
        return False


def _scan_embed_health(cache_dir: Path) -> CacheHealthReport:
    """扫描 embed python zip 缓存目录健康状态.

    embed 缓存目录 ``~/.fspack/cache/embed/`` 存放 Windows embed python zip
    （``python-<version>-embed-amd64.zip``）。扫描规则：

    - 损坏 zip（``BadZipFile``/CRC 校验失败）：扫描期删除，记入 ``corrupt_files``
    - 版本不在 :data:`fspack.config.KNOWN_EMBED_VERSIONS` 中的旧版本 zip：
      记入 ``stale_files``（用户可能保留多版本故不自动删，需 ``--stale`` 启用清理）
    - 非预期文件名（不匹配 ``_EMBED_ZIP_RE``）：跳过（不视为问题，可能是 README）

    :return: :class:`CacheHealthReport`，cache_dir 不存在时返回空报告
    """
    from fspack.config import KNOWN_EMBED_VERSIONS

    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir, cache_type="embed")

    all_files = sorted(p.name for p in cache_dir.iterdir() if p.is_file())
    corrupt: list[str] = []
    stale: list[str] = []
    issues_size = 0

    for name in all_files:
        match = _EMBED_ZIP_RE.match(name)
        if match is None:
            continue  # 非预期文件名（README 等），跳过
        version = match.group(1)
        path = cache_dir / name
        if not _is_zip_intact(path):
            corrupt.append(name)
            issues_size += _file_size(path)
            _try_unlink(path)
        elif version not in KNOWN_EMBED_VERSIONS.values():
            stale.append(name)
            issues_size += _file_size(path)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="embed",
        total_files=len(all_files),
        corrupt_files=tuple(corrupt),
        stale_files=tuple(stale),
        issues_size_bytes=issues_size,
    )


def _scan_standalone_health(cache_dir: Path) -> CacheHealthReport:
    """扫描 python-build-standalone tarball 缓存目录健康状态.

    standalone 缓存目录 ``~/.fspack/cache/standalone/`` 存放 Linux/macOS
    python-build-standalone tar.gz（``cpython-<version>+<tag>-<platform>-install_only.tar.gz``）。
    扫描规则与 :func:`_scan_embed_health` 类似：损坏 tar 删除+记 corrupt，
    版本不在 :data:`KNOWN_STANDALONE_VERSIONS` 的旧 tarball 记入 stale。
    """
    from fspack.config import KNOWN_STANDALONE_VERSIONS

    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir, cache_type="standalone")

    all_files = sorted(p.name for p in cache_dir.iterdir() if p.is_file())
    corrupt: list[str] = []
    stale: list[str] = []
    issues_size = 0

    for name in all_files:
        match = _STANDALONE_TAR_RE.match(name)
        if match is None:
            continue
        version = match.group(1)
        path = cache_dir / name
        if not _is_tar_intact(path):
            corrupt.append(name)
            issues_size += _file_size(path)
            _try_unlink(path)
        elif version not in KNOWN_STANDALONE_VERSIONS.values():
            stale.append(name)
            issues_size += _file_size(path)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="standalone",
        total_files=len(all_files),
        corrupt_files=tuple(corrupt),
        stale_files=tuple(stale),
        issues_size_bytes=issues_size,
    )


def _scan_nuitka_health(cache_dir: Path) -> CacheHealthReport:
    """扫描 Nuitka standalone python 缓存目录健康状态.

    nuitka 缓存目录 ``~/.fspack/cache/nuitka/`` 下按 py_version 分子目录
    （如 ``3.11.15/``），每个子目录解压后应含 ``python/python.exe``（Windows）
    或 ``python/bin/python<minor>``（Linux）。

    扫描规则：

    - 子目录名不匹配 ``_NUITKA_VERSION_RE``：跳过（可能是临时文件）
    - 解压目录缺关键 python 可执行：记入 ``corrupt_files``，扫描期删除整个子目录
    - 版本不在 :data:`KNOWN_STANDALONE_VERSIONS` 的旧版本子目录：记入 ``stale_files``
    - 残留 tarball（``cpython-*.tar.gz``）记入 ``corrupt_files`` 删除
      （Nuitka 解压后应已删 tarball，残留表示解压流程中断）
    """
    from fspack.config import KNOWN_STANDALONE_VERSIONS

    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir, cache_type="nuitka")

    all_entries = sorted(p.name for p in cache_dir.iterdir())
    corrupt: list[str] = []
    stale: list[str] = []
    issues_size = 0

    for name in all_entries:
        entry = cache_dir / name
        if entry.is_file() and _STANDALONE_TAR_RE.match(name):
            # 残留 tarball（Nuitka 解压后应已删，残留说明解压中断）
            corrupt.append(name)
            issues_size += _file_size(entry)
            _try_unlink(entry)
            continue

        if not entry.is_dir() or not _NUITKA_VERSION_RE.match(name):
            continue  # 非版本目录（可能是 README/.DS_Store）

        version = name
        # 检查 python 可执行存在性（Windows: python/python.exe，Linux: python/bin/pythonX.Y）
        major, minor = version.split(".")[:2]
        win_py = entry / "python" / "python.exe"
        linux_py = entry / "python" / "bin" / f"python{major}.{minor}"
        if not win_py.is_file() and not linux_py.is_file():
            corrupt.append(name)
            issues_size += _dir_size(entry)
            # best-effort 删除损坏的整个目录
            with contextlib.suppress(OSError):
                import shutil

                shutil.rmtree(entry, ignore_errors=True)
        elif version not in KNOWN_STANDALONE_VERSIONS.values():
            stale.append(name)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="nuitka",
        total_files=len(all_entries),
        corrupt_files=tuple(corrupt),
        stale_files=tuple(stale),
        issues_size_bytes=issues_size,
    )


def _scan_loader_health(cache_dir: Path) -> CacheHealthReport:
    """扫描 C loader 编译缓存目录健康状态.

    loader 缓存目录 ``~/.fspack/cache/loaders/`` 存放 mingw/gcc 编译的 exe
    （Windows ``<hash>.exe``，Linux/macOS ``<hash>``）。文件名为 sha256 hash
    前 16 字符，无版本信息。

    扫描规则：

    - 0 字节文件（编译中断残留）：记入 ``corrupt_files``，扫描期删除
    - 非 PE 文件（缺 MZ 头，Windows 路径下）：记入 ``corrupt_files``，扫描期删除
    - Linux/macOS 路径下不校验 PE 头（ELF/Mach-O 无 MZ magic），仅检查非空

    loader 文件名是 hash 无版本概念，不识别 ``stale_files``；无引用关系不识别
    ``orphan_files``（孤儿识别需要遍历所有可能的 cache_key，不可行）。
    """
    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir, cache_type="loaders")

    all_files = sorted(p.name for p in cache_dir.iterdir() if p.is_file())
    corrupt: list[str] = []
    issues_size = 0
    is_windows_target = sys.platform.startswith("win")

    for name in all_files:
        path = cache_dir / name
        size = _file_size(path)
        is_corrupt = False
        if size == 0:
            is_corrupt = True
        elif is_windows_target and name.endswith(".exe") and not _is_pe_file(path):
            # 仅 Windows 路径下 exe 校验 PE 头（Linux 路径下 Windows exe 也是 PE，
            # 但 mingw 交叉编译产物本应是 PE，跨平台校验保留 PE 头检查）
            is_corrupt = True
        elif is_windows_target and not name.endswith(".exe") and not _is_pe_file(path):
            # Windows 路径下非 exe 文件不应出现在 loader cache（可能是测试残留）
            # 不计为 corrupt（无法判断语义），仅跳过
            continue

        if is_corrupt:
            corrupt.append(name)
            issues_size += size
            _try_unlink(path)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="loaders",
        total_files=len(all_files),
        corrupt_files=tuple(corrupt),
        issues_size_bytes=issues_size,
    )


def _scan_ccache_health(cache_dir: Path) -> CacheHealthReport:
    """扫描 ccache 二进制缓存目录健康状态.

    ccache 缓存目录 ``~/.fspack/cache/ccache/`` 存放从 GitHub releases 下载的
    预编译 ccache 二进制（Windows ``ccache.exe``，Linux ``ccache``）与子目录
    残留（``ccache-<ver>-<platform>/``，旧版结构，由 :func:`NuitkaCcache._ensure_ccache`
    自动迁移到根目录）。

    扫描规则：

    - ccache 二进制缺失：记入 ``corrupt_files``（不可用），不删除（无文件可删）
    - 旧版子目录残留（``ccache-*/``）：记入 ``stale_files``，扫描期不删
      （可能正在被其他进程使用），由 ``--stale`` 显式清理
    - 损坏归档残留（``ccache.tar.xz``/``ccache.zip``）：记入 ``corrupt_files`` 删除
    """
    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir, cache_type="ccache")

    all_entries = sorted(p.name for p in cache_dir.iterdir())
    corrupt: list[str] = []
    stale: list[str] = []
    issues_size = 0

    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    ccache_exe = cache_dir / exe_name
    if not ccache_exe.is_file():
        # 二进制缺失：记入 corrupt（不影响打包，仅无加速），无文件可删
        corrupt.append(exe_name)

    for name in all_entries:
        entry = cache_dir / name
        if entry.is_file() and name in ("ccache.tar.xz", "ccache.zip"):
            # 下载归档残留（解压后应已删，残留说明解压中断）
            corrupt.append(name)
            issues_size += _file_size(entry)
            _try_unlink(entry)
        elif entry.is_dir() and name.startswith("ccache-"):
            # 旧版子目录残留（新版应已迁移到根目录）
            stale.append(name)
            issues_size += _dir_size(entry)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="ccache",
        total_files=len(all_entries),
        corrupt_files=tuple(corrupt),
        stale_files=tuple(stale),
        issues_size_bytes=issues_size,
    )


def _scan_tkinter_health(cache_dir: Path) -> CacheHealthReport:
    """扫描 tkinter 补充包缓存目录健康状态.

    tkinter 缓存目录 ``~/.fspack/cache/tkinter/`` 存放从 python-build-standalone
    Windows 构建提取的 tkinter 组件 zip（``tkinter-<version>.zip``）。

    扫描规则：

    - 损坏 zip：扫描期删除，记入 ``corrupt_files``
    - 版本不在 :data:`KNOWN_STANDALONE_VERSIONS` 的旧 zip：记入 ``stale_files``
    """
    from fspack.config import KNOWN_STANDALONE_VERSIONS

    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir, cache_type="tkinter")

    all_files = sorted(p.name for p in cache_dir.iterdir() if p.is_file())
    corrupt: list[str] = []
    stale: list[str] = []
    issues_size = 0

    for name in all_files:
        match = _TKINTER_ZIP_RE.match(name)
        if match is None:
            continue
        version = match.group(1)
        path = cache_dir / name
        if not _is_zip_intact(path):
            corrupt.append(name)
            issues_size += _file_size(path)
            _try_unlink(path)
        elif version not in KNOWN_STANDALONE_VERSIONS.values():
            stale.append(name)
            issues_size += _file_size(path)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="tkinter",
        total_files=len(all_files),
        corrupt_files=tuple(corrupt),
        stale_files=tuple(stale),
        issues_size_bytes=issues_size,
    )


# 各 cache 类型的扫描器与清理器分发注册表。
# 每项为 ``(scan_fn, clean_fn)`` 元组；wheels 用现有 _scan_cache_health/_clean_cache_issues。
_CACHE_TARGETS: tuple[tuple[str, str], ...] = (
    ("wheels", "wheels"),
    ("embed", "embed"),
    ("standalone", "standalone"),
    ("nuitka", "nuitka"),
    ("loaders", "loaders"),
    ("ccache", "ccache"),
    ("tkinter", "tkinter"),
)


def _scan_cache_by_type(cache_type: str) -> CacheHealthReport:
    """按 cache 类型分发到对应扫描器，返回 :class:`CacheHealthReport`.

    cache_type 不在已知列表中时抛 :class:`ValueError`。
    """
    from fspack.config.cache import (
        ccache_cache_dir,
        embed_cache_dir,
        loader_cache_dir,
        nuitka_cache_dir,
        standalone_cache_dir,
        tkinter_cache_dir,
        wheel_cache_dir,
    )

    # cache_type → (scanner_fn, cache_dir_fn) 分发表，避免多 if/return 分支
    dispatch: dict[str, tuple[Any, Any]] = {
        "wheels": (_scan_cache_health, wheel_cache_dir),
        "embed": (_scan_embed_health, embed_cache_dir),
        "standalone": (_scan_standalone_health, standalone_cache_dir),
        "nuitka": (_scan_nuitka_health, nuitka_cache_dir),
        "loaders": (_scan_loader_health, loader_cache_dir),
        "ccache": (_scan_ccache_health, ccache_cache_dir),
        "tkinter": (_scan_tkinter_health, tkinter_cache_dir),
    }
    entry = dispatch.get(cache_type)
    if entry is None:
        raise ValueError(f"未知 cache 类型: {cache_type}，可选: {', '.join(t for t, _ in _CACHE_TARGETS)}")
    scanner, dir_fn = entry
    return scanner(dir_fn())


def _scan_all_caches() -> tuple[CacheHealthReport, ...]:
    """扫描全部 cache 类型，返回报告元组（按注册表顺序）."""
    return tuple(_scan_cache_by_type(cache_type) for cache_type, _ in _CACHE_TARGETS)


def _clean_cache_by_type(
    cache_type: str,
    *,
    dry_run: bool = False,
    include_stale: bool = False,
) -> CacheHealthReport:
    """按 cache 类型分发到对应清理器.

    - ``dry_run=True``：仅扫描不删除（与 wheels 一致语义）
    - ``include_stale=True``：额外清理 ``stale_files``（旧版本 zip/tar/子目录），
      默认 ``False`` 仅清理损坏与 wheels 的 stale_deps/orphan_wheels

    wheels 类型仍委托给 :func:`_clean_cache_issues` 保持向后兼容（其 stale_deps
    与 orphan_wheels 默认清理，无需 ``include_stale`` 启用）。
    """
    if cache_type == "wheels":
        # wheels 的 stale_deps/orphan_wheels 始终清理（iter-139 既有行为）
        from fspack.config.cache import wheel_cache_dir

        return _clean_cache_issues(wheel_cache_dir(), dry_run=dry_run)

    # 非 wheels 类型：扫描后删除 corrupt（扫描期已删，这里再扫一次确认）+ 可选 stale
    report = _scan_cache_by_type(cache_type)
    if dry_run or not report.has_issues:
        return report
    if not include_stale and not report.corrupt_files:
        return report

    from fspack.config.cache import (
        ccache_cache_dir,
        embed_cache_dir,
        loader_cache_dir,
        nuitka_cache_dir,
        standalone_cache_dir,
        tkinter_cache_dir,
    )

    # 非 wheels 类型的 cache_dir 分发表，避免多 elif 分支
    dir_dispatch: dict[str, Any] = {
        "embed": embed_cache_dir,
        "standalone": standalone_cache_dir,
        "nuitka": nuitka_cache_dir,
        "loaders": loader_cache_dir,
        "ccache": ccache_cache_dir,
        "tkinter": tkinter_cache_dir,
    }
    dir_fn = dir_dispatch.get(cache_type)
    if dir_fn is None:
        return report  # 未知类型不应到达此分支（_scan_cache_by_type 已校验）
    cache_dir = dir_fn()

    # corrupt_files 已在扫描阶段删除，这里仅处理 stale_files（include_stale=True 时）
    if include_stale:
        import shutil

        for name in report.stale_files:
            target = cache_dir / name
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
            except OSError as e:
                _logger.warning("清理 stale 文件失败: %s: %s", target, e)

    # 重新扫描反映清理结果（corrupt 已删，stale 按 include_stale 处理后）
    return _scan_cache_by_type(cache_type) if include_stale else report


def _clean_all_caches(
    *,
    dry_run: bool = False,
    include_stale: bool = False,
) -> tuple[CacheHealthReport, ...]:
    """清理全部 cache 类型，返回清理后报告元组（按注册表顺序）."""
    return tuple(
        _clean_cache_by_type(cache_type, dry_run=dry_run, include_stale=include_stale)
        for cache_type, _ in _CACHE_TARGETS
    )
