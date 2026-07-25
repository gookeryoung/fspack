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
2. 检查 runtime python 是否已安装目标版本 nuitka（``import nuitka`` 成功）
3. 未安装则用构建机 ``pip install --target`` 从 sdist 构建并解压到 runtime site-packages

nuitka 4.x 在 PyPI 只发布 sdist（无预构建 wheel），且 sdist 构建出的 wheel 标签
与构建机 python ABI 绑定（如 ``cp313-cp313-win_amd64``），但实际内容是纯 Python
（无 ``.pyd``），跨 Python 版本可 ``import``。fspack 用构建机 python 执行
``pip install --target <runtime_site_packages>`` 让 pip 自动完成 sdist 下载、构建、
解压，绕过 :func:`download_wheels` 的 ``--only-binary=:all:`` 限制。

Nuitka 版本按目标 Python 版本锁定（:func:`nuitka_version_for`）：

- Python 3.8/3.9 → nuitka 2.5.1（4.x 已不再维护 EOL 的 3.8）
- Python 3.10+ → nuitka 4.1.3（当前最新稳定版）

stamp 缓存（:meth:`NuitkaCompiler.compile_with_stamp`）：重复构建时若
``dist/.nuitka_compile_stamp``（含 ``nuitka_version|py_version|src_fingerprint``）
匹配则跳过整个 Nuitka 阶段（含 ensure_env 与 compile_src），避免重复 subprocess
启动开销与编译耗时。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from fspack.config import MirrorConfig, nuitka_version_for
from fspack.exceptions import NuitkaError
from fspack.platform import Platform
from fspack.progress import StageRecorder

__all__ = ["NuitkaCompiler"]

_logger = logging.getLogger(__name__)


class NuitkaCompiler:
    """Nuitka 编译器：将用户源码编译为本机 ``.pyd``/``.so``.

    公共 API：

    - :meth:`is_available`：检查 runtime python 是否已安装 nuitka
    - :meth:`ensure_env`：检查 C 编译器并按目标 Python 版本安装锁定版 nuitka 到 runtime
    - :meth:`compile_src`：编译 ``dist/src`` 下所有 ``.py`` 为本机模块
    - :meth:`compile_with_stamp`：整合 ensure_env + stamp 缓存 + compile_src 的入口
    """

    @staticmethod
    def is_available(runtime_py: Path) -> bool:
        """检查 runtime python 是否已安装 nuitka 包.

        Args:
            runtime_py: runtime python 可执行文件路径（如 ``runtime/python.exe``）。

        Returns:
            已安装返回 ``True``，否则 ``False``。
        """
        if not runtime_py.is_file():
            return False
        result = subprocess.run(
            [str(runtime_py), "-c", "import nuitka"],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

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
    def _runtime_site_packages(runtime_dir: Path, py_version: str, target: Platform) -> Path:
        """解析 runtime site-packages 路径.

        Windows: ``runtime/Lib/site-packages``
        Linux: ``runtime/python/lib/python<major>.<minor>/site-packages``
        """
        if target is Platform.WINDOWS:
            return runtime_dir / "Lib" / "site-packages"
        major, minor = py_version.split(".")[:2]
        return runtime_dir / "python" / "lib" / f"python{major}.{minor}" / "site-packages"

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
    def _ensure_pip_available(python_exe: str) -> None:
        """检查构建机 python 是否有 pip 模块，无则 raise :class:`NuitkaError`.

        nuitka 4.x 在 PyPI 只发布 sdist，需要 pip 从 sdist 构建_wheel 再解压。
        fspack 自身用 uv 管理开发环境，uv venv 默认无 pip，需 ``uv pip install pip``
        （已在 CI workflow 配置）。本地开发机通常有系统 python + pip 在 PATH。
        """
        result = subprocess.run(
            [python_exe, "-c", "import pip"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise NuitkaError(
                f"构建机 python 缺 pip 模块: {python_exe}。"
                "nuitka 在 PyPI 只发布 sdist，需要 pip 从 sdist 构建。"
                "请用 `python -m ensurepip` 或 `uv pip install pip` 安装 pip"
            )

    @classmethod
    def ensure_env(
        cls,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        mirror: MirrorConfig,
        *,
        stage: StageRecorder,
    ) -> str:
        """确保 runtime python 已安装锁定版 nuitka，返回 nuitka 版本号.

        步骤：

        1. :meth:`_check_c_compiler` 检查 C 编译器，缺失 raise :class:`NuitkaError`
        2. 按 :func:`nuitka_version_for` 取锁定版本号
        3. ``import nuitka`` 检查 runtime python 是否已装该版本
        4. 未装则用构建机 ``pip install --target`` 从 sdist 构建并解压到 runtime site-packages
        5. 再次 ``import nuitka`` 验证安装成功

        nuitka 4.x 在 PyPI 只发布 sdist（无预构建 wheel），:func:`download_wheels` 的
        ``--only-binary=:all:`` 无法处理。改用 ``pip install --target <site-packages>``
        让 pip 自动完成 sdist 下载、构建、解压。nuitka 实际是纯 Python（无 ``.pyd``），
        构建机 python 版本与 runtime 不同也能 ``import``。

        Args:
            runtime_dir: runtime 根目录（含 ``python.exe`` 或 ``python/bin/``）。
            py_version: Python 完整版本号（如 ``3.11.9``）。
            target: 目标平台（决定 C 编译器检查与 site-packages 路径）。
            mirror: 镜像配置（提供 ``pypi_index``）。
            stage: 阶段记录器，回写缓存命中数与下载字节数。

        Returns:
            锁定的 Nuitka 版本号（如 ``4.1.3``）。

        Raises:
            NuitkaError: C 编译器缺失、构建机缺 pip、或安装后 ``import nuitka`` 仍失败。
        """
        cls._check_c_compiler(target)

        nuitka_ver = nuitka_version_for(py_version)
        py_exe = cls._runtime_python(runtime_dir, py_version, target)
        if not py_exe.is_file():
            raise NuitkaError(f"runtime python 未就绪: {py_exe}")

        # 已安装则跳过下载解压
        if cls.is_available(py_exe):
            _logger.info("nuitka %s 已就绪（runtime python 已安装）", nuitka_ver)
            stage.hit_cache()
            stage.set_detail(f"nuitka {nuitka_ver} 已就绪")
            return nuitka_ver

        # nuitka 4.x 在 PyPI 只发布 sdist，用构建机 pip install --target 从 sdist
        # 构建并解压到 runtime site-packages。nuitka 实际是纯 Python，跨版本可 import。
        build_python = sys.executable
        cls._ensure_pip_available(build_python)

        site_packages = cls._runtime_site_packages(runtime_dir, py_version, target)
        site_packages.mkdir(parents=True, exist_ok=True)

        # --no-compile: 不编译 .pyc（runtime python 版本可能与构建机不同）
        # --no-cache-dir: 不用 pip 缓存，避免污染
        # -i mirror.pypi_index: 用 fspack 镜像源
        cmd = [
            build_python,
            "-m",
            "pip",
            "install",
            "--target",
            str(site_packages),
            "--no-compile",
            "--no-cache-dir",
            "-i",
            mirror.pypi_index,
            f"nuitka=={nuitka_ver}",
        ]
        _logger.info("用构建机 pip 装 nuitka %s 到 %s", nuitka_ver, site_packages)
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise NuitkaError(f"pip install nuitka=={nuitka_ver} 失败:\n{result.stderr.strip()[:500]}")

        # 验证安装
        if not cls.is_available(py_exe):
            raise NuitkaError(f"nuitka 安装后 import nuitka 仍失败，请检查 runtime python: {py_exe}")
        stage.set_detail(f"nuitka {nuitka_ver} 安装完成")
        return nuitka_ver

    @classmethod
    def compile_src(
        cls,
        src_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        *,
        stage: StageRecorder,
    ) -> None:
        """编译 ``src_dir`` 下所有 ``.py`` 为 ``.pyd``/``.so``，编译后删除 ``.py`` 源码.

        步骤：

        1. 解析 runtime python 路径并检查 nuitka 可用性，不可用则告警并跳过
        2. 用 runtime python 调用 ``python -m nuitka --module`` 逐个编译 ``.py``
        3. 删除 ``.py`` 源码（保留 ``__init__.py`` 维持包标识，避免 PEP 420
           命名空间包导致 ``.pyd`` 不被识别为包成员）
        4. 清理 Nuitka 临时构建文件（``.build/`` 目录）

        单文件编译失败仅告警不中断，已成功编译的 ``.pyd`` 仍可用。``.py`` 删除
        策略与 :func:`fspack.builder._strip_py_sources` 一致：保留 ``__init__.py``
        维持包标识。

        Args:
            src_dir: 用户源码目录（``dist/src``）。
            runtime_dir: runtime 根目录（含 ``python.exe`` 或 ``python/bin/``）。
            py_version: Python 完整版本号（如 ``3.11.9``）。
            target: 目标平台（决定 runtime python 路径）。
            stage: 阶段记录器，记录编译项数与跳过数。
        """
        py_exe = cls._runtime_python(runtime_dir, py_version, target)
        if not py_exe.is_file():
            _logger.warning("Nuitka 编译跳过: runtime python 未就绪 %s", py_exe)
            stage.set_detail("runtime python 未就绪，跳过")
            return

        if not cls.is_available(py_exe):
            _logger.warning(
                "Nuitka 编译跳过: runtime python 未安装 nuitka，请用 '%s -m pip install nuitka' 安装",
                py_exe,
            )
            stage.set_detail("nuitka 未安装，跳过（回退到 .pyc 模式）")
            return

        py_files = sorted(src_dir.rglob("*.py"))
        if not py_files:
            stage.set_detail("无 .py 文件可编译")
            return

        # Nuitka 编译参数：
        # --module: 编译为可导入模块（.pyd/.so），不生成独立 exe
        # --output-dir: 输出目录与源码同目录（保持包结构）
        # --no-pyi-file: 不生成 .pyi 类型存根（运行时不需要）
        # --remove-output: 编译后删除临时构建文件（.build/ 目录）
        # --quiet: 静默模式，减少日志输出
        compiled = 0
        failed = 0
        for py_file in py_files:
            result = subprocess.run(
                [
                    str(py_exe),
                    "-m",
                    "nuitka",
                    "--module",
                    f"--output-dir={py_file.parent}",
                    "--no-pyi-file",
                    "--remove-output",
                    "--quiet",
                    str(py_file),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                compiled += 1
                stage.processed()
            else:
                failed += 1
                _logger.warning("Nuitka 编译失败 %s: %s", py_file, result.stderr.strip()[:200])

        # 删除非 __init__.py 的 .py 源码（保留包标识），与 pyc_strip 策略一致
        stripped = 0
        for py_file in py_files:
            if py_file.name == "__init__.py":
                continue
            try:
                py_file.unlink()
                stripped += 1
            except OSError as e:
                _logger.warning("删除 .py 失败 %s: %s", py_file, e)
        if stripped:
            stage.skip(stripped)

        # Nuitka 临时构建目录由 --remove-output 自动清理，无需额外处理

        if failed:
            stage.set_detail(f"编译 {compiled} 个，失败 {failed} 个，剥离 {stripped} 个 .py")
        else:
            stage.set_detail(f"编译 {compiled} 个，剥离 {stripped} 个 .py")

    @staticmethod
    def _stamp_path(dist_dir: Path) -> Path:
        """返回 Nuitka 编译 stamp 文件路径：``dist/.nuitka_compile_stamp``."""
        return dist_dir / ".nuitka_compile_stamp"

    @staticmethod
    def _stamp_key(src_dir: Path, nuitka_version: str, py_version: str) -> str:
        """计算 Nuitka 编译 stamp 键.

        三要素：

        - ``nuitka_version``：切换 Nuitka 版本时强制重编（如 3.10 从 4.1.3 升级到 4.2）
        - ``py_version``：切换 Python 版本时强制重编（.pyd ABI 绑定）
        - ``src_fingerprint``：用户源码变化时强制重编（按 ``rule-01`` 闭环要求）

        ``pyc_optimize`` 不纳入：Nuitka 编译不受 .pyc 优化级别影响，
        site-packages 的 .pyc 由 :func:`_precompile_pyc` 单独缓存。
        """
        from fspack.analyzer import source_fingerprint

        src_fp = source_fingerprint(src_dir) if src_dir.is_dir() else ""
        return f"{nuitka_version}|{py_version}|{src_fp}"

    @classmethod
    def compile_with_stamp(  # noqa: PLR0913
        cls,
        src_dir: Path,
        dist_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        mirror: MirrorConfig,
        *,
        stage: StageRecorder,
    ) -> None:
        """整合 ensure_env + stamp 缓存 + compile_src 的入口.

        重复构建时若 :meth:`_stamp_path` 文件内容与 :meth:`_stamp_key` 匹配，
        跳过整个 Nuitka 阶段（含 C 编译器检查、wheel 安装、源码编译），
        避免重复 subprocess 启动与编译耗时。

        首次构建或源码/版本变化时：

        1. :meth:`ensure_env` 检查 C 编译器并安装锁定版 nuitka 到 runtime
        2. :meth:`compile_src` 逐文件编译 ``.py`` 为 ``.pyd``
        3. 写入 stamp 文件供下次构建比对

        Args:
            src_dir: 用户源码目录（``dist/src``）。
            dist_dir: dist 根目录（stamp 文件写入位置）。
            runtime_dir: runtime 根目录（含 ``python.exe`` 或 ``python/bin/``）。
            py_version: Python 完整版本号（如 ``3.11.9``）。
            target: 目标平台。
            mirror: 镜像配置（提供 ``pypi_index`` 给 :meth:`ensure_env`）。
            stage: 阶段记录器。

        Raises:
            NuitkaError: C 编译器缺失，或 nuitka 安装失败。
        """
        nuitka_ver = nuitka_version_for(py_version)
        stamp = cls._stamp_path(dist_dir)
        stamp_key = cls._stamp_key(src_dir, nuitka_ver, py_version)

        # stamp 命中：跳过整个 Nuitka 阶段
        try:
            if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
                _logger.info("Nuitka stamp 命中，跳过编译")
                stage.hit_cache()
                stage.set_detail(f"stamp 命中，nuitka {nuitka_ver} 已编译")
                return
        except OSError:
            pass

        # 未命中：ensure_env + compile_src + 写 stamp
        cls.ensure_env(runtime_dir, py_version, target, mirror, stage=stage)
        cls.compile_src(src_dir, runtime_dir, py_version, target, stage=stage)

        # 编译后写 stamp（即使部分文件失败也写，避免下次重复尝试）
        stamp.parent.mkdir(parents=True, exist_ok=True)
        try:
            stamp.write_text(stamp_key, encoding="utf-8")
        except OSError as e:
            _logger.warning("写入 Nuitka stamp 失败: %s", e)
