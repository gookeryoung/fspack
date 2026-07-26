"""Nuitka 编译器：将用户源码 ``.py`` 编译为 ``.pyd`` 本机执行.

参考 RimSort 的 Nuitka 打包方案，用 ``python -m nuitka --module`` 将每个 ``.py``
编译为对应平台的 ``.pyd``（Windows）/ ``.so``（Linux）。运行时 ``.pyd`` 优先级
高于 ``.pyc``，Python 自动加载本机代码版本，执行速度提升 30-50%。

与 RimSort 区别：fspack 仅编译用户源码（``dist/src/``），第三方依赖保持 wheel
解压 + ``.pyc``（构建速度优先）。RimSort 用 Nuitka ``--follow-imports`` 全量编译，
构建耗时几十分钟；fspack 用户源码通常较小，编译时间可控。

Nuitka 环境就绪流程（:meth:`NuitkaCompiler.ensure_env`）：

1. 检查 C 编译器（Windows: ``mingw_available()``，Linux: ``gcc_available()``），
   缺失直接 raise :class:`NuitkaError`（不静默跳过）
2. 检查本地缓存 ``~/.fspack/cache/nuitka/<py_version>/site-packages`` 是否已装
   目标版本 nuitka（文件系统检查 ``nuitka/__init__.py``，无 subprocess 开销）
3. 未安装则用构建机 ``pip install --target`` 从 sdist 构建并解压到本地缓存，
   不污染 ``dist/runtime`` 发行产物

nuitka 4.x 在 PyPI 只发布 sdist（无预构建 wheel），且 sdist 构建出的 wheel 标签
与构建机 python ABI 绑定（如 ``cp313-cp313-win_amd64``），但实际内容是纯 Python
（无 ``.pyd``），跨 Python 版本可 ``import``。fspack 用构建机 python 执行
``pip install --target <cache_site_packages>`` 让 pip 自动完成 sdist 下载、构建、
解压，绕过 :func:`download_wheels` 的 ``--only-binary=:all:`` 限制。

Nuitka 版本按目标 Python 版本锁定（:func:`nuitka_version_for`）：

- Python 3.8/3.9 → nuitka 2.5.1（4.x 已不再维护 EOL 的 3.8）
- Python 3.10+ → nuitka 4.1.3（当前最新稳定版）

**编译 Python 环境**（:meth:`NuitkaCompiler._ensure_build_python`）：

Nuitka 官方建议"用目标 Python 解释器运行 nuitka"。fspack 的 ``dist/runtime/python.exe``
是 embed 版本（无完整标准库 .py 源码、_pth 限制 sys.path），Nuitka 的 reExecute
机制 + scons 调用会反复衍生 ``python.exe`` 子进程导致 CPU 卡死。

解决方案：下载 python-build-standalone Windows 版到 ``~/.fspack/cache/python/<py_version>/``
作为 nuitka 编译环境（完整 Python，含 .py 源码）。版本按 :data:`KNOWN_STANDALONE_VERSIONS`
查询（如 3.10 → 3.10.20），与 embed runtime 版本（3.10.11）可能不同但 ABI 兼容
（CPython 按 major.minor 兼容）。编译出的 ``.pyd`` 可在 embed runtime 上运行。

stamp 缓存（:meth:`NuitkaCompiler.compile_with_stamp`）：重复构建时若
``dist/.nuitka_compile_stamp``（含 ``nuitka_version|py_version|src_fingerprint|entry_rels``）
匹配则跳过整个 Nuitka 阶段（含 ensure_env 与 compile_src），避免重复 subprocess
启动开销与编译耗时。入口文件（``entry_rels``）不编译不删除，保留 ``.py`` 供
入口包装器 ``runpy.run_path()`` 调用。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

from fspack.config import KNOWN_STANDALONE_VERSIONS, MirrorConfig, nuitka_version_for
from fspack.exceptions import NuitkaError
from fspack.platform import Platform
from fspack.progress import StageRecorder

__all__ = ["NuitkaCompiler"]

_logger = logging.getLogger(__name__)

# 心跳间隔：nuitka reExecute 机制导致子进程输出不可靠，每 N 秒输出编译耗时让用户看到进度
_HEARTBEAT_INTERVAL = 10.0


class NuitkaCompiler:
    """Nuitka 编译器：将用户源码编译为本机 ``.pyd``/``.so``.

    nuitka 装到本地缓存 ``~/.fspack/cache/nuitka/<py_version>/site-packages/``，
    不污染 ``dist/runtime`` 发行产物。编译时用 **standalone python**（非 embed runtime）
    运行 nuitka，避免 embed python 不完整导致 reExecute 进程衍生。

    Windows 编译 Python 来源：python-build-standalone Windows 版（完整 CPython 发行版，
    含 .py 源码），缓存到 ``~/.fspack/cache/python/<py_version>/python/python.exe``。
    Linux 直接用 runtime 的 standalone python（已是完整发行版）。

    用临时脚本文件而非 ``-c``：Nuitka 的 ``reExecuteNuitka`` 无条件访问
    ``sys.modules["__main__"].__file__``，``-c`` 模式下该属性不存在会
    ``AttributeError``。

    公共 API：

    - :meth:`ensure_env`：检查 C 编译器并按目标 Python 版本安装锁定版 nuitka 到本地缓存
    - :meth:`compile_src`：编译 ``dist/src`` 下所有 ``.py`` 为本机模块
    - :meth:`compile_with_stamp`：整合 ensure_env + stamp 缓存 + compile_src 的入口
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
            return py_exe

        archive_path = cls._download_standalone_python(build_python_dir, standalone_version, stage)
        cls._extract_standalone_python(archive_path, build_python_dir, standalone_version)

        if not py_exe.is_file():
            raise NuitkaError(f"standalone python 解压后未找到 {py_exe}，请检查缓存目录 {build_python_dir}")

        stage.set_detail(f"python {standalone_version} 安装完成")
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
    def _stream_compile(cmd: list[str]) -> tuple[int, str, str]:
        """运行 nuitka 编译命令，实时流式输出 stdout/stderr 到终端.

        用 ``Popen`` + 两个守护线程通过 ``os.read`` 读取 stdout/stderr 文件描述符
        字节块并实时写入 ``sys.stdout``/``sys.stderr``，支持 nuitka 的 ``Nuitka:INFO``
        步骤输出和 C 编译器调用过程实时显示，避免单文件编译数十秒无输出被误认为卡死。

        同时累积 stdout/stderr 内容供失败时诊断（当前仅返回未使用，保留以备扩展）。

        参考 :func:`fspack.packaging.wheels._stream_subprocess` 的实现模式，区别在于
        nuitka 的 INFO 输出可能走 stdout 或 stderr，需同时流式两者。
        """
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        def _drain(stream: object, chunks: list[bytes], out: object) -> None:
            assert stream is not None
            fd = stream.fileno()  # type: ignore[union-attr]
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
                out.buffer.write(chunk)  # type: ignore[union-attr]
                out.buffer.flush()  # type: ignore[union-attr]

        t_out = threading.Thread(target=_drain, args=(process.stdout, stdout_chunks, sys.stdout), daemon=True)
        t_err = threading.Thread(target=_drain, args=(process.stderr, stderr_chunks, sys.stderr), daemon=True)
        t_out.start()
        t_err.start()
        returncode = process.wait()
        t_out.join()
        t_err.join()
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return returncode, stdout, stderr

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
            text=True,
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
            text=True,
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
        result = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=None)
        if result.returncode != 0:
            raise NuitkaError(f"pip install nuitka=={nuitka_ver} 失败（退出码 {result.returncode}），详见上方 pip 输出")

        # 验证安装：检查缓存目录有 nuitka 包
        if not cls._is_nuitka_cached(cache_dir):
            raise NuitkaError(f"nuitka 安装后缓存目录仍无 nuitka 包: {cache_dir}")
        stage.set_detail(f"nuitka {nuitka_ver} 安装完成")
        return nuitka_ver

    @classmethod
    def compile_src(  # noqa: PLR0913
        cls,
        src_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        nuitka_cache: Path,
        *,
        stage: StageRecorder,
        build_python_exe: Path | None = None,
        entry_rels: set[str] | None = None,
    ) -> None:
        """编译 ``src_dir`` 下所有 ``.py`` 为 ``.pyd``/``.so``，编译后删除 ``.py`` 源码.

        用 **standalone python**（``build_python_exe``）运行 nuitka，避免 embed runtime
        python 不完整导致 reExecute 进程衍生。``build_python_exe`` 为 None 或不存在时
        回退到 runtime python（Linux runtime 已是完整 standalone，无需单独下载）。

        用临时脚本文件而非 ``-c``：Nuitka 的 ``reExecuteNuitka`` 无条件访问
        ``sys.modules["__main__"].__file__``，``-c`` 模式下该属性不存在会
        ``AttributeError``。脚本内 ``sys.path.insert`` 注入 nuitka 缓存路径。

        步骤：

        1. 解析编译用 python 路径（优先 standalone，回退 runtime）并检查缓存目录有 nuitka
        2. 创建临时 bootstrap 脚本注入 sys.path 调用 nuitka ``--module`` 逐个编译 ``.py``
           （跳过 ``__init__.py``：包标识文件通常为空或仅含 import，编译无收益）
        3. 删除成功编译的 ``.py`` 源码（``.pyd`` 已生成可替代）
        4. 清理 Nuitka 临时构建文件（``.build/`` 目录）

        单文件编译失败仅告警不中断，已成功编译的 ``.pyd`` 仍可用。``__init__.py``
        不编译不删除，保留 ``.py`` 维持包标识（与 :func:`fspack.builder._strip_py_sources`
        策略一致，避免 PEP 420 命名空间包导致 ``.pyd``/``.pyc`` 不被识别为包成员）。

        **入口文件跳过**（``entry_rels``）：入口包装器用 ``runpy.run_path()`` 显式
        指定 ``.py`` 路径调用用户代码（按 project_memory 约定，用户拒绝直接 import
        方案）。若入口 ``.py`` 被 Nuitka 编译后删除，``run_path`` 会
        ``FileNotFoundError``。故入口文件必须保留 ``.py`` 形态，由预编译字节码阶段
        编译为 ``.pyc`` 优化（速度略逊 ``.pyd`` 但兼容 ``run_path``）。

        Args:
            src_dir: 用户源码目录（``dist/src``）。
            runtime_dir: runtime 根目录（含 ``python.exe`` 或 ``python/bin/``）。
            py_version: Python 完整版本号（如 ``3.11.9``）。
            target: 目标平台（决定 runtime python 路径回退）。
            nuitka_cache: nuitka 缓存目录（含 ``nuitka/`` 包，由 :meth:`ensure_env` 安装）。
            stage: 阶段记录器，记录编译项数与跳过数。
            build_python_exe: standalone python 可执行文件路径（Windows 由
                :meth:`_ensure_build_python` 下载）。None 或不存在时回退到 runtime python。
            entry_rels: 入口文件相对 ``src_dir`` 的 POSIX 路径集合（如 ``{"snake.py"}``）。
                这些文件不编译不删除，保留 ``.py`` 供 ``runpy.run_path()`` 调用。
        """
        py_exe = cls._resolve_compile_python(build_python_exe, runtime_dir, py_version, target, stage)
        if py_exe is None:
            return

        if not cls._is_nuitka_cached(nuitka_cache):
            _logger.warning(
                "Nuitka 编译跳过: 缓存目录无 nuitka %s，请用 fsp b --nuitka 触发安装",
                nuitka_cache,
            )
            stage.set_detail("nuitka 未安装，跳过（回退到 .pyc 模式）")
            return

        py_files = cls._collect_py_files(src_dir, entry_rels)
        if not py_files:
            stage.set_detail("无 .py 文件可编译")
            return

        bootstrap_script = cls._create_bootstrap_script(nuitka_cache)
        try:
            compiled_files, failed = cls._compile_files(py_exe, bootstrap_script, py_files, stage)
        finally:
            shutil.rmtree(bootstrap_script.parent, ignore_errors=True)

        stripped = cls._strip_compiled_sources(compiled_files, stage)
        compiled = len(compiled_files)
        if failed:
            stage.set_detail(f"编译 {compiled} 个，失败 {failed} 个，剥离 {stripped} 个 .py")
        else:
            stage.set_detail(f"编译 {compiled} 个，剥离 {stripped} 个 .py")

    @classmethod
    def _resolve_compile_python(
        cls,
        build_python_exe: Path | None,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        stage: StageRecorder,
    ) -> Path | None:
        """解析编译用 python 路径，优先 standalone，回退 runtime python，未就绪返回 None."""
        if build_python_exe is not None and build_python_exe.is_file():
            _logger.info("用 standalone python 运行 nuitka: %s", build_python_exe)
            return build_python_exe
        py_exe = cls._runtime_python(runtime_dir, py_version, target)
        if not py_exe.is_file():
            _logger.warning("Nuitka 编译跳过: runtime python 未就绪 %s", py_exe)
            stage.set_detail("runtime python 未就绪，跳过")
            return None
        return py_exe

    @staticmethod
    def _collect_py_files(src_dir: Path, entry_rels: set[str] | None) -> list[Path]:
        """收集待编译的 .py 文件，排除 Nuitka 残留目录、__init__.py 与入口文件.

        排除规则：

        1. Nuitka 残留的 ``<name>.build/`` 目录：``--remove-output`` 只在编译成功时清理，
           失败时残留。下次构建若不排除会扫到 scons-debug.py 等产物并尝试编译。
        2. ``__init__.py``：包标识文件通常为空或仅含 import，编译为 .pyd 无收益且
           增加 subprocess 开销。.py 保留作包标识（PEP 420），.pyc 预编译提供
           字节码优化。跳过后 compiled_files 不含 __init__.py，删除循环天然跳过。
        3. 入口文件（``entry_rels``）：入口包装器用 ``runpy.run_path()`` 显式指定 .py 路径，
           编译后 .py 被删除会导致 FileNotFoundError。入口文件保留 .py 形态，由 .pyc 优化。
        """
        py_files = sorted(
            p
            for p in src_dir.rglob("*.py")
            if not any(part.lower().endswith(".build") for part in p.parts) and p.name != "__init__.py"
        )
        if entry_rels:
            py_files = [p for p in py_files if p.relative_to(src_dir).as_posix() not in entry_rels]
        return py_files

    @staticmethod
    def _create_bootstrap_script(nuitka_cache: Path) -> Path:
        """创建临时 bootstrap 脚本注入 sys.path 调用 nuitka.

        用临时脚本文件启动 nuitka（不能用 ``-c``）：
        nuitka.utils.ReExecute.reExecuteNuitka 无条件访问 ``sys.modules["__main__"].__file__``
        设置 NUITKA_BINARY_NAME，``-c`` 模式下 ``__main__`` 无 ``__file__`` 会 AttributeError。
        临时脚本让 ``__main__.__file__`` 指向脚本路径，reExecute 能正常工作。
        ``sys.path.insert`` 注入缓存目录绕过 ``python3X._pth`` 对 PYTHONPATH 的限制。
        """
        bootstrap_dir = Path(tempfile.mkdtemp(prefix="fspack_nuitka_"))
        bootstrap_script = bootstrap_dir / "_nuitka_bootstrap.py"
        bootstrap_script.write_text(
            f"import sys; sys.path.insert(0, r'{nuitka_cache}'); from nuitka.__main__ import main; main()",
            encoding="utf-8",
        )
        return bootstrap_script

    @classmethod
    def _compile_files(
        cls,
        py_exe: Path,
        bootstrap_script: Path,
        py_files: list[Path],
        stage: StageRecorder,
    ) -> tuple[set[Path], int]:
        """逐个编译 .py 文件，返回 (成功编译的文件集合, 失败数).

        Nuitka 编译参数（作为脚本参数传入，进入 ``sys.argv[1:]``）：

        - ``--module``：编译为可导入模块（.pyd/.so），不生成独立 exe
        - ``--output-dir``：输出目录与源码同目录（保持包结构）
        - ``--no-pyi-file``：不生成 .pyi 类型存根（运行时不需要）
        - ``--remove-output``：编译后删除临时构建文件（.build/ 目录）
        - ``--jobs=1``：限制 C 编译并行度为 1。Nuitka 默认使用全部 CPU 核心，多文件并行编译时
          每个 scons 子进程再启动 gcc，进程数指数级膨胀导致 CPU 卡死。限制为 1 串行编译，
          虽然慢但稳定，避免资源耗尽。

        不需要 ``--python-for-scons``：已用 standalone python（完整环境）运行 nuitka，
        scons 自动继承 ``sys.executable``，无需另指定。
        注意：nuitka 4.x 的 ``--show-progress`` 已 obsolete 无效；nuitka 的 reExecute 机制
        (os._exit 退出子进程 A，Windows close_fds=True 导致子进程 B 不继承 PIPE) 使得
        _stream_compile 的 PIPE 捕获不可靠。用心跳线程保证用户看到编译进度。
        """
        compiled_files: set[Path] = set()
        failed = 0
        total = len(py_files)
        # 记录成功编译的文件：仅这些 .py 可安全删除（.pyd 已生成）。
        # 失败的 .py 保留，让运行时回退到 .pyc 加载，避免编译失败导致 dist/src 无可用代码。
        for idx, py_file in enumerate(py_files, 1):
            _logger.info("编译 [%d/%d] %s", idx, total, py_file.name)
            # 心跳线程：每 10 秒输出编译耗时，避免单文件编译数十秒无输出被误认为卡死。
            # nuitka reExecute 的子进程 B 输出可能不到 PIPE，心跳是唯一的进度反馈。
            stop_heartbeat = threading.Event()
            start_ts = time.monotonic()

            def _heartbeat(_stop: threading.Event = stop_heartbeat, _start: float = start_ts) -> None:
                while not _stop.wait(_HEARTBEAT_INTERVAL):
                    elapsed = int(time.monotonic() - _start)
                    _logger.info("Nuitka 编译中... 已耗时 %ds", elapsed)

            hb_thread = threading.Thread(target=_heartbeat, daemon=True)
            hb_thread.start()
            try:
                returncode, _stdout, _stderr = cls._stream_compile(
                    [
                        str(py_exe),
                        str(bootstrap_script),
                        "--module",
                        f"--output-dir={py_file.parent}",
                        "--no-pyi-file",
                        "--remove-output",
                        # --jobs=1：必须用 = 形式传参。Nuitka 4.x 的 argparse 配置要求
                        # --jobs=N 格式，用空格分隔（"--jobs", "1"）会报错：
                        # "The '--jobs' option requires an argument with '--jobs='."
                        "--jobs=1",
                        str(py_file),
                    ]
                )
            finally:
                stop_heartbeat.set()
                hb_thread.join(timeout=1.0)
            if returncode == 0:
                compiled_files.add(py_file)
                stage.processed()
            else:
                failed += 1
                _logger.warning("Nuitka 编译失败 %s（退出码 %s），详见上方输出", py_file, returncode)
        return compiled_files, failed

    @staticmethod
    def _strip_compiled_sources(compiled_files: set[Path], stage: StageRecorder) -> int:
        """删除成功编译的 .py 源码（.pyd 已生成可替代），返回删除数.

        失败的 .py 必须保留：运行时可回退到 .pyc 加载，避免编译失败导致 dist/src 无可用代码。
        ``__init__.py`` 不在 ``compiled_files`` 中（收集时已跳过），无需额外检查。
        """
        stripped = 0
        for py_file in compiled_files:
            try:
                py_file.unlink()
                stripped += 1
            except OSError as e:
                _logger.warning("删除 .py 失败 %s: %s", py_file, e)
        if stripped:
            stage.skip(stripped)
        return stripped

    @staticmethod
    def _stamp_path(dist_dir: Path) -> Path:
        """返回 Nuitka 编译 stamp 文件路径：``dist/.nuitka_compile_stamp``."""
        return dist_dir / ".nuitka_compile_stamp"

    @staticmethod
    def _stamp_key(
        src_dir: Path,
        nuitka_version: str,
        py_version: str,
        entry_rels: set[str] | None = None,
    ) -> str:
        """计算 Nuitka 编译 stamp 键.

        四要素：

        - ``nuitka_version``：切换 Nuitka 版本时强制重编（如 3.10 从 4.1.3 升级到 4.2）
        - ``py_version``：切换 Python 版本时强制重编（.pyd ABI 绑定）
        - ``src_fingerprint``：用户源码变化时强制重编（按 ``rule-01`` 闭环要求）
        - ``entry_rels``：入口文件集合变化时强制重编（影响哪些文件被跳过，
          避免上次编译删除了 .py、本次新增入口跳过但 .py 已不在导致 run_path 失败）

        ``pyc_optimize`` 不纳入：Nuitka 编译不受 .pyc 优化级别影响，
        site-packages 的 .pyc 由 :func:`_precompile_pyc` 单独缓存。
        """
        from fspack.analyzer import source_fingerprint

        src_fp = source_fingerprint(src_dir) if src_dir.is_dir() else ""
        # entry_rels 排序后拼接，避免集合迭代顺序不稳定导致 stamp 抖动
        entry_part = ",".join(sorted(entry_rels)) if entry_rels else ""
        return f"{nuitka_version}|{py_version}|{src_fp}|{entry_part}"

    @classmethod
    def compile_with_stamp(  # noqa: PLR0913
        cls,
        src_dir: Path,
        dist_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        mirror: MirrorConfig,
        cache_root: Path,
        *,
        stage: StageRecorder,
        entry_rels: set[str] | None = None,
    ) -> None:
        """整合 ensure_env + standalone python + stamp 缓存 + compile_src 的入口.

        重复构建时若 :meth:`_stamp_path` 文件内容与 :meth:`_stamp_key` 匹配，
        跳过整个 Nuitka 阶段（含 C 编译器检查、wheel 安装、源码编译），
        避免重复 subprocess 启动与编译耗时。

        首次构建或源码/版本变化时：

        1. :meth:`ensure_env` 检查 C 编译器并安装锁定版 nuitka 到本地缓存
        2. :meth:`_ensure_build_python` 准备 standalone python（Windows 专用，
           embed runtime python 不完整会导致 Nuitka reExecute fork bomb）
        3. :meth:`compile_src` 用 standalone python 运行 nuitka 逐文件编译 ``.py`` 为 ``.pyd``
        4. 写入 stamp 文件供下次构建比对

        Args:
            src_dir: 用户源码目录（``dist/src``）。
            dist_dir: dist 根目录（stamp 文件写入位置）。
            runtime_dir: runtime 根目录（含 ``python.exe`` 或 ``python/bin/``）。
            py_version: Python 完整版本号（如 ``3.11.9``）。
            target: 目标平台。
            mirror: 镜像配置（提供 ``pypi_index`` 给 :meth:`ensure_env`）。
            cache_root: nuitka 缓存根目录（如 ``~/.fspack/cache/nuitka``）。
                standalone python 缓存目录与之同根（``cache_root.parent / "python"``）。
            stage: 阶段记录器。
            entry_rels: 入口文件相对 ``src_dir`` 的 POSIX 路径集合（如 ``{"snake.py"}``）。
                传给 :meth:`compile_src` 跳过编译与删除，并纳入 stamp key。

        Raises:
            NuitkaError: C 编译器缺失，或 nuitka 安装失败，或 standalone python 下载失败。
        """
        nuitka_ver = nuitka_version_for(py_version)
        stamp = cls._stamp_path(dist_dir)
        stamp_key = cls._stamp_key(src_dir, nuitka_ver, py_version, entry_rels)

        # stamp 命中：跳过整个 Nuitka 阶段
        try:
            if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
                _logger.info("Nuitka stamp 命中，跳过编译")
                stage.hit_cache()
                stage.set_detail(f"stamp 命中，nuitka {nuitka_ver} 已编译")
                return
        except OSError:
            pass

        # 未命中：ensure_env + ensure_build_python + compile_src + 写 stamp
        cls.ensure_env(cache_root, py_version, target, mirror, stage=stage)
        nuitka_cache = cls._nuitka_cache_dir(cache_root, py_version)

        # Windows 编译环境：下载 python-build-standalone 完整发行版运行 nuitka
        # embed runtime python 不完整（无 .py 源码、_pth 限制 sys.path），Nuitka 的
        # reExecute 机制（os._exit 子进程 + scons 调用）会反复衍生 python.exe 子进程
        # 导致 CPU 卡死（Nuitka 官方文档称此为 Fork Bomb）。
        # standalone python 是完整 CPython，sys.executable 可被 nuitka/scons 安全调用。
        # Linux runtime 已是 standalone，返回空 Path 占位（compile_src 内部回退到 runtime python）。
        build_python_exe = cls._ensure_build_python(
            cache_root.parent / "python",
            py_version,
            target,
            stage=stage,
        )

        cls.compile_src(
            src_dir,
            runtime_dir,
            py_version,
            target,
            nuitka_cache,
            stage=stage,
            build_python_exe=build_python_exe,
            entry_rels=entry_rels,
        )

        # 编译后写 stamp（即使部分文件失败也写，避免下次重复尝试）
        stamp.parent.mkdir(parents=True, exist_ok=True)
        try:
            stamp.write_text(stamp_key, encoding="utf-8")
        except OSError as e:
            _logger.warning("写入 Nuitka stamp 失败: %s", e)
