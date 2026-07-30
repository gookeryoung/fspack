"""Nuitka ccache 管理：PATH 查找、本地缓存、预编译二进制下载.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的 ccache 管理 mixin，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

职责边界：

- ccache PATH 查找与本地缓存复用（``_ensure_ccache``）
- ccache 预编译二进制下载与解压（``_download_and_extract_ccache``，仅 Linux/Windows x86_64）
- 旧版子目录结构自动迁移到根目录（兼容历史缓存）

不涉及：环境就绪主流程（见 :mod:`fspack.packaging.nuitka.env`）、
编译流程（见 :mod:`fspack.packaging.nuitka.compile`）、
standalone python 准备（见 :mod:`fspack.packaging.nuitka.standalone`）。

从 :mod:`fspack.packaging.nuitka.env` 拆分而来，降低 ``env.py`` 行数。
ccache 管理是独立的"获取 C 编译缓存加速工具"职责，独立成 mixin 便于复用与测试。
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

from fspack.config import is_offline
from fspack.platform import Platform
from fspack.progress import StageRecorder

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")

# ccache 版本与下载地址：首次启用 ccache 时下载预编译二进制到 ~/.fspack/cache/ccache/
# ccache 缓存 gcc 编译结果，源码未变时跳过 C 编译，二次构建近零耗时。
# 仅 Linux x86_64 与 Windows x86_64 有预编译二进制，其他架构需用户自行安装 ccache 到 PATH。
CCACHE_VERSION = "4.10.2"
_CCACHE_BASE = f"https://github.com/ccache/ccache/releases/download/v{CCACHE_VERSION}"
CCACHE_URLS: dict[Platform, str] = {
    Platform.LINUX: f"{_CCACHE_BASE}/ccache-{CCACHE_VERSION}-linux-x86_64.tar.xz",
    Platform.WINDOWS: f"{_CCACHE_BASE}/ccache-{CCACHE_VERSION}-windows-x86_64.zip",
}


class NuitkaCcache:
    """Nuitka ccache 管理 mixin：PATH 查找、本地缓存、预编译二进制下载.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。

    ccache 缓存 gcc 编译结果（.o 文件），源码未变时直接返回缓存跳过 C 编译。
    首次构建填充缓存，后续构建（即使清理 dist）近零耗时。
    """

    @classmethod
    def _ensure_ccache(  # noqa: PLR0911
        cls,
        cache_root: Path,
        target: Platform,
        stage: StageRecorder,
    ) -> Path | None:
        """确保 ccache 可用：优先 PATH，缺失则下载预编译二进制到本地缓存.

        ccache 缓存 gcc 编译结果（.o 文件），源码未变时直接返回缓存跳过 C 编译。
        首次构建填充缓存，后续构建（即使清理 dist）近零耗时。

        查找顺序：
        1. ``shutil.which("ccache")`` — 系统已安装
        2. ``~/.fspack/cache/ccache/ccache[.exe]`` — 本地缓存
        3. 从 GitHub releases 下载预编译二进制（仅 Linux/Windows x86_64）

        其他架构（arm64 等）无预编译二进制，需用户自行安装 ccache 到 PATH。
        下载失败仅 warning 不中断，回退到无 ccache 模式（编译仍可完成，仅无缓存加速）。

        Returns:
            ccache 可执行文件路径；不可用返回 None。
        """
        # 1. 系统已安装（任意版本均可用，ccache 4.x 协议稳定）
        found = shutil.which("ccache")
        if found:
            _logger.info("使用系统 ccache: %s", found)
            return Path(found)

        # 2. 本地缓存
        ccache_dir = cache_root.parent / "ccache"
        exe_name = "ccache.exe" if target is Platform.WINDOWS else "ccache"
        ccache_exe = ccache_dir / exe_name
        if ccache_exe.is_file():
            _logger.info("使用本地缓存 ccache: %s", ccache_exe)
            return ccache_exe

        # 2.1 容错：本地缓存根目录无 ccache，但存在旧版子目录结构
        # （ccache-<ver>-<platform>/ccache[.exe]），自动迁移到根目录复用，避免重新下载
        nested: list[Path] = list(ccache_dir.glob(f"ccache-*/{exe_name}")) if ccache_dir.is_dir() else []
        if nested:
            nested[0].rename(ccache_exe)
            for d in ccache_dir.glob("ccache-*/"):
                shutil.rmtree(d, ignore_errors=True)
            if target is Platform.LINUX:
                with contextlib.suppress(OSError):
                    ccache_exe.chmod(0o755)
            _logger.info("迁移本地缓存 ccache 到根目录: %s", ccache_exe)
            return ccache_exe

        # 3. 下载预编译二进制
        url = CCACHE_URLS.get(target)
        if url is None:
            _logger.warning("ccache 无 %s 平台预编译二进制，请手动安装到 PATH", target.value)
            return None

        # 离线模式跳过下载，回退到无 ccache 模式（编译仍可完成，仅无缓存加速）
        if is_offline():
            _logger.warning(
                "离线模式下 ccache 缓存未命中且无系统 ccache，跳过下载回退到无缓存模式。"
                "请预先下载 ccache 放入 %s 或安装系统 ccache 到 PATH",
                ccache_dir,
            )
            return None

        _logger.info("下载 ccache %s 到 %s", CCACHE_VERSION, ccache_dir)
        try:
            cls._download_and_extract_ccache(url, ccache_dir, target)
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as e:
            _logger.warning("ccache 下载失败，回退到无缓存模式: %s", e)
            return None
        if not ccache_exe.is_file():
            _logger.warning("ccache 下载后未找到可执行文件 %s", ccache_exe)
            return None
        # Linux 需可执行权限
        if target is Platform.LINUX:
            with contextlib.suppress(OSError):
                ccache_exe.chmod(0o755)
        stage.set_detail(f"ccache {CCACHE_VERSION} 已下载")
        return ccache_exe

    @staticmethod
    def _download_and_extract_ccache(url: str, ccache_dir: Path, target: Platform) -> None:
        """下载 ccache 归档并解压 ccache 二进制到 ``ccache_dir``.

        Linux 归档为 ``.tar.xz``，内含 ``ccache-<ver>-linux-x86_64/ccache``；
        Windows 归档为 ``.zip``，内含 ``ccache.exe``。
        解压后仅提取 ccache 可执行文件到 ``ccache_dir`` 根目录（扁平布局）。
        """
        from fspack.packaging.net import Downloader

        ccache_dir.mkdir(parents=True, exist_ok=True)
        downloader = Downloader(timeout=120)
        if target is Platform.LINUX:
            archive = ccache_dir / "ccache.tar.xz"
            downloader.download(url, archive, label="ccache")
            with tarfile.open(archive, "r:xz") as tf:
                # PEP 706: 3.12+ 需 filter="data" 防路径穿越
                if sys.version_info >= (3, 12):
                    tf.extractall(ccache_dir, filter="data")  # type: ignore[call-arg]
                else:  # pragma: no cover
                    tf.extractall(ccache_dir)
            archive.unlink()
            # 归档内 ccache 在 ccache-<ver>-linux-x86_64/ccache，移动到根目录
            extracted = list(ccache_dir.glob("ccache-*/ccache"))
            if extracted:
                extracted[0].rename(ccache_dir / "ccache")
                # 清理空目录
                for d in ccache_dir.glob("ccache-*/"):
                    shutil.rmtree(d, ignore_errors=True)
        else:
            archive = ccache_dir / "ccache.zip"
            downloader.download(url, archive, label="ccache")
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(ccache_dir)
            archive.unlink()
            # 归档内 ccache.exe 在 ccache-<ver>-windows-x86_64/ccache.exe，移动到根目录
            extracted = list(ccache_dir.glob("ccache-*/ccache.exe"))
            if extracted:
                extracted[0].rename(ccache_dir / "ccache.exe")
                # 清理子目录（LICENSE/MANUAL/README 等）
                for d in ccache_dir.glob("ccache-*/"):
                    shutil.rmtree(d, ignore_errors=True)
