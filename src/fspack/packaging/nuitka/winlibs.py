"""Nuitka winlibs-mingw 工具链管理：缓存查找、下载、解压.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的 winlibs 管理 mixin，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

背景：Nuitka scons 在 Windows 上无条件拒绝外部 gcc（``CC`` 环境变量指向的
mingw gcc 会被打印 "Non downloaded winlibs-gcc ... ignored" 后忽略），
只信任自己下载缓存的 winlibs gcc。fspack 预填充该缓存并注入
``NUITKA_CACHE_DIR_DOWNLOADS`` 环境变量指向 fspack 缓存目录
（``<cache_root>/nuitka-winlibs-mingw``），使 Nuitka 缓存命中直接使用，
不重复下载、不打印拒绝提示。

另：Nuitka 4.1 起 py>=3.13 默认 fallback 到 zig 编译器，其编译的 .pyd 可能
损坏（returncode==0 但运行时访问违例 0xC0000005）。fspack 在 Windows 上
全版本强制 winlibs：py>=3.13 时编译命令追加 ``--experimental=force-mingw64``
（见 :mod:`fspack.packaging.nuitka.progress` 的 ``_compile_files``）。

缓存目录结构（与 Nuitka ``getCachedDownload`` 约定一致）::

    <cache_root>/nuitka-winlibs-mingw/
    ├── winlibs-*.zip                # 用户手动放置的归档（识别后解压，不删除）
    └── gcc/                          # basename(binary)="gcc.exe" 去扩展名
        └── x86_64/                   # is_arch_specific（target_arch）
            └── <specificity>/        # winlibs release URL 倒数第二段
                └── mingw64/
                    └── bin/
                        └── gcc.exe   # 缓存命中标志（Nuitka 检查此文件）

职责边界：

- winlibs URL 按 Nuitka 版本映射（与 Nuitka 源码 ``getCachedDownloadedMinGW64``
  同步维护，版本升级时须核对）
- 缓存命中检查、本地归档识别解压与下载解压
  （:meth:`NuitkaWinlibs.ensure_winlibs_mingw`）
- 离线模式缓存未命中（无 gcc.exe 且无本地归档）fail-fast

不涉及：编译环境变量注入（见 :mod:`fspack.packaging.nuitka.env` 的
``_build_compile_env``）、ccache 管理（见 :mod:`fspack.packaging.nuitka.ccache`）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.config import is_offline, nuitka_version_for
from fspack.config.cache import nuitka_winlibs_cache_dir
from fspack.config.versions import _split_t_suffix
from fspack.exceptions import NuitkaError
from fspack.platform import Platform
from fspack.progress import StageRecorder

if TYPE_CHECKING:
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")

# winlibs zip 下载超时（秒）：归档 ~200MB，慢网络需数分钟
_WINLIBS_DOWNLOAD_TIMEOUT = 1800

# Nuitka 版本 → winlibs x86_64 zip URL。
# 与 Nuitka 源码 nuitka/utils/Download.py 的 getCachedDownloadedMinGW64 同步维护：
# Nuitka 升级锁定的 winlibs URL 变化时须同步更新此映射（specificity 派生自 URL）。
WINLIBS_URLS: dict[str, str] = {
    "4.1.3": (
        "https://github.com/brechtsanders/winlibs_mingw/releases/download/"
        "15.2.0posix-13.0.0-msvcrt-r6/"
        "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-13.0.0-r6.zip"
    ),
    "2.5.1": (
        "https://github.com/brechtsanders/winlibs_mingw/releases/download/"
        "14.2.0posix-19.1.1-12.0.0-msvcrt-r2/"
        "winlibs-x86_64-posix-seh-gcc-14.2.0-llvm-19.1.1-mingw-w64msvcrt-12.0.0-r2.zip"
    ),
}


def uses_winlibs(py_version: str) -> bool:
    """判断该 Python 版本在 Windows 上是否由 Nuitka fallback 到 winlibs gcc.

    Nuitka scons 在 Windows 上无有效 ``CC``/MSVC 时的编译器 fallback：

    - py<3.13 → winlibs gcc（可由 :meth:`NuitkaWinlibs.ensure_winlibs_mingw`
      预填充 fspack 缓存，scons 缓存命中不下载）
    - py>=3.13 → zig（``--assume-yes-for-downloads`` 自动下载，无需预填充；
      Nuitka 4.1 起官方默认 mingw 构建不再支持新 Python 版本）

    free-threaded 版本（``t`` 后缀）剥后缀后按同规则判断。
    """
    base, _ = _split_t_suffix(py_version)
    major, minor = base.split(".")[:2]
    return (int(major), int(minor)) < (3, 13)


# vswhere 探测超时（秒）：正常 <1s，留余量兜底冷启动/杀软扫描
_VSWHERE_TIMEOUT = 30.0


@lru_cache(maxsize=1)
def msvc_available() -> bool:
    """探测构建机是否装了 Visual Studio C++ 编译工具（MSVC）.

    Nuitka scons 在 Windows 上的编译器选择优先级：MSVC（VS2022）> 自下载
    winlibs gcc > zig fallback。装了 MSVC 时 scons 直接用 MSVC，winlibs
    预填充与 ``--experimental=force-mingw64`` 均无必要（预填充 200MB 纯浪费，
    force flag 反而把 MSVC 顶掉退回 winlibs）。

    探测顺序：

    1. ``vswhere.exe``（随 VS2017+ Installer 必装）：查最新含 C++ 工具集
       （``Microsoft.VisualStudio.Component.VC.Tools.x86.x64``）的 VS 实例
    2. vswhere 不存在（无 VS2017+ 的机器）：fallback 查 ``cl.exe`` 在 PATH
       （罕见：仅开发者手动配置过 VS 环境时命中）

    结果进程内缓存（:func:`functools.lru_cache`）：探测含 subprocess 开销
    （~100ms），编译命令构造与 ensure_env 各调一次，无必要重复探测。

    漏报安全（实际有 MSVC 但探测为无）：走 winlibs 预填充 + force flag，
    产物有效仅多 200MB 下载；误报安全（实际无 MSVC 但探测为有）：scons
    找不到 MSVC 会 fallback 到 winlibs/zig——py>=3.13 误报时有 zig 损坏
    风险，但 vswhere 输出非空即真装了 VS，误报概率可忽略。
    """
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
    vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        try:
            result = subprocess.run(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property",
                    "installationPath",
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_VSWHERE_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())
    return shutil.which("cl.exe") is not None


def needs_force_mingw64(target: Platform, py_version: str) -> bool:
    """判断编译命令是否需追加 ``--experimental=force-mingw64`` 强制 winlibs.

    需要的条件（全部满足）：

    - Windows 目标（Linux 用系统 gcc 无 zig 风险）
    - py>=3.13（Nuitka 4.1 起该版本段默认 fallback 到 zig，产物可能损坏；
      py<3.13 默认即 winlibs 无需 flag；空 ``py_version`` 未知版本不加
      flag 保持旧行为）
    - 无 MSVC（:func:`msvc_available` 为 False）：MSVC 优先级高于 fallback
      链，scons 直接用 MSVC；此时加 flag 反而把 MSVC 顶掉退回 winlibs

    与 :meth:`NuitkaEnv.ensure_env` 的 winlibs 预填充条件配套：预填充跳过
    MSVC 机器（省 200MB），本函数同样跳过（编译器统一走 MSVC），两层判断
    必须一致否则出现"预填充了却 force 走 MSVC"或"没预填充却 force 要
    winlibs"的资源错配。
    """
    if target is not Platform.WINDOWS:
        return False
    if not py_version or uses_winlibs(py_version):
        return False
    return not msvc_available()


class NuitkaWinlibs:
    """Nuitka winlibs-mingw 工具链管理 mixin：缓存查找、下载、解压.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。
    """

    @staticmethod
    def _winlibs_gcc_dir(nuitka_ver: str) -> Path:
        """返回 winlibs gcc 缓存目录（不含 gcc.exe 自身）.

        目录结构与 Nuitka ``getCachedDownload`` 拼接逻辑一致：
        ``<downloads>/gcc/x86_64/<specificity>/``，其中 ``specificity``
        为 winlibs release URL 倒数第二段（release tag）。

        :raises NuitkaError: Nuitka 版本不在 :data:`WINLIBS_URLS` 映射中。
        """
        url = WINLIBS_URLS.get(nuitka_ver)
        if url is None:
            raise NuitkaError(
                f"Nuitka {nuitka_ver} 的 winlibs-mingw 下载地址未收录，"
                f"请更新 WINLIBS_URLS 映射（当前收录: {sorted(WINLIBS_URLS)}）"
            )
        specificity = url.rsplit("/", 2)[1]
        return nuitka_winlibs_cache_dir() / "gcc" / "x86_64" / specificity

    @classmethod
    def ensure_winlibs_mingw(
        cls: type[NuitkaCompilerProtocol],
        py_version: str,
        stage: StageRecorder,
    ) -> Path:
        """确保 Nuitka 所需的 winlibs-mingw 工具链就绪，返回下载缓存根目录.

        查找顺序：

        1. fspack 缓存 ``gcc/x86_64/<specificity>/mingw64/bin/gcc.exe`` 已存在
           → 缓存命中
        2. 缓存目录下存在对应版本的 winlibs zip 归档（用户手动放置，或上次
           下载中断的残留）→ 解压到约定目录替代下载（**不删除归档**：用户
           资产须保留；纯本地操作，离线模式同样适用）。归档损坏时删除该
           归档回退下载，离线模式直接 raise
        3. 离线模式 → raise :class:`NuitkaError`（与其他下载层 fail-fast 一致）
        4. 在线模式 → 下载 winlibs zip 解压（zip 解压后删除，Nuitka 按
           gcc.exe 存在判定命中）

        调用方（:meth:`NuitkaEnv.ensure_env`）在 Windows 目标时调用；
        返回的缓存根目录经 ``_build_compile_env`` 注入
        ``NUITKA_CACHE_DIR_DOWNLOADS``，Nuitka scons 检测到 gcc.exe 已存在
        即直接使用，不触发下载与拒绝提示。

        Args:
            py_version: Python 完整版本号（映射 Nuitka 锁定版本 → winlibs URL）。
            stage: 阶段记录器，回写缓存命中数。

        Returns:
            winlibs 下载缓存根目录（``<cache_root>/nuitka-winlibs-mingw``）。

        Raises:
            NuitkaError: Nuitka 版本未收录、离线缓存未命中、或下载解压失败。
        """
        nuitka_ver = nuitka_version_for(py_version)
        gcc_dir = cls._winlibs_gcc_dir(nuitka_ver)
        gcc_exe = gcc_dir / "mingw64" / "bin" / "gcc.exe"

        # 缓存命中：gcc.exe 已存在，Nuitka 直接使用
        if gcc_exe.is_file():
            _logger.info("winlibs-mingw 已就绪（缓存命中 %s）", gcc_exe)
            stage.hit_cache()
            stage.set_detail(f"winlibs-mingw {nuitka_ver} 已就绪")
            return nuitka_winlibs_cache_dir()

        # 本地归档解压：识别缓存目录下用户手动放置（或下载中断残留）的 zip，
        # 解压替代下载；纯本地操作，离线模式同样适用
        local_zip = cls._find_local_winlibs_zip(nuitka_ver)
        if local_zip is not None:
            _logger.info("从本地归档解压 winlibs-mingw: %s", local_zip)
            try:
                cls._extract_winlibs(local_zip, gcc_dir, gcc_exe)
            except NuitkaError:
                # 归档损坏（如下载中断的半成品）：删除后回退下载；
                # 离线模式无法下载，重抛原异常
                _logger.warning("本地 winlibs 归档损坏，删除后回退下载: %s", local_zip)
                with contextlib.suppress(OSError):
                    local_zip.unlink()
                if is_offline():
                    raise
            else:
                stage.set_detail(f"winlibs-mingw {nuitka_ver} 从本地归档解压完成")
                return nuitka_winlibs_cache_dir()

        # 离线模式 fail-fast：无法下载 winlibs
        if is_offline():
            raise NuitkaError(
                f"离线模式下 winlibs-mingw 缓存未命中: {gcc_exe}，"
                "请预先在联网机器执行一次 Nuitka 构建填充缓存后拷贝，"
                f"或将 winlibs zip 归档放入 {nuitka_winlibs_cache_dir()}，"
                "或取消 FSPACK_OFFLINE 环境变量"
            )

        _logger.info("下载 winlibs-mingw（Nuitka %s）到 %s", nuitka_ver, gcc_dir)
        cls._download_and_extract_winlibs(nuitka_ver, gcc_dir, gcc_exe)
        stage.set_detail(f"winlibs-mingw {nuitka_ver} 下载完成")
        return nuitka_winlibs_cache_dir()

    @staticmethod
    def _find_local_winlibs_zip(nuitka_ver: str) -> Path | None:
        """在 winlibs 缓存目录递归查找当前 Nuitka 版本对应的 zip 归档.

        精确匹配 :data:`WINLIBS_URLS` 的归档文件名（版本不匹配的 winlibs
        工具链不识别，避免 ABI 不兼容的 gcc 被误用）。缓存根目录与任意
        子目录（含 specificity 目录下下载中断的残留）均扫描。

        前置条件：``nuitka_ver`` 已收录（调用方 :meth:`ensure_winlibs_mingw`
        先经 :meth:`_winlibs_gcc_dir` 校验 raise）。
        """
        zip_name = WINLIBS_URLS[nuitka_ver].rsplit("/", 1)[1]
        cache_dir = nuitka_winlibs_cache_dir()
        if not cache_dir.is_dir():
            return None
        for zip_path in sorted(cache_dir.rglob(zip_name)):
            if zip_path.is_file():
                return zip_path
        return None

    @staticmethod
    def _extract_winlibs(archive: Path, gcc_dir: Path, gcc_exe: Path) -> None:
        """解压 winlibs zip 归档到 ``gcc_dir``，验证 gcc.exe 就位.

        zip 顶层即 ``mingw64/`` 目录树，解压到 ``gcc_dir`` 得到
        ``gcc_dir/mingw64/bin/gcc.exe``，与 Nuitka 自行下载解压的布局一致。
        不删除归档（删除时机由调用方决定：本地资产保留、下载临时文件清理）。

        :raises NuitkaError: 解压失败（I/O 错误、归档损坏）或 gcc.exe 缺失。
        """
        gcc_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(gcc_dir)
        except (OSError, zipfile.BadZipFile) as e:
            raise NuitkaError(f"winlibs-mingw 解压失败 {archive}: {e}") from e
        if not gcc_exe.is_file():
            raise NuitkaError(f"winlibs-mingw 解压后未找到 gcc: {gcc_exe}")

    @classmethod
    def _download_and_extract_winlibs(
        cls: type[NuitkaCompilerProtocol],
        nuitka_ver: str,
        gcc_dir: Path,
        gcc_exe: Path,
    ) -> None:
        """下载 winlibs zip 并解压到 ``gcc_dir``，验证 gcc.exe 就位.

        下载的 zip 解压完成后删除（成功与失败路径均清理，避免半成品占
        ~200MB；Nuitka 按 gcc.exe 存在判定缓存命中，无需保留归档）。

        :raises NuitkaError: 下载或解压失败、解压后 gcc.exe 缺失。
        """
        from fspack.packaging.net import Downloader

        url = WINLIBS_URLS[nuitka_ver]
        archive = gcc_dir / url.rsplit("/", 1)[1]
        gcc_dir.mkdir(parents=True, exist_ok=True)
        try:
            Downloader(timeout=_WINLIBS_DOWNLOAD_TIMEOUT).download(url, archive, label="winlibs-mingw")
            cls._extract_winlibs(archive, gcc_dir, gcc_exe)
        except (OSError, zipfile.BadZipFile) as e:
            raise NuitkaError(f"winlibs-mingw 下载或解压失败: {e}") from e
        finally:
            # zip 解压完成后删除（删除失败如杀软占用不中断流程）
            with contextlib.suppress(OSError):
                archive.unlink(missing_ok=True)
