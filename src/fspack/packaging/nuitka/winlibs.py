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

缓存目录结构（与 Nuitka ``getCachedDownload`` 约定一致）::

    <cache_root>/nuitka-winlibs-mingw/
    └── gcc/                          # basename(binary)="gcc.exe" 去扩展名
        └── x86_64/                   # is_arch_specific（target_arch）
            └── <specificity>/        # winlibs release URL 倒数第二段
                └── mingw64/
                    └── bin/
                        └── gcc.exe   # 缓存命中标志（Nuitka 检查此文件）

职责边界：

- winlibs URL 按 Nuitka 版本映射（与 Nuitka 源码 ``getCachedDownloadedMinGW64``
  同步维护，版本升级时须核对）
- 缓存命中检查与下载解压（:meth:`NuitkaWinlibs.ensure_winlibs_mingw`）
- 离线模式缓存未命中 fail-fast

不涉及：编译环境变量注入（见 :mod:`fspack.packaging.nuitka.env` 的
``_build_compile_env``）、ccache 管理（见 :mod:`fspack.packaging.nuitka.ccache`）。
"""

from __future__ import annotations

import contextlib
import logging
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.config import is_offline, nuitka_version_for
from fspack.config.cache import nuitka_winlibs_cache_dir
from fspack.config.versions import _split_t_suffix
from fspack.exceptions import NuitkaError
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

        1. fspack 缓存 ``<cache_root>/nuitka-winlibs-mingw/gcc/x86_64/<specificity>/
           mingw64/bin/gcc.exe`` 已存在 → 缓存命中
        2. 在线模式 → 下载 winlibs zip 解压到上述目录（zip 解压后删除，
           Nuitka 按 gcc.exe 存在判定命中）
        3. 离线模式缓存未命中 → raise :class:`NuitkaError`（与其他下载层
           fail-fast 行为一致）

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

        # 离线模式 fail-fast：无法下载 winlibs
        if is_offline():
            raise NuitkaError(
                f"离线模式下 winlibs-mingw 缓存未命中: {gcc_exe}，"
                "请预先在联网机器执行一次 Nuitka 构建填充缓存后拷贝，"
                "或取消 FSPACK_OFFLINE 环境变量"
            )

        _logger.info("下载 winlibs-mingw（Nuitka %s）到 %s", nuitka_ver, gcc_dir)
        cls._download_and_extract_winlibs(nuitka_ver, gcc_dir, gcc_exe)
        stage.set_detail(f"winlibs-mingw {nuitka_ver} 下载完成")
        return nuitka_winlibs_cache_dir()

    @staticmethod
    def _download_and_extract_winlibs(nuitka_ver: str, gcc_dir: Path, gcc_exe: Path) -> None:
        """下载 winlibs zip 并解压到 ``gcc_dir``，验证 gcc.exe 就位.

        zip 顶层即 ``mingw64/`` 目录树，解压到 ``gcc_dir`` 得到
        ``gcc_dir/mingw64/bin/gcc.exe``，与 Nuitka 自行下载解压的布局一致。
        解压完成后删除 zip（Nuitka 按 gcc.exe 存在判定缓存命中，无需保留）。

        :raises NuitkaError: 下载或解压失败、解压后 gcc.exe 缺失。
        """
        from fspack.packaging.net import Downloader

        url = WINLIBS_URLS[nuitka_ver]
        archive = gcc_dir / url.rsplit("/", 1)[1]
        gcc_dir.mkdir(parents=True, exist_ok=True)
        try:
            Downloader(timeout=_WINLIBS_DOWNLOAD_TIMEOUT).download(url, archive, label="winlibs-mingw")
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(gcc_dir)
        except (OSError, zipfile.BadZipFile) as e:
            raise NuitkaError(f"winlibs-mingw 下载或解压失败: {e}") from e
        finally:
            # zip 解压完成后删除（成功与失败路径均清理，避免半成品占 ~200MB）；
            # 删除失败（如杀软占用）不中断流程
            with contextlib.suppress(OSError):
                archive.unlink(missing_ok=True)

        if not gcc_exe.is_file():
            raise NuitkaError(f"winlibs-mingw 解压后未找到 gcc: {gcc_exe}")
