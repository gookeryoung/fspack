"""Nuitka 编译进度 mixin：流式 subprocess 输出 + 并行编译池.

本模块是 :class:`NuitkaProgress` mixin 定义，所有方法为 staticmethod/classmethod，
无实例状态。通过多继承组合到 :class:`fspack.packaging.nuitka.NuitkaCompiler`
facade，MRO 顺序见 :mod:`fspack.packaging.nuitka.compiler`。

职责：

- 流式编译输出（``_stream_compile``）：实时显示 nuitka INFO 与 gcc 调用，
  累积 stdout/stderr 供失败诊断，含超时与死锁防护。
- 并行编译池（``_compile_files``）：ThreadPoolExecutor 并行调 nuitka
  ``--mode=module`` 批量编译 .py，全局心跳进度反馈，gcc 总并行度防超订。

跨 mixin 调用（``cls.<method>()``）通过
:class:`fspack.packaging.nuitka.protocol.NuitkaCompilerProtocol` 类型契约声明。

测试 patch 兼容：以下常量/名字在 :mod:`fspack.packaging.nuitka.compile`
顶层 re-export，测试 monkeypatch 修改的是 compile 模块属性。本模块函数在
运行时通过 :func:`_C` 延迟 dispatch 到 compile 层同名属性，保证 patch 生效。
dispatch 不可用时 fallback 到本模块定义的默认值：
``_HEARTBEAT_INTERVAL``, ``_MAX_COMPILE_WORKERS``,
``_STREAM_ACCUM_LIMIT``, ``_COMPILE_TIMEOUT``, ``_DRAIN_JOIN_TIMEOUT``,
``ThreadPoolExecutor``。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor as _DefaultThreadPoolExecutor
from concurrent.futures import as_completed
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, TextIO

from fspack.packaging.nuitka.winlibs import needs_force_mingw64
from fspack.platform import Platform
from fspack.progress import StageRecorder

if TYPE_CHECKING:
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")

# ---- 默认值：compile 层同名名字不可用时 fallback 的本地常量 ----
# 心跳间隔：nuitka reExecute 机制导致子进程输出不可靠，每 N 秒输出编译进度让用户看到进度
_DEFAULT_HEARTBEAT_INTERVAL = 10.0
# 并行编译 .py 文件的最大线程数上限
_DEFAULT_MAX_COMPILE_WORKERS = 4
# stdout/stderr 累积上限（16MB），超过后停止累积（继续写终端实时显示）
_DEFAULT_STREAM_ACCUM_LIMIT = 16 * 1024 * 1024
# 单次 nuitka 编译超时（秒）
_DEFAULT_COMPILE_TIMEOUT = 600.0
# drain 线程 join 超时（秒）
_DEFAULT_DRAIN_JOIN_TIMEOUT = 5.0

# 保留与旧常量名一致的导出（compile.py re-export 这些字面名字维持 patch 路径）：
# 它们仅作 compile.py re-export 的源值定义，实际运行时通过 _C dispatch 动态获取
# compile 层的被 patch 属性，保证 monkeypatch 生效。详见 :func:`_C` 注释。
_HEARTBEAT_INTERVAL = _DEFAULT_HEARTBEAT_INTERVAL
_MAX_COMPILE_WORKERS = _DEFAULT_MAX_COMPILE_WORKERS
_STREAM_ACCUM_LIMIT = _DEFAULT_STREAM_ACCUM_LIMIT
_COMPILE_TIMEOUT = _DEFAULT_COMPILE_TIMEOUT
_DRAIN_JOIN_TIMEOUT = _DEFAULT_DRAIN_JOIN_TIMEOUT
ThreadPoolExecutor = _DefaultThreadPoolExecutor

# 延迟 dispatch 缓存（与 indexes 模块原理一致）：保存已解析的 compile 模块对象引用，
# 每次调用 _C 时动态 getattr，保证 monkeypatch 后变化被感知。
_compile_mod_holder: list[Any] = [None]


def _C(const_name: str, default: Any) -> Any:
    """返回 compile 模块级的常量/名字，不可用时 fallback 到 ``default``.

    测试通过 ``monkeypatch.setattr("fspack.packaging.nuitka.compile.<name>", ...)``
    修改 compile 模块属性（心跳间隔、最大 worker 数、ThreadPoolExecutor 等）。
    由于本 progress 模块的 import 发生在 compile 初始化之前（MRO 链上 compile
    排在 progress 之后，但 compiler.py 的 import 顺序是 compile 在 progress 前），
    采用**运行时延迟导入 + 动态 getattr**，每次调用时从 compile 模块对象拿
    最新属性值，保证 monkeypatch 替换生效。

    注意：函数/类属性（如 ThreadPoolExecutor）同样支持——monkeypatch.setattr 替换
    的是 compile 模块对象的属性，而 _C 每次都动态 getattr，能拿到替换后的新类。
    """
    mod = _compile_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging.nuitka import compile as _compile_mod

            mod = _compile_mod
            _compile_mod_holder[0] = mod
        except ImportError:
            return default
    return getattr(mod, const_name, default)


class NuitkaProgress:
    """Nuitka 编译进度 mixin：流式 subprocess 输出 + 并行编译池.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。
    """

    @staticmethod
    def _stream_compile(
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """运行 nuitka 编译命令，实时流式输出 stdout/stderr 到终端.

        用 ``Popen`` + 两个守护线程通过 ``os.read`` 读取 stdout/stderr 文件描述符
        字节块并实时写入 ``sys.stdout``/``sys.stderr``，支持 nuitka 的 ``Nuitka:INFO``
        步骤输出和 C 编译器调用过程实时显示，避免单文件编译数十秒无输出被误认为卡死。

        ``env`` 为 None 时继承当前进程环境；非 None 时替换环境（用于注入
        ``CC="ccache gcc"`` 让 scons 通过 ccache 调用 gcc，加速重复编译）。

        同时累积 stdout/stderr 内容供失败时诊断与测试验证捕获行为。

        **内存保护**：stdout/stderr 累积上限通过 :func:`_C` dispatch 从 compile
        层获取（默认 16MB），超过后停止累积（继续写终端实时显示），避免大型项目
        累积输出导致内存膨胀。

        **超时防护**：``timeout`` 秒后子进程未退出则终止整个进程树。默认值不在
        函数定义时绑定（避免绕过 :func:`_C` dispatch 使 monkeypatch 失效），
        None 时运行时 dispatch compile 层 ``_COMPILE_TIMEOUT``（默认 600s），
        可通过调用参数显式覆盖。

        **死锁防护**：drain 线程持续 ``os.read`` 消费 PIPE 防止 PIPE 缓冲区满
        导致子进程 ``write()`` 阻塞。主线程 ``wait(timeout=)`` 控制总时长，
        ``finally`` 块 ``join(timeout=)`` 确保 drain 线程不泄漏。

        Args:
            cmd: 子进程命令列表.
            env: 子进程环境变量. None 继承当前进程环境.
            timeout: 超时秒数. None 时运行时取 compile 层 ``_COMPILE_TIMEOUT``
                （默认 600s），可显式覆盖. 超时 kill 进程树并返回非零退出码.
        """
        # 运行时 dispatch：优先 compile 层的同名属性（保证 monkeypatch 生效），
        # 否则 fallback 到本模块 _DEFAULT_* 常量。一次性 resolve 后通过默认参数
        # 传入闭包，避免闭包每次循环都重新 dispatch 增加开销。
        stream_accum_limit: int = _C("_STREAM_ACCUM_LIMIT", _DEFAULT_STREAM_ACCUM_LIMIT)
        drain_timeout: float = _C("_DRAIN_JOIN_TIMEOUT", _DEFAULT_DRAIN_JOIN_TIMEOUT)
        if timeout is None:
            # 默认值运行时 dispatch（同 stream_accum_limit 用法）：定义期绑定
            # _COMPILE_TIMEOUT 常量会绕过 compile 层 monkeypatch
            timeout = float(_C("_COMPILE_TIMEOUT", _DEFAULT_COMPILE_TIMEOUT))

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_total: list[int] = [0]
        stderr_total: list[int] = [0]

        def _drain(
            stream: IO[bytes] | None,
            chunks: list[bytes],
            out: TextIO,
            total_ref: list[int],
            *,
            _limit: int = stream_accum_limit,
        ) -> None:
            assert stream is not None
            fd = stream.fileno()
            while True:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:  # pragma: no cover - fd 被关闭的竞态防御
                    break
                if not chunk:
                    break
                if total_ref[0] < _limit:
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

        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _logger.warning("Nuitka 编译超时（%ds），终止子进程: %s", int(timeout), " ".join(cmd[:3]))
            timed_out = True
            # 杀整个进程树：nuitka 会衍生 scons→gcc 孙进程，仅 kill 直接子进程
            # 时孙进程存活并持有 PIPE 写端，drain 线程无法收到 EOF 导致 join 卡住
            if sys.platform == "win32":
                # Windows 无进程组，用 taskkill /T 递归终止进程树
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                # POSIX：Popen 未设 start_new_session（无独立进程组），无法
                # os.killpg 杀组，回退仅杀直接子进程；孙进程由超时路径的
                # PIPE 关闭与 nuitka 自身退出机制兜底
                process.kill()
            try:
                # kill 后收尸加超时保护：进程树未完全退出时不无限阻塞
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - taskkill /F 后残留极罕见
                _logger.warning("超时 kill 后子进程 %d 5s 内未退出，放弃等待", process.pid)
                returncode = -1
        finally:
            t_out.join(timeout=drain_timeout)
            t_err.join(timeout=drain_timeout)

        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        if timed_out and returncode == 0:  # pragma: no cover - kill 后 returncode 极少为 0
            returncode = -1
        return returncode, stdout, stderr

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
        py_version: str = "",
        compiler: str = "auto",
    ) -> tuple[set[Path], list[Path]]:
        """并行编译 .py 文件，返回 (成功编译的文件集合, 失败文件路径列表).

        用 dispatch 的 ThreadPoolExecutor（compile 层 patch 后的类，供测试注入 mock）
        并行调 nuitka ``--mode=module``（subprocess 释放 GIL，线程足够并行）。
        ``max_workers = min(cpu_count, _MAX_COMPILE_WORKERS)`` 平衡并行收益与
        Windows 资源限制。常量均通过 :func:`_C` dispatch，保证 monkeypatch 生效。

        **每进程 --jobs 调整**：单进程串行时 ``--jobs=cpu_count``（全核 gcc 并行）；
        并行时改为 ``--jobs = max(1, cpu_count // max_workers)``，使总 gcc 进程数 ≈ cpu_count，
        避免 ``max_workers * cpu_count`` 过度超订导致内存膨胀/OOM。

        Nuitka 编译参数（作为脚本参数传入，进入 ``sys.argv[1:]``）：

        - ``--mode=module``：编译为可导入模块（.pyd/.so），不生成独立 exe
          （Nuitka 4.x 须用 ``--mode=module``，旧 ``--module`` 已废弃且不再被
          模块模式专属选项检查识别，会触发无效果 WARNING）
        - ``--nofollow-imports``：显式不跟随导入（单文件编译本就不跟随，
          显式声明消除 "did not specify to follow or include anything" 警告）
        - ``--output-dir``：输出目录与源码同目录（保持包结构）
        - ``--no-pyi-file``：不生成 .pyi 类型存根（运行时不需要）
        - ``--remove-output``：编译后删除临时构建文件（.build/ 目录）
        - ``--jobs=N``：单进程内 C 编译并行度
        - ``--mingw64 --experimental=force-mingw64``（Windows：``compiler=mingw``
          强制 winlibs 无视 MSVC；``compiler=auto`` 时仅 py>=3.13 且无 MSVC）：
          强制 scons 走 winlibs gcc 而非 zig/MSVC——zig 编译的 .pyd 可能
          损坏（returncode==0、文件已生成，运行时访问违例 0xC0000005）。
          ``--mingw64`` 实际选择 winlibs（单独的 experimental flag 只是
          py>=3.13 使用 ``--mingw64`` 的解锁许可，不选择 mingw）；
          py<3.13 默认即 winlibs 无需 flag（compiler=mingw 且有 MSVC 时除外）；
          ``compiler=msvc`` 恒不加。Linux 不涉及（用系统 gcc）。判断逻辑集中见
          :func:`fspack.packaging.nuitka.winlibs.needs_force_mingw64`，
          winlibs 工具链由 :meth:`NuitkaEnv.ensure_env` 预填充到
          ``nuitka-winlibs-mingw`` 缓存（两层判断一致），scons 缓存命中不下载

        ``ccache_exe`` 非 None 时，设置 ``CC="ccache <compiler>"`` 环境变量注入子进程，
        scons 通过 ccache 调用 gcc，缓存 C 编译结果加速重复编译。

        **全局心跳**：单线程每 dispatch 的 ``_HEARTBEAT_INTERVAL`` 秒输出进度。
        nuitka reExecute 机制使子进程输出不可靠，心跳是唯一的进度反馈。

        **线程安全**：``compiled_files``/``failed``/``stage.processed()`` 仅在主线程
        （``as_completed`` 迭代）聚合，无共享可变状态竞争。``completed_count`` 用 list
        容器：主线程写、心跳线程读，GIL 下 int 读写原子。

        **异常传播**：worker 内 ``_stream_compile`` 抛 ``OSError``（Popen 启动失败）
        时按"退出码非零"等价结果处理（仅告警记入失败列表），不中断其余文件编译；
        非 ``OSError`` 异常经 ``future.result()`` 重抛，``with`` 块 ``__exit__`` 的
        ``shutdown(wait=True)`` 等待在途任务后传播，传播前尽力取消排队任务。
        ``finally`` 块确保心跳线程停止。
        """
        # dispatch 常量与类（保证 monkeypatch 生效）：一次性 resolve 后使用
        heartbeat_interval: float = _C("_HEARTBEAT_INTERVAL", _DEFAULT_HEARTBEAT_INTERVAL)
        max_workers_cap: int = _C("_MAX_COMPILE_WORKERS", _DEFAULT_MAX_COMPILE_WORKERS)
        tpe_cls: type = _C("ThreadPoolExecutor", _DefaultThreadPoolExecutor)

        compile_env = cls._build_compile_env(target, ccache_exe)  # NuitkaEnv mixin（MRO 派发）
        cpu = os.cpu_count() or 1
        max_workers = min(cpu, max_workers_cap)
        jobs = max(1, cpu // max_workers)
        compiled_files: set[Path] = set()
        failed_files: list[Path] = []
        total = len(py_files)

        def _compile_one(py_file: Path) -> tuple[Path, int]:
            """单文件编译 worker：调 nuitka --mode=module，返回 (文件路径, 退出码).

            ``_stream_compile`` 内 ``Popen`` 抛 ``OSError``（如 py_exe 不存在、
            系统句柄耗尽）时按"退出码非零"等价结果处理（返回 -1），与
            "单文件失败仅告警不中断构建"的承诺一致，不向上重抛中断整个构建。
            """
            cmd = [
                str(py_exe),
                str(bootstrap_script),
                # Nuitka 4.x：--module 已废弃为兼容写法，须用 --mode=module，
                # 否则 --no-pyi-file 等模块模式专属选项触发无效果 WARNING
                "--mode=module",
                # 显式声明不跟随导入：单文件逐个编译本就不跟随（模块模式默认行为），
                # 显式传入避免 Nuitka "did not specify to follow or include anything" 警告
                "--nofollow-imports",
                f"--output-dir={py_file.parent}",
                "--no-pyi-file",
                "--remove-output",
                "--assume-yes-for-downloads",
                f"--jobs={jobs}",
            ]
            # 强制 winlibs gcc 的两个 Nuitka flag（判断逻辑集中见
            # needs_force_mingw64，触发条件：compiler=mingw 强制无视 MSVC；
            # compiler=auto 时仅 py>=3.13 且无 MSVC——该版本段 Nuitka 默认
            # fallback 到 zig，产物可能损坏（运行时访问违例）；有 MSVC 时
            # auto 不加 flag：scons 优先 MSVC，加了反而顶掉）：
            # - ``--mingw64``：实际选择 winlibs（scons tools=["mingw"] 并禁用
            #   MSVC 工具，装了 VS 的机器也被顶掉）。**单独传 experimental
            #   不选择 mingw**，装了 MSVC 的机器仍走 cl.exe
            # - ``--experimental=force-mingw64``：仅为 py>=3.13 解锁 ``--mingw64``
            #   （Nuitka 4.1.3 对该组合有硬限制）；py<3.13 冗余但无害（旧版
            #   Nuitka 2.5.1 不认识该 experimental 值，静默忽略）
            if needs_force_mingw64(target, py_version, compiler):
                cmd.append("--mingw64")
                cmd.append("--experimental=force-mingw64")
            cmd.append(str(py_file))
            try:
                returncode, _stdout, _stderr = cls._stream_compile(cmd, env=compile_env)
            except OSError as e:
                # Popen 启动失败（FileNotFoundError/句柄不足等）：按该文件编译失败处理，
                # 与 CalledProcessError（退出码非零）路径一致仅告警，不中断其余文件
                _logger.warning("Nuitka 编译进程启动失败 %s: %s", py_file, e)
                returncode = -1
            return py_file, returncode

        completed_count: list[int] = [0]
        stop_heartbeat = threading.Event()
        start_ts = time.monotonic()

        def _global_heartbeat(
            _stop: threading.Event = stop_heartbeat,
            _start: float = start_ts,
            _total: int = total,
            _done: list[int] = completed_count,
            _interval: float = heartbeat_interval,  # dispatch 后的心跳间隔通过默认参数绑定
        ) -> None:
            while not _stop.wait(_interval):
                elapsed = int(time.monotonic() - _start)
                _logger.info("Nuitka 并行编译中: 已完成 %d/%d, 已耗时 %ds", _done[0], _total, elapsed)

        hb_thread = threading.Thread(target=_global_heartbeat, daemon=True)
        hb_thread.start()
        _logger.info("提交 %d 个 .py 文件到并行编译池（max_workers=%d, jobs=%d）", total, max_workers, jobs)
        try:
            with tpe_cls(max_workers=max_workers) as pool:
                futures = {pool.submit(_compile_one, f): f for f in py_files}
                try:
                    for future in as_completed(futures):
                        py_file, returncode = future.result()
                        completed_count[0] += 1
                        idx = completed_count[0]
                        if returncode == 0:
                            compiled_files.add(py_file)
                            stage.processed()
                            _logger.info("编译 [%d/%d] %s 成功", idx, total, py_file.name)
                        else:
                            failed_files.append(py_file)
                            _logger.warning("Nuitka 编译失败 %s（退出码 %s），详见上方输出", py_file, returncode)
                finally:
                    # 异常传播（如 KeyboardInterrupt）时尽力取消尚未开始的排队任务，
                    # 避免 with __exit__ 的 shutdown(wait=True) 等待全部排队任务执行完
                    # 才退出（项目最低 Python 3.8，无 shutdown(cancel_futures=True)）。
                    # 正常完成时所有 future 已结束，cancel 对已完成任务无副作用。
                    for f in futures:
                        f.cancel()
        finally:
            stop_heartbeat.set()
            hb_thread.join(timeout=1.0)
        return compiled_files, failed_files
