"""Runtime 精简与 Win7 兼容 DLL 注入.

拆自 :mod:`fspack.packaging.pyc`，含四类功能：

- Win7 兼容 DLL 注入（``_needs_win7_compat_dll`` / ``_inject_win7_compat_dll``）
- Linux/macOS standalone 标准库精简（``_trim_stdlib``）
- standalone runtime 开发文件剥离（``_trim_standalone_runtime``）
- ELF/Mach-O strip 与 Tcl/Tk 运行时剥离（``_strip_elf_symbols`` / ``_strip_tcl_tk_counted``）

测试通过 ``monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", ...)`` 替换
subprocess.run，因此本模块通过 :func:`_P` 延迟从 pyc facade 解析 ``subprocess``
模块属性，确保 patch 生效。
"""

from __future__ import annotations

import logging
import shutil
import subprocess as _default_subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fspack.packaging.sync import _dir_size
from fspack.platform import Platform

if TYPE_CHECKING:
    from fspack.progress import StageRecorder

_logger = logging.getLogger(__name__)

# Win7 兼容性 DLL：Python 3.9+ 官方不再支持 Win7，需注入 api-ms-win-core-path-l1-1-0.dll。
# DLL 来源 https://github.com/adang1345/api-ms-win-core-path（LGPL-2.1，基于 Wine 实现）。
# 随 fspack 分发（assets/runtime/），无需网络下载。
_WIN7_COMPAT_DLL_NAME = "api-ms-win-core-path-l1-1-0.dll"

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

# ---------------------------------------------------------------------------
# pyc facade 延迟 dispatch：兼容 monkeypatch.setattr("pyc.subprocess", ...) 等替换
# ---------------------------------------------------------------------------
_pyc_mod_holder: list[Any] = [None]


def _P(attr_name: str, fallback: Any) -> Any:
    """从 ``fspack.packaging.pyc`` 模块按名取属性，取不到时回退 fallback.

    测试 patch ``pyc`` 模块级属性（如 ``subprocess`` 模块）时，本函数在首次调用时
    延迟解析 pyc facade 并返回其当前属性值，确保 patch 版本被感知。
    """
    mod = _pyc_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging import pyc as _pyc_mod

            mod = _pyc_mod
            _pyc_mod_holder[0] = mod
        except ImportError:
            return fallback
    return getattr(mod, attr_name, fallback)


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
    常量名 ``_WIN7_COMPAT_DLL_NAME`` 通过 pyc facade dispatch，确保
    ``monkeypatch.setattr("pyc._WIN7_COMPAT_DLL_NAME", ...)`` 生效。
    """
    _WIN7_COMPAT_DLL_NAME_dispatch: str = _P("_WIN7_COMPAT_DLL_NAME", _WIN7_COMPAT_DLL_NAME)
    dest = runtime_dir / _WIN7_COMPAT_DLL_NAME_dispatch
    if dest.is_file():
        _logger.info("Win7 兼容 DLL 已就绪: %s", dest)
        return
    src = Path(__file__).parent.parent.parent / "assets" / "runtime" / _WIN7_COMPAT_DLL_NAME_dispatch
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

    if strip_symbols:
        lib_dir = python_dir / "lib"
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

    bin_dir = python_dir / "bin"
    if bin_dir.is_dir():
        py_bin = bin_dir / f"python{major}.{minor}"
        if py_bin.is_file():
            saved_bytes += py_bin.stat().st_size
            py_bin.unlink()
            removed_files += 1
            _logger.info("精简 runtime: 删除 python 二进制 %s", py_bin.name)
        for link_name in (f"python{major}.{minor}-config", "python3-config", "python3", "python"):
            link = bin_dir / link_name
            if link.is_symlink() or link.exists():
                try:
                    link.unlink()
                    removed_files += 1
                except OSError as e:  # pragma: no cover - 文件系统异常容错
                    _logger.warning("删除 %s 失败: %s", link, e)
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

    for dirname in ("include", "share"):
        d = python_dir / dirname
        if d.is_dir():
            saved_bytes += _dir_size(d)
            shutil.rmtree(d)
            removed_dirs += 1
            _logger.info("精简 runtime: 删除 %s/", dirname)

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

    ``subprocess`` 通过 pyc facade dispatch 获取，确保
    ``monkeypatch.setattr("pyc.subprocess.run", ...)`` 生效。
    """
    args = ["strip", "--strip-all"] if platform == "linux" else ["strip", "-x"]
    args.append(str(lib_path))
    try:
        size_before = lib_path.stat().st_size
    except OSError:
        size_before = 0
    subprocess_dispatch: Any = _P("subprocess", _default_subprocess)
    try:
        result = subprocess_dispatch.run(args, check=False, capture_output=True, encoding="utf-8", errors="replace")
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
