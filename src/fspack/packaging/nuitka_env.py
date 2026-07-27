"""Nuitka 环境就绪：C 编译器检查、standalone python 准备、nuitka 安装、ccache.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的环境准备 mixin，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

职责边界：

- C 编译器检查（Windows mingw / Linux gcc）
- standalone python 下载与缓存（Windows 用 python-build-standalone 完整发行版）
- nuitka 锁定版本安装到本地缓存（``pip install --target`` 从 sdist 构建）
- ccache 二进制下载与 PATH 查找（缓存 gcc 编译结果加速重复构建）
- 构建机 pip 模块可用性检查与两轮自助安装（ensurepip / uv pip install pip）

不涉及：编译流程（见 :mod:`fspack.packaging.nuitka_compile`）、
验证逻辑（见 :mod:`fspack.packaging.nuitka_verify`）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from fspack.config import KNOWN_STANDALONE_VERSIONS, MirrorConfig, nuitka_version_for
from fspack.exceptions import NuitkaError
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


class NuitkaEnv:
    """Nuitka 环境就绪 mixin：C 编译器、standalone python、nuitka、ccache.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。
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
            NuitkaError: 下载失败。
        """
        # 惰性导入避免循环依赖
        from fspack.packaging.net import Downloader
        from fspack.packaging.runtime import STANDALONE_RELEASE_TAG, standalone_url

        url = standalone_url(standalone_version, STANDALONE_RELEASE_TAG, windows=True)
        _logger.info("下载 standalone python %s: %s", standalone_version, url)

        build_python_dir.mkdir(parents=True, exist_ok=True)
        archive_path = build_python_dir / f"cpython-{standalone_version}+{STANDALONE_RELEASE_TAG}-windows.tar.gz"

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

    @staticmethod
    def _nuitka_cache_dir(cache_root: Path, py_version: str) -> Path:
        """返回 nuitka 缓存目录：``cache_root / py_version / site-packages``.

        与 :func:`fspack.builder.embed_cache_dir` 等缓存约定一致，nuitka 包
        安装到此目录，编译时用 ``sys.path.insert`` 注入。缓存按 py_version
        隔离，避免不同版本 ABI 冲突。
        """
        return cache_root / py_version / "site-packages"

    @staticmethod
    def _is_nuitka_cached(cache_dir: Path) -> bool:
        """检查缓存目录是否有 nuitka 包（文件系统检查，无 subprocess 开销）."""
        return (cache_dir / "nuitka" / "__init__.py").is_file()

    @staticmethod
    def _runtime_python(runtime_dir: Path, py_version: str, target: Platform) -> Path:
        """解析 runtime python 可执行文件路径.

        Windows: ``runtime/python.exe``
        Linux: ``runtime/python/bin/python<major>.<minor>``
        """
        if target is Platform.WINDOWS:
            return runtime_dir / "python.exe"
        major, minor = py_version.split(".")[:2]
        return runtime_dir / "python" / "bin" / f"python{major}.{minor}"

    @staticmethod
    def _check_c_compiler(target: Platform) -> None:
        """检查目标平台 C 编译器是否可用，不可用 raise :class:`NuitkaError`.

        Nuitka 调用 GCC/MSVC 生成 ``.pyd``/``.so``，无 C 编译器无法编译。

        - Windows 目标：检查 mingw 交叉编译器（``x86_64-w64-mingw32-gcc``）
        - Linux 目标：检查 gcc

        Linux 缺 gcc 时直接 raise（用户确认需显式报错而非静默跳过）；
        Windows 缺 mingw 时同样 raise，提示用户安装 mingw-w64。
        """
        # 惰性导入避免循环依赖（loader 导入 config，nuitka 也导入 config）
        from fspack.packaging.loader import gcc_available, mingw_available

        if target is Platform.WINDOWS and not mingw_available():
            raise NuitkaError(
                "Nuitka 编译需要 mingw-w64 交叉编译器，未找到 x86_64-w64-mingw32-gcc。"
                "请安装 mingw-w64（如 `choco install mingw` 或 `apt install mingw-w64`）"
            )
        if target is Platform.LINUX and not gcc_available():
            raise NuitkaError(
                "Nuitka 编译需要 gcc，未找到 gcc 可执行文件。请安装 gcc（如 `apt install gcc` 或 `yum install gcc`）"
            )

    @staticmethod
    def _resolve_jobs() -> int:
        """计算 Nuitka C 编译并行度：使用全部 CPU 核心加速单文件内的 C 代码编译.

        Nuitka ``--jobs=N`` 控制 scons 内部 gcc 并行编译 C 代码的并行度。
        串行编译每个 .py 文件（一次一个 nuitka 进程），单进程内 N 个 gcc 并行：
        4 核机器 → 1 nuitka + 1 scons + 4 gcc = 6 进程，无多进程膨胀风险。
        （若同时多 nuitka 进程并行 + 每个 --jobs=N，进程数指数级膨胀导致 CPU 卡死，
        这也是 fspack 保持串行编译 .py 文件的原因。）
        """
        return os.cpu_count() or 4

    @staticmethod
    def _build_compile_env(target: Platform, ccache_exe: Path | None) -> dict[str, str]:
        """构建注入 Nuitka 子进程的环境变量，始终设置 ``CC`` 指定 C 编译器.

        **为何始终设置 ``CC``**：Nuitka 4.x 内置 zig 作为可选 C 编译器，默认交互式
        询问是否下载。即使用 ``--assume-yes-for-downloads`` 自动接受，离线时仍会
        等待下载超时。显式设置 ``CC`` 让 scons 直接用指定编译器，Nuitka 不会选择
        zig，从根源上避免 zig 下载。:meth:`ensure_env` 已校验 gcc/mingw 可用。

        scons 读取 ``CC`` 环境变量决定 C 编译器路径：

        - ccache 启用：``CC="ccache <compiler>"``，ccache 透明缓存编译结果
          （源码未变时直接返回 .o 缓存），并设 ``CCACHE_DIR`` 指定缓存目录
        - ccache 未启用：``CC="<compiler>"``，直接用 gcc/mingw 编译

        Linux 用 ``gcc``，Windows 用 mingw 交叉编译器 ``x86_64-w64-mingw32-gcc``
        （与 :func:`fspack.packaging.loader.MINGW_GCC` 一致）。

        **Win7 兼容**：Windows 目标额外设置 ``CFLAGS=-D_WIN32_WINNT=0x0601``，
        限制 MinGW 头文件 targeting Win7（默认 ``0x0A00`` 即 Win10），避免 .pyd
        调用 Win10+ API 导致 Win7 加载失败。scons 读取 ``CFLAGS`` 追加到 ``CCFLAGS``。
        """
        from fspack.packaging.loader import LINUX_GCC, MINGW_GCC

        compiler = LINUX_GCC if target is Platform.LINUX else MINGW_GCC
        env = os.environ.copy()
        if ccache_exe is not None:
            env["CC"] = f"{ccache_exe} {compiler}"
            # ccache 缓存目录：默认 ~/.cache/ccache，显式指定到 fspack 缓存根便于管理
            ccache_dir = Path.home() / ".fspack" / "cache" / "ccache-cache"
            ccache_dir.mkdir(parents=True, exist_ok=True)
            env["CCACHE_DIR"] = str(ccache_dir)
            _logger.info("启用 ccache: CC=%s, CCACHE_DIR=%s", env["CC"], env["CCACHE_DIR"])
        else:
            env["CC"] = compiler
            _logger.info("使用系统 C 编译器: CC=%s", env["CC"])

        # Win7 兼容：MinGW 头文件默认 _WIN32_WINNT=0x0A00（Win10），.pyd 可能调用
        # Win10+ API 导致 Win7 加载失败。覆盖为 0x0601（Win7）确保 .pyd 仅调用
        # Win7 可用 API。scons 读取 CFLAGS 环境变量追加到 CCFLAGS 传给 gcc。
        if target is Platform.WINDOWS:
            win7_flag = "-D_WIN32_WINNT=0x0601"
            existing_cflags = env.get("CFLAGS", "")
            if win7_flag not in existing_cflags:
                env["CFLAGS"] = f"{existing_cflags} {win7_flag}".strip()
                _logger.info("设置 CFLAGS=%s（Win7 兼容）", env["CFLAGS"])

        return env

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

    @staticmethod
    def _has_pip(python_exe: str) -> bool:
        """检查 python 是否有 pip 模块（``import pip`` 成功）."""
        result = subprocess.run(
            [python_exe, "-c", "import pip"],
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    @staticmethod
    def _try_ensurepip(python_exe: str) -> bool:
        """第一轮自救：``python -m ensurepip --default-pip`` 安装 pip.

        标准库自带 ensurepip 模块，但 uv 创建的 venv 是精简 venv，可能不含
        ensurepip 模块（uv 用 Rust 实现的 ``uv pip`` 替代 pip）。失败时返回
        False，由调用方进入第二轮自救。
        """
        _logger.info("尝试 ensurepip 自助安装 pip: %s", python_exe)
        result = subprocess.run(
            [python_exe, "-m", "ensurepip", "--default-pip"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            _logger.warning("ensurepip 失败: %s", result.stderr.strip()[:200])
        return result.returncode == 0

    @staticmethod
    def _try_uv_install_pip() -> bool:
        """第二轮自救：``uv pip install pip`` 安装 pip 到当前 venv.

        fspack 自身用 uv 管理开发环境，uv venv 默认无 pip。``uv pip install pip``
        显式安装 pip 模块到当前 venv（uv 会从 ``VIRTUAL_ENV`` 环境变量或当前目录
        ``.venv`` 推断目标 venv）。需要 ``uv`` 命令在 PATH 中。
        """
        _logger.info("尝试 uv pip install pip 自助安装")
        result = subprocess.run(
            ["uv", "pip", "install", "pip"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            _logger.warning("uv pip install pip 失败: %s", result.stderr.strip()[:200])
        return result.returncode == 0

    @classmethod
    def _ensure_pip_available(cls, python_exe: str) -> None:
        """确保构建机 python 有 pip 模块，缺则两轮自救，仍缺才 raise.

        nuitka 4.x 在 PyPI 只发布 sdist，需要 pip 从 sdist 构建_wheel 再解压。
        fspack 用 uv 管理开发环境，uv venv 默认无 pip。按 rule-01 错误自主恢复
        原则，缺 pip 时先尝试两轮自救：

        1. ``python -m ensurepip --default-pip``（标准库 ensurepip，uv venv 可能精简掉）
        2. ``uv pip install pip``（uv 显式安装 pip 到当前 venv）

        两轮均失败才 raise :class:`NuitkaError`，避免用户手动中断。
        """
        if cls._has_pip(python_exe):
            return

        # 第一轮：ensurepip（标准库自带，但 uv venv 可能精简掉了）
        if cls._try_ensurepip(python_exe) and cls._has_pip(python_exe):
            _logger.info("ensurepip 安装 pip 成功")
            return

        # 第二轮：uv pip install pip（fspack 用 uv 管理环境）
        if cls._try_uv_install_pip() and cls._has_pip(python_exe):
            _logger.info("uv pip install pip 成功")
            return

        raise NuitkaError(
            f"构建机 python 缺 pip 模块且两轮自助安装失败: {python_exe}。"
            "nuitka 在 PyPI 只发布 sdist，需要 pip 从 sdist 构建。"
            "已尝试 `python -m ensurepip` 和 `uv pip install pip` 均失败，"
            "请检查 uv 是否在 PATH、网络是否可用，或手动安装 pip"
        )

    @classmethod
    def ensure_env(
        cls,
        cache_root: Path,
        py_version: str,
        target: Platform,
        mirror: MirrorConfig,
        *,
        stage: StageRecorder,
    ) -> str:
        """确保本地缓存已装锁定版 nuitka，返回 nuitka 版本号.

        nuitka 装到 ``cache_root / py_version / site-packages``，不污染
        ``dist/runtime``。重复构建时缓存命中直接返回，无需重装。

        步骤：

        1. :meth:`_check_c_compiler` 检查 C 编译器，缺失 raise :class:`NuitkaError`
        2. 按 :func:`nuitka_version_for` 取锁定版本号
        3. :meth:`_is_nuitka_cached` 检查缓存目录是否已有 nuitka
        4. 无则用构建机 ``pip install --target`` 从 sdist 构建并解压到缓存目录
        5. :meth:`_is_nuitka_cached` 再次验证安装成功

        nuitka 4.x 在 PyPI 只发布 sdist（无预构建 wheel），:func:`download_wheels` 的
        ``--only-binary=:all:`` 无法处理。改用 ``pip install --target <cache>`` 让 pip
        自动完成 sdist 下载、构建、解压。nuitka 实际是纯 Python（无 ``.pyd``），
        构建机 python 版本与 runtime 不同也能 ``import``。

        Args:
            cache_root: 缓存根目录（如 ``~/.fspack/cache/nuitka``）。
            py_version: Python 完整版本号（如 ``3.11.9``）。
            target: 目标平台（决定 C 编译器检查）。
            mirror: 镜像配置（提供 ``pypi_index``）。
            stage: 阶段记录器，回写缓存命中数与下载字节数。

        Returns:
            锁定的 Nuitka 版本号（如 ``4.1.3``）。

        Raises:
            NuitkaError: C 编译器缺失、构建机缺 pip、或安装后缓存目录仍无 nuitka。
        """
        cls._check_c_compiler(target)

        nuitka_ver = nuitka_version_for(py_version)
        cache_dir = cls._nuitka_cache_dir(cache_root, py_version)

        # 缓存命中：已装则跳过下载解压
        if cls._is_nuitka_cached(cache_dir):
            _logger.info("nuitka %s 已就绪（缓存命中 %s）", nuitka_ver, cache_dir)
            stage.hit_cache()
            stage.set_detail(f"nuitka {nuitka_ver} 已就绪")
            return nuitka_ver

        # nuitka 4.x 在 PyPI 只发布 sdist，用构建机 pip install --target 从 sdist
        # 构建并解压到本地缓存（不污染 dist/runtime）。nuitka 是纯 Python，跨版本可 import。
        build_python = sys.executable
        cls._ensure_pip_available(build_python)

        cache_dir.mkdir(parents=True, exist_ok=True)

        # --no-compile: 不编译 .pyc（缓存可能跨 Python 版本复用）
        # --no-cache-dir: 不用 pip 缓存，避免污染
        # -i mirror.pypi_index: 用 fspack 镜像源
        cmd = [
            build_python,
            "-m",
            "pip",
            "install",
            "--target",
            str(cache_dir),
            "--no-compile",
            "--no-cache-dir",
            "-i",
            mirror.pypi_index,
            f"nuitka=={nuitka_ver}",
        ]
        _logger.info("用构建机 pip 装 nuitka %s 到缓存 %s", nuitka_ver, cache_dir)
        # stderr=None: pip 进度（Collecting/Downloading/Building wheel/Installing）实时
        # 输出到终端，避免 sdist 构建数分钟无输出被误认为卡死。stdout 捕获但 pip install
        # 的 stdout 通常为空（成功信息走 stderr），保留以备诊断。
        result = subprocess.run(
            cmd, check=False, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=None
        )
        if result.returncode != 0:
            raise NuitkaError(f"pip install nuitka=={nuitka_ver} 失败（退出码 {result.returncode}），详见上方 pip 输出")

        # 验证安装：检查缓存目录有 nuitka 包
        if not cls._is_nuitka_cached(cache_dir):
            raise NuitkaError(f"nuitka 安装后缓存目录仍无 nuitka 包: {cache_dir}")
        stage.set_detail(f"nuitka {nuitka_ver} 安装完成")
        return nuitka_ver
