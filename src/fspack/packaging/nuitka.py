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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fspack.config import MirrorConfig, nuitka_version_for
from fspack.exceptions import NuitkaError
from fspack.platform import Platform
from fspack.progress import StageRecorder

__all__ = ["NuitkaCompiler"]

_logger = logging.getLogger(__name__)


class NuitkaCompiler:
    """Nuitka 编译器：将用户源码编译为本机 ``.pyd``/``.so``.

    nuitka 装到本地缓存 ``~/.fspack/cache/nuitka/<py_version>/site-packages/``，
    不污染 ``dist/runtime`` 发行产物。编译时用 ``runtime/python.exe <bootstrap.py>``
    注入 ``sys.path`` 指向缓存目录，绕过 ``python3X._pth`` 对 ``PYTHONPATH`` 的限制。
    用临时脚本文件而非 ``-c``：Nuitka 的 ``reExecuteNuitka`` 无条件访问
    ``sys.modules["__main__"].__file__``，``-c`` 模式下该属性不存在会
    ``AttributeError``。

    公共 API：

    - :meth:`ensure_env`：检查 C 编译器并按目标 Python 版本安装锁定版 nuitka 到本地缓存
    - :meth:`compile_src`：编译 ``dist/src`` 下所有 ``.py`` 为本机模块
    - :meth:`compile_with_stamp`：整合 ensure_env + stamp 缓存 + compile_src 的入口
    """

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
    def compile_src(  # noqa: PLR0912, PLR0913
        cls,
        src_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        nuitka_cache: Path,
        *,
        stage: StageRecorder,
    ) -> None:
        """编译 ``src_dir`` 下所有 ``.py`` 为 ``.pyd``/``.so``，编译后删除 ``.py`` 源码.

        用 ``runtime/python.exe <bootstrap.py>`` 注入缓存路径调用 nuitka，绕过
        ``python3X._pth`` 对 ``PYTHONPATH`` 的限制（_pth 存在时 PYTHONPATH 不生效，
        但脚本模式仍读取 _pth 配置的 sys.path，运行时 ``sys.path.insert`` 可注入
        额外路径）。用临时脚本文件而非 ``-c``：Nuitka 的 ``reExecuteNuitka`` 无条件
        访问 ``sys.modules["__main__"].__file__``，``-c`` 模式下该属性不存在会
        ``AttributeError``。

        步骤：

        1. 解析 runtime python 路径并检查缓存目录有 nuitka，无则告警并跳过
        2. 创建临时 bootstrap 脚本注入 sys.path 调用 nuitka ``--module`` 逐个编译 ``.py``
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
            nuitka_cache: nuitka 缓存目录（含 ``nuitka/`` 包，由 :meth:`ensure_env` 安装）。
            stage: 阶段记录器，记录编译项数与跳过数。
        """
        py_exe = cls._runtime_python(runtime_dir, py_version, target)
        if not py_exe.is_file():
            _logger.warning("Nuitka 编译跳过: runtime python 未就绪 %s", py_exe)
            stage.set_detail("runtime python 未就绪，跳过")
            return

        if not cls._is_nuitka_cached(nuitka_cache):
            _logger.warning(
                "Nuitka 编译跳过: 缓存目录无 nuitka %s，请用 fsp b --nuitka 触发安装",
                nuitka_cache,
            )
            stage.set_detail("nuitka 未安装，跳过（回退到 .pyc 模式）")
            return

        py_files = sorted(src_dir.rglob("*.py"))
        if not py_files:
            stage.set_detail("无 .py 文件可编译")
            return

        # 用临时脚本文件启动 nuitka（不能用 -c）：
        # nuitka.utils.ReExecute.reExecuteNuitka 无条件访问 sys.modules["__main__"].__file__
        # 设置 NUITKA_BINARY_NAME，-c 模式下 __main__ 无 __file__ 会 AttributeError。
        # 临时脚本让 __main__.__file__ 指向脚本路径，reExecute 能正常工作。
        # sys.path.insert 注入缓存目录绕过 python3X._pth 对 PYTHONPATH 的限制。
        bootstrap_dir = Path(tempfile.mkdtemp(prefix="fspack_nuitka_"))
        bootstrap_script = bootstrap_dir / "_nuitka_bootstrap.py"
        bootstrap_script.write_text(
            f"import sys; sys.path.insert(0, r'{nuitka_cache}'); from nuitka.__main__ import main; main()",
            encoding="utf-8",
        )

        # Nuitka 编译参数（作为脚本参数传入，进入 sys.argv[1:]）：
        # --module: 编译为可导入模块（.pyd/.so），不生成独立 exe
        # --output-dir: 输出目录与源码同目录（保持包结构）
        # --no-pyi-file: 不生成 .pyi 类型存根（运行时不需要）
        # --remove-output: 编译后删除临时构建文件（.build/ 目录）
        # --quiet: 静默模式，减少日志输出
        compiled = 0
        failed = 0
        total = len(py_files)
        try:
            for idx, py_file in enumerate(py_files, 1):
                # 显示当前编译进度，避免多文件编译时长时间无输出被误认为卡死
                _logger.info("编译 [%d/%d] %s", idx, total, py_file.name)
                # stderr=None: nuitka 编译过程（C 编译/链接进度）实时输出到终端，
                # 避免单文件编译数十秒无输出被误认为卡死。stdout 捕获但 --quiet 模式下通常为空。
                result = subprocess.run(
                    [
                        str(py_exe),
                        str(bootstrap_script),
                        "--module",
                        f"--output-dir={py_file.parent}",
                        "--no-pyi-file",
                        "--remove-output",
                        "--quiet",
                        str(py_file),
                    ],
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=None,
                )
                if result.returncode == 0:
                    compiled += 1
                    stage.processed()
                else:
                    failed += 1
                    _logger.warning("Nuitka 编译失败 %s（退出码 %s），详见上方输出", py_file, result.returncode)
        finally:
            shutil.rmtree(bootstrap_dir, ignore_errors=True)

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
        cache_root: Path,
        *,
        stage: StageRecorder,
    ) -> None:
        """整合 ensure_env + stamp 缓存 + compile_src 的入口.

        重复构建时若 :meth:`_stamp_path` 文件内容与 :meth:`_stamp_key` 匹配，
        跳过整个 Nuitka 阶段（含 C 编译器检查、wheel 安装、源码编译），
        避免重复 subprocess 启动与编译耗时。

        首次构建或源码/版本变化时：

        1. :meth:`ensure_env` 检查 C 编译器并安装锁定版 nuitka 到本地缓存
        2. :meth:`compile_src` 逐文件编译 ``.py`` 为 ``.pyd``
        3. 写入 stamp 文件供下次构建比对

        Args:
            src_dir: 用户源码目录（``dist/src``）。
            dist_dir: dist 根目录（stamp 文件写入位置）。
            runtime_dir: runtime 根目录（含 ``python.exe`` 或 ``python/bin/``）。
            py_version: Python 完整版本号（如 ``3.11.9``）。
            target: 目标平台。
            mirror: 镜像配置（提供 ``pypi_index`` 给 :meth:`ensure_env`）。
            cache_root: nuitka 缓存根目录（如 ``~/.fspack/cache/nuitka``）。
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
        cls.ensure_env(cache_root, py_version, target, mirror, stage=stage)
        nuitka_cache = cls._nuitka_cache_dir(cache_root, py_version)
        cls.compile_src(src_dir, runtime_dir, py_version, target, nuitka_cache, stage=stage)

        # 编译后写 stamp（即使部分文件失败也写，避免下次重复尝试）
        stamp.parent.mkdir(parents=True, exist_ok=True)
        try:
            stamp.write_text(stamp_key, encoding="utf-8")
        except OSError as e:
            _logger.warning("写入 Nuitka stamp 失败: %s", e)
