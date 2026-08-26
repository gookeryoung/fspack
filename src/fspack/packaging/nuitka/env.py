"""Nuitka 环境就绪：C 编译器检查、nuitka 安装、pip 可用性、构建机编译环境变量.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的环境准备 mixin，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

职责边界：

- C 编译器检查（Windows mingw / Linux gcc）
- Windows winlibs-mingw 预填充编排（经 :mod:`fspack.packaging.nuitka.winlibs`
  的 :meth:`NuitkaWinlibs.ensure_winlibs_mingw`，全版本强制 winlibs 避免 zig 产物损坏）
- nuitka 锁定版本安装到本地缓存（``pip install --target`` 从 sdist 构建），
  优先复用 wheel 缓存目录（``<cache_root>/wheels``）下用户放置的
  ``Nuitka-<ver>.tar.gz`` 本地 sdist 归档（纯本地操作，离线模式同样适用，
  构建/运行依赖经 ``--find-links`` 从同目录解析）
- 构建机 pip 模块可用性检查与两轮自助安装（ensurepip / uv pip install pip）
- 构建机编译环境变量构建（``_build_compile_env``：Linux 设 ``CC``，
  Windows 重定向 ``NUITKA_CACHE_DIR_DOWNLOADS`` 到 fspack 缓存目录）
- nuitka 缓存目录推导与缓存命中检查

不涉及：standalone python 准备（见 :mod:`fspack.packaging.nuitka.standalone`）、
ccache 管理（见 :mod:`fspack.packaging.nuitka.ccache`）、
winlibs 下载解压实现（见 :mod:`fspack.packaging.nuitka.winlibs`）、
编译流程（见 :mod:`fspack.packaging.nuitka.compile`）、
验证逻辑（见 :mod:`fspack.packaging.nuitka.verify`）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.config import MirrorConfig, is_offline, nuitka_version_for, wheel_cache_dir
from fspack.config.versions import _split_t_suffix
from fspack.exceptions import NuitkaError
from fspack.platform import Platform
from fspack.progress import StageRecorder

if TYPE_CHECKING:
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")

# ``python -c "import pip"`` 快速检查超时：正常 <1s，留余量兜底解释器冷启动/杀软扫描
_PIP_CHECK_TIMEOUT = 60.0
# ensurepip / uv pip install pip 自助安装超时：需网络下载 pip wheel，5 分钟足够
_PIP_BOOTSTRAP_TIMEOUT = 300.0
# pip install --target nuitka 超时：sdist 下载 + 构建 + 解压在慢网络/慢机需数分钟，
# 无超时会使构建永久挂起（网络半开时 pip 可能不报错也不退出）
_NUITKA_INSTALL_TIMEOUT = 1800.0


class NuitkaEnv:
    """Nuitka 环境就绪 mixin：C 编译器、nuitka 安装、pip 可用性、编译环境变量.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。

    不提供 ``_ensure_build_python``（由 :class:`NuitkaStandalone` 提供）与
    ``_ensure_ccache``（由 :class:`NuitkaCcache` 提供），这两个方法的 stub 与
    真实实现均在对应 mixin 中。:class:`NuitkaCompile` 通过 MRO 派发调用。
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
    def _find_local_nuitka_sdist(nuitka_ver: str) -> Path | None:
        """在 wheel 缓存目录（``<cache_root>/wheels``）递归查找锁定版本的 nuitka sdist 归档.

        精确匹配文件名 ``nuitka-<ver>.tar.gz``（大小写不敏感：PyPI 官方 sdist 为
        ``Nuitka-<ver>.tar.gz``，部分镜像规范化为小写），版本不匹配的归档不识别
        （避免装错版本破坏版本锁定约束）。缓存根与任意子目录均扫描，与
        :meth:`NuitkaWinlibs._find_local_winlibs_archive` 的本地归档识别模式一致：
        用户手动放置（离线准备）或 ``pip download --no-binary`` 的产物均可命中。

        纯本地文件系统操作，离线模式同样适用。
        """
        wheels_dir = wheel_cache_dir()
        if not wheels_dir.is_dir():
            return None
        expected = f"nuitka-{nuitka_ver.lower()}.tar.gz"
        for sdist in sorted(wheels_dir.rglob("*.tar.gz")):
            if sdist.is_file() and sdist.name.lower() == expected:
                return sdist
        return None

    @staticmethod
    def _runtime_python(runtime_dir: Path, py_version: str, target: Platform) -> Path:
        """解析 runtime python 可执行文件路径.

        Windows: ``runtime/python.exe``
        Linux: ``runtime/python/bin/python<major>.<minor>`` 或 ``python<major>.<minor>t``
        """
        if target is Platform.WINDOWS:
            return runtime_dir / "python.exe"
        # free-threaded build 二进制名带 t 后缀（python3.13t）
        base, is_t = _split_t_suffix(py_version)
        major, minor = base.split(".")[:2]
        suffix = "t" if is_t else ""
        return runtime_dir / "python" / "bin" / f"python{major}.{minor}{suffix}"

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
    def _build_compile_env(target: Platform, ccache_exe: Path | None) -> dict[str, str]:
        """构建注入 Nuitka 子进程的环境变量：重定向缓存、Linux 设 ``CC``.

        **全平台缓存重定向**（``NUITKA_CACHE_DIR`` → ``<cache_root>/nuitka-work``）：
        Nuitka 默认把 clcache/scons-config 等编译中间缓存写到系统位置
        （Windows ``%LOCALAPPDATA%\\Nuitka\\Nuitka\\Cache``、Linux
        ``~/.cache/Nuitka``）。历史教训：系统位置可能沉淀其他工具/旧版本
        Nuitka 留下的污染条目，坏 clcache 缓存被反复命中导致 .pyd 大量
        损坏（returncode==0 但运行时访问违例）。全量重定向到 fspack 管理
        的干净目录，与系统缓存彻底隔离。

        **Windows**：不设 ``CC``/``CFLAGS``，仅重定向缓存：

        - ``CC``：Nuitka scons 在 Windows 上无条件拒绝外部 gcc（打印
          "Non downloaded winlibs-gcc ... is being ignored" 后忽略，仅信任
          自己下载缓存的 winlibs gcc），设置无效且产生噪音提示。清除宿主
          可能残留的 ``CC``/``CFLAGS``，让 scons 走下载缓存 fallback 到
          winlibs gcc。编译器选择：有 MSVC 时 scons 直接用 MSVC（优先级
          最高）；无 MSVC 时 py<3.13 默认即 winlibs，py>=3.13 由编译命令
          ``--mingw64 --experimental=force-mingw64`` 强制（zig 产物可能损坏
          不再使用）
        - ``CFLAGS``：scons 自设 ``_WIN32_WINNT``（Nuitka 4.1.3 无条件
          ``0x0601`` 即 Win7，2.5.1 mingw 分支 ``0x0501`` 更保守），fspack
          再注入同宏触发 "Inherited CFLAGS" 提示且值被覆盖，纯冗余已删除
          （Win7 兼容不受影响，见上两版本自设值）
        - ``NUITKA_CACHE_DIR``：编译中间缓存（clcache/scons-config 等）
          重定向到 ``<cache_root>/nuitka-work``，隔离系统位置的陈旧污染
        - ``NUITKA_CACHE_DIR_DOWNLOADS``：下载缓存（winlibs gcc/zig）单独
          指向 ``<cache_root>/nuitka-winlibs-mingw``（专属变量优先于
          ``NUITKA_CACHE_DIR``），与 :meth:`ensure_winlibs_mingw` 预填充
          布局一致，scons 检测 gcc.exe 已存在即缓存命中不下载

        **Linux**：重定向 ``NUITKA_CACHE_DIR``（同上）并始终设置 ``CC``
        指定 C 编译器。Nuitka 4.x 内置 zig 作为可选 C 编译器，默认交互式
        询问是否下载。即使用 ``--assume-yes-for-downloads`` 自动接受，
        离线时仍会等待下载超时。显式设置 ``CC`` 让 scons 直接用指定编译器，
        Nuitka 不会选择 zig，从根源上避免 zig 下载。:meth:`ensure_env`
        已校验 gcc 可用。

        - ccache 启用：``CC="ccache gcc"``，ccache 透明缓存编译结果
          （源码未变时直接返回 .o 缓存），并设 ``CCACHE_DIR`` 指定缓存目录
        - ccache 未启用：``CC="gcc"``，直接编译

        Windows 上 ccache 不生效（``CC`` 被 scons 忽略，编译走 Nuitka 下载的
        winlibs/zig 完整路径不经 ccache 包装），``ccache_exe`` 参数被忽略。
        """
        from fspack.packaging.loader import LINUX_GCC

        env = os.environ.copy()

        # 全平台：编译中间缓存重定向到 fspack 干净目录，隔离系统位置
        # （%LOCALAPPDATA%/~/.cache）可能沉淀的历史污染条目
        from fspack.config.cache import nuitka_work_cache_dir

        work_dir = nuitka_work_cache_dir()
        work_dir.mkdir(parents=True, exist_ok=True)
        env["NUITKA_CACHE_DIR"] = str(work_dir)
        _logger.info("Nuitka 编译缓存重定向到 %s", env["NUITKA_CACHE_DIR"])

        if target is Platform.WINDOWS:
            # Windows：CC 被 scons 无条件忽略（见 docstring），清除宿主可能残留的
            # CC/CFLAGS 避免噪音提示（"Non downloaded winlibs-gcc ... ignored" /
            # "Inherited CFLAGS ... variable"），编译器来源由 fspack 接管；
            # 下载缓存单独指向 winlibs 目录（专属变量优先于 NUITKA_CACHE_DIR），
            # 与 ensure_winlibs_mingw 预填充布局一致，scons 检测 gcc.exe 存在即直接使用
            from fspack.config.cache import nuitka_winlibs_cache_dir

            env.pop("CC", None)
            env.pop("CFLAGS", None)
            winlibs_dir = nuitka_winlibs_cache_dir()
            winlibs_dir.mkdir(parents=True, exist_ok=True)
            env["NUITKA_CACHE_DIR_DOWNLOADS"] = str(winlibs_dir)
            _logger.info("Nuitka 下载缓存重定向到 %s", env["NUITKA_CACHE_DIR_DOWNLOADS"])
            return env

        if ccache_exe is not None:
            # ccache 路径含空格（如 Windows 用户目录）时须引号包裹，
            # 否则 scons 解析 CC 会按空格切分导致编译器路径截断
            env["CC"] = f'"{ccache_exe}" {LINUX_GCC}'
            # ccache 缓存目录：默认 ~/.cache/ccache，显式指定到 fspack 缓存根便于管理
            from fspack.config.cache import cache_root

            ccache_dir = cache_root() / "ccache-cache"
            ccache_dir.mkdir(parents=True, exist_ok=True)
            env["CCACHE_DIR"] = str(ccache_dir)
            _logger.info("启用 ccache: CC=%s, CCACHE_DIR=%s", env["CC"], env["CCACHE_DIR"])
        else:
            env["CC"] = LINUX_GCC
            _logger.info("使用系统 C 编译器: CC=%s", env["CC"])

        return env

    @staticmethod
    def _has_pip(python_exe: str) -> bool:
        """检查 python 是否有 pip 模块（``import pip`` 成功）.

        超时（:data:`_PIP_CHECK_TIMEOUT`）按无 pip 处理：网络盘/损坏解释器
        卡死时中断探测，交由调用方进入自助安装或报错，不永久挂起。
        """
        try:
            result = subprocess.run(
                [python_exe, "-c", "import pip"],
                check=False,
                capture_output=True,
                timeout=_PIP_CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _logger.warning("检查 pip 超时（%ds），按无 pip 处理: %s", int(_PIP_CHECK_TIMEOUT), python_exe)
            return False
        return result.returncode == 0

    @staticmethod
    def _try_ensurepip(python_exe: str) -> bool:
        """第一轮自救：``python -m ensurepip --default-pip`` 安装 pip.

        标准库自带 ensurepip 模块，但 uv 创建的 venv 是精简 venv，可能不含
        ensurepip 模块（uv 用 Rust 实现的 ``uv pip`` 替代 pip）。失败时返回
        False，由调用方进入第二轮自救。
        """
        _logger.info("尝试 ensurepip 自助安装 pip: %s", python_exe)
        try:
            result = subprocess.run(
                [python_exe, "-m", "ensurepip", "--default-pip"],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PIP_BOOTSTRAP_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _logger.warning("ensurepip 超时（%ds），按失败处理", int(_PIP_BOOTSTRAP_TIMEOUT))
            return False
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
        try:
            result = subprocess.run(
                ["uv", "pip", "install", "pip"],
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PIP_BOOTSTRAP_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            _logger.warning("uv pip install pip 超时（%ds），按失败处理", int(_PIP_BOOTSTRAP_TIMEOUT))
            return False
        if result.returncode != 0:
            _logger.warning("uv pip install pip 失败: %s", result.stderr.strip()[:200])
        return result.returncode == 0

    @classmethod
    def _ensure_pip_available(cls: type[NuitkaCompilerProtocol], python_exe: str) -> None:
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

    @staticmethod
    def _pip_install_nuitka(
        build_python: str,
        cache_dir: Path,
        mirror: MirrorConfig,
        requirement: str,
        *,
        from_local_sdist: bool = False,
    ) -> None:
        """用构建机 ``pip install --target`` 安装 nuitka 到 cache_dir.

        ``requirement`` 两种形态：

        - ``nuitka==<ver>``：从镜像索引在线解析（nuitka 4.x 在 PyPI 只发布
          sdist，pip 自动下载 sdist 构建 wheel 再解压到 ``--target``）
        - 本地 sdist 归档路径（``<cache_root>/wheels`` 下识别的
          ``Nuitka-<ver>.tar.gz``，``from_local_sdist=True``）：pip 从本地归档
          构建，追加 ``--find-links <wheels>`` 使构建依赖（setuptools/wheel）与
          运行依赖（ordered-set/zstandard）优先从 wheel 缓存目录解析；离线
          模式换 ``--no-index`` 纯本地解析（pip 的构建隔离环境同样支持从
          find-links 取构建依赖）

        其余参数与原内联实现一致：``--no-compile`` 不编译 .pyc（缓存跨版本
        复用）、``--no-cache-dir`` 不污染 pip 缓存、超时防网络半开永久挂起。

        Raises:
            NuitkaError: pip 超时（网络半开挂起）或非零退出码。
        """
        # --no-compile: 不编译 .pyc（缓存可能跨 Python 版本复用）
        # --no-cache-dir: 不用 pip 缓存，避免污染
        cmd = [
            build_python,
            "-m",
            "pip",
            "install",
            "--target",
            str(cache_dir),
            "--no-compile",
            "--no-cache-dir",
        ]
        if from_local_sdist:
            if is_offline():
                # 离线：禁用索引，构建/运行依赖全部经 --find-links 从本地取
                cmd.append("--no-index")
            else:
                cmd.extend(["-i", mirror.pypi_index])
            cmd.extend(["--find-links", str(wheel_cache_dir())])
        else:
            # -i mirror.pypi_index: 用 fspack 镜像源
            cmd.extend(["-i", mirror.pypi_index])
        cmd.append(requirement)

        _logger.info("用构建机 pip 装 nuitka 到缓存 %s: %s", cache_dir, requirement)
        # stderr=None: pip 进度（Collecting/Downloading/Building wheel/Installing）实时
        # 输出到终端，避免 sdist 构建数分钟无输出被误认为卡死。stdout 捕获但 pip install
        # 的 stdout 通常为空（成功信息走 stderr），保留以备诊断。
        # timeout: 网络半开/挂起时 pip 可能既不报错也不退出，无超时会使构建永久挂起
        try:
            result = subprocess.run(
                cmd,
                check=False,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=None,
                timeout=_NUITKA_INSTALL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise NuitkaError(
                f"pip install {requirement} 超时（{int(_NUITKA_INSTALL_TIMEOUT)}s），"
                "请检查网络后重试，或手动执行上述命令确认可完成"
            ) from None
        if result.returncode != 0:
            raise NuitkaError(f"pip install {requirement} 失败（退出码 {result.returncode}），详见上方 pip 输出")

    @classmethod
    def _verify_nuitka_installed(cls: type[NuitkaCompilerProtocol], cache_dir: Path) -> None:
        """验证安装后缓存目录有 nuitka 包，缺失 raise :class:`NuitkaError`.

        经 ``cls`` 调用 ``_is_nuitka_cached``（保持测试可经
        ``NuitkaCompiler._is_nuitka_cached`` monkeypatch 拦截）。
        """
        if not cls._is_nuitka_cached(cache_dir):
            raise NuitkaError(f"nuitka 安装后缓存目录仍无 nuitka 包: {cache_dir}")

    @classmethod
    def ensure_env(  # noqa: PLR0912, PLR0913
        cls: type[NuitkaCompilerProtocol],
        cache_root: Path,
        py_version: str,
        target: Platform,
        mirror: MirrorConfig,
        *,
        stage: StageRecorder,
        compiler: str = "auto",
    ) -> str:
        """确保本地缓存已装锁定版 nuitka，返回 nuitka 版本号.

        nuitka 装到 ``cache_root / py_version / site-packages``，不污染
        ``dist/runtime``。重复构建时缓存命中直接返回，无需重装。

        步骤：

        1. :meth:`_check_c_compiler` 检查 C 编译器，缺失 raise :class:`NuitkaError`
        2. Windows：:meth:`NuitkaWinlibs.ensure_winlibs_mingw` 预填充 winlibs gcc
           到 ``<cache_root>/nuitka-winlibs-mingw``（全版本：py<3.13 scons 默认
           走 winlibs，py>=3.13 由编译命令 ``--mingw64`` 强制走 winlibs），
           scons 编译时缓存命中不下载。``compiler="mingw"`` 时无视 MSVC
           恒预填充（编译命令 ``--mingw64`` 顶掉 MSVC，scons 需 winlibs 缓存）；
           ``compiler="msvc"`` 时跳过预填充（MSVC 缺失由构建入口
           ``_normalize_exclusive_options`` fail-fast）
        3. 按 :func:`nuitka_version_for` 取锁定版本号
        4. :meth:`_is_nuitka_cached` 检查缓存目录是否已有 nuitka
        5. 无则 :meth:`_find_local_nuitka_sdist` 识别 wheel 缓存目录
           （``<cache_root>/wheels``）下的本地 sdist 归档（``Nuitka-<ver>.tar.gz``），
           命中则 ``pip install --target`` 从本地归档安装（``--find-links`` 解析
           构建/运行依赖，离线模式 ``--no-index`` 纯本地）；安装失败时在线模式
           回退索引安装（归档保留不删——用户显式放置的资产，与 winlibs 下载
           中断残留的处置不同），离线模式直接抛出
        6. 本地归档未命中时用构建机 ``pip install --target`` 从索引下载 sdist
           构建并解压到缓存目录
        7. :meth:`_verify_nuitka_installed` 再次验证安装成功

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
            compiler: Windows Nuitka 编译器选择（``auto``/``msvc``/``mingw``，
                仅 Windows 目标生效；非 Windows 目标显式指定非 auto 值时告警忽略）。

        Returns:
            锁定的 Nuitka 版本号（如 ``4.1.3``）。

        Raises:
            NuitkaError: C 编译器缺失、构建机缺 pip、或安装后缓存目录仍无 nuitka。
        """
        cls._check_c_compiler(target)

        if compiler != "auto" and target is not Platform.WINDOWS:
            # compiler 仅影响 Windows scons 编译器选择；Linux/macOS 用系统
            # gcc/clang，显式指定时告警（不中断，Nuitka 阶段照常）
            _logger.warning("compiler=%s 仅对 Windows Nuitka 编译生效，当前目标平台忽略", compiler)

        # Windows 预填充 winlibs gcc 到 fspack 缓存目录：py<3.13 时
        # Nuitka scons 默认 fallback 到 winlibs（缓存命中不下载）；py>=3.13
        # 默认 fallback 到 zig（其编译的 .pyd 可能损坏），编译命令已追加
        # --mingw64 --experimental=force-mingw64 强制走 winlibs（见 progress
        # 的 _compile_files），此处预填充与编译命令两层须一致。
        # compiler="mingw"：无视 MSVC 恒预填充（编译命令 --mingw64 顶掉
        #   MSVC，scons 需 winlibs 缓存命中）；
        # compiler="msvc"：跳过预填充（MSVC 缺失由构建入口
        #   _normalize_exclusive_options fail-fast，此处不再重复报错）；
        # compiler="auto"：MSVC 机器跳过预填充——scons 编译器选择优先级
        #   MSVC > winlibs > zig，装了 Visual Studio C++ 工具链时直接用
        #   MSVC，winlibs 预填充（~200MB 下载）纯浪费；force flag 判断
        #   （needs_force_mingw64）同样跳过 MSVC 机器，两层条件保持一致
        if target is Platform.WINDOWS:
            from fspack.packaging.nuitka.winlibs import msvc_available

            if compiler == "mingw":
                if msvc_available():
                    _logger.info("compiler=mingw：强制 winlibs gcc（--mingw64 顶掉 MSVC），预填充工具链")
                cls.ensure_winlibs_mingw(py_version, stage)
            elif compiler == "msvc":
                _logger.info("compiler=msvc：强制使用 MSVC，跳过 winlibs 预填充")
            elif msvc_available():
                _logger.info("检测到 MSVC（Visual Studio C++ 工具链），Nuitka 将优先使用 MSVC，跳过 winlibs 预填充")
            else:
                cls.ensure_winlibs_mingw(py_version, stage)

        nuitka_ver = nuitka_version_for(py_version)
        cache_dir = cls._nuitka_cache_dir(cache_root, py_version)

        # 缓存命中：已装则跳过下载解压
        if cls._is_nuitka_cached(cache_dir):
            _logger.info("nuitka %s 已就绪（缓存命中 %s）", nuitka_ver, cache_dir)
            stage.hit_cache()
            stage.set_detail(f"nuitka {nuitka_ver} 已就绪")
            return nuitka_ver

        # 本地 sdist 归档识别：wheel 缓存目录下用户放置（离线准备）或
        # pip download 产物的 Nuitka-<ver>.tar.gz，纯本地安装优先于网络下载
        local_sdist = cls._find_local_nuitka_sdist(nuitka_ver)

        # 离线模式 fail-fast：nuitka 在 PyPI 只发布 sdist，缓存未命中且无本地
        # 归档时无法离线构建（有本地归档时继续走本地安装，纯本地操作离线可用）
        if local_sdist is None and is_offline():
            raise NuitkaError(
                f"离线模式下 nuitka 缓存未命中: nuitka=={nuitka_ver}，"
                f"请预先 `pip install --target {cache_dir} nuitka=={nuitka_ver}` 安装到缓存目录，"
                f"或将 Nuitka-{nuitka_ver}.tar.gz 及其依赖 wheel（setuptools/wheel/ordered-set 等）"
                f"放入 {wheel_cache_dir()}，"
                f"或取消 FSPACK_OFFLINE 环境变量"
            )

        # nuitka 4.x 在 PyPI 只发布 sdist，用构建机 pip install --target 从 sdist
        # 构建并解压到本地缓存（不污染 dist/runtime）。nuitka 是纯 Python，跨版本可 import。
        build_python = sys.executable
        cls._ensure_pip_available(build_python)

        cache_dir.mkdir(parents=True, exist_ok=True)

        if local_sdist is not None:
            try:
                cls._pip_install_nuitka(build_python, cache_dir, mirror, str(local_sdist), from_local_sdist=True)
            except NuitkaError:
                # 本地归档安装失败（归档损坏/依赖缺失等）：归档保留不删（用户显式
                # 放置的资产），在线模式回退索引安装，离线模式无法回退直接抛出
                if is_offline():
                    raise
                _logger.warning("从本地 sdist 安装 nuitka 失败，回退索引安装: %s", local_sdist)
            else:
                cls._verify_nuitka_installed(cache_dir)
                stage.set_detail(f"nuitka {nuitka_ver} 从本地 sdist 安装完成")
                return nuitka_ver

        cls._pip_install_nuitka(build_python, cache_dir, mirror, f"nuitka=={nuitka_ver}")

        # 验证安装：检查缓存目录有 nuitka 包
        cls._verify_nuitka_installed(cache_dir)
        stage.set_detail(f"nuitka {nuitka_ver} 安装完成")
        return nuitka_ver
