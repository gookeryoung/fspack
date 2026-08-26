"""``fsp doctor`` 缓存内容盘点：压缩包类缓存的版本清单诊断项.

离线模式（``FSPACK_OFFLINE=1``）下所有下载阶段跳过网络，仅使用本地缓存，
缓存缺失即构建失败且报错点分散在各阶段。本模块为 ``fsp doctor`` 提供各
压缩包缓存的**内容盘点**（版本清单 + 总体积），用户可对照目标 Python
版本确认缓存是否就绪，离线问题前置排查：

- ``embed``：Windows embed python zip（``python-<version>-embed-amd64.zip``）
- ``standalone``：Linux/macOS python-build-standalone tarball
- ``standalone-windows``：Windows standalone tarball（Nuitka 构建 python 与
  tkinter 提取的共享源，见 :mod:`fspack.packaging.builtin`）
- ``nuitka``：Nuitka 解压后的构建用 python（按 py_version 分目录，含 ``t``
  后缀 free-threaded 版本）；wheels 目录下的 nuitka sdist 归档
  （``nuitka-<ver>.tar.gz``，构建安装 nuitka 包时免下载）一并盘点
- ``tkinter``：tkinter 组件 zip（``tkinter-<version>.zip``）
- ``winlibs``：Nuitka winlibs-mingw gcc 工具链（Windows，按 specificity 分目录）

与 :mod:`fspack.doctor.cache_health` 的职责边界：本模块仅盘点**有什么**
（只读目录枚举，不做完整性校验）；损坏/stale 检测归
``fsp doctor --check-cache`` / ``fsp cache status``。

状态规则：有内容即 OK（详情列版本清单）；空缓存在线为 OK（首次打包自动
下载），离线为 WARN（无法下载，建议预填充）；目录扫描异常为 WARN。
winlibs 额外区分：检测到 MSVC 时未缓存亦为 OK（scons 优先用 MSVC，
无需 winlibs）；本地归档与锁定 Nuitka 版本精确匹配的 ``.zip``/``.7z``
才算"待解压"（``.7z`` 需系统 7-Zip 解压），版本不符的归档在详情中单独
提示，避免"放了归档却显示未缓存"的困惑。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from fspack.config import _ver_key
from fspack.config.cache import (
    cache_root,
    embed_cache_dir,
    is_offline,
    nuitka_cache_dir,
    nuitka_winlibs_cache_dir,
    standalone_cache_dir,
    tkinter_cache_dir,
    wheel_cache_dir,
)
from fspack.doctor.cache_health import (
    _EMBED_ZIP_RE,
    _STANDALONE_TAR_RE,
    _TKINTER_ZIP_RE,
)
from fspack.doctor.envs import _format_size
from fspack.doctor.models import CheckResult, CheckStatus
from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
from fspack.platform import Platform

__all__ = [
    "_cache_content_fns",
    "_check_embed_contents",
    "_check_nuitka_contents",
    "_check_standalone_contents",
    "_check_standalone_windows_contents",
    "_check_tkinter_contents",
    "_check_winlibs_contents",
]

# nuitka 构建用 python 的版本目录名：``<py_version>``（如 ``3.11.15``/
# ``3.13.14t``，free-threaded 版本号带 t 后缀，见
# NuitkaStandalone._build_python_cache_dir）
_NUITKA_DIR_RE = re.compile(r"^\d+\.\d+\.\d+t?$")

# nuitka sdist 归档名：``nuitka-<ver>.tar.gz``（PyPI 官方大写 ``Nuitka-``，
# 部分镜像规范化为小写，大小写不敏感；与 env.py `_find_local_nuitka_sdist`
# 的识别口径一致）
_NUITKA_SDIST_RE = re.compile(r"^nuitka-(\d+(?:\.\d+)+)\.tar\.gz$", re.IGNORECASE)

# 版本清单预览上限（超出追加"等 N 个"，表格详情列不至于过长）
_VERSION_PREVIEW_LIMIT = 5


def _match_files(cache_dir: Path, pattern: re.Pattern[str]) -> list[tuple[str, int]]:
    """列出目录下文件名匹配正则的 ``(版本, 字节数)``，目录缺失返回空列表.

    :raises OSError: 目录枚举失败（权限/磁盘 I/O），由调用方转为 WARN 诊断项。
    """
    matched: list[tuple[str, int]] = []
    if not cache_dir.is_dir():
        return matched
    for path in sorted(cache_dir.iterdir()):
        if not path.is_file():
            continue
        m = pattern.match(path.name)
        if m is None:
            continue  # 非预期文件名（README 等），跳过
        try:
            matched.append((m.group(1), path.stat().st_size))
        except OSError:
            continue  # 枚举后被删除的竞态：不计入
    return matched


def _preview_versions(versions: list[str]) -> str:
    """版本清单预览：前若干个顿号连接，超出追加"等 N 个"."""
    preview = "、".join(versions[:_VERSION_PREVIEW_LIMIT])
    if len(versions) > _VERSION_PREVIEW_LIMIT:
        preview += f" 等 {len(versions)} 个"
    return preview


def _empty_cache_result(name: str, cache_dir: Path) -> CheckResult:
    """空缓存诊断项：离线 WARN（无法下载需预填充），在线 OK（按需下载）."""
    if is_offline():
        return CheckResult(
            name=name,
            status=CheckStatus.WARN,
            detail="未缓存",
            suggestion=f"离线模式无法下载：请在联网机器执行一次打包填充缓存后拷贝到 {cache_dir}，或手动放置归档",
        )
    return CheckResult(name=name, status=CheckStatus.OK, detail="未缓存（首次打包自动下载）")


def _scan_error_result(name: str, cache_dir: Path, exc: OSError) -> CheckResult:
    """目录扫描异常诊断项：WARN（不影响打包，仅诊断信息缺失）."""
    return CheckResult(
        name=name,
        status=CheckStatus.WARN,
        detail=str(cache_dir),
        suggestion=f"扫描缓存目录失败: {exc}（不影响打包，仅诊断信息缺失）",
    )


def _check_archive_inventory(name: str, cache_dir: Path, pattern: re.Pattern[str]) -> CheckResult:
    """通用归档缓存盘点：版本清单 + 归档总体积.

    :param name: 诊断项名称（如 ``"embed 缓存"``）
    :param cache_dir: 缓存目录
    :param pattern: 归档文件名正则（group(1) 为版本号）
    """
    try:
        entries = _match_files(cache_dir, pattern)
    except OSError as exc:
        return _scan_error_result(name, cache_dir, exc)
    if not entries:
        return _empty_cache_result(name, cache_dir)
    versions = sorted((v for v, _ in entries), key=_ver_key)
    total = sum(size for _, size in entries)
    detail = f"已缓存 {len(versions)} 个：{_preview_versions(versions)}（共 {_format_size(total)}）"
    return CheckResult(name=name, status=CheckStatus.OK, detail=detail)


def _check_embed_contents() -> CheckResult:
    """盘点 embed python zip 缓存（Windows embed 运行时源）."""
    return _check_archive_inventory("embed 缓存", embed_cache_dir(), _EMBED_ZIP_RE)


def _check_standalone_contents() -> CheckResult:
    """盘点 python-build-standalone tarball 缓存（Linux/macOS 运行时源）."""
    return _check_archive_inventory("standalone 缓存", standalone_cache_dir(), _STANDALONE_TAR_RE)


def _check_standalone_windows_contents() -> CheckResult:
    """盘点 Windows standalone tarball 缓存（Nuitka 构建 python 与 tkinter 共享源）.

    目录名与 :mod:`fspack.packaging.builtin` / ``packaging.nuitka.standalone``
    的共享缓存约定一致（``<cache_root>/standalone-windows``），构建侧按
    ``cache_dir`` 参数拼接、无独立目录函数，此处经 :func:`cache_root` 拼接。
    """
    return _check_archive_inventory("standalone-windows 缓存", cache_root() / "standalone-windows", _STANDALONE_TAR_RE)


def _check_tkinter_contents() -> CheckResult:
    """盘点 tkinter 组件 zip 缓存（embed 运行时 tkinter 补充包）."""
    return _check_archive_inventory("tkinter 缓存", tkinter_cache_dir(), _TKINTER_ZIP_RE)


def _check_nuitka_contents() -> CheckResult:
    """盘点 Nuitka 构建用 python 缓存（按 py_version 解压的目录树）与 wheels 下 sdist.

    两处缓存均影响"首次构建是否需要下载"：

    - ``<cache_root>/nuitka`` 解压目录树：解压含数千文件，walk 统计体积成本高
      （Windows 杀软逐文件扫描），仅列版本不统计体积；残留 tarball（上次
      解压中断）单独计数提示
    - ``<cache_root>/wheels`` 下 nuitka sdist 归档（``nuitka-<ver>.tar.gz``，
      大小写不敏感、递归子目录）：构建安装 nuitka 包时经
      ``NuitkaEnv._find_local_nuitka_sdist`` 识别本地安装免下载，离线同样
      可用——盘点不可见会造成"放了 sdist 却显示未缓存"的困惑
    """
    cache_dir = nuitka_cache_dir()
    try:
        versions = _nuitka_versions(cache_dir)
        residual = _nuitka_residual_tarballs(cache_dir)
        sdists = _nuitka_sdist_versions(wheel_cache_dir())
    except OSError as exc:
        return _scan_error_result("nuitka 缓存", cache_dir, exc)
    if not versions and not residual and not sdists:
        return _empty_cache_result("nuitka 缓存", cache_dir)
    detail = f"已解压 {len(versions)} 个版本：{_preview_versions(versions)}" if versions else "无已解压版本"
    if sdists:
        detail += f"；sdist 已缓存 {len(sdists)} 个（{_preview_versions(sdists)}，构建安装免下载）"
    if residual:
        detail += f"；残留 tarball {len(residual)} 个（上次解压中断，`fsp cache clean --target nuitka` 可清理）"
    return CheckResult(name="nuitka 缓存", status=CheckStatus.OK, detail=detail)


def _nuitka_sdist_versions(wheels_dir: Path) -> list[str]:
    """递归列出 wheels 缓存下的 nuitka sdist 归档版本（大小写不敏感）.

    与 ``NuitkaEnv._find_local_nuitka_sdist`` 的识别范围一致（缓存根与任意
    子目录）；此处盘点全部版本（按版本去重），是否命中锁定版本由构建侧
    精确匹配文件名决定。

    :raises OSError: 目录遍历失败。
    """
    if not wheels_dir.is_dir():
        return []
    found: set[str] = set()
    for path in wheels_dir.rglob("*.tar.gz"):
        m = _NUITKA_SDIST_RE.match(path.name)
        if m is not None and path.is_file():
            found.add(m.group(1))
    return sorted(found, key=_ver_key)


def _nuitka_versions(cache_dir: Path) -> list[str]:
    """列出 nuitka 缓存下匹配版本正则的子目录名（含 t 后缀变体）.

    :raises OSError: 目录枚举失败。
    """
    if not cache_dir.is_dir():
        return []
    return sorted(entry.name for entry in cache_dir.iterdir() if entry.is_dir() and _NUITKA_DIR_RE.match(entry.name))


def _nuitka_residual_tarballs(cache_dir: Path) -> list[str]:
    """列出 nuitka 缓存下解压中断残留的 standalone tarball 文件名.

    :raises OSError: 目录枚举失败。
    """
    if not cache_dir.is_dir():
        return []
    return sorted(
        entry.name for entry in cache_dir.iterdir() if entry.is_file() and _STANDALONE_TAR_RE.match(entry.name)
    )


def _check_winlibs_contents() -> CheckResult:
    """盘点 winlibs-mingw 工具链缓存（Windows Nuitka 编译器后端）.

    目录结构（与 Nuitka ``getCachedDownload`` 约定一致）：
    ``gcc/x86_64/<specificity>/mingw64/bin/gcc.exe`` 为缓存命中标志；缓存根
    或子目录下与锁定 Nuitka 版本精确匹配的 ``winlibs-*.zip``/``*.7z`` 归档
    可被构建流程识别解压（纯本地操作，离线同样适用；``.7z`` 需系统 7-Zip）。

    本地归档区分两类：

    - **匹配**：文件名与 :data:`WINLIBS_URLS` 锁定版本精确一致（``.zip``
      或同内容 ``.7z`` 变体；构建侧 ``_find_local_winlibs_archive`` 按完整
      文件名匹配，防 ABI 不兼容的 gcc 被误用）→ "待解压" OK
    - **不匹配**：版本不符（如更新的 release）→ 不算可用缓存，在详情中
      单独提示所需的确切归档名，避免"放了归档却显示未缓存"的困惑

    未缓存时结合 :func:`fspack.packaging.nuitka.winlibs.msvc_available` 判定：
    装了 MSVC 的机器 scons 优先用 MSVC，无需 winlibs（OK）；否则在线 OK
    （首次构建自动下载，约 200 MiB），离线 WARN。
    """
    cache_dir = nuitka_winlibs_cache_dir()
    name = "winlibs 工具链"
    try:
        specificities = _winlibs_gcc_specificities(cache_dir)
        archives = _winlibs_local_archives(cache_dir)
    except OSError as exc:
        return _scan_error_result(name, cache_dir, exc)

    if specificities:
        return CheckResult(
            name=name,
            status=CheckStatus.OK,
            detail=f"gcc 已就绪（{_preview_versions(specificities)}）",
        )

    expected = _winlibs_expected_archive_names()
    matched = [a for a in archives if a in expected]
    if matched:
        # 本地归档待解压：下次构建自动识别解压，离线模式同样可用
        return CheckResult(
            name=name,
            status=CheckStatus.OK,
            detail=f"本地归档 {len(matched)} 个待解压（首次构建自动解压）",
        )

    # 未缓存：不匹配的本地归档（版本不符）追加提示
    mismatched = [a for a in archives if a not in expected]
    suffix = f"；另有 {len(mismatched)} 个本地归档不被识别" if mismatched else ""
    suggestion = (
        f"本地归档 {_preview_versions(mismatched)} 不会被构建使用（版本不匹配："
        f".zip/.7z 格式均可，但须与锁定 Nuitka 版本对应的归档名完全一致）："
        f" {_preview_versions(expected)}"
        if mismatched
        else ""
    )
    if _msvc_available():
        return CheckResult(
            name=name,
            status=CheckStatus.OK,
            detail=f"未缓存（检测到 MSVC，编译器优先用 MSVC）{suffix}",
            suggestion=suggestion,
        )
    if is_offline():
        return CheckResult(
            name=name,
            status=CheckStatus.WARN,
            detail=f"未缓存{suffix}",
            suggestion=(
                f"离线模式无法下载 winlibs gcc：请在联网机器执行一次 Nuitka 构建填充缓存后拷贝，"
                f"或将 winlibs zip 归档放入 {cache_dir}（构建时自动识别解压）"
                + (f"。{suggestion}" if suggestion else "")
            ),
        )
    return CheckResult(
        name=name,
        status=CheckStatus.OK,
        detail=f"未缓存（首次 Nuitka 构建自动下载，约 200 MiB）{suffix}",
        suggestion=suggestion,
    )


def _winlibs_gcc_specificities(cache_dir: Path) -> list[str]:
    """列出已解压 winlibs gcc 的 specificity 目录名（gcc.exe 就位的目录）.

    :raises OSError: 目录枚举失败。
    """
    gcc_root = cache_dir / "gcc" / "x86_64"
    if not gcc_root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in gcc_root.iterdir()
        if entry.is_dir() and (entry / "mingw64" / "bin" / "gcc.exe").is_file()
    )


def _winlibs_local_archives(cache_dir: Path) -> list[str]:
    """递归列出缓存目录下用户手动放置的 winlibs 归档文件名（.zip/.7z）.

    扫描范围与 ``NuitkaWinlibs._find_local_winlibs_archive`` 一致（缓存根与
    任意子目录），但不校验版本匹配——用户常放置其他版本的 winlibs 归档，
    盘点须全部可见；是否可用（匹配判定）由调用方结合
    :func:`_winlibs_expected_archive_names` 区分。

    :raises OSError: 目录遍历失败。
    """
    if not cache_dir.is_dir():
        return []
    return sorted(
        path.name for path in cache_dir.rglob("winlibs-*") if path.is_file() and path.suffix.lower() in {".zip", ".7z"}
    )


def _winlibs_expected_archive_names() -> list[str]:
    """返回 :data:`WINLIBS_URLS` 各锁定版本对应的归档文件名（.zip 与 .7z 变体，去重排序）.

    构建侧按完整文件名精确匹配本地归档（防 ABI 不兼容的 gcc 被误用），
    该清单即"哪些归档会被自动识别解压"的判定依据。
    """
    names: set[str] = set()
    for url in WINLIBS_URLS.values():
        zip_name = url.rsplit("/", 1)[1]
        names.add(zip_name)
        names.add(zip_name[: -len(".zip")] + ".7z")
    return sorted(names)


def _msvc_available() -> bool:
    """探测 MSVC 可用性（函数体延迟导入保持测试可 patch）.

    :func:`winlibs.msvc_available` 带 ``lru_cache`` 进程内缓存（探测含
    vswhere 子进程开销），此处包一层仅为本模块测试 patch 提供稳定落点。
    """
    from fspack.packaging.nuitka import winlibs

    return winlibs.msvc_available()


def _cache_content_fns(platform: Platform) -> list[Callable[[], CheckResult]]:
    """按平台返回缓存内容盘点函数列表（供 ``run_doctor`` 线程池并行执行）.

    - Windows：embed（embed 运行时）/ standalone-windows（Nuitka 构建 python
      与 tkinter 源）/ nuitka / tkinter / winlibs
    - Linux/macOS：standalone（运行时源）/ nuitka
    """
    fns: list[Callable[[], CheckResult]] = [_check_nuitka_contents]
    if platform is Platform.WINDOWS:
        fns.extend(
            [
                _check_embed_contents,
                _check_standalone_windows_contents,
                _check_tkinter_contents,
                _check_winlibs_contents,
            ]
        )
    else:
        fns.append(_check_standalone_contents)
    return fns
