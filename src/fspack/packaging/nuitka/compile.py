"""Nuitka 编译流程：单文件 ``--module`` 编译、stamp 缓存.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的编译 mixin，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

职责边界：

- 流式 subprocess 输出（``_stream_compile`` 实时显示 nuitka INFO 与 gcc 调用）
- 单文件编译（``_compile_files`` 串行调 nuitka ``--module``，心跳线程防误判卡死）
- stamp 缓存（``compile_with_stamp`` 整合 env + compile_src + stamp 比对）
- 第三方包编译（``compile_packages`` 编译 site-packages 中指定包）

不涉及：环境就绪（见 :mod:`fspack.packaging.nuitka.env`）、
产物剥离与构建目录清理（见 :mod:`fspack.packaging.nuitka.strip`）、
验证逻辑（见 :mod:`fspack.packaging.nuitka.verify`，通过 ``cls._verify_compiled_modules``
经 MRO 调用）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, TextIO

from fspack.config import MirrorConfig, nuitka_version_for
from fspack.exceptions import NuitkaError
from fspack.platform import Platform
from fspack.progress import StageRecorder

if TYPE_CHECKING:
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")

# 心跳间隔：nuitka reExecute 机制导致子进程输出不可靠，每 N 秒输出编译耗时让用户看到进度
_HEARTBEAT_INTERVAL = 10.0

# stdout/stderr 累积上限：Nuitka 编译输出（gcc 调用、reExecute 日志）可达 10MB+，
# 16MB 上限足以容纳正常输出供失败诊断。超过后停止累积（继续写终端实时显示），
# 避免大型项目（数百 .py 文件）累积输出导致内存膨胀。
_STREAM_ACCUM_LIMIT = 16 * 1024 * 1024


class NuitkaCompile:
    """Nuitka 编译流程 mixin：单文件编译、stamp 缓存.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。

    跨 mixin 调用（``cls.<method>()``）通过 :class:`NuitkaCompilerProtocol`
    类型契约声明，pyrefly 据此解析方法签名，无需 stub 方法占位。运行时由
    :class:`NuitkaCompiler` MRO 链派发到对应 mixin 的真实实现。

    依赖 :class:`fspack.packaging.nuitka.env.NuitkaEnv` 提供：
    ``_runtime_python`` / ``_is_nuitka_cached`` /
    ``_build_compile_env`` / ``_resolve_jobs`` / ``ensure_env`` /
    ``_nuitka_cache_dir``。

    依赖 :class:`fspack.packaging.nuitka.standalone.NuitkaStandalone` 提供：
    ``_ensure_build_python``。

    依赖 :class:`fspack.packaging.nuitka.ccache.NuitkaCcache` 提供：
    ``_ensure_ccache``。

    依赖 :class:`fspack.packaging.nuitka.strip.NuitkaStrip` 提供：
    ``_strip_compiled_sources`` / ``_cleanup_build_dirs``（经 MRO 派发）。
    """

    @staticmethod
    def _stream_compile(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        """运行 nuitka 编译命令，实时流式输出 stdout/stderr 到终端.

        用 ``Popen`` + 两个守护线程通过 ``os.read`` 读取 stdout/stderr 文件描述符
        字节块并实时写入 ``sys.stdout``/``sys.stderr``，支持 nuitka 的 ``Nuitka:INFO``
        步骤输出和 C 编译器调用过程实时显示，避免单文件编译数十秒无输出被误认为卡死。

        ``env`` 为 None 时继承当前进程环境；非 None 时替换环境（用于注入
        ``CC="ccache gcc"`` 让 scons 通过 ccache 调用 gcc，加速重复编译）。

        同时累积 stdout/stderr 内容供失败时诊断（当前仅返回未使用，保留以备扩展）。

        **内存保护**：stdout/stderr 累积上限 :data:`_STREAM_ACCUM_LIMIT`（16MB），
        超过后停止累积（继续写终端实时显示），避免大型项目（数百 .py 文件）
        累积 Nuitka 编译输出（gcc 调用、reExecute 日志可达 10MB+/文件）导致内存膨胀。

        参考 :func:`fspack.packaging.wheels._stream_subprocess` 的实现模式，区别在于
        nuitka 的 INFO 输出可能走 stdout 或 stderr，需同时流式两者。
        """
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        # 用 list[int] 容器而非 nonlocal：两个 drain 线程分别追踪各自累计量，
        # nonlocal 需为每个流写独立闭包，list 容器更简洁且线程安全（GIL 保护单元素赋值）。
        stdout_total: list[int] = [0]
        stderr_total: list[int] = [0]

        def _drain(stream: IO[bytes] | None, chunks: list[bytes], out: TextIO, total_ref: list[int]) -> None:
            assert stream is not None
            fd = stream.fileno()
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                # 累积上限保护：超过 _STREAM_ACCUM_LIMIT 后仅写终端不再累积，
                # 避免 Nuitka 编译输出（gcc 调用日志）累积导致内存膨胀。
                if total_ref[0] < _STREAM_ACCUM_LIMIT:
                    chunks.append(chunk)
                    total_ref[0] += len(chunk)
                out.buffer.write(chunk)
                out.buffer.flush()

        t_out = threading.Thread(
            target=_drain, args=(process.stdout, stdout_chunks, sys.stdout, stdout_total), daemon=True
        )
        t_err = threading.Thread(
            target=_drain, args=(process.stderr, stderr_chunks, sys.stderr, stderr_total), daemon=True
        )
        t_out.start()
        t_err.start()
        returncode = process.wait()
        t_out.join()
        t_err.join()
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return returncode, stdout, stderr

    @classmethod
    def compile_src(  # noqa: PLR0913
        cls: type[NuitkaCompilerProtocol],
        src_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        nuitka_cache: Path,
        *,
        stage: StageRecorder,
        build_python_exe: Path | None = None,
        entry_rels: frozenset[str] | None = None,
        ccache: bool = False,
        cache_root: Path | None = None,
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
            ccache: 启用 ccache 缓存加速重复编译。True 时调 :meth:`_ensure_ccache`
                下载 ccache 到本地缓存，编译时设置 ``CC="ccache gcc"`` 注入子进程。
            cache_root: ccache 缓存根目录（``~/.fspack/cache/nuitka``），用于推导
                ccache 下载目录。None 时 ccache 无效。
        """
        py_exe = cls._resolve_compile_python(build_python_exe, runtime_dir, py_version, target, stage)
        if py_exe is None:
            return

        if not cls._is_nuitka_cached(nuitka_cache):  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
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

        # ccache 就绪：优先系统 PATH，缺失则下载到 ~/.fspack/cache/ccache/
        ccache_exe = None
        if ccache and cache_root is not None:
            ccache_exe = cls._ensure_ccache(
                cache_root, target, stage
            )  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）

        bootstrap_script = cls._create_bootstrap_script(nuitka_cache)
        try:
            compiled_files, failed = cls._compile_files(
                py_exe, bootstrap_script, py_files, stage, target=target, ccache_exe=ccache_exe
            )
        finally:
            shutil.rmtree(bootstrap_script.parent, ignore_errors=True)

        # 验证 .pyd 可加载才删除 .py：Nuitka 4.x 在 Python 3.13+ Windows 上忽略 CC
        # 环境变量自动回退到 zig 编译器，zig 编译的 .pyd 可能损坏（运行时访问违例）。
        # 用 runtime python（.pyd ABI 绑定 runtime）批量 import 验证，损坏的 .pyd
        # 删除产物保留 .py，回退到 .pyc 加载。
        runtime_py_exe = cls._runtime_python(
            runtime_dir, py_version, target
        )  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
        verify_py_exe = runtime_py_exe if runtime_py_exe.is_file() else None
        stripped = cls._strip_compiled_sources(
            compiled_files,
            stage,
            verify_py_exe=verify_py_exe,
            verify_search_root=src_dir if verify_py_exe is not None else None,
        )
        # 清理 Nuitka 编译失败的 .build 残留目录（--remove-output 仅成功时清理）
        cls._cleanup_build_dirs(src_dir)
        compiled = len(compiled_files)
        if failed:
            stage.set_detail(f"编译 {compiled} 个，失败 {failed} 个，剥离 {stripped} 个 .py")
        else:
            stage.set_detail(f"编译 {compiled} 个，剥离 {stripped} 个 .py")

    @classmethod
    def compile_packages(  # noqa: PLR0913, PLR0912
        cls: type[NuitkaCompilerProtocol],
        site_packages: Path,
        packages: tuple[str, ...],
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        nuitka_cache: Path,
        *,
        stage: StageRecorder,
        build_python_exe: Path | None = None,
        ccache: bool = False,
        cache_root: Path | None = None,
    ) -> None:
        """编译 ``site-packages`` 中指定的第三方包为 ``.pyd``/``.so``.

        用户通过 ``[tool.fspack] nuitka_packages = ["rich", "click"]`` 或 CLI
        ``--nuitka-pkg <name>`` 手动指定需编译的包名。编译成功后删除 ``.py``
        （``.pyd`` 优先级高于 ``.pyc``，自动加载本机代码），失败保留 ``.py``
        回退到 ``.pyc``。

        与 :meth:`compile_src` 区别：

        - :meth:`compile_src` 编译用户源码（``dist/src``），必须编译
        - 本方法编译第三方依赖（``site-packages``），用户可选
        - 两者复用 :meth:`_compile_files` 单文件编译机制与 :meth:`_strip_compiled_sources`

        **风险提示**：动态导入（``importlib.import_module``）、元编程（装饰器栈、
        ``__init_subclass__``）的包可能不兼容，风险由用户承担。C 扩展包（如 numpy）
        编译无收益（核心已是 ``.pyd``），不建议指定。

        Args:
            site_packages: site-packages 目录路径。
            packages: 待编译的包名元组（已去重）。
            runtime_dir: runtime 根目录（用于回退解析编译 python）。
            py_version: Python 完整版本号。
            target: 目标平台。
            nuitka_cache: nuitka 缓存目录。
            stage: 阶段记录器。
            build_python_exe: standalone python 路径。
            ccache: 启用 ccache 缓存。
            cache_root: ccache 缓存根目录。
        """
        if not packages:
            return

        py_exe = cls._resolve_compile_python(build_python_exe, runtime_dir, py_version, target, stage)
        if py_exe is None:
            return

        if not cls._is_nuitka_cached(nuitka_cache):  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
            _logger.warning("Nuitka 包编译跳过: 缓存目录无 nuitka %s", nuitka_cache)
            return

        # 收集所有指定包的 .py 文件
        py_files: list[Path] = []
        missing: list[str] = []
        for pkg in packages:
            pkg_dir = site_packages / pkg
            if not pkg_dir.is_dir():
                missing.append(pkg)
                continue
            py_files.extend(cls._collect_py_files(pkg_dir, None))

        if missing:
            _logger.warning("未找到包目录，跳过编译: %s", ", ".join(missing))

        if not py_files:
            _logger.info("Nuitka 包编译: 无 .py 文件可编译（packages=%s）", packages)
            return

        _logger.info("Nuitka 包编译: %d 个 .py 文件（packages=%s）", len(py_files), packages)

        # ccache 就绪
        ccache_exe = None
        if ccache and cache_root is not None:
            ccache_exe = cls._ensure_ccache(
                cache_root, target, stage
            )  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）

        bootstrap_script = cls._create_bootstrap_script(nuitka_cache)
        try:
            compiled_files, failed = cls._compile_files(
                py_exe, bootstrap_script, py_files, stage, target=target, ccache_exe=ccache_exe
            )
        finally:
            shutil.rmtree(bootstrap_script.parent, ignore_errors=True)

        # 验证 .pyd 可加载才删除 .py：Nuitka 4.x 在 Python 3.13+ Windows 上忽略 CC
        # 环境变量自动回退到 zig 编译器，zig 编译的 .pyd 可能损坏（运行时访问违例）。
        # 用 runtime python（.pyd ABI 绑定 runtime）批量 import 验证，损坏的 .pyd
        # 删除产物保留 .py，回退到 .pyc 加载。
        runtime_py_exe = cls._runtime_python(
            runtime_dir, py_version, target
        )  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
        verify_py_exe = runtime_py_exe if runtime_py_exe.is_file() else None
        stripped = cls._strip_compiled_sources(
            compiled_files,
            stage,
            verify_py_exe=verify_py_exe,
            verify_search_root=site_packages if verify_py_exe is not None else None,
        )
        # 清理 Nuitka 编译失败的 .build 残留目录（--remove-output 仅成功时清理）
        for pkg in packages:
            pkg_dir = site_packages / pkg
            if pkg_dir.is_dir():
                cls._cleanup_build_dirs(pkg_dir)
        compiled = len(compiled_files)
        if failed:
            _logger.warning("Nuitka 包编译完成: 成功 %d 个，失败 %d 个，剥离 %d 个 .py", compiled, failed, stripped)
        else:
            _logger.info("Nuitka 包编译完成: 成功 %d 个，剥离 %d 个 .py", compiled, stripped)

    @classmethod
    def _resolve_compile_python(
        cls: type[NuitkaCompilerProtocol],
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
        py_exe = cls._runtime_python(
            runtime_dir, py_version, target
        )  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
        if not py_exe.is_file():
            _logger.warning("Nuitka 编译跳过: runtime python 未就绪 %s", py_exe)
            stage.set_detail("runtime python 未就绪，跳过")
            return None
        return py_exe

    @staticmethod
    def _collect_py_files(src_dir: Path, entry_rels: frozenset[str] | None) -> list[Path]:
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
    def _compile_files(  # noqa: PLR0913
        cls: type[NuitkaCompilerProtocol],
        py_exe: Path,
        bootstrap_script: Path,
        py_files: list[Path],
        stage: StageRecorder,
        *,
        target: Platform,
        ccache_exe: Path | None = None,
    ) -> tuple[set[Path], int]:
        """逐个编译 .py 文件，返回 (成功编译的文件集合, 失败数).

        Nuitka 编译参数（作为脚本参数传入，进入 ``sys.argv[1:]``）：

        - ``--module``：编译为可导入模块（.pyd/.so），不生成独立 exe
        - ``--output-dir``：输出目录与源码同目录（保持包结构）
        - ``--no-pyi-file``：不生成 .pyi 类型存根（运行时不需要）
        - ``--remove-output``：编译后删除临时构建文件（.build/ 目录）
        - ``--jobs=N``：C 编译并行度，N = :meth:`_resolve_jobs`（CPU 核心数）。
          串行编译每个 .py 文件（一次一个 nuitka 进程），单进程内 N 个 gcc 并行，
          无多进程膨胀风险。

        ``ccache_exe`` 非 None 时，设置 ``CC="ccache <compiler>"`` 环境变量注入子进程，
        scons 通过 ccache 调用 gcc，缓存 C 编译结果加速重复编译。

        不需要 ``--python-for-scons``：已用 standalone python（完整环境）运行 nuitka，
        scons 自动继承 ``sys.executable``，无需另指定。
        注意：nuitka 4.x 的 ``--show-progress`` 已 obsolete 无效；nuitka 的 reExecute 机制
        (os._exit 退出子进程 A，Windows close_fds=True 导致子进程 B 不继承 PIPE) 使得
        _stream_compile 的 PIPE 捕获不可靠。用心跳线程保证用户看到编译进度。
        """
        # 构建编译环境变量：CC="<compiler>" 或 CC="ccache <compiler>"（启用 ccache 时）
        # 始终设置 CC 指定 C 编译器，避免 Nuitka 4.x 选择 zig 触发交互式下载
        compile_env = cls._build_compile_env(target, ccache_exe)  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
        jobs = cls._resolve_jobs()  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
        compiled_files: set[Path] = set()
        failed = 0
        total = len(py_files)
        # 记录成功编译的文件：仅这些 .py 可安全删除（.pyd 已生成）。
        # 失败的 .py 保留，让运行时回退到 .pyc 加载，避免编译失败导致 dist/src 无可用代码。
        for idx, py_file in enumerate(py_files, 1):
            _logger.info("编译 [%d/%d] %s", idx, total, py_file.name)
            # 心跳线程：每 10 秒输出编译耗时与当前文件名，避免单文件编译数十秒
            # 无输出被误认为卡死。nuitka reExecute 的子进程 B 输出可能不到 PIPE，
            # 心跳是唯一的进度反馈。显示文件名让用户知道哪个文件正在编译。
            stop_heartbeat = threading.Event()
            start_ts = time.monotonic()
            file_label = py_file.name

            def _heartbeat(
                _stop: threading.Event = stop_heartbeat, _start: float = start_ts, _label: str = file_label
            ) -> None:
                while not _stop.wait(_HEARTBEAT_INTERVAL):
                    elapsed = int(time.monotonic() - _start)
                    _logger.info("Nuitka 编译中 %s... 已耗时 %ds", _label, elapsed)

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
                        # --assume-yes-for-downloads：Nuitka 4.x 内置 zig 作为可选 C 编译器，
                        # 默认交互式询问 "Is it OK to download and put it in local user cache"。
                        # 自动接受避免阻塞构建（zig 缓存到 ~/.cache/nuitka 或 %APPDATA%/Nuitka）。
                        "--assume-yes-for-downloads",
                        # --jobs=N：必须用 = 形式传参。Nuitka 4.x 的 argparse 配置要求
                        # --jobs=N 格式，用空格分隔（"--jobs", "N"）会报错：
                        # "The '--jobs' option requires an argument with '--jobs='."
                        f"--jobs={jobs}",
                        str(py_file),
                    ],
                    env=compile_env,
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
    def _stamp_path(dist_dir: Path) -> Path:
        """返回 Nuitka 编译 stamp 文件路径：``dist/.nuitka_compile_stamp``."""
        return dist_dir / ".nuitka_compile_stamp"

    @staticmethod
    def _stamp_key(
        src_dir: Path,
        nuitka_version: str,
        py_version: str,
        entry_rels: frozenset[str] | None = None,
        nuitka_packages: tuple[str, ...] = (),
    ) -> str:
        """计算 Nuitka 编译 stamp 键.

        五要素：

        - ``nuitka_version``：切换 Nuitka 版本时强制重编（如 3.10 从 4.1.3 升级到 4.2）
        - ``py_version``：切换 Python 版本时强制重编（.pyd ABI 绑定）
        - ``src_fingerprint``：用户源码变化时强制重编（按 ``rule-01`` 闭环要求）
        - ``entry_rels``：入口文件集合变化时强制重编（影响哪些文件被跳过，
          避免上次编译删除了 .py、本次新增入口跳过但 .py 已不在导致 run_path 失败）
        - ``nuitka_packages``：第三方包编译列表变化时强制重编（影响 site-packages 编译范围）

        ``pyc_optimize`` 不纳入：Nuitka 编译不受 .pyc 优化级别影响，
        site-packages 的 .pyc 由 :func:`_precompile_pyc` 单独缓存。
        """
        from fspack.analyzer import source_fingerprint

        src_fp = source_fingerprint(src_dir) if src_dir.is_dir() else ""
        # entry_rels 排序后拼接，避免集合迭代顺序不稳定导致 stamp 抖动
        entry_part = ",".join(sorted(entry_rels)) if entry_rels else ""
        # nuitka_packages 已是去重 tuple，排序拼接保证稳定性
        pkg_part = ",".join(nuitka_packages) if nuitka_packages else ""
        return f"{nuitka_version}|{py_version}|{src_fp}|{entry_part}|{pkg_part}"

    @staticmethod
    def _site_packages_dir(runtime_dir: Path, py_version: str, target: Platform) -> Path:
        """推导 runtime 的 site-packages 路径.

        Windows: ``runtime/Lib/site-packages``
        Linux: ``runtime/python/lib/python{major}.{minor}/site-packages``
        """
        if target is Platform.WINDOWS:
            return runtime_dir / "Lib" / "site-packages"
        major, minor = py_version.split(".")[:2]
        return runtime_dir / "python" / "lib" / f"python{major}.{minor}" / "site-packages"

    @classmethod
    def compile_with_stamp(  # noqa: PLR0913
        cls: type[NuitkaCompilerProtocol],
        src_dir: Path,
        dist_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        mirror: MirrorConfig,
        cache_root: Path,
        *,
        stage: StageRecorder,
        entry_rels: frozenset[str] | None = None,
        ccache: bool = False,
        nuitka_packages: tuple[str, ...] = (),
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

        **回退机制**：Nuitka 是可选优化（默认关闭），环境就绪失败时不应中断构建。
        :meth:`ensure_env`（nuitka 安装、C 编译器检查）与 :meth:`_ensure_build_python`
        （standalone python 下载）任一抛 :class:`NuitkaError` 时，warning 并 return，
        回退到 .pyc 模式（由 :func:`fspack.builder._precompile_pyc` 接管）。
        :meth:`compile_src` 的单文件编译失败不触发回退（已有 warning 继续）。

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
        """
        nuitka_ver = nuitka_version_for(py_version)
        stamp = cls._stamp_path(dist_dir)
        stamp_key = cls._stamp_key(src_dir, nuitka_ver, py_version, entry_rels, nuitka_packages)

        # stamp 命中：跳过整个 Nuitka 阶段
        try:
            if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
                _logger.info("Nuitka stamp 命中，跳过编译")
                stage.hit_cache()
                stage.set_detail(f"stamp 命中，nuitka {nuitka_ver} 已编译")
                return
        except OSError:
            pass

        # 环境就绪阶段（ensure_env + ensure_build_python）失败时回退到 .pyc 模式：
        # Nuitka 是可选优化，网络不可用/C 编译器缺失/下载失败不应中断构建。
        # compile_src 不在捕获范围（单文件编译失败已有 warning 继续，非环境问题）。
        try:
            cls.ensure_env(
                cache_root, py_version, target, mirror, stage=stage
            )  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
            nuitka_cache = cls._nuitka_cache_dir(
                cache_root, py_version
            )  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）

            # Windows 编译环境：下载 python-build-standalone 完整发行版运行 nuitka
            # embed runtime python 不完整（无 .py 源码、_pth 限制 sys.path），Nuitka 的
            # reExecute 机制（os._exit 子进程 + scons 调用）会反复衍生 python.exe 子进程
            # 导致 CPU 卡死（Nuitka 官方文档称此为 Fork Bomb）。
            # standalone python 是完整 CPython，sys.executable 可被 nuitka/scons 安全调用。
            # Linux runtime 已是 standalone，返回空 Path 占位（compile_src 内部回退到 runtime python）。
            build_python_exe = cls._ensure_build_python(  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
                cache_root.parent / "python",
                py_version,
                target,
                stage=stage,
            )
        except NuitkaError as e:
            _logger.warning("Nuitka 环境就绪失败，回退到 .pyc 模式: %s", e)
            stage.set_detail(f"回退到 .pyc 模式: {e}")
            return

        cls.compile_src(
            src_dir,
            runtime_dir,
            py_version,
            target,
            nuitka_cache,
            stage=stage,
            build_python_exe=build_python_exe,
            entry_rels=entry_rels,
            ccache=ccache,
            cache_root=cache_root,
        )

        # 编译用户指定的第三方包（site-packages 中的纯 Python 包）
        if nuitka_packages:
            site_packages = cls._site_packages_dir(runtime_dir, py_version, target)
            if site_packages.is_dir():
                cls.compile_packages(
                    site_packages,
                    nuitka_packages,
                    runtime_dir,
                    py_version,
                    target,
                    nuitka_cache,
                    stage=stage,
                    build_python_exe=build_python_exe,
                    ccache=ccache,
                    cache_root=cache_root,
                )
            else:
                _logger.warning("site-packages 不存在，跳过包编译: %s", site_packages)

        # 编译后写 stamp（即使部分文件失败也写，避免下次重复尝试）
        stamp.parent.mkdir(parents=True, exist_ok=True)
        try:
            stamp.write_text(stamp_key, encoding="utf-8")
        except OSError as e:
            _logger.warning("写入 Nuitka stamp 失败: %s", e)
