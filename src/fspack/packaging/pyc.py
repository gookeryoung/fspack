"""字节码预编译：.pyc 编译、Win7 兼容 DLL 注入、Linux stdlib 精简.

本模块从 :mod:`fspack.builder` 抽离，仅含字节码预编译相关函数。
``builder.py`` 通过 re-export 保持公开 API 不变。

依赖 :mod:`fspack.packaging.sync` 提供 ``_dir_size``（用于 ``_trim_stdlib``）
与 ``_site_packages_fingerprint``（用于 ``_pyc_stamp_key``）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from fspack.packaging.sync import _dir_size, _site_packages_fingerprint
from fspack.platform import Platform
from fspack.progress import StageRecorder

_logger = logging.getLogger(__name__)

# Win7 兼容性 DLL：Python 3.9+ 官方不再支持 Win7，需注入 api-ms-win-core-path-l1-1-0.dll。
# DLL 来源 https://github.com/adang1345/api-ms-win-core-path（LGPL-2.1，基于 Wine 实现）。
# 随 fspack 分发（assets/runtime/），无需网络下载。
_WIN7_COMPAT_DLL_NAME = "api-ms-win-core-path-l1-1-0.dll"

# Linux standalone 标准库精简：剥离运行时无用的模块目录。
# Windows embed 标准库在 python3XX.zip 内（只读、官方已精简），无需处理。
_STDLIB_TRIM_DIRS = ("test", "ensurepip", "idlelib", "pydoc_data", "turtledemo", "tkinter/test", "sqlite3/test")


def _needs_win7_compat_dll(py_version: str) -> bool:
    """Python 3.9+ 官方不再支持 Win7，需注入兼容 DLL.

    Python 3.8 是最后官方支持 Win7 的版本；3.9+ 调用 ``PathCchSkipRoot`` 等
    API，需 ``api-ms-win-core-path-l1-1-0.dll`` 提供（Win8+ 自带，Win7 缺失）。
    """
    parts = py_version.split(".")
    return (int(parts[0]), int(parts[1])) >= (3, 9)


def _inject_win7_compat_dll(runtime_dir: Path) -> None:
    """将内置 ``api-ms-win-core-path-l1-1-0.dll`` 复制到 runtime 根目录.

    Python 3.9+ 在 Win7 SP1 上启动时需此 DLL（提供 ``PathCchSkipRoot`` 等 API）。
    DLL 随 fspack 分发（``assets/runtime/``），无需网络下载。重复构建时若
    DLL 已存在则跳过。DLL 缺失时仅告警不报错（向后兼容旧 fspack 安装）。
    """
    dest = runtime_dir / _WIN7_COMPAT_DLL_NAME
    if dest.is_file():
        _logger.info("Win7 兼容 DLL 已就绪: %s", dest)
        return
    # assets/runtime/ 与 pyc.py 同处 fspack 包内，通过 __file__ 定位
    src = Path(__file__).parent.parent / "assets" / "runtime" / _WIN7_COMPAT_DLL_NAME
    if not src.is_file():
        _logger.warning("Win7 兼容 DLL 缺失: %s，跳过注入", src)
        return
    shutil.copy2(src, dest)
    _logger.info("注入 Win7 兼容 DLL: %s", dest)


def _trim_stdlib(runtime_dir: Path, py_version: str, target: Platform, stage: StageRecorder) -> None:
    """剥离 Linux standalone 标准库中运行时无用的模块目录.

    Windows embed 标准库在 python3XX.zip 内（只读、官方已精简），跳过。
    重复构建时已剥离的目录不存在则跳过，幂等。
    """
    if target is not Platform.LINUX:
        stage.set_detail("embed zip 已精简，跳过")
        return
    major, minor = py_version.split(".")[:2]
    stdlib = runtime_dir / "python" / "lib" / f"python{major}.{minor}"
    if not stdlib.is_dir():
        stage.set_detail("标准库目录不存在，跳过")
        return
    removed = 0
    saved_bytes = 0
    for name in _STDLIB_TRIM_DIRS:
        d = stdlib / name
        if d.is_dir():
            saved_bytes += _dir_size(d)
            shutil.rmtree(d)
            removed += 1
            _logger.info("精简标准库: 剥离 %s", d)
    stage.skip(removed)
    stage.add_saved_bytes(saved_bytes)
    stage.set_detail(f"剥离 {removed} 目录")


def _pyc_stamp_path(dist_dir: Path) -> Path:
    """预编译 stamp 文件路径：``dist/.pyc_stamp``."""
    return dist_dir / ".pyc_stamp"


def _pyc_stamp_key(src_dir: Path, site_packages: Path, strip_py: bool, optimize: int = 0) -> str:
    """计算预编译 stamp 键：src 指纹 + site-packages 指纹 + strip_py + optimize.

    ``copy_source`` 在预编译前已将 ``.py`` 同步到 ``dist/src``（``strip_py`` 模式下
    也会重新复制），故 ``src_fp`` 始终反映完整源码状态，无需特殊处理 ``strip_py``
    的 ``.py`` 缺失场景。stamp 键在检查与写入时复用，避免重复计算指纹。

    ``optimize`` 纳入 stamp 键：切换 ``--pyc-optimize`` 时强制重编译，避免旧的
    optimize=0 .pyc 被运行时加载而无法享受 -OO 优化。
    """
    from fspack.analyzer import source_fingerprint

    src_fp = source_fingerprint(src_dir) if src_dir.is_dir() else ""
    sp_fp = _site_packages_fingerprint(site_packages)
    return f"{src_fp}|{sp_fp}|{strip_py}|{optimize}"


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
) -> None:
    """预编译 src 与 site-packages 的 .py 为 .pyc，加速首次启动.

    用 runtime 自身的 python 调用 ``compileall``，保证 ABI 一致。生成
    ``__pycache__/{name}.cpython-{ver}.pyc``，运行时默认加载。

    ``optimize`` 控制 ``compileall -o`` 级别（CPython ``compile()`` 的 ``optimize``
    参数）：

    - ``0``（默认）：保留 docstring 与 assert，最大兼容性
    - ``1``：剥离 assert，保留 docstring（``-O``）
    - ``2``：剥离 assert 与 docstring（``-OO``），体积减少 5-15%，启动提速 5-10%

    参考 rimsort 等 Nuitka 打包产物：本机代码无 docstring 开销；fspack 通过
    ``-o 2`` 编译可缩小与 Nuitka 的执行速度差距。注意 ``-OO`` 会移除 ``__doc__``
    属性，依赖文档字符串的程序（如 Sphinx 运行时）应使用 ``0`` 或 ``1``。

    ``strip_py=True`` 时额外删除非 ``__init__.py`` 的 ``.py`` 源码（保留包标识，
    避免 PEP 420 命名空间包导致 ``.pyc`` 不被加载）。``entry_rels`` 中的入口文件
    跳过剥离（入口包装器需 ``.py`` 存在以供 ``runpy`` 定位）。

    重复构建时用 ``dist/.pyc_stamp``（src 指纹 + site-packages 指纹 + strip_py +
    optimize）跳过 compileall，避免 subprocess 启动与文件遍历开销。
    """
    if target is Platform.WINDOWS:
        py_exe = runtime_dir / "python.exe"
        site_packages = runtime_dir / "Lib" / "site-packages"
    else:
        major, minor = py_version.split(".")[:2]
        py_exe = runtime_dir / "python" / "bin" / f"python{major}.{minor}"
        site_packages = runtime_dir / "python" / "lib" / f"python{major}.{minor}" / "site-packages"
    src_dir = dist_dir / "src"
    if not py_exe.is_file():
        _logger.warning("预编译跳过: runtime python 未就绪 %s", py_exe)
        stage.set_detail("runtime python 未就绪，跳过")
        return

    # stamp 检查：命中则跳过 compileall，stamp_key 留待未命中时写入
    stamp_key = _pyc_stamp_key(src_dir, site_packages, strip_py, optimize)
    stamp = _pyc_stamp_path(dist_dir)
    try:
        if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
            stage.hit_cache()
            stage.set_detail("缓存命中，跳过编译")
            return
    except OSError:
        pass

    targets = [d for d in (src_dir, site_packages) if d.is_dir()]
    compiled = 0
    if targets:
        # 合并多目录为单次 compileall 调用，减少 subprocess 启动开销（~50-100ms/次）
        # compileall 支持多位置参数：python -m compileall dir1 dir2 -q -j 0 -o N
        result = subprocess.run(
            [str(py_exe), "-m", "compileall", *[str(d) for d in targets], "-q", "-j", "0", "-o", str(optimize)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            _logger.warning("compileall 失败: %s", result.stderr.strip())
        else:
            compiled = len(targets)
        stage.processed()

    # 写 stamp（编译后、strip 前写入，存编译前的 src_fp）
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(stamp_key, encoding="utf-8")

    stripped = _strip_py_sources(targets, entry_rels, optimize=optimize, py_version=py_version) if strip_py else 0
    if stripped:
        stage.skip(stripped)
        stage.set_detail(f"编译 {compiled} 目录，剥离 {stripped} 个 .py")
    else:
        stage.set_detail(f"编译 {compiled} 目录")


def _strip_py_sources(
    targets: list[Path],
    entry_rels: frozenset[str] = frozenset(),
    *,
    optimize: int = 0,
    py_version: str = "",
) -> int:
    """删除 targets 中非 ``__init__.py`` 的 ``.py`` 源码，返回剥离数量.

    保留 ``__init__.py`` 维持包标识，避免 PEP 420 命名空间包导致 ``.pyc`` 不被加载。

    **PEP 3147 迁移**：删除 ``.py`` 前，将对应的
    ``__pycache__/{stem}.cpython-{ver}{opt}.pyc`` 迁移到 ``{stem}.pyc``（legacy 布局）。
    PEP 3147 规定 ``__pycache__`` 中的 ``.pyc`` 仅在源码 ``.py`` 存在时才被加载，
    删除 ``.py`` 后 Python 不会从 ``__pycache__`` 加载 ``.pyc``，必须迁移到 legacy
    布局才能被 :class:`importlib.machinery.SourcelessFileLoader` 加载。
    若 ``.pyc`` 不存在（编译失败），保留 ``.py`` 避免模块完全丢失。

    ``entry_rels`` 为入口文件相对 ``targets[0]``（dist/src）的 POSIX 路径集合，
    这些文件会被跳过：入口包装器用 ``runpy.run_module``/``run_path`` 调用用户代码，
    需 ``.py`` 存在才能被 ``find_spec`` 定位（``__pycache__`` 下的 ``.pyc`` 不在
    ``FileFinder`` 搜索范围，``.pyd`` 模块无 Python 字节码无法被 ``runpy`` 执行）。
    """
    # 推导 .pyc 文件名后缀：cpython-{major}{minor}[-opt-N]
    if py_version:
        major, minor = py_version.split(".")[:2]
        ver_tag = f"cpython-{major}{minor}"
    else:  # pragma: no cover - py_version 始终由 _precompile_pyc 传入
        ver_tag = "cpython-*"
    opt_suffix = "" if optimize == 0 else f".opt-{optimize}"
    pyc_name_pattern = f"{{stem}}.{ver_tag}{opt_suffix}.pyc"

    stripped = 0
    for d in targets:
        for py in d.rglob("*.py"):
            if py.name == "__init__.py":
                continue
            try:
                rel = py.relative_to(d).as_posix()
            except ValueError:  # pragma: no cover - rglob 结果必在 d 下
                rel = ""
            if rel in entry_rels:
                continue
            # 迁移 .pyc 到 legacy 布局，确保无源码时仍可加载
            pyc_in_cache = py.parent / "__pycache__" / pyc_name_pattern.format(stem=py.stem)
            if pyc_in_cache.is_file():
                legacy_pyc = py.parent / f"{py.stem}.pyc"
                try:
                    # 已存在同名 legacy .pyc 时先删除（避免 rename 失败）
                    if legacy_pyc.is_file():
                        legacy_pyc.unlink()
                    pyc_in_cache.rename(legacy_pyc)
                except OSError as e:  # pragma: no cover - 文件系统异常容错
                    _logger.warning("迁移 .pyc 到 legacy 布局失败 %s: %s", pyc_in_cache, e)
                    continue
            else:
                # .pyc 不存在（编译失败），保留 .py 避免模块完全丢失
                continue
            py.unlink()
            stripped += 1
    return stripped
