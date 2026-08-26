"""Nuitka 编译流程 mixin：编译编排 + stamp 缓存.

本模块是 :class:`fspack.packaging.nuitka.NuitkaCompiler` 的编译 mixin 主体，
仅含 staticmethod/classmethod 无实例状态。通过多继承组合到 ``NuitkaCompiler``
facade，所有 ``cls.`` 调用经 MRO 自动派发到对应 mixin。

职责拆分（iter-149 深化）：

- **indexes.py**：hash 索引 + 失败文件列表（纯模块级函数，与 mixin 类无关）
- **progress.py**：``NuitkaProgress`` mixin — 流式 subprocess 输出（``_stream_compile``）
  + 并行编译池（``_compile_files``）
- **本模块 compile.py**：``NuitkaCompile`` mixin — 编译编排入口
  （``compile_src`` / ``compile_packages`` / ``compile_with_stamp``）
  + stamp 缓存（``_stamp_path`` / ``_stamp_key``）

**测试 patch 兼容**：以下常量/名字保留在本模块顶层（从 progress.py/indexes.py
re-export），维持 ``fspack.packaging.nuitka.compile.*`` patch 路径不变：

- ``_HEARTBEAT_INTERVAL`` / ``_MAX_COMPILE_WORKERS`` / ``_STREAM_ACCUM_LIMIT``
  / ``_COMPILE_TIMEOUT`` / ``_DRAIN_JOIN_TIMEOUT`` / ``_HASH_INDEX_MAX``
- ``_atomic_write_text`` / ``_safe_unlink``
- ``ThreadPoolExecutor``（被 ``CapturingTPE`` 替换测试）

跨 mixin 调用（``cls.<method>()``）通过
:class:`fspack.packaging.nuitka.protocol.NuitkaCompilerProtocol` 类型契约声明。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor  # noqa: F401  - 维持 patch 路径
from pathlib import Path
from typing import TYPE_CHECKING

from fspack._util.fsutil import atomic_write_text, safe_unlink
from fspack.config import MirrorConfig, nuitka_version_for
from fspack.exceptions import NuitkaError

# -- 索引相关函数/常量：从 indexes.py re-export，维持 patch 路径。
# 注意：``_atomic_write_text`` / ``_safe_unlink`` **单独在本模块定义薄封装**（直接调 util 层），
# 不从 indexes.py 导入——这样 indexes.py 内部的 dispatch 机制通过
# ``getattr(compile_mod, "_atomic_write_text")`` 动态获取 compile 模块属性时，
# 拿到的是 compile 层独立的薄封装（或被 monkeypatch 替换后的 mock 函数），
# 不会与 indexes 的 dispatch 函数对象混淆，避免递归死循环。
from fspack.packaging.nuitka.indexes import (  # noqa: F401 - re-export for patch compat
    _HASH_INDEX_MAX,
    _failed_files_path,
    _hash_index_path,
    _load_failed_files,
    _load_hash_index,
    _save_failed_files,
    _update_hash_index,
)

# -- 进度相关常量：从 progress.py re-export，维持 fspack.packaging.nuitka.compile.* patch 路径
from fspack.packaging.nuitka.progress import (  # noqa: F401 - re-export for patch compat
    _COMPILE_TIMEOUT,
    _DRAIN_JOIN_TIMEOUT,
    _HEARTBEAT_INTERVAL,
    _MAX_COMPILE_WORKERS,
    _STREAM_ACCUM_LIMIT,
)
from fspack.packaging.pyc.source_strip import _is_in_data_dirs
from fspack.platform import Platform
from fspack.progress import StageRecorder

if TYPE_CHECKING:
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

# 共享 logger 名：测试用 caplog.at_level(..., logger="fspack.packaging.nuitka") 锁定
_logger = logging.getLogger("fspack.packaging.nuitka")

# 损坏自愈阈值：编译产物异常数 ≥3 且 ≥50% 时判定编译缓存污染，清缓存重试一轮。
# 阈值设计：≤2 个异常多为单文件边界问题（文件名特殊字符等），清缓存重编整轮
# 代价（数分钟）不值；数量过半才具有"缓存级系统性损坏"特征（坏 clcache 条目
# 被反复命中，历史教训：系统位置的 Nuitka 缓存污染导致 .pyd 大量损坏）
_CORRUPT_RETRY_MIN = 3


def _purge_nuitka_compile_cache() -> None:
    """清空 Nuitka 编译工作缓存（``<cache_root>/nuitka-work``），损坏自愈用.

    仅清编译中间缓存（clcache/scons-config 等，``NUITKA_CACHE_DIR`` 重定向
    目标），不清下载缓存（winlibs 工具链在专用目录 ``nuitka-winlibs-mingw``，
    经 ``NUITKA_CACHE_DIR_DOWNLOADS`` 指向，不受影响）。清理后下次编译全部
    cache miss，用干净缓存重新产出——坏缓存条目（历史污染或磁盘故障）被
    彻底驱逐。
    """
    from fspack.config.cache import nuitka_work_cache_dir

    work_dir = nuitka_work_cache_dir()
    if not work_dir.is_dir():
        return
    shutil.rmtree(work_dir, ignore_errors=True)
    if work_dir.exists():
        _logger.warning("清理 Nuitka 编译缓存不完整（文件被占用?）: %s", work_dir)
    else:
        _logger.warning("已清理 Nuitka 编译缓存（损坏自愈）: %s", work_dir)


def _atomic_write_text(target: Path, content: str, *, encoding: str = "utf-8") -> None:
    """原子写入文本文件：先写临时文件再 rename，避免半写入文件被读取.

    直接调 :func:`fspack._util.fsutil.atomic_write_text`。
    **本薄封装必须是 compile 模块自己的函数对象**：indexes.py 内部的 dispatch
    机制会 ``getattr(compile_mod, "_atomic_write_text")`` 动态引用它，
    monkeypatch 修改 ``fspack.packaging.nuitka.compile._atomic_write_text``
    注入 OSError 时也作用于此对象。
    """
    atomic_write_text(target, content, encoding=encoding)


def _safe_unlink(path: Path) -> None:
    """删除文件，OSError 仅告警不抛（用于索引损坏时的清理）.

    直接调 :func:`fspack._util.fsutil.safe_unlink`，沿用本模块 logger。
    与 :func:`_atomic_write_text` 同理：必须是 compile 模块自有函数对象，
    保证 indexes 的 dispatch 与 monkeypatch 路径兼容。
    """
    safe_unlink(path, logger=_logger)


class NuitkaCompile:
    """Nuitka 编译流程 mixin：编译编排 + stamp 缓存.

    所有方法为 staticmethod/classmethod，无实例状态。
    通过 :class:`fspack.packaging.nuitka.NuitkaCompiler` 多继承组合使用。
    ``_stream_compile`` 与 ``_compile_files`` 已拆分到
    :class:`fspack.packaging.nuitka.progress.NuitkaProgress` mixin。

    跨 mixin 调用（``cls.<method>()``）通过 :class:`NuitkaCompilerProtocol`
    类型契约声明，pyrefly 据此解析方法签名，无需 stub 方法占位。运行时由
    :class:`NuitkaCompiler` MRO 链派发到对应 mixin 的真实实现。

    依赖 :class:`fspack.packaging.nuitka.env.NuitkaEnv` 提供：
    ``_runtime_python`` / ``_is_nuitka_cached`` / ``_build_compile_env``
    / ``ensure_env`` / ``_nuitka_cache_dir``。

    依赖 :class:`fspack.packaging.nuitka.standalone.NuitkaStandalone` 提供：
    ``_ensure_build_python``。

    依赖 :class:`fspack.packaging.nuitka.ccache.NuitkaCcache` 提供：
    ``_ensure_ccache``。

    依赖 :class:`fspack.packaging.nuitka.strip.NuitkaStrip` 提供：
    ``_strip_compiled_sources`` / ``_cleanup_build_dirs``（经 MRO 派发）。

    依赖 :class:`fspack.packaging.nuitka.progress.NuitkaProgress` 提供：
    ``_stream_compile`` / ``_compile_files``（经 MRO 派发）。
    """

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
        data_dirs: tuple[Path, ...] = (),
        compiler: str = "auto",
    ) -> list[str]:
        """编译 ``src_dir`` 下所有 ``.py`` 为 ``.pyd``/``.so``，编译后删除 ``.py`` 源码.

        返回失败文件的相对 POSIX 路径列表（相对 ``src_dir``），供调用方
        :meth:`compile_with_stamp` 写入 ``.nuitka_failed_files.json`` 作诊断记录
        （stamp 未命中时下次构建全量重试，不据其跳过文件）。

        Args:
            skip_files: 需跳过的文件相对 ``src_dir`` 的 POSIX 路径集合，由
                :meth:`_collect_py_files` 排除（不编译不删除）。
                None 表示不跳过任何文件。``compile_with_stamp`` 恒传 None
                （源码变化后失败文件可能已修复，须全量重试）。
            data_dirs: 数据资源目录树（``[tool.fspack] data-dirs`` 与
                ``web-static-dirs`` 解析到 ``src_dir`` 下的绝对路径元组），
                其下 ``.py`` 是模板/前端产物等数据资源，不编译不删除
                （与 ``_precompile_pyc``/``_strip_py_sources`` 的保护语义一致，
                如 fspack 自构建时 ``assets/templates/`` 含完整示例项目模板）。

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
        4. 产物异常率过高（≥:data:`_CORRUPT_RETRY_MIN` 且过半）时清 Nuitka
           编译缓存重试一轮（编译缓存污染自愈，仅一次防死循环）
        5. 清理 Nuitka 临时构建文件（``.build/`` 目录）

        单文件编译失败仅告警不中断，已成功编译的 ``.pyd`` 仍可用。``__init__.py``
        不编译不删除，保留 ``.py`` 维持包标识（与 :func:`fspack.builder._strip_py_sources`
        策略一致，避免 PEP 420 命名空间包导致 ``.pyd``/``.pyc`` 不被识别为包成员）。

        **入口文件跳过**（``entry_rels``）：入口包装器用 ``runpy.run_path()`` 显式
        指定 ``.py`` 路径调用用户代码（按 project_memory 约定，用户拒绝直接 import
        方案）。若入口 ``.py`` 被 Nuitka 编译后删除，``run_path`` 会
        ``FileNotFoundError``。故入口文件必须保留 ``.py`` 形态，由预编译字节码阶段
        编译为 ``.pyc`` 优化（速度略逊 ``.pyd`` 但兼容 ``run_path``）。
        """
        py_exe = cls._resolve_compile_python(build_python_exe, runtime_dir, py_version, target, stage)
        if py_exe is None:
            return []

        if not cls._is_nuitka_cached(nuitka_cache):  # NuitkaEnv mixin（MRO 派发）
            _logger.warning(
                "Nuitka 编译跳过: 缓存目录无 nuitka %s，请用 fsp b --nuitka 触发安装",
                nuitka_cache,
            )
            stage.set_detail("nuitka 未安装，跳过（回退到 .pyc 模式）")
            return []

        py_files = cls._collect_py_files(src_dir, entry_rels, skip_files, data_dirs)
        if not py_files:
            stage.set_detail("无 .py 文件可编译")
            return []

        # ccache 就绪：优先系统 PATH，缺失则下载到 ~/.fspack/cache/ccache/
        ccache_exe = None
        if ccache and cache_root is not None:
            ccache_exe = cls._ensure_ccache(cache_root, target, stage)  # NuitkaCcache mixin（MRO 派发）

        bootstrap_script = cls._create_bootstrap_script(nuitka_cache)
        try:
            # 损坏自愈循环：首轮编译后产物异常率过高（编译缓存污染特征）时，
            # 清 Nuitka 编译缓存（_purge_nuitka_compile_cache）重试一轮（仅
            # 一次防死循环）。重试轮重新收集 py_files——首轮成功剥离的 .py
            # 已删除（对应 .pyd 已验证有效无需重编），仅重编仍存在 .py 的
            # 异常文件
            for attempt in range(2):
                try:
                    compiled_files, failed_files = cls._compile_files(
                        py_exe,
                        bootstrap_script,
                        py_files,
                        stage,
                        target=target,
                        ccache_exe=ccache_exe,
                        py_version=py_version,
                        compiler=compiler,
                    )
                finally:
                    shutil.rmtree(bootstrap_script.parent, ignore_errors=True)

                # 验证 .pyd 可加载才删除 .py：防御层（历史教训：Nuitka zig 编译器产物
                # 曾大量损坏——returncode==0 但运行时访问违例，现已强制 winlibs 根治，
                # 验证保留兜底编译器异常/静默失败）。用 runtime python（.pyd ABI 绑定
                # runtime）批量 import 验证，损坏的 .pyd 删除产物保留 .py，回退到 .pyc 加载。
                runtime_py_exe = cls._runtime_python(runtime_dir, py_version, target)  # NuitkaEnv mixin（MRO 派发）
                verify_py_exe = runtime_py_exe if runtime_py_exe.is_file() else None
                stripped = cls._strip_compiled_sources(
                    compiled_files,
                    stage,
                    verify_py_exe=verify_py_exe,
                    verify_search_root=src_dir if verify_py_exe is not None else None,
                )

                # 产物异常数 = 编译成功但未剥离 .py 的数量（verify 判损坏 +
                # 产物缺失 + 删除失败），过半且 ≥3 时是编译缓存级系统性损坏，
                # 清缓存重试；首轮损坏率低时直接结束
                corrupted = len(compiled_files) - stripped
                if (
                    attempt == 0
                    and compiled_files
                    and corrupted >= _CORRUPT_RETRY_MIN
                    and corrupted * 2 >= len(compiled_files)
                ):
                    _logger.warning(
                        "编译产物异常 %d/%d 个（数量过半），疑为编译缓存污染，清理 Nuitka 编译缓存后重试",
                        corrupted,
                        len(compiled_files),
                    )
                    _purge_nuitka_compile_cache()
                    py_files = cls._collect_py_files(src_dir, entry_rels, skip_files, data_dirs)
                    if not py_files:
                        break
                    # 重试轮重建 bootstrap 临时脚本（上轮已随临时目录清理）
                    bootstrap_script = cls._create_bootstrap_script(nuitka_cache)
                    continue
                break
        finally:
            # 清理 Nuitka 编译失败的 .build 残留目录（--remove-output 仅成功时清理）。
            # 放在 finally：_compile_files 抛异常时也清理，避免残留目录污染下次构建。
            cls._cleanup_build_dirs(src_dir)
        compiled = len(compiled_files)
        if failed_files:
            stage.set_detail(f"编译 {compiled} 个，失败 {len(failed_files)} 个，剥离 {stripped} 个 .py")
        else:
            stage.set_detail(f"编译 {compiled} 个，剥离 {stripped} 个 .py")
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
        compiler: str = "auto",
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
        """
        if not packages:
            return

        py_exe = cls._resolve_compile_python(build_python_exe, runtime_dir, py_version, target, stage)
        if py_exe is None:
            return

        if not cls._is_nuitka_cached(nuitka_cache):  # NuitkaEnv mixin（MRO 派发）
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
            ccache_exe = cls._ensure_ccache(cache_root, target, stage)  # NuitkaCcache mixin（MRO 派发）

        bootstrap_script = cls._create_bootstrap_script(nuitka_cache)
        try:
            compiled_files, failed_files = cls._compile_files(
                py_exe,
                bootstrap_script,
                py_files,
                stage,
                target=target,
                ccache_exe=ccache_exe,
                py_version=py_version,
                compiler=compiler,
            )
        finally:
            shutil.rmtree(bootstrap_script.parent, ignore_errors=True)

        runtime_py_exe = cls._runtime_python(runtime_dir, py_version, target)  # NuitkaEnv mixin（MRO 派发）
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
        py_exe = cls._runtime_python(runtime_dir, py_version, target)  # NuitkaEnv mixin（MRO 派发）
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
        data_dirs: tuple[Path, ...] = (),
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
        4. 指定跳过文件（``skip_files``）：相对 ``src_dir`` 的 POSIX 路径集合，
           这些文件本次构建不编译不删除。``compile_with_stamp`` 已不传该参数
           （stamp 未命中即全量重试，避免已修复文件被永久跳过）。注意：仅删除
           stamp 文件无法强制重试——hash 索引兜底命中会重建 stamp 跳过编译；
           源码变化（stamp 键变化）才是全量重试的触发条件。
        5. 数据资源目录树（``data_dirs``）：``[tool.fspack] data-dirs`` 与
           ``web-static-dirs`` 配置的目录树（绝对路径，位于 ``src_dir`` 下），
           其下 ``.py`` 是模板/前端产物等数据资源，不编译不删除。如 fspack
           自构建时 ``assets/templates/`` 含完整示例项目模板，逐一编译既拖慢
           构建也无运行收益。
        """
        py_files = sorted(
            p
            for p in src_dir.rglob("*.py")
            if not any(part.lower().endswith(".build") for part in p.parts)
            and p.name != "__init__.py"
            and not _is_in_data_dirs(p, data_dirs)
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

    @staticmethod
    def _stamp_path(dist_dir: Path) -> Path:
        """返回 Nuitka 编译 stamp 文件路径：``dist/.nuitka_compile_stamp``."""
        return dist_dir / ".nuitka_compile_stamp"

    @staticmethod
    def _stamp_key(  # noqa: PLR0913
        src_dir: Path,
        nuitka_version: str,
        py_version: str,
        entry_rels: frozenset[str] | None = None,
        nuitka_packages: tuple[str, ...] = (),
        data_dirs: tuple[Path, ...] = (),
        compiler: str = "auto",
    ) -> str:
        """计算 Nuitka 编译 stamp 键.

        要素：

        - ``nuitka_version``：切换 Nuitka 版本时强制重编（如 3.10 从 4.1.3 升级到 4.2）
        - ``py_version``：切换 Python 版本时强制重编（.pyd ABI 绑定）
        - ``src_fingerprint``：用户源码变化时强制重编（按 ``rule-01`` 闭环要求）；
          ``data_dirs`` 目录树从指纹中排除——其下 .py 不参与编译，内容变化
          （如模板示例项目编辑）不触发重编
        - ``entry_rels``：入口文件集合变化时强制重编（影响哪些文件被跳过，
          避免上次编译删除了 .py、本次新增入口跳过但 .py 已不在导致 run_path 失败）
        - ``nuitka_packages``：第三方包编译列表变化时强制重编（影响 site-packages 编译范围）
        - ``data_dirs``：数据资源目录树变化时强制重编（影响哪些文件被跳过编译，
          data-dirs 增删改变编译范围，stamp 仍命中会导致新纳入编译的文件永不编译）
        - ``compiler``：编译器选择变化时强制重编（仅非 ``auto`` 时拼接——
          不同编译器产物虽都有效，但显式指定是强意图，切换后须重编落实；
          ``auto`` 不拼接保持既有 stamp 键格式兼容，存量缓存不失效）

        ``pyc_optimize`` 不纳入：Nuitka 编译不受 .pyc 优化级别影响，
        site-packages 的 .pyc 由 :func:`_precompile_pyc` 单独缓存。

        ``data_dirs`` 须为位于 ``src_dir`` 下的绝对路径（由
        :meth:`compile_with_stamp` 调用方解析保证），本方法转相对 POSIX 路径
        供指纹排除与键拼接（排序保证顺序无关）。
        """
        from fspack.analyzer.fingerprint import cached_source_fingerprint

        data_rels = tuple(sorted(d.relative_to(src_dir).as_posix() for d in data_dirs))
        src_fp = cached_source_fingerprint(src_dir, data_rels) if src_dir.is_dir() else ""
        entry_part = ",".join(sorted(entry_rels)) if entry_rels else ""
        pkg_part = ",".join(nuitka_packages) if nuitka_packages else ""
        data_part = ",".join(data_rels)
        key = f"{nuitka_version}|{py_version}|{src_fp}|{entry_part}|{pkg_part}|{data_part}"
        if compiler != "auto":
            key += f"|{compiler}"
        return key

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
        data_dirs: tuple[Path, ...] = (),
        compiler: str = "auto",
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

        ``data_dirs`` 为数据资源目录树（``[tool.fspack] data-dirs`` 与
        ``web-static-dirs`` 解析到 ``src_dir`` 下的绝对路径元组），透传给
        :meth:`compile_src` 排除其下 ``.py``，并纳入 :meth:`_stamp_key`。

        ``compiler``（``auto``/``msvc``/``mingw``）透传给 :meth:`ensure_env`
        （winlibs 预填充决策）与 :meth:`compile_src`（``--mingw64`` 强制 flag），
        并纳入 :meth:`_stamp_key`（非 auto 时拼接，切换编译器强制重编）。

        **回退机制**：Nuitka 是可选优化（默认关闭），环境就绪失败时不应中断构建。
        :meth:`ensure_env`（nuitka 安装、C 编译器检查）与 :meth:`_ensure_build_python`
        （standalone python 下载）任一抛 :class:`NuitkaError` 时，warning 并 return，
        回退到 .pyc 模式（由 :func:`fspack.builder._precompile_pyc` 接管）。
        :meth:`compile_src` 的单文件编译失败不触发回退（已有 warning 继续）。
        """
        nuitka_ver = nuitka_version_for(py_version)
        stamp = cls._stamp_path(dist_dir)
        stamp_key = cls._stamp_key(src_dir, nuitka_ver, py_version, entry_rels, nuitka_packages, data_dirs, compiler)

        # stamp 命中：跳过整个 Nuitka 阶段
        try:
            if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
                _logger.info("Nuitka stamp 命中，跳过编译")
                stage.hit_cache()
                stage.set_detail(f"stamp 命中，nuitka {nuitka_ver} 已编译")
                return
        except (OSError, UnicodeDecodeError):
            pass

        # stamp 未命中但 hash 索引命中：dist 完整保留但 stamp 单独丢失/损坏时，
        # 跳过编译并重建 stamp。索引与 stamp 同在 dist/，删除 dist 时
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
                cache_root, py_version, target, mirror, stage=stage, compiler=compiler
            )  # NuitkaEnv mixin（MRO 派发）
            nuitka_cache = cls._nuitka_cache_dir(cache_root, py_version)  # NuitkaEnv mixin（MRO 派发）

            # Windows 编译环境：下载 python-build-standalone 完整发行版运行 nuitka
            # embed runtime python 不完整（无 .py 源码、_pth 限制 sys.path），Nuitka 的
            # reExecute 机制（os._exit 子进程 + scons 调用）会反复衍生 python.exe 子进程
            # 导致 CPU 卡死（Nuitka 官方文档称此为 Fork Bomb）。
            # standalone python 是完整 CPython，sys.executable 可被 nuitka/scons 安全调用。
            # Linux runtime 已是 standalone，返回空 Path 占位（compile_src 内部回退到 runtime python）。
            build_python_exe = cls._ensure_build_python(  # NuitkaStandalone mixin（MRO 派发）
                cache_root.parent / "python",
                py_version,
                target,
                stage=stage,
            )
        except NuitkaError as e:
            _logger.warning("Nuitka 环境就绪失败，回退到 .pyc 模式: %s", e)
            stage.set_detail(f"回退到 .pyc 模式: {e}")
            return

        # stamp 未命中（源码已变化）时不读取上次失败文件列表：失败文件可能已被
        # 用户修复，若继续传 skip_files 跳过且编译后用不含该文件的新列表覆盖写入，
        # 该文件将永远不被编译（旧 BUG）。缓存命中路径（stamp/hash 索引命中）直接
        # 早退不编译，无需 skip_files。故编译路径恒全量重试，失败列表仅作诊断记录。
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
            data_dirs=data_dirs,
            compiler=compiler,
        )

        # 编译用户指定的第三方包（site-packages 中的纯 Python 包）
        if nuitka_packages:
            site_packages = dist_dir / "site-packages"
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
                    compiler=compiler,
                )
            else:
                _logger.warning("site-packages 不存在，跳过包编译: %s", site_packages)

        # 编译后写 stamp（即使部分文件失败也写，避免下次重复尝试）。
        # 原子化写入（tempfile + os.replace）：构建被 Ctrl+C 中断后，
        # 半写入的 stamp 文件可能被下次构建误读为有效缓存，跳过编译输出陈旧 .pyd。
        # 原子 rename 保证 stamp 要么完整写入要么不存在，无中间状态。
        # 同步更新 hash 索引：stamp 单独丢失/损坏时，索引命中可跳过编译重建 stamp。
        try:
            _atomic_write_text(stamp, stamp_key)
        except OSError as e:
            _logger.warning("写入 Nuitka stamp 失败: %s", e)
        _update_hash_index(dist_dir, stamp_key)
        # 写入失败文件列表（诊断记录：用户可据此定位反复失败的文件；
        # stamp 未命中时下次构建全量重试，不再据其跳过）
        _save_failed_files(dist_dir, failed_files)
        # Nuitka 编译已修改 dist/src 树（删除成功编译的 .py），失效构建级
        # 指纹缓存，保证后续 pyc stamp 等阶段的指纹反映最新目录树状态
        from fspack.analyzer.fingerprint import clear_fingerprint_cache

        clear_fingerprint_cache()
