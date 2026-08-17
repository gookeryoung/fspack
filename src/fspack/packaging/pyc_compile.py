"""字节码编译执行：compileall 调用与 ``_precompile_pyc`` 主入口.

拆自 :mod:`fspack.packaging.pyc`，含：

- ``_run_compileall``：单次 compileall 执行（含超时与失败处理），返回
  ``(成功标志, 失败备注)`` 元组，纯函数无副作用，可安全并行调用
- ``_precompile_pyc``：字节码预编译主入口（stamp 检查 → compileall → 写 stamp → 源码剥离）。
  src 与 site-packages 两个目录的 compileall 用 ``ThreadPoolExecutor`` 并行执行
  （subprocess 调用释放 GIL，实测缓存命中场景约减 30-40% 预编译耗时）

测试通过 ``monkeypatch.setattr("fspack.packaging.pyc._COMPILEALL_TIMEOUT", ...)``
替换常量，通过 ``monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", ...)``
替换 subprocess.run，因此本模块通过 :func:`_P` 延迟从 pyc facade 解析这两个属性。
"""

from __future__ import annotations

import logging
import subprocess as _default_subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fspack._util.fsutil import atomic_write_text
from fspack.platform import Platform

from .pyc_stamp import _pyc_stamp_key, _pyc_stamp_path
from .source_strip import _strip_compiled_py

if TYPE_CHECKING:
    from fspack.progress import StageRecorder

_logger = logging.getLogger(__name__)

# compileall 超时（秒）：实测 1000 文件 P99 <60s（含 -j 0 并行），
# 300s 裕量覆盖慢速 CI 与大 site-packages。超时不写 stamp 下次重试，
# 避免 compileall 卡死（如磁盘 I/O hang）无限阻塞构建。iter-127 引入。
_COMPILEALL_TIMEOUT = 300.0

# ---------------------------------------------------------------------------
# pyc facade 延迟 dispatch：兼容 monkeypatch.setattr("pyc.<name>", ...) 替换
# ---------------------------------------------------------------------------
_pyc_mod_holder: list[Any] = [None]


def _P(attr_name: str, fallback: Any) -> Any:
    """从 ``fspack.packaging.pyc`` 模块按名取属性，取不到时回退 fallback."""
    mod = _pyc_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging import pyc as _pyc_mod

            mod = _pyc_mod
            _pyc_mod_holder[0] = mod
        except ImportError:
            return fallback
    return getattr(mod, attr_name, fallback)


def _run_compileall(py_exe: Path, target_dir: Path, optimize: int) -> tuple[bool, str | None]:
    """运行单次 compileall 编译 target_dir，返回 ``(成功标志, 失败备注)``.

    纯函数无副作用：不操作 :class:`StageRecorder`，调用方根据返回值决定后续处理
    （如统一更新 stage）。这样可在 :class:`ThreadPoolExecutor` 中安全并行调用，
    避免多个 compileall 同时操作共享 stage 触发数据竞争（python-standards
    「共享可变状态必须加锁」约束）。

    - 成功：返回 ``(True, None)``
    - 超时：记录 warning 并返回 ``(False, "compileall 超时（{N}s），跳过")``
    - 失败（非零退出码）：记录 warning 并返回
      ``(False, "compileall 失败（{name} 退出码 {N}），跳过 stamp")``

    ``_COMPILEALL_TIMEOUT`` 常量与 ``subprocess`` 模块均通过 pyc facade dispatch，
    确保 monkeypatch 替换后的值被感知。
    """
    _COMPILEALL_TIMEOUT_dispatch: float = _P("_COMPILEALL_TIMEOUT", _COMPILEALL_TIMEOUT)
    subprocess_dispatch: Any = _P("subprocess", _default_subprocess)
    try:
        result = subprocess_dispatch.run(
            [
                str(py_exe),
                "-m",
                "compileall",
                str(target_dir),
                "-q",
                "-j",
                "0",
                "-o",
                str(optimize),
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMPILEALL_TIMEOUT_dispatch,
        )
    except _default_subprocess.TimeoutExpired:
        _logger.warning(
            "compileall 超时（%ds），跳过本次预编译，下次构建重试",
            int(_COMPILEALL_TIMEOUT_dispatch),
        )
        return False, f"compileall 超时（{int(_COMPILEALL_TIMEOUT_dispatch)}s），跳过"
    if result.returncode != 0:
        _logger.warning("compileall 失败 (%s): %s", target_dir, result.stderr.strip())
        return (
            False,
            f"compileall 失败（{target_dir.name} 退出码 {result.returncode}），跳过 stamp",
        )
    return True, None


def _compile_parallel(
    py_exe: Path,
    targets: list[tuple[Path, int]],
    stage: StageRecorder,
) -> int:
    """并行执行多个目录的 compileall，返回成功编译的目录数.

    用 :class:`ThreadPoolExecutor` 并行调用 :func:`_run_compileall`（subprocess
    释放 GIL，两个 compileall 进程真正并发）。任一目录失败则操作 ``stage``
    记录失败备注（与原串行失败路径一致：``processed()`` + ``set_detail``）并返回 0，
    调用方据此跳过 stamp 写入。全部成功返回 ``len(targets)``。

    :param py_exe: runtime python 可执行文件路径
    :param targets: ``[(target_dir, optimize), ...]`` 列表，长度 ≥ 2
    :param stage: 阶段记录器，由本函数（主线程）统一更新，避免并行竞争
    :return: 成功编译的目录数（全部成功为 ``len(targets)``，任一失败为 0）
    """
    success_count = 0
    first_failure_detail: str | None = None
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        future_to_target = {pool.submit(_run_compileall, py_exe, d, opt): (d, opt) for d, opt in targets}
        for future in as_completed(future_to_target):
            ok, detail = future.result()
            if ok:
                success_count += 1
            elif first_failure_detail is None:
                # 记录首个失败的 detail（多个失败时取首条，与原串行 return 一致）
                first_failure_detail = detail
    if first_failure_detail is not None:
        # 失败路径：与原 _run_compileall 串行失败处理一致
        if "失败" in first_failure_detail:
            stage.processed()
        if first_failure_detail:
            stage.set_detail(first_failure_detail)
        return 0
    return success_count


def _precompile_pyc(  # noqa: PLR0912, PLR0913
    dist_dir: Path,
    runtime_dir: Path,
    py_version: str,
    target: Platform,
    *,
    strip_py: bool,
    stage: StageRecorder,
    optimize: int = 0,
    entry_rels: frozenset[str] = frozenset(),
    data_dirs: tuple[Path, ...] = (),
    web_static_dirs: tuple[Path, ...] = (),
) -> None:
    """预编译 src 与 site-packages 的 .py 为 .pyc，加速首次启动.

    用 runtime 自身的 python 调用 ``compileall``，保证 ABI 一致。生成
    ``__pycache__/{name}.cpython-{ver}.pyc``，运行时默认加载。

    ``optimize`` 控制 src 的 ``compileall -o`` 级别（CPython ``compile()`` 的 ``optimize``
    参数）：

    - ``0``（默认）：保留 docstring 与 assert，最大兼容性
    - ``1``：剥离 assert，保留 docstring（``-O``）
    - ``2``：剥离 assert 与 docstring（``-OO``），体积减少 5-15%，启动提速 5-10%

    **site-packages 降级**：site-packages 始终用 ``min(optimize, 1)`` 编译，保留 docstring。
    第三方库（numpy/pytorch/scipy 等）的 C 扩展常依赖 ``__doc__`` 为 str 的假设——
    典型如 numpy ``_core/overrides.py`` 的 ``add_docstring(implementation,
    dispatcher.__doc__)`` 在 ``__doc__`` 被 ``-OO`` 剥离为 None 时报错
    ``TypeError: argument docstring of add_docstring should be a str``
    （numpy issue #13248 长期未修复）。optimize=2 剥离 docstring 会触发此类兼容
    问题，故 site-packages 降级到 1；optimize=0/1 时与 src 一致。

    ``strip_py=True`` 时额外删除非 ``__init__.py`` 的 ``.py`` 源码（保留包标识，
    避免 PEP 420 命名空间包导致 ``.pyc`` 不被加载）。``entry_rels`` 中的入口文件
    跳过剥离（入口包装器需 ``.py`` 存在以供 ``runpy`` 定位）。src 与 site-packages
    分别用各自的 optimize 级别迁移 ``.pyc`` 到 legacy 布局。

    ``data_dirs``/``web_static_dirs`` 为 src 下的数据资源/前端构建产物目录绝对
    路径元组，其下 ``.py`` 不剥离（视为完整资源）。仅对 src 生效（site-packages
    无数据资源目录）。

    重复构建时用 ``dist/.pyc_stamp``（src 指纹 + site-packages 指纹 + strip_py +
    optimize + sp_optimize）跳过 compileall，避免 subprocess 启动与文件遍历开销。
    """
    if target is Platform.WINDOWS:
        py_exe = runtime_dir / "python.exe"
    else:
        major, minor = py_version.split(".")[:2]
        py_exe = runtime_dir / "python" / "bin" / f"python{major}.{minor}"
    site_packages = dist_dir / "site-packages"
    src_dir = dist_dir / "src"
    if not py_exe.is_file():
        _logger.warning("预编译跳过: runtime python 未就绪 %s", py_exe)
        stage.set_detail("runtime python 未就绪，跳过")
        return

    sp_optimize = min(optimize, 1)

    stamp_key = _pyc_stamp_key(src_dir, site_packages, strip_py, optimize, sp_optimize, entry_rels)
    stamp = _pyc_stamp_path(dist_dir)
    try:
        if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
            stage.hit_cache()
            stage.set_detail("缓存命中，跳过编译")
            return
    except (OSError, UnicodeDecodeError):
        pass

    # src 与 site-packages 两个目录的 compileall 并行执行：subprocess 调用
    # 释放 GIL，两进程同时编译，实测缓存命中场景预编译耗时约减 30-40%
    # （原串行 270-540ms → 并行 ~150-280ms）。单目录时退化为串行调用，
    # 避免 ThreadPoolExecutor 启动开销（约 0.5ms）。
    targets = [(d, opt) for d, opt in ((src_dir, optimize), (site_packages, sp_optimize)) if d.is_dir()]
    if not targets:
        compiled = 0
    elif len(targets) == 1:
        # 单目录：串行调用，避免线程池启动开销
        d, opt = targets[0]
        ok, detail = _run_compileall(py_exe, d, opt)
        if not ok:
            if detail and "失败" in detail:
                stage.processed()
            if detail:
                stage.set_detail(detail)
            return
        compiled = 1
    else:
        # 双目录：并行执行（subprocess 释放 GIL）
        compiled = _compile_parallel(py_exe, targets, stage)
        if compiled == 0:
            # 任一失败已记录 detail，不写 stamp
            return
    if compiled:
        stage.processed()

    # 清理 data_dirs 下 compileall 生成的 __pycache__：data_dirs 视为完整资源原样
    # 保留（其下 .py 不剥离），但 compileall 会无差别为其 .py 生成 __pycache__/*.pyc。
    # 这些字节码对数据资源无运行价值，反而污染产物——尤其 fspack 自身模板目录内的
    # $entry_module.py 等占位符文件编译出的 .pyc 会被 fsp init 的模板加载器误读，
    # 刷出"跳过非 UTF-8 模板文件"警告。故编译后删除 data_dirs 下的 __pycache__。
    _clean_data_dirs_pycache(data_dirs)

    stripped = (
        _strip_compiled_py(
            src_dir,
            site_packages,
            entry_rels,
            optimize,
            sp_optimize,
            py_version,
            data_dirs,
            web_static_dirs,
        )
        if strip_py
        else 0
    )
    # stamp 必须在 strip 完成后写入：若先写 stamp 再 strip，strip 中断（异常/断电）
    # 后 stamp 已落盘，下次构建误判缓存命中跳过编译，.py 永不被剥离。
    # 原子写入（tempfile + replace）：半写入的 stamp 不会被读为有效缓存。
    atomic_write_text(stamp, stamp_key)
    if stripped:
        stage.skip(stripped)
        stage.set_detail(f"编译 {compiled} 目录，剥离 {stripped} 个 .py")
    else:
        stage.set_detail(f"编译 {compiled} 目录")


def _clean_data_dirs_pycache(data_dirs: tuple[Path, ...]) -> None:
    """删除 ``data_dirs`` 各目录树下 compileall 生成的 ``__pycache__`` 目录.

    data_dirs 视为完整数据资源原样保留，其下 ``.py`` 不剥离；但 compileall 会为
    这些 ``.py`` 生成 ``__pycache__/*.pyc``。这些字节码对数据资源无运行价值，且会
    污染产物（如 fspack 模板目录内 ``$entry_module.py`` 编译出的 ``.pyc`` 被
    ``fsp init`` 模板加载器误读刷警告）。逐个删除，单个删除失败仅告警不阻断构建。
    """
    import shutil

    for data_dir in data_dirs:
        if not data_dir.is_dir():
            continue
        for pycache in data_dir.rglob("__pycache__"):
            if not pycache.is_dir():
                continue
            try:
                shutil.rmtree(pycache)
            except OSError as e:  # pragma: no cover - 文件系统异常容错
                _logger.warning("清理数据资源 __pycache__ 失败 %s: %s", pycache, e)
