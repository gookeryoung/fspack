"""Nuitka standalone python 准备：Windows python-build-standalone 下载与缓存.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的 standalone python 准备
mixin，仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

职责边界：

- standalone python 缓存目录推导（``_build_python_cache_dir`` / ``_build_python_exe``）
- Windows python-build-standalone 下载与解压（``_ensure_build_python`` /
  ``_download_standalone_python`` / ``_extract_standalone_python``）
- Win7 兼容 DLL 注入（解压或缓存命中时调用 :func:`fspack.builder._inject_win7_compat_dll`）

不涉及：环境就绪主流程（见 :mod:`fspack.packaging.nuitka.env`）、
编译流程（见 :mod:`fspack.packaging.nuitka.compile`）、
ccache 管理（见 :mod:`fspack.packaging.nuitka.ccache`）。

从 :mod:`fspack.packaging.nuitka.env` 拆分而来，降低 ``env.py`` 行数。
standalone python 准备是独立的"获取编译用 Python 解释器"职责，独立成 mixin 便于
复用与测试。Linux runtime 已是完整 standalone，本 mixin 仅 Windows 路径有实际下载行为。
"""

from __future__ import annotations

import logging
import shutil
import sys
import tarfile
from pathlib import Path

from fspack.config import KNOWN_STANDALONE_VERSIONS
from fspack.exceptions import NuitkaError
from fspack.platform import Platform
from fspack.progress import StageRecorder

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")


class NuitkaStandalone:
    """Nuitka standalone python 准备 mixin：Windows python-build-standalone 下载与缓存.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。

    Linux runtime 已是 standalone python（完整发行版），本 mixin 返回空 Path 占位，
    由 :meth:`fspack.packaging.nuitka.compile.NuitkaCompile.compile_src` 回退到
    runtime python。仅 Windows 路径实际下载 python-build-standalone 完整发行版。
    """

    @staticmethod
    def _build_python_cache_dir(cache_root: Path, py_version: str) -> Path:
        """返回 standalone python 缓存目录：``cache_root / py_version``.

        解压后结构：``<cache>/<py_version>/python/python.exe``（Windows）或
        ``<cache>/<py_version>/python/bin/python<major>.<minor>``（Linux）。
        与 :meth:`_nuitka_cache_dir` 同根，按 py_version 隔离避免 ABI 冲突。
        """
        return cache_root / py_version

    @staticmethod
    def _build_python_exe(build_python_dir: Path, py_version: str, target: Platform) -> Path:
        """返回 standalone python 可执行文件路径.

        Windows: ``<dir>/python/python.exe``
        Linux: ``<dir>/python/bin/python<major>.<minor>``
        """
        if target is Platform.WINDOWS:
            return build_python_dir / "python" / "python.exe"
        major, minor = py_version.split(".")[:2]
        return build_python_dir / "python" / "bin" / f"python{major}.{minor}"

    @classmethod
    def _ensure_build_python(
        cls,
        cache_root: Path,
        py_version: str,
        target: Platform,
        *,
        stage: StageRecorder,
    ) -> Path:
        """确保本地缓存有 standalone python 用于运行 nuitka，返回 python 可执行文件路径.

        embed runtime python 不完整（无 .py 源码、_pth 限制 sys.path），Nuitka 的
        reExecute 机制 + scons 调用会反复衍生 ``python.exe`` 子进程导致 CPU 卡死。
        改用 python-build-standalone 完整发行版运行 nuitka。

        Windows 下载 standalone python 到 ``~/.fspack/cache/python/<py_version>/``；
        Linux 直接用 runtime 的 standalone python（已是完整发行版，无需重复下载）。

        版本按 :data:`KNOWN_STANDALONE_VERSIONS` 查询（如 3.10 → 3.10.20），与 embed
        runtime 版本（3.10.11）可能不同但 ABI 兼容（CPython 按 major.minor 兼容）。

        Args:
            cache_root: 缓存根目录（如 ``~/.fspack/cache/python``）。
            py_version: 目标 Python 完整版本号（如 ``3.10.11``）。
            target: 目标平台（决定可执行文件路径与是否下载）。
            stage: 阶段记录器。

        Returns:
            standalone python 可执行文件路径。

        Raises:
            NuitkaError: 下载或解压失败。
        """
        # Linux runtime 已是 standalone python（完整发行版），直接用 runtime python
        if target is Platform.LINUX:
            # Linux runtime python 路径由 compile_src 的 runtime_dir 参数提供，
            # 这里不重复下载，返回空 Path 占位（实际调用方用 runtime_dir 解析）
            return Path()

        # Windows: 下载 python-build-standalone Windows 版
        major_minor = ".".join(py_version.split(".")[:2])
        standalone_version = KNOWN_STANDALONE_VERSIONS.get(major_minor)
        if standalone_version is None:
            raise NuitkaError(
                f"Python {py_version} 无对应 python-build-standalone Windows 版本，"
                f"KNOWN_STANDALONE_VERSIONS 支持的 minor: {sorted(KNOWN_STANDALONE_VERSIONS)}"
            )

        build_python_dir = cls._build_python_cache_dir(cache_root, standalone_version)
        py_exe = cls._build_python_exe(build_python_dir, standalone_version, target)

        # 缓存命中：python.exe 已存在
        if py_exe.is_file():
            _logger.info(
                "standalone python %s 已就绪（缓存命中 %s）",
                standalone_version,
                build_python_dir,
            )
            stage.hit_cache()
            stage.set_detail(f"python {standalone_version} 已就绪")
        else:
            archive_path = cls._download_standalone_python(build_python_dir, standalone_version, stage)
            cls._extract_standalone_python(archive_path, build_python_dir, standalone_version)

            if not py_exe.is_file():
                raise NuitkaError(f"standalone python 解压后未找到 {py_exe}，请检查缓存目录 {build_python_dir}")

            stage.set_detail(f"python {standalone_version} 安装完成")

        # Win7 兼容性：Python 3.9+ 官方不再支持 Win7，standalone python 启动需
        # api-ms-win-core-path-l1-1-0.dll（与 embed runtime 同样需要）。复用 builder
        # 的注入逻辑：惰性导入避免 nuitka → builder 顶层循环依赖（builder 函数体内
        # 才惰性导入 nuitka）。注入幂等，缓存命中与新建均安全。
        # KNOWN_STANDALONE_VERSIONS 最低 3.10，故 standalone python 始终需要此 DLL。
        from fspack.builder import _inject_win7_compat_dll

        _inject_win7_compat_dll(py_exe.parent)
        return py_exe

    @classmethod
    def _download_standalone_python(
        cls,
        build_python_dir: Path,
        standalone_version: str,
        stage: StageRecorder,
    ) -> Path:
        """下载 python-build-standalone Windows tarball 到 build_python_dir，返回 tarball 路径.

        Raises:
            NuitkaError: 下载失败，或离线模式下缓存未命中。
        """
        # 惰性导入避免循环依赖
        from fspack.config import is_offline
        from fspack.packaging.net import Downloader
        from fspack.packaging.runtime import STANDALONE_RELEASE_TAG, standalone_url

        build_python_dir.mkdir(parents=True, exist_ok=True)
        archive_path = build_python_dir / f"cpython-{standalone_version}+{STANDALONE_RELEASE_TAG}-windows.tar.gz"

        # 离线模式 fail-fast：缓存未命中时立即报错，避免等待网络超时卡死
        if is_offline():
            raise NuitkaError(
                f"离线模式下 standalone python 缓存未命中: {archive_path.name}，"
                f"请预先下载放入 {build_python_dir} 或取消 FSPACK_OFFLINE 环境变量"
            )

        url = standalone_url(standalone_version, STANDALONE_RELEASE_TAG, windows=True)
        _logger.info("下载 standalone python %s: %s", standalone_version, url)

        try:
            downloader = Downloader(timeout=300)
            downloader.download(
                url,
                archive_path,
                stage=stage,
                label=f"standalone python {standalone_version}",
            )
        except OSError as e:
            raise NuitkaError(f"下载 standalone python 失败: {url} -> {e}") from e
        return archive_path

    @classmethod
    def _extract_standalone_python(
        cls,
        archive_path: Path,
        build_python_dir: Path,
        standalone_version: str,
    ) -> None:
        """解压 standalone python tarball 并提升内层目录到 build_python_dir 根.

        解压后结构：``build_python_dir/cpython-<ver>+<tag>-x86_64-pc-windows-msvc-install_only/python/python.exe``
        需将内层 ``python/`` 目录提升到 ``build_python_dir/python``，清理其他文件。

        Raises:
            NuitkaError: tarball 损坏或解压失败。
        """
        from fspack.packaging.runtime import STANDALONE_RELEASE_TAG

        _logger.info("解压 standalone python 到 %s", build_python_dir)
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                # Python 3.12+ 显式指定 data 过滤器（PEP 706）：消除 DeprecationWarning，
                # 并阻止绝对路径/路径穿越等恶意条目（tarball 来自网络下载）。
                # 低版本无 filter 参数，回退原行为。
                if sys.version_info >= (3, 12):
                    tf.extractall(build_python_dir, filter="data")
                else:
                    tf.extractall(build_python_dir)  # pragma: no cover
        except (tarfile.TarError, OSError) as e:
            raise NuitkaError(f"standalone python tarball 损坏: {archive_path}") from e

        # 解压后结构：build_python_dir/cpython-<ver>+<tag>-x86_64-pc-windows-msvc-install_only/python/python.exe
        # 需将内层目录提升到 build_python_dir 根
        extracted_root = (
            build_python_dir
            / f"cpython-{standalone_version}+{STANDALONE_RELEASE_TAG}-x86_64-pc-windows-msvc-install_only"
        )
        if extracted_root.is_dir():
            python_dir = extracted_root / "python"
            target_python_dir = build_python_dir / "python"
            if python_dir.is_dir() and not target_python_dir.exists():
                shutil.move(str(python_dir), str(target_python_dir))
            # 清理其他文件（share/doc 等）
            shutil.rmtree(extracted_root, ignore_errors=True)

        # 删除 tarball 节省空间
        archive_path.unlink(missing_ok=True)
