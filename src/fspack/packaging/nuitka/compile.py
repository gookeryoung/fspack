"""Nuitka 编译流程：单文件 ``--module`` 编译、stamp 缓存.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的编译 mixin，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

职责边界：

- 流式 subprocess 输出（``_stream_compile`` 实时显示 nuitka INFO 与 gcc 调用）
- 并行编译（``_compile_files`` 用 ``ThreadPoolExecutor`` 并行调 nuitka ``--module``，全局心跳防误判卡死）
- stamp 缓存（``compile_with_stamp`` 整合 env + compile_src + stamp 比对）
- 第三方包编译（``compile_packages`` 编译 site-packages 中指定包）

不涉及：环境就绪（见 :mod:`fspack.packaging.nuitka.env`）、
产物剥离与构建目录清理（见 :mod:`fspack.packaging.nuitka.strip`）、
验证逻辑（见 :mod:`fspack.packaging.nuitka.verify`，通过 ``cls._verify_compiled_modules``
经 MRO 调用）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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

# 心跳间隔：nuitka reExecute 机制导致子进程输出不可靠，每 N 秒输出编译进度让用户看到进度
# iter-131 起为全局心跳（并行编译时输出已完成数/总数），非每文件心跳
_HEARTBEAT_INTERVAL = 10.0

# 并行编译 .py 文件的最大线程数上限：subprocess 释放 GIL，线程足够并行调 nuitka。
# min(cpu_count, 4) 平衡并行收益与 Windows 资源限制（句柄/内存）。iter-131 引入。
_MAX_COMPILE_WORKERS = 4

# stdout/stderr 累积上限：Nuitka 编译输出（gcc 调用、reExecute 日志）可达 10MB+，
# 16MB 上限足以容纳正常输出供失败诊断。超过后停止累积（继续写终端实时显示），
# 避免大型项目（数百 .py 文件）累积输出导致内存膨胀。
_STREAM_ACCUM_LIMIT = 16 * 1024 * 1024

# 单次 nuitka 编译超时（秒）：实测 50 文件项目单文件 P99 <60s（含 gcc 启动），
# 600s 裕量覆盖冷启动 ccache miss + 慢速 CI。超时 kill 子进程避免 reExecute fork bomb
# 与 scons 死锁无限阻塞构建。iter-127 引入。
_COMPILE_TIMEOUT = 600.0

# drain 线程 join 超时：子进程被 kill 后 stdout/stderr fd 关闭，drain 线程读到 EOF
# 自动退出。5s 裕量覆盖 OS 关闭 fd 与线程调度延迟，避免极端情况下主线程无限等待。
_DRAIN_JOIN_TIMEOUT = 5.0

# hash 索引上限：超过后按 compiled_at 时间戳淘汰最旧条目，避免索引无限增长。
# 50 条覆盖常见多版本/多入口/多包组合场景（每条 ~200 字节，索引文件 <10KB）。
_HASH_INDEX_MAX = 50


def _atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    """原子写入文本文件：先写临时文件再 rename，避免半写入文件被读取.

    用 ``tempfile.mkstemp`` 在目标目录创建临时文件（同目录保证 ``Path.replace``
    是原子操作：POSIX rename(2) 原子，Windows ReplaceFile 原子），写入完成后
    ``Path.replace`` 替换目标文件。任何失败都清理临时文件并重抛 ``OSError``。

    iter-128 引入：Nuitka stamp 写入用原子化避免构建被中断（Ctrl+C/进程崩溃）后
    stamp 文件半写入被下次构建误读为有效缓存，从而跳过编译输出陈旧 .pyd。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=target.parent, prefix=".tmp_", suffix=target.suffix)
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(content)
        tmp_path.replace(target)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _hash_index_path(dist_dir: Path) -> Path:
    """返回 Nuitka hash 索引文件路径：``dist/.nuitka_hash_index.json``.

    iter-129 引入：与 stamp 文件同目录（dist/），删除 dist 时一并清理，
    保证索引命中场景仅限于"dist 完整保留但 stamp 单独丢失/损坏"。
    """
    return dist_dir / ".nuitka_hash_index.json"


def _failed_files_path(dist_dir: Path) -> Path:
    """返回 Nuitka 失败文件列表路径：``dist/.nuitka_failed_files.json``.

    iter-137 引入：记录上次构建编译失败的 .py 文件相对 ``src_dir`` 的 POSIX 路径。
    与 stamp 文件同目录（dist/），删除 dist 时一并清理。stamp 不命中时读取，
    传给 :meth:`NuitkaCompile.compile_src` 跳过这些文件避免反复尝试。
    """
    return dist_dir / ".nuitka_failed_files.json"


def _load_failed_files(dist_dir: Path) -> frozenset[str]:
    """读取失败文件列表，返回相对 ``src_dir`` 的 POSIX 路径集合.

    文件不存在或损坏返回空 frozenset（不影响构建，相当于"无上次失败文件"）。
    与 :func:`_load_hash_index` 的"内容损坏删文件"策略一致。
    """
    path = _failed_files_path(dist_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except OSError:
        _logger.warning("读取失败文件列表失败，视为空: %s", path)
        return frozenset()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        _logger.warning("失败文件列表损坏，删除并重建: %s: %s", path, e)
        _safe_unlink(path)
        return frozenset()
    if not isinstance(data, list):
        _logger.warning("失败文件列表非 list，删除并重建: %s", path)
        _safe_unlink(path)
        return frozenset()
    # 类型校验：仅保留 str 条目
    return frozenset(s for s in data if isinstance(s, str))


def _save_failed_files(dist_dir: Path, failed_files: list[str]) -> None:
    """写入失败文件列表到 ``dist/.nuitka_failed_files.json``.

    用 :func:`_atomic_write_text` 原子写入（与 stamp/hash 索引一致，避免半写入）。
    空列表也写入（覆盖上次失败记录，表示本次无失败）。任何 I/O 错误仅告警不中断
    构建（失败文件列表是优化项，写入失败不影响主流程）。
    """
    path = _failed_files_path(dist_dir)
    try:
        _atomic_write_text(path, json.dumps(failed_files, ensure_ascii=False, indent=2))
    except OSError as e:
        _logger.warning("写入失败文件列表失败（不影响构建）: %s: %s", path, e)


def _load_hash_index(dist_dir: Path) -> dict[str, str]:
    """读取 hash 索引文件，返回 ``{stamp_key: compiled_at_iso}`` 字典.

    文件不存在返回空 dict。内容损坏（JSON 非法/结构错误/编码错误）删除文件
    并返回空 dict，与 iter-128 ``_load_deps_cache`` 的"内容损坏删文件"策略一致。
    OSError（权限/磁盘 I/O）不删除，返回空 dict（瞬时错误，下次重试）。

    索引结构校验：顶层须为 dict，键须为 str，值须为 str（ISO 时间戳）。
    """
    index_file = _hash_index_path(dist_dir)
    try:
        raw = index_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        _logger.warning("读取 hash 索引失败，视为空索引: %s", index_file)
        return {}

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        _logger.warning("hash 索引损坏，删除并重建: %s: %s", index_file, e)
        _safe_unlink(index_file)
        return {}

    if not isinstance(data, dict):
        _logger.warning("hash 索引非 dict，删除并重建: %s", index_file)
        _safe_unlink(index_file)
        return {}

    # 类型校验：键值均须 str，剔除异常条目
    cleaned: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            cleaned[key] = value
    if len(cleaned) != len(data):
        _logger.warning("hash 索引含非 str 条目，已剔除（保留 %d/%d）", len(cleaned), len(data))
        _atomic_write_text(index_file, json.dumps(cleaned, ensure_ascii=False, indent=2))
    return cleaned


def _safe_unlink(path: Path) -> None:
    """删除文件，OSError 仅告警不抛（用于索引损坏时的清理）."""
    try:
        path.unlink()
    except OSError as e:
        _logger.warning("删除文件失败: %s: %s", path, e)


def _update_hash_index(dist_dir: Path, stamp_key: str) -> None:
    """更新 hash 索引：记录 ``stamp_key → 当前 ISO 时间``，LRU 淘汰超限条目.

    读取现有索引 → 合并新条目 → 超过 :data:`_HASH_INDEX_MAX` 时按时间戳
    删除最旧的 → 原子写入。任何 I/O 错误仅告警不中断构建（索引是回退优化，
    写入失败不影响主流程，下次构建仍可走完整编译）。
    """
    index_file = _hash_index_path(dist_dir)
    index = _load_hash_index(dist_dir)
    index[stamp_key] = datetime.now().isoformat(timespec="seconds")

    # LRU 淘汰：按时间戳升序排序，保留最新的 _HASH_INDEX_MAX 条
    if len(index) > _HASH_INDEX_MAX:
        sorted_items = sorted(index.items(), key=lambda kv: kv[1])
        index = dict(sorted_items[-_HASH_INDEX_MAX:])

    try:
        _atomic_write_text(index_file, json.dumps(index, ensure_ascii=False, indent=2))
    except OSError as e:
        _logger.warning("写入 hash 索引失败（不影响构建）: %s: %s", index_file, e)


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
    def _stream_compile(
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = _COMPILE_TIMEOUT,
    ) -> tuple[int, str, str]:
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

        **超时防护**（iter-127）：``timeout`` 秒后子进程未退出则 ``kill()`` 终止，
        避免 Nuitka reExecute fork bomb、scons 死锁、gcc 挂起无限阻塞构建。
        kill 后仍 join drain 线程读取已缓冲输出供诊断。

        **死锁防护**（iter-127）：drain 线程持续 ``os.read`` 消费 PIPE 防止
        PIPE 缓冲区满导致子进程 ``write()`` 阻塞。主线程 ``wait(timeout=)``
        控制总时长，``finally`` 块 ``join(timeout=)`` 确保 drain 线程不泄漏。
        即使子进程被 kill，fd 关闭后 ``os.read`` 返回 EOF 让 drain 线程退出。

        参考 :func:`fspack.packaging.wheels._stream_subprocess` 的实现模式，区别在于
        nuitka 的 INFO 输出可能走 stdout 或 stderr，需同时流式两者。

        Args:
            cmd: 子进程命令列表.
            env: 子进程环境变量. None 继承当前进程环境.
            timeout: 超时秒数. 超时 kill 子进程并返回非零退出码.
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
                try:
                    chunk = os.read(fd, 4096)
                except OSError:  # pragma: no cover - fd 被关闭的竞态防御，极难稳定触发
                    # fd 被关闭（子进程 kill 后）：退出循环，避免线程泄漏
                    break
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

        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _logger.warning("Nuitka 编译超时（%ds），终止子进程: %s", int(timeout), " ".join(cmd[:3]))
            timed_out = True
            process.kill()
            # kill 后 wait 确保子进程彻底退出，避免僵尸进程；无超时（kill 后必退出）
            returncode = process.wait()
        finally:
            # join drain 线程：子进程退出/kill 后 fd 关闭，drain 线程读 EOF 退出。
            # 带超时防止极端情况下 fd 未关闭导致主线程卡死。
            t_out.join(timeout=_DRAIN_JOIN_TIMEOUT)
            t_err.join(timeout=_DRAIN_JOIN_TIMEOUT)

        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        if timed_out and returncode == 0:  # pragma: no cover - kill 后 returncode 极少为 0
            # kill 后 returncode 通常非 0（SIGKILL=−9 on POSIX / 1 on Windows）；
            # 极端情况返回 0 时强制改为非 0 让上层识别失败。
            returncode = -1
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
        skip_files: frozenset[str] | None = None,
    ) -> list[str]:
        """编译 ``src_dir`` 下所有 ``.py`` 为 ``.pyd``/``.so``，编译后删除 ``.py`` 源码.

        返回失败文件的相对 POSIX 路径列表（相对 ``src_dir``），供调用方
        :meth:`compile_with_stamp` 写入 ``.nuitka_failed_files.json``，
        下次构建跳过这些文件避免反复尝试（iter-137）。

        Args:
            skip_files: 上次构建失败的文件相对 ``src_dir`` 的 POSIX 路径集合。
                这些文件本次构建跳过（不编译不删除），由 :meth:`_collect_py_files` 排除。
                None 表示不跳过任何文件（首次构建或上次无失败）。

        其余参数与返回行为见类级 docstring。

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
            return []

        if not cls._is_nuitka_cached(nuitka_cache):  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
            _logger.warning(
                "Nuitka 编译跳过: 缓存目录无 nuitka %s，请用 fsp b --nuitka 触发安装",
                nuitka_cache,
            )
            stage.set_detail("nuitka 未安装，跳过（回退到 .pyc 模式）")
            return []

        py_files = cls._collect_py_files(src_dir, entry_rels, skip_files)
        if not py_files:
            stage.set_detail("无 .py 文件可编译")
            return []

        # ccache 就绪：优先系统 PATH，缺失则下载到 ~/.fspack/cache/ccache/
        ccache_exe = None
        if ccache and cache_root is not None:
            ccache_exe = cls._ensure_ccache(
                cache_root, target, stage
            )  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）

        bootstrap_script = cls._create_bootstrap_script(nuitka_cache)
        try:
            try:
                compiled_files, failed_files = cls._compile_files(
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
        finally:
            # 清理 Nuitka 编译失败的 .build 残留目录（--remove-output 仅成功时清理）。
            # 放在 finally：_compile_files 抛异常时也清理，避免残留目录污染下次构建。
            cls._cleanup_build_dirs(src_dir)
        compiled = len(compiled_files)
        if failed_files:
            stage.set_detail(f"编译 {compiled} 个，失败 {len(failed_files)} 个，剥离 {stripped} 个 .py")
        else:
            stage.set_detail(f"编译 {compiled} 个，剥离 {stripped} 个 .py")
        # 返回失败文件相对 src_dir 的 POSIX 路径，供 compile_with_stamp 写入
        # .nuitka_failed_files.json，下次构建跳过这些文件避免反复尝试（iter-137）
        return [f.relative_to(src_dir).as_posix() for f in failed_files]

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
            compiled_files, failed_files = cls._compile_files(
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
        if failed_files:
            _logger.warning(
                "Nuitka 包编译完成: 成功 %d 个，失败 %d 个，剥离 %d 个 .py", compiled, len(failed_files), stripped
            )
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
    def _collect_py_files(
        src_dir: Path,
        entry_rels: frozenset[str] | None,
        skip_files: frozenset[str] | None = None,
    ) -> list[Path]:
        """收集待编译的 .py 文件，排除 Nuitka 残留目录、__init__.py、入口文件与上次失败文件.

        排除规则：

        1. Nuitka 残留的 ``<name>.build/`` 目录：``--remove-output`` 只在编译成功时清理，
           失败时残留。下次构建若不排除会扫到 scons-debug.py 等产物并尝试编译。
        2. ``__init__.py``：包标识文件通常为空或仅含 import，编译为 .pyd 无收益且
           增加 subprocess 开销。.py 保留作包标识（PEP 420），.pyc 预编译提供
           字节码优化。跳过后 compiled_files 不含 __init__.py，删除循环天然跳过。
        3. 入口文件（``entry_rels``）：入口包装器用 ``runpy.run_path()`` 显式指定 .py 路径，
           编译后 .py 被删除会导致 FileNotFoundError。入口文件保留 .py 形态，由 .pyc 优化。
        4. 上次失败文件（``skip_files``，iter-137）：相对 ``src_dir`` 的 POSIX 路径集合，
           这些文件上次构建编译失败，本次跳过避免反复尝试。用户修复后需删除
           ``.nuitka_failed_files.json`` 或 stamp 文件强制重试。
        """
        py_files = sorted(
            p
            for p in src_dir.rglob("*.py")
            if not any(part.lower().endswith(".build") for part in p.parts) and p.name != "__init__.py"
        )
        if entry_rels:
            py_files = [p for p in py_files if p.relative_to(src_dir).as_posix() not in entry_rels]
        if skip_files:
            py_files = [p for p in py_files if p.relative_to(src_dir).as_posix() not in skip_files]
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
    ) -> tuple[set[Path], list[Path]]:
        """并行编译 .py 文件，返回 (成功编译的文件集合, 失败文件路径列表).

        用 :class:`ThreadPoolExecutor` 并行调 nuitka ``--module``（subprocess 释放 GIL，
        线程足够并行）。``max_workers = min(cpu_count, :data:`_MAX_COMPILE_WORKERS`)`` 平衡
        并行收益与 Windows 资源限制（句柄/内存）。

        **每进程 --jobs 调整**：单进程串行时 ``--jobs=cpu_count``（全核 gcc 并行）；
        并行时改为 ``--jobs = max(1, cpu_count // max_workers)``，使总 gcc 进程数 ≈ cpu_count，
        避免 ``max_workers * cpu_count`` 过度超订导致内存膨胀/OOM。

        Nuitka 编译参数（作为脚本参数传入，进入 ``sys.argv[1:]``）：

        - ``--module``：编译为可导入模块（.pyd/.so），不生成独立 exe
        - ``--output-dir``：输出目录与源码同目录（保持包结构）
        - ``--no-pyi-file``：不生成 .pyi 类型存根（运行时不需要）
        - ``--remove-output``：编译后删除临时构建文件（.build/ 目录）
        - ``--jobs=N``：单进程内 C 编译并行度（见上方调整说明）

        ``ccache_exe`` 非 None 时，设置 ``CC="ccache <compiler>"`` 环境变量注入子进程，
        scons 通过 ccache 调用 gcc，缓存 C 编译结果加速重复编译。

        **全局心跳**（iter-131）：替代原每文件心跳，单线程每 :data:`_HEARTBEAT_INTERVAL`
        秒输出"已完成 X/Y, 已耗时 Zs"。nuitka reExecute 机制使子进程输出不可靠，
        心跳是唯一的进度反馈。

        **线程安全**：``compiled_files``/``failed``/``stage.processed()`` 仅在主线程
        （``as_completed`` 迭代）聚合，无共享可变状态竞争。``completed_count`` 用 list
        容器：主线程写、心跳线程读，GIL 下 int 读写原子。

        **异常传播**：worker 内 ``_stream_compile`` 抛异常（如 ``FileNotFoundError``，
        py_exe 不存在）时 ``future.result()`` 重抛，``with ThreadPoolExecutor`` 的
        ``__exit__`` 调 ``shutdown(wait=True)`` 等待在途任务后传播异常。
        ``finally`` 块确保心跳线程停止。

        不需要 ``--python-for-scons``：已用 standalone python（完整环境）运行 nuitka，
        scons 自动继承 ``sys.executable``，无需另指定。
        """
        # 构建编译环境变量：CC="<compiler>" 或 CC="ccache <compiler>"（启用 ccache 时）
        # 始终设置 CC 指定 C 编译器，避免 Nuitka 4.x 选择 zig 触发交互式下载
        compile_env = cls._build_compile_env(target, ccache_exe)  # NuitkaEnv mixin（stub 在类顶部，运行时 MRO 派发）
        cpu = os.cpu_count() or 1
        max_workers = min(cpu, _MAX_COMPILE_WORKERS)
        # 每进程 gcc 并行度：总并行度 ≈ cpu_count，避免 max_workers * jobs 过度超订
        # （4 并行 * 8 gcc = 32 gcc 进程会 OOM）。单文件场景 max_workers 不影响（只 submit 1 个任务）。
        jobs = max(1, cpu // max_workers)
        compiled_files: set[Path] = set()
        failed_files: list[Path] = []
        total = len(py_files)
        # 记录成功编译的文件：仅这些 .py 可安全删除（.pyd 已生成）。
        # 失败的 .py 保留，让运行时回退到 .pyc 加载，避免编译失败导致 dist/src 无可用代码。
        # iter-137：失败文件列表返回给 compile_src → compile_with_stamp，写入
        # .nuitka_failed_files.json，下次构建跳过这些文件避免反复尝试。

        def _compile_one(py_file: Path) -> tuple[Path, int]:
            """单文件编译 worker：调 nuitka --module，返回 (文件路径, 退出码)."""
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
            return py_file, returncode

        # 全局心跳：每 N 秒输出已完成数/总数/已耗时，避免并行编译数十秒无输出被误判卡死。
        # list 容器 [0]：主线程写（as_completed 迭代中 +1），心跳线程读（GIL 下 int 原子）。
        completed_count: list[int] = [0]
        stop_heartbeat = threading.Event()
        start_ts = time.monotonic()

        def _global_heartbeat(
            _stop: threading.Event = stop_heartbeat,
            _start: float = start_ts,
            _total: int = total,
            _done: list[int] = completed_count,
        ) -> None:
            while not _stop.wait(_HEARTBEAT_INTERVAL):
                elapsed = int(time.monotonic() - _start)
                _logger.info("Nuitka 并行编译中: 已完成 %d/%d, 已耗时 %ds", _done[0], _total, elapsed)

        hb_thread = threading.Thread(target=_global_heartbeat, daemon=True)
        hb_thread.start()
        _logger.info("提交 %d 个 .py 文件到并行编译池（max_workers=%d, jobs=%d）", total, max_workers, jobs)
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_compile_one, f): f for f in py_files}
                for future in as_completed(futures):
                    # future.result() 重抛 worker 异常（如 FileNotFoundError），由 with 块 __exit__
                    # 的 shutdown(wait=True) 等待在途任务后传播
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
            stop_heartbeat.set()
            hb_thread.join(timeout=1.0)
        return compiled_files, failed_files

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

        # stamp 未命中但 hash 索引命中：dist 完整保留但 stamp 单独丢失/损坏时，
        # 跳过编译并重建 stamp（iter-129）。索引与 stamp 同在 dist/，删除 dist 时
        # 一并清理，保证索引命中场景仅限于 dist 完整保留的情况（.pyd 产物仍在）。
        hash_index = _load_hash_index(dist_dir)
        if stamp_key in hash_index:
            _logger.info("Nuitka stamp 未命中但 hash 索引命中，跳过编译并重建 stamp")
            stage.hit_cache()
            stage.set_detail(f"hash 索引命中，nuitka {nuitka_ver} 已编译（重建 stamp）")
            try:
                _atomic_write_text(stamp, stamp_key)
            except OSError as e:
                _logger.warning("重建 Nuitka stamp 失败: %s", e)
            return

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

        # iter-137：读取上次构建失败的文件列表，传给 compile_src 跳过这些文件
        # 避免反复尝试（非源码原因失败的文件，如 Nuitka 不支持的语法）
        skip_files = _load_failed_files(dist_dir)
        if skip_files:
            _logger.info("跳过上次失败的 %d 个 .py 文件: %s", len(skip_files), sorted(skip_files))

        failed_files = cls.compile_src(
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
            skip_files=skip_files,
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

        # 编译后写 stamp（即使部分文件失败也写，避免下次重复尝试）。
        # iter-128 用原子化写入（tempfile + os.replace）：构建被 Ctrl+C 中断后，
        # 半写入的 stamp 文件可能被下次构建误读为有效缓存，跳过编译输出陈旧 .pyd。
        # 原子 rename 保证 stamp 要么完整写入要么不存在，无中间状态。
        # iter-129 同步更新 hash 索引：stamp 单独丢失/损坏时，索引命中可跳过编译重建 stamp。
        try:
            _atomic_write_text(stamp, stamp_key)
        except OSError as e:
            _logger.warning("写入 Nuitka stamp 失败: %s", e)
        _update_hash_index(dist_dir, stamp_key)
        # iter-137：写入失败文件列表，下次构建跳过这些文件避免反复尝试
        _save_failed_files(dist_dir, failed_files)
