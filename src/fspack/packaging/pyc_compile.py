"""字节码编译执行：compileall 调用与 ``_precompile_pyc`` 主入口.

拆自 :mod:`fspack.packaging.pyc`，含：

- ``_run_compileall``：单次 compileall 执行（含超时与失败处理）
- ``_precompile_pyc``：字节码预编译主入口（stamp 检查 → compileall → 写 stamp → 源码剥离）

测试通过 ``monkeypatch.setattr("fspack.packaging.pyc._COMPILEALL_TIMEOUT", ...)``
替换常量，通过 ``monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", ...)``
替换 subprocess.run，因此本模块通过 :func:`_P` 延迟从 pyc facade 解析这两个属性。
"""

from __future__ import annotations

import logging
import subprocess as _default_subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def _run_compileall(py_exe: Path, target_dir: Path, optimize: int, stage: StageRecorder) -> bool:
    """运行单次 compileall 编译 target_dir，成功返回 True，失败返回 False.

    失败（超时或非零退出码）时记录 warning 与 ``stage.set_detail``，不抛异常。
    ``returncode != 0`` 时调 ``stage.processed()``（与原逻辑一致：有编译活动但失败）；
    超时不调（完全无编译活动）。调用方根据返回值决定是否继续编译下一个目录。

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
        stage.set_detail(f"compileall 超时（{int(_COMPILEALL_TIMEOUT_dispatch)}s），跳过")
        return False
    if result.returncode != 0:
        _logger.warning("compileall 失败 (%s): %s", target_dir, result.stderr.strip())
        stage.processed()
        stage.set_detail(f"compileall 失败（{target_dir.name} 退出码 {result.returncode}），跳过 stamp")
        return False
    return True


def _precompile_pyc(  # noqa: PLR0913
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

    stamp_key = _pyc_stamp_key(src_dir, site_packages, strip_py, optimize, sp_optimize)
    stamp = _pyc_stamp_path(dist_dir)
    try:
        if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
            stage.hit_cache()
            stage.set_detail("缓存命中，跳过编译")
            return
    except (OSError, UnicodeDecodeError):
        pass

    compiled = 0
    for d, opt in ((src_dir, optimize), (site_packages, sp_optimize)):
        if not d.is_dir():
            continue
        if not _run_compileall(py_exe, d, opt, stage):
            return
        compiled += 1
    if compiled:
        stage.processed()

    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(stamp_key, encoding="utf-8")

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
    if stripped:
        stage.skip(stripped)
        stage.set_detail(f"编译 {compiled} 目录，剥离 {stripped} 个 .py")
    else:
        stage.set_detail(f"编译 {compiled} 目录")
