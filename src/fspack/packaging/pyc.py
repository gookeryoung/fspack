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
from typing import TYPE_CHECKING

from fspack.packaging.sync import _dir_size, _site_packages_fingerprint
from fspack.platform import Platform

if TYPE_CHECKING:
    # StageRecorder 仅用于类型注解（``from __future__ import annotations``
    # 使注解不在运行时求值），顶部不导入 fspack.progress 避免连锁触发
    # rich.progress/rich.table 加载（省 ~12ms），仅在实际编译时才加载。
    from fspack.progress import StageRecorder

_logger = logging.getLogger(__name__)

# Win7 兼容性 DLL：Python 3.9+ 官方不再支持 Win7，需注入 api-ms-win-core-path-l1-1-0.dll。
# DLL 来源 https://github.com/adang1345/api-ms-win-core-path（LGPL-2.1，基于 Wine 实现）。
# 随 fspack 分发（assets/runtime/），无需网络下载。
_WIN7_COMPAT_DLL_NAME = "api-ms-win-core-path-l1-1-0.dll"

# compileall 超时（秒）：实测 1000 文件 P99 <60s（含 -j 0 并行），
# 300s 裕量覆盖慢速 CI 与大 site-packages。超时不写 stamp 下次重试，
# 避免 compileall 卡死（如磁盘 I/O hang）无限阻塞构建。iter-127 引入。
_COMPILEALL_TIMEOUT = 300.0

# Linux standalone 标准库精简：剥离运行时无用的模块目录。
# Windows embed 标准库在 python3XX.zip 内（只读、官方已精简），无需处理。
# 顶层目录：test/ensurepip/idlelib/pydoc_data/turtledemo 是开发/测试/文档工具，运行时不用
# 嵌套 test/tests：各 stdlib 子模块下的测试目录（Python 3.12+ 移除 distutils/lib2to3 时跳过）
_STDLIB_TRIM_DIRS = (
    "test",
    "ensurepip",
    "idlelib",
    "pydoc_data",
    "turtledemo",
    "tkinter/test",
    "sqlite3/test",
    "ctypes/test",
    "unittest/test",
    "distutils/tests",
    "lib2to3/tests",
)

# Linux/macOS standalone 运行时精简：剥离构建期需要但运行时无用的文件
# python3.X 二进制（53MB）：loader 用 dlopen+Py_BytesMain，从不 exec python3.X；
#   仅 _precompile_pyc 构建期调它跑 compileall，构建完成后可删
# include/（C 头文件）：运行时不需要，仅编译 C 扩展用
# share/（terminfo + man）：终端信息与 man 手册，运行时不用
# *-config 脚本：pkg-config 配置，仅编译时用
# 2to3/idle3/pip3/pydoc3：开发工具，运行时不用（pip 已在 site-packages 中）
_STANDALONE_DEV_BIN_FILES = (
    "2to3",
    "2to3-3",
    "idle3",
    "idle3.9",
    "idle3.10",
    "idle3.11",
    "idle3.12",
    "idle3.13",
    "idle3.14",
    "pydoc3",
    "pydoc3.9",
    "pydoc3.10",
    "pydoc3.11",
    "pydoc3.12",
    "pydoc3.13",
    "pydoc3.14",
    "pip",
    "pip3",
    "pip3.9",
    "pip3.10",
    "pip3.11",
    "pip3.12",
    "pip3.13",
    "pip3.14",
)


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
    """剥离 standalone 标准库中运行时无用的模块目录.

    Windows embed 标准库在 python3XX.zip 内（只读、官方已精简），跳过。
    Linux 与 macOS 用 python-build-standalone，标准库在
    ``runtime/python/lib/pythonX.Y/`` 下，需剥离 test/ensurepip/idlelib 等。
    重复构建时已剥离的目录不存在则跳过，幂等。
    """
    if target is Platform.WINDOWS:
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


def _trim_standalone_runtime(  # noqa: PLR0912, PLR0913
    runtime_dir: Path,
    py_version: str,
    target: Platform,
    stage: StageRecorder,
    *,
    has_tkinter: bool,
    strip_symbols: bool = True,
) -> None:
    """精简 standalone runtime 到运行时最小集（在 ``_precompile_pyc`` 之后调用）.

    剥离四类运行时无用文件（仅 Linux/macOS 目标，Windows embed 已精简且无调试符号）：

    - **A. strip libpython 调试符号**：``libpython3.X.so.1.0`` 含完整 DWARF 调试
      符号（53MB），``strip --strip-all`` 后 ~19MB，运行时零影响
    - **B. 删 ``python/bin/python3.X`` 二进制**：loader 用 ``dlopen``+``Py_BytesMain``
      从不 ``exec`` 这个二进制；仅构建期 ``_precompile_pyc`` 调它跑 ``compileall``，
      构建完成后可整个删除（53MB）。同时删 ``python3``/``python`` 符号链接
    - **C. 删 ``python/include/``、``python/share/``**：C 头文件与 terminfo/man 手册
      运行时不用（~9MB）
    - **D. 按 ``has_tkinter`` 剥离 Tcl/Tk**：非 tkinter 项目剥离
      ``lib/libtcl9*.so``/``lib/libtk9*.so``/``lib/tcl9*``/``lib/tk9*``/``lib/itcl*``/
      ``lib/thread*``（~9MB）；tkinter 项目保留（``_tkinter.pyd`` 运行时需要 Tcl/Tk）

    同时删除 ``python/bin/`` 下开发工具脚本（2to3/idle3/pip3/pydoc3 与 ``*-config``
    脚本，运行时不用；``pip`` 已在 ``site-packages`` 中可用）。

    幂等：重复调用时已删除的文件不存在则跳过，``bytes_saved`` 仅累计首次删除大小。

    Args:
        runtime_dir: ``dist/runtime`` 目录
        py_version: Python 完整版本号（如 ``3.11.15``）
        target: 目标平台
        stage: 进度记录器
        has_tkinter: 项目是否使用 tkinter（True 保留 Tcl/Tk，False 剥离）
        strip_symbols: 是否 strip libpython 调试符号（``--no-strip-symbols`` 关闭）
    """
    if target is Platform.WINDOWS:
        stage.set_detail("embed python 无调试符号，跳过")
        return

    python_dir = runtime_dir / "python"
    if not python_dir.is_dir():
        stage.set_detail("standalone runtime 不存在，跳过")
        return

    major, minor = py_version.split(".")[:2]
    saved_bytes = 0
    removed_files = 0
    removed_dirs = 0
    stripped_libs = 0

    # A. strip libpython 调试符号
    if strip_symbols:
        lib_dir = python_dir / "lib"
        # glob 匹配 libpython3.X.so.1.0 / libpython3.X.so / libpython3.X.dylib
        # macOS 用 .dylib（无 .1.0 后缀），Linux 用 .so / .so.1.0
        for lib in lib_dir.glob(f"libpython{major}.{minor}.so*"):
            if lib.is_symlink() or not lib.is_file():
                continue
            ok, saved = _strip_elf_symbols(lib, "linux")
            stripped_libs += ok
            saved_bytes += saved
        for lib in lib_dir.glob(f"libpython{major}.{minor}.dylib"):
            if lib.is_symlink() or not lib.is_file():
                continue
            ok, saved = _strip_elf_symbols(lib, "macos")
            stripped_libs += ok
            saved_bytes += saved

    # B. 删 python3.X 二进制与所有符号链接
    bin_dir = python_dir / "bin"
    if bin_dir.is_dir():
        # 主二进制 python3.X（53MB），与指向它的符号链接 python3 / python
        py_bin = bin_dir / f"python{major}.{minor}"
        if py_bin.is_file():
            saved_bytes += py_bin.stat().st_size
            py_bin.unlink()
            removed_files += 1
            _logger.info("精简 runtime: 删除 python 二进制 %s", py_bin.name)
        # 删指向已删除二进制的悬空符号链接：python3 / python / python3-config / python3.X-config
        for link_name in (f"python{major}.{minor}-config", "python3-config", "python3", "python"):
            link = bin_dir / link_name
            if link.is_symlink() or link.exists():
                try:
                    link.unlink()
                    removed_files += 1
                except OSError as e:  # pragma: no cover - 文件系统异常容错
                    _logger.warning("删除 %s 失败: %s", link, e)
        # 删开发工具脚本：2to3/idle3/pip3/pydoc3 系列
        for name in _STANDALONE_DEV_BIN_FILES:
            f = bin_dir / name
            if f.is_symlink() or f.is_file():
                try:
                    if f.is_file():
                        saved_bytes += f.stat().st_size
                    f.unlink()
                    removed_files += 1
                except OSError as e:  # pragma: no cover - 文件系统异常容错
                    _logger.warning("删除 %s 失败: %s", f, e)

    # C. 删 include/ 与 share/
    for dirname in ("include", "share"):
        d = python_dir / dirname
        if d.is_dir():
            saved_bytes += _dir_size(d)
            shutil.rmtree(d)
            removed_dirs += 1
            _logger.info("精简 runtime: 删除 %s/", dirname)

    # D. 按 has_tkinter 剥离 Tcl/Tk 运行时
    if not has_tkinter:
        tk_saved, tk_dirs, tk_files = _strip_tcl_tk_counted(python_dir)
        saved_bytes += tk_saved
        removed_dirs += tk_dirs
        removed_files += tk_files

    stage.skip(removed_files + removed_dirs)
    stage.add_saved_bytes(saved_bytes)
    details = []
    if stripped_libs:
        details.append(f"strip {stripped_libs} 个 libpython")
    if removed_files or removed_dirs:
        details.append(f"删 {removed_files + removed_dirs} 个文件/目录")
    stage.set_detail("，".join(details) or "无操作")


def _strip_elf_symbols(lib_path: Path, platform: str) -> tuple[int, int]:
    """调 ``strip`` 剥离 ELF/Mach-O 调试符号，返回 (成功标志, 节省字节数).

    Linux: ``strip --strip-all``（剥符号表+调试符号+非必要字符串）
    macOS: ``strip -x``（剥局部符号；macOS strip 不支持 --strip-all）

    ``strip`` 命令缺失（如 minimal docker 镜像）时静默跳过，不阻断构建。
    其他失败（权限/文件损坏）记 WARNING 跳过。

    节省字节数 = strip 前文件大小 - strip 后文件大小（strip 是 in-place 修改）。
    已 stripped 的文件 diff 为 0 或负，返回 0。
    """
    args = ["strip", "--strip-all"] if platform == "linux" else ["strip", "-x"]
    args.append(str(lib_path))
    try:
        size_before = lib_path.stat().st_size
    except OSError:
        size_before = 0
    try:
        result = subprocess.run(args, check=False, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        _logger.warning("strip 命令缺失，跳过 libpython 调试符号剥离: %s", lib_path.name)
        return 0, 0
    if result.returncode != 0:
        _logger.warning("strip %s 失败: %s", lib_path.name, (result.stderr or "").strip()[:200])
        return 0, 0
    try:
        size_after = lib_path.stat().st_size
    except OSError:
        size_after = size_before
    saved = max(0, size_before - size_after)
    _logger.info("精简 runtime: strip %s（节省 %d 字节）", lib_path.name, saved)
    return 1, saved


def _strip_tcl_tk_counted(python_dir: Path) -> tuple[int, int, int]:
    """剥离 Tcl/Tk 运行时文件，返回 (saved_bytes, removed_dirs, removed_files).

    剥离内容：
    - ``lib/libtcl*.so*`` / ``lib/libtk*.so*``：Tcl/Tk 动态库
    - ``lib/tcl9`` / ``lib/tcl9.0`` / ``lib/tk9.0``：Tcl/Tk 脚本运行时
    - ``lib/itcl*`` / ``lib/thread*``：[incr Tcl] 与 Thread 扩展

    注：保留 ``lib/libpython3.X.so`` 等 libpython 文件（仅剥离 tcl/tk 相关）。
    """
    lib_dir = python_dir / "lib"
    saved = 0
    dirs = 0
    files = 0
    if not lib_dir.is_dir():
        return 0, 0, 0

    # libtcl*.so / libtk*.so（注意：tk 共享库名为 libtcl9tk9.0.so，含 tcl 关键字）
    for entry in lib_dir.iterdir():
        name = entry.name.lower()
        is_tcl_tk = (
            name.startswith("libtcl")
            or name.startswith("libtk")
            or name in {"itcl4.3.5", "itcl4.3.6", "thread3.0.4", "thread3.0.5"}
            or name.startswith("tcl9")
            or name.startswith("tk9")
            or name.startswith("itcl")
            or name.startswith("thread")
        )
        if not is_tcl_tk:
            continue
        try:
            if entry.is_dir():
                saved += _dir_size(entry)
                shutil.rmtree(entry)
                dirs += 1
            elif entry.is_file() and not entry.is_symlink():
                saved += entry.stat().st_size
                entry.unlink()
                files += 1
            elif entry.is_symlink():
                entry.unlink()
                files += 1
        except OSError as e:  # pragma: no cover - 文件系统异常容错
            _logger.warning("剥离 Tcl/Tk 文件失败 %s: %s", entry, e)

    _logger.info("精简 runtime: 剥离 Tcl/Tk 运行时 %d 目录 %d 文件", dirs, files)
    return saved, dirs, files


def _pyc_stamp_path(dist_dir: Path) -> Path:
    """预编译 stamp 文件路径：``dist/.pyc_stamp``."""
    return dist_dir / ".pyc_stamp"


def _pyc_stamp_key(
    src_dir: Path,
    site_packages: Path,
    strip_py: bool,
    optimize: int = 0,
    sp_optimize: int = 0,
) -> str:
    """计算预编译 stamp 键：src 指纹 + site-packages 指纹 + strip_py + optimize + sp_optimize.

    ``copy_source`` 在预编译前已将 ``.py`` 同步到 ``dist/src``（``strip_py`` 模式下
    也会重新复制），故 ``src_fp`` 始终反映完整源码状态，无需特殊处理 ``strip_py``
    的 ``.py`` 缺失场景。stamp 键在检查与写入时复用，避免重复计算指纹。

    ``optimize`` 与 ``sp_optimize`` 均纳入 stamp 键：src 与 site-packages 分别用不同
    optimize 级别编译（site-packages 用 ``min(optimize, 1)`` 保留 docstring，见
    :func:`_precompile_pyc`），切换任一级别时强制重编译对应目录。老 stamp（无
    ``sp_optimize`` 字段）自然失效触发全量重编译，避免旧的剥离 docstring 的 .pyc 被加载。
    """
    from fspack.analyzer import source_fingerprint

    src_fp = source_fingerprint(src_dir) if src_dir.is_dir() else ""
    sp_fp = _site_packages_fingerprint(site_packages)
    return f"{src_fp}|{sp_fp}|{strip_py}|{optimize}|{sp_optimize}"


def _run_compileall(py_exe: Path, target_dir: Path, optimize: int, stage: StageRecorder) -> bool:
    """运行单次 compileall 编译 target_dir，成功返回 True，失败返回 False.

    失败（超时或非零退出码）时记录 warning 与 ``stage.set_detail``，不抛异常。
    ``returncode != 0`` 时调 ``stage.processed()``（与原逻辑一致：有编译活动但失败）；
    超时不调（完全无编译活动）。调用方根据返回值决定是否继续编译下一个目录。

    提取为辅助函数以降低 :func:`_precompile_pyc` 分支数（PLR0912）。
    """
    try:
        result = subprocess.run(
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
            timeout=_COMPILEALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # 超时：subprocess.run 内部已 kill 子进程。不写 stamp 下次重试（iter-127
        # 引入，iter-128 统一为"失败不缓存"策略：returncode != 0 与超时都不写 stamp）。
        _logger.warning(
            "compileall 超时（%ds），跳过本次预编译，下次构建重试",
            int(_COMPILEALL_TIMEOUT),
        )
        stage.set_detail(f"compileall 超时（{int(_COMPILEALL_TIMEOUT)}s），跳过")
        return False
    if result.returncode != 0:
        _logger.warning("compileall 失败 (%s): %s", target_dir, result.stderr.strip())
        stage.processed()
        # 编译失败不写 stamp：让下次构建重试，避免失败的编译被 stamp 跳过
        # 导致用户长期运行未编译的 .py。iter-128 引入（与 iter-127 超时分支
        # 一致的"失败不缓存"策略）。
        stage.set_detail(f"compileall 失败（{target_dir.name} 退出码 {result.returncode}），跳过 stamp")
        return False
    return True


def _strip_compiled_py(  # noqa: PLR0913
    src_dir: Path,
    site_packages: Path,
    entry_rels: frozenset[str],
    optimize: int,
    sp_optimize: int,
    py_version: str,
) -> int:
    """剥离 src 与 site-packages 的非 ``__init__.py`` 源码，返回剥离总数.

    src 用 ``optimize``、site-packages 用 ``sp_optimize`` 匹配 .pyc 文件名后缀
    （``cpython-{ver}.opt-{N}.pyc``）。``entry_rels`` 仅对 src 生效（入口文件在
    src 下，需保留 ``.py`` 供 ``runpy`` 定位模块）。

    提取为辅助函数以降低 :func:`_precompile_pyc` 分支数（PLR0912）。
    """
    stripped = 0
    if src_dir.is_dir():
        stripped += _strip_py_sources([src_dir], entry_rels, optimize=optimize, py_version=py_version)
    if site_packages.is_dir():
        stripped += _strip_py_sources([site_packages], frozenset(), optimize=sp_optimize, py_version=py_version)
    return stripped


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

    参考 rimsort 等 Nuitka 打包产物：本机代码无 docstring 开销；fspack 通过
    对 src 用 ``-o 2`` 编译可缩小与 Nuitka 的执行速度差距（site-packages 因降级
    保留 docstring，体积与启动略增，但避免兼容问题）。注意 ``-OO`` 会移除
    ``__doc__`` 属性，依赖文档字符串的程序（如 Sphinx 运行时）应使用 ``0`` 或 ``1``。

    ``strip_py=True`` 时额外删除非 ``__init__.py`` 的 ``.py`` 源码（保留包标识，
    避免 PEP 420 命名空间包导致 ``.pyc`` 不被加载）。``entry_rels`` 中的入口文件
    跳过剥离（入口包装器需 ``.py`` 存在以供 ``runpy`` 定位）。src 与 site-packages
    分别用各自的 optimize 级别迁移 ``.pyc`` 到 legacy 布局。

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

    # site-packages 降级到 min(optimize, 1)：保留 docstring 避免第三方库 C 扩展因
    # __doc__ 为 None 报错（numpy add_docstring 等，issue #13248 长期未修复）。
    # optimize=2 仅用于 src 享受 -OO 优化；optimize=0/1 时 sp_optimize 与 optimize 一致。
    sp_optimize = min(optimize, 1)

    # stamp 检查：命中则跳过 compileall，stamp_key 留待未命中时写入
    stamp_key = _pyc_stamp_key(src_dir, site_packages, strip_py, optimize, sp_optimize)
    stamp = _pyc_stamp_path(dist_dir)
    try:
        if stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_key:
            stage.hit_cache()
            stage.set_detail("缓存命中，跳过编译")
            return
    except OSError:
        pass

    # 分别编译 src 与 site-packages：src 用 optimize，site-packages 用 sp_optimize。
    # 拆分两次 compileall 调用（而非合并），因 compileall 仅接受单个 -o 级别，
    # src 与 site-packages 需不同 optimize 级别。每次 subprocess 启动 ~50-100ms，
    # 多一次调用可接受（构建期一次性开销）。
    compiled = 0
    for d, opt in ((src_dir, optimize), (site_packages, sp_optimize)):
        if not d.is_dir():
            continue
        if not _run_compileall(py_exe, d, opt, stage):
            return
        compiled += 1
    if compiled:
        stage.processed()

    # 写 stamp（编译成功后、strip 前写入，存编译前的 src_fp）
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(stamp_key, encoding="utf-8")

    # 分别剥离 src 与 site-packages：entry_rels 仅对 src 生效（入口文件在 src 下），
    # site-packages 无入口文件故传空集合。src 与 site-packages 用各自的 optimize
    # 级别匹配 .pyc 文件名后缀（cpython-{ver}.opt-{N}.pyc）。
    stripped = (
        _strip_compiled_py(src_dir, site_packages, entry_rels, optimize, sp_optimize, py_version) if strip_py else 0
    )
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
