"""缓存健康扫描与清理引擎（多 cache 类型）.

从 :mod:`fspack.doctor.envs` 拆出的缓存健康底层引擎，供命令层
(:mod:`fspack.doctor.cache` 的 ``fsp cache status``/``fsp cache clean``) 与
诊断项 (:func:`fspack.doctor.envs._check_cache_integrity`) 复用：

- wheels 扫描/清理：:func:`_scan_cache_health`（损坏/stale/orphan 三维识别，
  两遍法避免 O(deps×wheels) 次 stat）与 :func:`_clean_cache_issues`
- 6 类非 wheels 扫描器：embed/standalone/nuitka/loaders/ccache/tkinter，
  统一返回 :class:`fspack.doctor.models.CacheHealthReport`
- 分发注册表 :data:`_CACHE_TARGETS` 与聚合入口 :func:`_scan_all_caches`/
  :func:`_clean_all_caches`

设计要点：

- 损坏文件（zip/tar 结构非法、PE 头缺失、空文件）默认只报告不删除；扫描器
  仅在 ``delete_corrupt=True``（``_clean_cache_by_type`` 非 dry_run 清理路径
  传入）时才 best-effort 删除，status/dry-run 只读路径无删除副作用。
- 完整性三态检查委托 :mod:`fspack.doctor.integrity`，``None``（IO 异常无法
  判定）不计损坏也不删除，仅记 warning 日志。
- 过期文件（如版本不在 ``KNOWN_*_VERSIONS`` 的旧 embed zip）扫描期不删除，
  由 ``_clean_cache_by_type(..., include_stale=True)`` 显式清理。
- 非 wheels cache 类型无引用关系，不识别 orphan，``orphan_files`` 始终为空。
- 目录函数按名延迟解析（:func:`_cache_dir_by_attr` 调用时 getattr），保持
  测试 monkeypatch ``fspack.config.cache.*_cache_dir`` 动态生效。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from fspack._util.fsutil import walk_dir_size
from fspack.doctor.integrity import _file_size, _is_pe_file, _is_tar_intact, _is_zip_intact, _try_unlink
from fspack.doctor.models import CacheHealthReport

_logger = logging.getLogger(__name__)

__all__ = [
    "_cache_dir_by_attr",
    "_clean_all_caches",
    "_clean_cache_by_type",
    "_clean_cache_issues",
    "_parse_deps_entry",
    "_scan_all_caches",
    "_scan_cache_by_type",
    "_scan_cache_health",
    "_scan_ccache_health",
    "_scan_embed_health",
    "_scan_loader_health",
    "_scan_nuitka_health",
    "_scan_standalone_health",
    "_scan_tkinter_health",
]

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

# nuitka 解压进行中的宽限期（秒）：目录名匹配版本正则但暂无 python 可执行、
# 且目录 mtime 距今不足该值时，视为另一进程正在解压 standalone，跳过判定
# 不删除（避免并发竞态误删）。standalone tar.gz 解压耗时可达分钟级，取 10 分钟。
_NUITKA_EXTRACT_GRACE_SEC = 600.0


def _parse_deps_entry(f: Path, corrupt_names: list[str], *, delete_corrupt: bool) -> tuple[str, list[str]] | None:
    """解析单个 ``.deps-*.json`` 文件，返回 ``(文件名, wheel 名列表)``；损坏或跳过返回 ``None``.

    供 :func:`_scan_cache_health` 第一遍循环调用，判定规则与其 docstring 一致：

    - 读取失败：``FileNotFoundError``（glob 后被外部删除的竞态）静默跳过；
      其余 ``OSError``（权限/文件锁等瞬时 IO）warning 后跳过——均不计损坏
      也不删除，文件名不追加到 ``corrupt_names``
    - 内容损坏（非 UTF-8 字节、JSON 非法、根对象非 dict、wheels 字段非 list）：
      warning 后把 ``f.name`` 追加到 ``corrupt_names``，仅 ``delete_corrupt=True``
      （清理路径）时 best-effort 删除
    - 有效：返回 ``(f.name, wheels 中的字符串项列表)``（非字符串项过滤）

    :param f: deps 缓存文件路径
    :param corrupt_names: 损坏文件名收集列表（就地追加，调用方用于报告统计）
    :param delete_corrupt: True 时损坏文件 best-effort 删除
    :return: 有效条目 ``(文件名, wheel 名列表)``；损坏或跳过时 ``None``
    """
    try:
        raw = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        # glob 后被外部删除（竞态）：无内容可判，跳过
        return None
    except OSError as e:
        # 权限/文件锁等瞬时 IO 问题：不计损坏也不删除
        _logger.warning("读取 deps 缓存失败，跳过判定: %s: %s", f, e)
        return None
    except ValueError as e:
        # UnicodeDecodeError：文件含非法 UTF-8 字节，属内容损坏
        _logger.warning("deps 缓存损坏: %s: %s", f, e)
        corrupt_names.append(f.name)
        if delete_corrupt:
            _try_unlink(f)
        return None

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"根对象不是 dict: {type(data).__name__}")
    except ValueError as e:
        # JSON 非法或根对象非 dict：属内容损坏
        _logger.warning("deps 缓存损坏: %s: %s", f, e)
        corrupt_names.append(f.name)
        if delete_corrupt:
            _try_unlink(f)
        return None

    names = data.get("wheels", [])
    if not isinstance(names, list):
        # wheels 字段类型错误：属缓存损坏
        _logger.warning("deps 缓存 wheels 字段非 list: %s", f)
        corrupt_names.append(f.name)
        if delete_corrupt:
            _try_unlink(f)
        return None
    return f.name, [n for n in names if isinstance(n, str)]


def _scan_cache_health(cache_dir: Path, *, delete_corrupt: bool = False) -> CacheHealthReport:
    """扫描 wheel 缓存目录健康状态，返回 :class:`CacheHealthReport`.

    iter-139 引入：``fsp doctor --check-cache``/``fsp cache status``/``fsp cache clean``
    共用的扫描入口，避免重复扫描。

    扫描规则：

    - ``.deps-*.json`` 文件：JSON 结构校验（根对象 dict、wheels 字段 list）。
      损坏文件记录到 ``corrupt_deps_files``；仅 ``delete_corrupt=True``（清理路径）
      时 best-effort 删除（删除失败不影响扫描继续），默认只报告不删除，
      保证 status/dry-run 等只读路径无删除副作用。
    - 有效 deps 文件中 ``wheels`` 列表指向的 wheel 文件名聚合为 ``referenced`` 集合。
      若引用的 wheel 不在 cache_dir 中，该 deps 文件记入 ``stale_deps_files``，
      缺失的 wheel 名记入 ``missing_wheels``（不删除 deps 文件，由 ``fsp cache clean`` 处理）。
    - cache_dir 下的 ``*.whl`` 文件聚合为 ``existing`` 集合，未出现在任何 deps
      引用集合中的记入 ``orphan_wheels``，并累加 ``orphan_size_bytes``。
    - 引用检查采用两遍法：第一遍解析全部 deps 收集 referenced 集合，第二遍
      一次 glob 现有 wheel 求差集（orphan = existing - referenced，
      missing = referenced - existing），避免 O(deps×wheels) 次 stat。

    ``OSError``（权限/磁盘 I/O/文件锁）不计为损坏也不删除：可能是瞬时问题，
    与 :func:`fspack.packaging.wheels.cache._load_deps_cache` 行为一致。

    Args:
        cache_dir: wheel 缓存目录。
        :param delete_corrupt: True 时损坏的 ``.deps-*.json`` 扫描期 best-effort
            删除；默认 False 只报告（只读路径/预览用）。

    :return: :class:`CacheHealthReport`，cache_dir 不存在时返回空报告
        （total_deps_files/total_wheels 均为 0）。
    """
    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir)

    cache_files = sorted(cache_dir.glob(".deps-*.json"))
    corrupt_names: list[str] = []
    valid_deps: list[tuple[str, list[str]]] = []
    referenced: set[str] = set()

    # 第一遍：逐个解析 deps 文件（_parse_deps_entry 判定损坏并就地收集），
    # 聚合有效条目引用的 wheel 名
    for f in cache_files:
        parsed = _parse_deps_entry(f, corrupt_names, delete_corrupt=delete_corrupt)
        if parsed is None:
            continue
        valid_deps.append(parsed)
        referenced.update(parsed[1])

    # 第二遍：一次 glob 现有 wheel 求差集（仅顶层目录，与 _save_deps_cache 写入位置一致）
    existing_wheels = sorted(p.name for p in cache_dir.glob("*.whl"))
    existing_set = set(existing_wheels)
    missing_set = referenced - existing_set

    stale_names: list[str] = []
    missing_wheels: list[str] = []
    for deps_name, wheel_names in valid_deps:
        missing = [n for n in wheel_names if n in missing_set]
        if missing:
            stale_names.append(deps_name)
            missing_wheels.extend(missing)

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
      ``dry_run=False`` 时扫描带 ``delete_corrupt=True``，损坏的 ``.deps-*.json``
      在扫描阶段即删除；``dry_run=True`` 时扫描不删除任何文件（纯预览）。
    - 删除 ``stale_deps_files``（引用缺失 wheel 的 ``.deps-*.json``）：deps 文件
      指向的 wheel 已不在 cache_dir，下次构建会重新解析依赖，删除安全。
    - 删除 ``orphan_wheels``（未被任何 deps 引用的 ``*.whl``）：可能来自历史
      项目已删除/依赖变更。``dry_run=True`` 时仅扫描不删除，输出待删除列表。

    非 dry_run 时损坏的 ``.deps-*.json`` 在 :func:`_scan_cache_health` 阶段已删除，
    本函数返回的报告中 ``corrupt_deps_files`` 记录的是本次已删除的损坏文件
    （供调用方统计清理量）。

    删除失败 best-effort：单个文件 ``OSError`` 不阻断其他文件清理，仅 warning 日志。
    仍返回扫描报告（用户可看到实际删除了哪些、哪些失败）。

    Args:
        cache_dir: wheel 缓存目录。
        :param dry_run: True 时仅扫描不删除，用于 ``fsp cache clean --dry-run`` 预览。

    :return: 清理前的 :class:`CacheHealthReport`（含本次扫描发现的所有问题）。
        调用方可基于 ``corrupt_deps_files``/``stale_deps_files``/``orphan_wheels``
        字段统计本次清理量。
    """
    report = _scan_cache_health(cache_dir, delete_corrupt=not dry_run)

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


def _scan_embed_health(
    cache_dir: Path, *, delete_corrupt: bool = False, full_verify: bool = False
) -> CacheHealthReport:
    """扫描 embed python zip 缓存目录健康状态.

    embed 缓存目录 ``~/.fspack/cache/embed/`` 存放 Windows embed python zip
    （``python-<version>-embed-amd64.zip``）。扫描规则：

    - 损坏 zip（``BadZipFile``/CRC 校验失败）：记入 ``corrupt_files``，仅
      ``delete_corrupt=True``（清理路径）时扫描期删除
    - zip 完整性无法判定（IO 异常，:func:`fspack.doctor.integrity._is_zip_intact`
      返回 ``None``）：跳过判定不删不计，仅 warning 日志
    - 版本不在 :data:`fspack.config.KNOWN_EMBED_VERSIONS` 中的旧版本 zip：
      记入 ``stale_files``（用户可能保留多版本故不自动删，需 ``--stale`` 启用清理）
    - 非预期文件名（不匹配 ``_EMBED_ZIP_RE``）：跳过（不视为问题，可能是 README）

    :param full_verify: True 时 zip 完整性用全量 CRC 校验（``testzip``，慢但
        准确），False 时快检中心目录（默认，``fsp cache status --verify`` 启用全量）
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
        intact = _is_zip_intact(path, full=full_verify)
        if intact is False:
            corrupt.append(name)
            issues_size += _file_size(path)
            if delete_corrupt:
                _try_unlink(path)
        elif intact is None:
            # IO 异常（杀软占用/文件锁）无法判定完整性：不删不计，仅告警
            _logger.warning("zip 完整性无法判定（IO 异常），跳过: %s", path)
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


def _scan_standalone_health(cache_dir: Path, *, delete_corrupt: bool = False) -> CacheHealthReport:
    """扫描 python-build-standalone tarball 缓存目录健康状态.

    standalone 缓存目录 ``~/.fspack/cache/standalone/`` 存放 Linux/macOS
    python-build-standalone tar.gz（``cpython-<version>+<tag>-<platform>-install_only.tar.gz``）。
    扫描规则与 :func:`_scan_embed_health` 类似：损坏 tar 记 corrupt（仅
    ``delete_corrupt=True`` 时删除），完整性无法判定（IO 异常）跳过不删不计，
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
        intact = _is_tar_intact(path)
        if intact is False:
            corrupt.append(name)
            issues_size += _file_size(path)
            if delete_corrupt:
                _try_unlink(path)
        elif intact is None:
            # IO 异常（杀软占用/文件锁）无法判定完整性：不删不计，仅告警
            _logger.warning("tar 完整性无法判定（IO 异常），跳过: %s", path)
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


def _scan_nuitka_health(cache_dir: Path, *, delete_corrupt: bool = False) -> CacheHealthReport:
    """扫描 Nuitka standalone python 缓存目录健康状态.

    nuitka 缓存目录 ``~/.fspack/cache/nuitka/`` 下按 py_version 分子目录
    （如 ``3.11.15/``），每个子目录解压后应含 ``python/python.exe``（Windows）
    或 ``python/bin/python<minor>``（Linux）。

    扫描规则：

    - 子目录名不匹配 ``_NUITKA_VERSION_RE``：跳过（可能是临时文件）
    - 解压目录缺关键 python 可执行：记入 ``corrupt_files``，仅
      ``delete_corrupt=True``（清理路径）时删除整个子目录；目录 mtime 距今
      不足 :data:`_NUITKA_EXTRACT_GRACE_SEC` 秒视为另一进程解压进行中，
      跳过判定不删不计（并发竞态防护）
    - 版本不在 :data:`KNOWN_STANDALONE_VERSIONS` 的旧版本子目录：记入
      ``stale_files`` 并累计 ``issues_size_bytes``
    - 残留 tarball（``cpython-*.tar.gz``）记入 ``corrupt_files``
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
            if delete_corrupt:
                _try_unlink(entry)
            continue

        if not entry.is_dir() or not _NUITKA_VERSION_RE.match(name):
            continue  # 非版本目录（可能是 README/.DS_Store）

        version = name
        # 检查 python 可执行存在性（Windows: python/python.exe，Linux: python/bin/pythonX.Y[或 pythonX.Yt]）
        # free-threaded build 二进制名带 t 后缀（python3.13t）
        is_t = version.endswith("t")
        base = version[:-1] if is_t else version
        major, minor = base.split(".")[:2]
        suffix = "t" if is_t else ""
        win_py = entry / "python" / "python.exe"
        linux_py = entry / "python" / "bin" / f"python{major}.{minor}{suffix}"
        if not win_py.is_file() and not linux_py.is_file():
            # 并发竞态防护：目录名匹配版本正则但暂无 python 可执行，可能是
            # 另一进程正在解压 standalone（解压耗时可达分钟级）。目录 mtime
            # 距今不足宽限期视为解压进行中，跳过判定避免误删。
            try:
                extract_age = time.time() - entry.stat().st_mtime
            except OSError:
                extract_age = 0.0  # mtime 不可读时保守视为进行中，跳过不删
            if extract_age < _NUITKA_EXTRACT_GRACE_SEC:
                continue
            corrupt.append(name)
            issues_size += walk_dir_size(entry)
            if delete_corrupt:
                # best-effort 删除损坏的整个目录
                shutil.rmtree(entry, ignore_errors=True)
        elif version not in KNOWN_STANDALONE_VERSIONS.values():
            stale.append(name)
            issues_size += walk_dir_size(entry)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="nuitka",
        total_files=len(all_entries),
        corrupt_files=tuple(corrupt),
        stale_files=tuple(stale),
        issues_size_bytes=issues_size,
    )


def _scan_loader_health(cache_dir: Path, *, delete_corrupt: bool = False) -> CacheHealthReport:
    """扫描 C loader 编译缓存目录健康状态.

    loader 缓存目录 ``~/.fspack/cache/loaders/`` 存放 mingw/gcc 编译的 exe
    （Windows ``<hash>.exe``，Linux/macOS ``<hash>``）。文件名为 sha256 hash
    前 16 字符，无版本信息。

    扫描规则：

    - 0 字节文件（编译中断残留）：记入 ``corrupt_files``，仅
      ``delete_corrupt=True``（清理路径）时扫描期删除
    - 非 PE 的 exe 文件（缺 MZ 头）：记入 ``corrupt_files``；exe 为 mingw
      编译产物（含 Linux 交叉编译场景），任何平台下都应为 PE。PE 头无法
      判定（IO 异常，:func:`fspack.doctor.integrity._is_pe_file` 返回 ``None``）
      时跳过不删不计
    - Linux/macOS 路径下无扩展名文件为 ELF/Mach-O loader（无 MZ magic），仅检查非空

    loader 文件名是 hash 无版本概念，不识别 ``stale_files``；无引用关系不识别
    ``orphan_files``（孤儿识别需要遍历所有可能的 cache_key，不可行）。
    """
    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir, cache_type="loaders")

    all_files = sorted(p.name for p in cache_dir.iterdir() if p.is_file())
    corrupt: list[str] = []
    issues_size = 0

    for name in all_files:
        path = cache_dir / name
        size = _file_size(path)
        if size == 0:
            is_corrupt = True
        elif name.endswith(".exe"):
            # exe 为 mingw 编译产物（含 Linux 交叉编译场景），任何平台下都应含 MZ 头
            pe = _is_pe_file(path)
            if pe is None:
                # IO 异常（杀软占用/文件锁）无法判定 PE 头：不删不计，仅告警
                _logger.warning("loader PE 头无法判定（IO 异常），跳过: %s", path)
                continue
            is_corrupt = pe is False
        else:
            # Windows 路径下非 exe 文件不应出现在 loader cache（可能是测试残留），
            # 不计为 corrupt（无法判断语义）仅跳过；Linux/macOS 无扩展名 loader
            # （ELF/Mach-O）非空即健康
            continue

        if is_corrupt:
            corrupt.append(name)
            issues_size += size
            if delete_corrupt:
                _try_unlink(path)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="loaders",
        total_files=len(all_files),
        corrupt_files=tuple(corrupt),
        issues_size_bytes=issues_size,
    )


def _scan_ccache_health(cache_dir: Path, *, delete_corrupt: bool = False) -> CacheHealthReport:
    """扫描 ccache 二进制缓存目录健康状态.

    ccache 缓存目录 ``~/.fspack/cache/ccache/`` 存放从 GitHub releases 下载的
    预编译 ccache 二进制（Windows ``ccache.exe``，Linux ``ccache``）与子目录
    残留（``ccache-<ver>-<platform>/``，旧版结构，由 :func:`NuitkaCcache._ensure_ccache`
    自动迁移到根目录）。

    扫描规则：

    - ccache 二进制缺失：记入 ``missing_files``（与损坏分列：无文件可删，
      不计入 ``corrupt_files``，避免渲染"已删除"误导与清理统计虚增）
    - 旧版子目录残留（``ccache-*/``）：记入 ``stale_files``，扫描期不删
      （可能正在被其他进程使用），由 ``--stale`` 显式清理
    - 损坏归档残留（``ccache.tar.xz``/``ccache.zip``）：记入 ``corrupt_files``，
      仅 ``delete_corrupt=True``（清理路径）时删除
    """
    if not cache_dir.is_dir():
        return CacheHealthReport(cache_dir=cache_dir, cache_type="ccache")

    all_entries = sorted(p.name for p in cache_dir.iterdir())
    corrupt: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    issues_size = 0

    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    ccache_exe = cache_dir / exe_name
    if not ccache_exe.is_file():
        # 二进制缺失（不影响打包，仅无加速）：无文件可删，与损坏分列记入
        # missing_files，不计入 corrupt_files
        missing.append(exe_name)

    for name in all_entries:
        entry = cache_dir / name
        if entry.is_file() and name in ("ccache.tar.xz", "ccache.zip"):
            # 下载归档残留（解压后应已删，残留说明解压中断）
            corrupt.append(name)
            issues_size += _file_size(entry)
            if delete_corrupt:
                _try_unlink(entry)
        elif entry.is_dir() and name.startswith("ccache-"):
            # 旧版子目录残留（新版应已迁移到根目录）
            stale.append(name)
            issues_size += walk_dir_size(entry)

    return CacheHealthReport(
        cache_dir=cache_dir,
        cache_type="ccache",
        total_files=len(all_entries),
        corrupt_files=tuple(corrupt),
        stale_files=tuple(stale),
        missing_files=tuple(missing),
        issues_size_bytes=issues_size,
    )


def _scan_tkinter_health(
    cache_dir: Path, *, delete_corrupt: bool = False, full_verify: bool = False
) -> CacheHealthReport:
    """扫描 tkinter 补充包缓存目录健康状态.

    tkinter 缓存目录 ``~/.fspack/cache/tkinter/`` 存放从 python-build-standalone
    Windows 构建提取的 tkinter 组件 zip（``tkinter-<version>.zip``）。

    扫描规则：

    - 损坏 zip：记入 ``corrupt_files``，仅 ``delete_corrupt=True``（清理路径）
      时扫描期删除；完整性无法判定（IO 异常）跳过不删不计
    - 版本不在 :data:`KNOWN_STANDALONE_VERSIONS` 的旧 zip：记入 ``stale_files``

    :param full_verify: True 时 zip 完整性用全量 CRC 校验（``testzip``），
        False 时快检中心目录（默认）
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
        intact = _is_zip_intact(path, full=full_verify)
        if intact is False:
            corrupt.append(name)
            issues_size += _file_size(path)
            if delete_corrupt:
                _try_unlink(path)
        elif intact is None:
            # IO 异常（杀软占用/文件锁）无法判定完整性：不删不计，仅告警
            _logger.warning("zip 完整性无法判定（IO 异常），跳过: %s", path)
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


# 各 cache 类型的扫描器分发注册表（模块级常量，避免每次调用重建 dict 与重复 import）。
# 每项为 ``(cache_type, scanner_fn, dir_fn_name, supports_full_verify)`` 四元组：
# scanner 为本模块函数；dir_fn_name 为 ``fspack.config.cache`` 模块中目录函数的名字，
# 按名延迟解析（调用时 getattr），保持测试 monkeypatch ``fspack.config.cache.*_cache_dir``
# 能动态生效，故不做模块级 from-import 绑定；supports_full_verify 标记扫描器是否
# 接受 ``full_verify`` 参数（仅 zip 归档类扫描器实现快检/全量两级深度，其余类型
# 的完整性检查本身无 CRC 快慢之分，分发器按标记决定是否透传该参数）。
_CACHE_TARGETS: tuple[tuple[str, Callable[..., CacheHealthReport], str, bool], ...] = (
    ("wheels", _scan_cache_health, "wheel_cache_dir", False),
    ("embed", _scan_embed_health, "embed_cache_dir", True),
    ("standalone", _scan_standalone_health, "standalone_cache_dir", False),
    ("nuitka", _scan_nuitka_health, "nuitka_cache_dir", False),
    ("loaders", _scan_loader_health, "loader_cache_dir", False),
    ("ccache", _scan_ccache_health, "ccache_cache_dir", False),
    ("tkinter", _scan_tkinter_health, "tkinter_cache_dir", True),
)


def _cache_dir_by_attr(dir_fn_name: str) -> Path:
    """按函数名从 ``fspack.config.cache`` 解析缓存目录（延迟解析保持 monkeypatch 兼容）."""
    from fspack.config import cache as _cache_module

    dir_fn: Callable[[], Path] = getattr(_cache_module, dir_fn_name)
    return dir_fn()


def _scan_cache_by_type(
    cache_type: str, *, delete_corrupt: bool = False, full_verify: bool = False
) -> CacheHealthReport:
    """按 cache 类型分发到对应扫描器，返回 :class:`CacheHealthReport`.

    cache_type 不在已知列表中时抛 :class:`ValueError`。

    :param delete_corrupt: 透传给扫描器；True 时扫描期 best-effort 删除损坏文件
        （仅 ``_clean_cache_by_type`` 非 dry_run 清理路径传入），默认 False
        只报告不删除（status 等只读路径）。
    :param full_verify: True 时对支持全量校验的扫描器（embed/tkinter 的 zip）
        启用逐项 CRC 校验（慢但准确，``fsp cache status --verify``），默认
        False 快检中心目录；不支持该参数的扫描器忽略此开关。
    """
    entry = next((e for e in _CACHE_TARGETS if e[0] == cache_type), None)
    if entry is None:
        raise ValueError(f"未知 cache 类型: {cache_type}，可选: {', '.join(t for t, *_ in _CACHE_TARGETS)}")
    _, scanner, dir_fn_name, supports_full = entry
    cache_dir = _cache_dir_by_attr(dir_fn_name)
    if supports_full and full_verify:
        return scanner(cache_dir, delete_corrupt=delete_corrupt, full_verify=True)
    return scanner(cache_dir, delete_corrupt=delete_corrupt)


def _scan_all_caches(*, full_verify: bool = False) -> tuple[CacheHealthReport, ...]:
    """扫描全部 cache 类型，返回报告元组（按注册表顺序，只读不删除）.

    7 类缓存目录相互独立，用 :class:`~concurrent.futures.ThreadPoolExecutor`
    并行扫描：各扫描器为 I/O 密集（目录枚举/zip 中心目录读取/PE 头读取），
    stat/open 等待释放 GIL，线程是真并行。串行 ~0.8s（Windows 下杀软对每次
    open 实时扫描 ~15ms）并行后墙钟时间取决于最慢一类。``executor.map``
    保序，返回顺序与 ``_CACHE_TARGETS`` 注册表顺序一致。

    :param full_verify: True 时对 zip 归档类扫描器启用全量 CRC 校验，默认快检。
    """
    from concurrent.futures import ThreadPoolExecutor

    types = [cache_type for cache_type, *_ in _CACHE_TARGETS]
    with ThreadPoolExecutor(max_workers=min(len(types), 8)) as pool:
        reports = pool.map(lambda t: _scan_cache_by_type(t, full_verify=full_verify), types)
        return tuple(reports)


def _clean_cache_by_type(
    cache_type: str,
    *,
    dry_run: bool = False,
    include_stale: bool = False,
) -> CacheHealthReport:
    """按 cache 类型分发到对应清理器.

    - ``dry_run=True``：仅扫描不删除（扫描器带默认 ``delete_corrupt=False``，
      与 wheels 一致语义，预览结果与实际清理共享同一判定）
    - ``dry_run=False``：扫描带 ``delete_corrupt=True``，损坏文件在扫描阶段删除
    - ``include_stale=True``：额外清理 ``stale_files``（旧版本 zip/tar/子目录），
      默认 ``False`` 仅清理损坏与 wheels 的 stale_deps/orphan_wheels

    wheels 类型仍委托给 :func:`_clean_cache_issues` 保持向后兼容（其 stale_deps
    与 orphan_wheels 默认清理，无需 ``include_stale`` 启用）。
    """
    if cache_type == "wheels":
        # wheels 的 stale_deps/orphan_wheels 始终清理（iter-139 既有行为）
        return _clean_cache_issues(_cache_dir_by_attr("wheel_cache_dir"), dry_run=dry_run)

    # 非 wheels 类型：非 dry_run 时扫描带 delete_corrupt=True（损坏文件扫描期删除）
    report = _scan_cache_by_type(cache_type, delete_corrupt=not dry_run)
    if dry_run or not report.has_issues:
        return report
    if not include_stale and not report.corrupt_files:
        return report

    entry = next((e for e in _CACHE_TARGETS if e[0] == cache_type), None)
    if entry is None:
        return report  # 未知类型不应到达此分支（_scan_cache_by_type 已校验）
    _, _, dir_fn_name, _ = entry
    cache_dir = _cache_dir_by_attr(dir_fn_name)

    # corrupt_files 已在扫描阶段删除，这里仅处理 stale_files（include_stale=True 时）
    if include_stale:
        for name in report.stale_files:
            target = cache_dir / name
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
            except OSError as e:
                _logger.warning("清理 stale 文件失败: %s: %s", target, e)

        # 重新扫描反映清理结果（corrupt 已删，stale 已按 include_stale 处理）
        return _scan_cache_by_type(cache_type, delete_corrupt=True)
    return report


def _clean_all_caches(
    *,
    dry_run: bool = False,
    include_stale: bool = False,
) -> tuple[CacheHealthReport, ...]:
    """清理全部 cache 类型，返回清理后报告元组（按注册表顺序）."""
    return tuple(
        _clean_cache_by_type(cache_type, dry_run=dry_run, include_stale=include_stale)
        for cache_type, *_ in _CACHE_TARGETS
    )
