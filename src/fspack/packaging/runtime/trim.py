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

import contextlib
import logging
import shutil
import subprocess as _default_subprocess
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fspack.config.versions import _split_t_suffix
from fspack.packaging.runtime.urls import embed_dirname
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
# 顶层目录：test/ensurepip/idlelib/pydoc_data/turtledemo 是开发/测试/文档工具，运行时不用
# 嵌套 test/tests：各 stdlib 子模块下的测试目录（Python 3.12+ 移除 distutils/lib2to3 时跳过）
# Windows embed 标准库在 python3XX.zip 内，走 zip 重写（见 _EMBED_TRIM_*）
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

# Windows embed 标准库 zip（python3XX.zip）重写剥离清单——保守档（默认）：
# 仅删确定安全项（纯文档/演示数据），任何正常运行代码都不导入这些模块，
# 剥离后产物行为零变化，收益约 0.2MB/发行版
_EMBED_TRIM_CONSERVATIVE = frozenset(
    {
        "pydoc_data",  # pydoc 主题/关键字数据（纯文档数据）
        "idlelib",  # IDLE 编辑器（官方 embed 通常已删，防御性）
        "turtledemo",  # 海龟绘图演示（同上）
        "__phello__",  # 嵌入示例包
        "__hello__",  # 嵌入示例包
        "site-packages",  # embed zip 内残留目录（防御性）
    }
)

# 激进档（``slim-stdlib = "aggressive"`` 显式开启）：保守档 + 大块可选模块。
# 剥离后 import 对应模块直接 ImportError——面向确定不使用这些模块的项目：
# xml/email/http/html（网络标记与邮件）、unittest（测试）、asyncio/
# multiprocessing（并发模型）、wsgiref/xmlrpc/dbm/zoneinfo（服务与数据源）、
# lib2to3/distutils/msilib（3.12+/3.13+ 已移除，老版本残留）与开发工具单文件
# （pydoc/pdb/doctest/tarfile 等，embed zip 内为 ``*.pyc`` 形态，按 stem 匹配）。
# logging/concurrent 不在清单：前者几乎所有应用都用，后者 futures 与线程池
# 是通用基础设施
_EMBED_TRIM_AGGRESSIVE = _EMBED_TRIM_CONSERVATIVE | frozenset(
    {
        "xml",
        "xmlrpc",
        "email",
        "http",
        "html",
        "unittest",
        "wsgiref",
        "dbm",
        "zoneinfo",
        "asyncio",
        "multiprocessing",
        "lib2to3",
        "distutils",
        "msilib",
        "pydoc",
        "pdb",
        "doctest",
        "this",
        "antigravity",
        "pickletools",
        "tarfile",
        "mailbox",
    }
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

    自由线程版本（PEP 703/779，``py_version`` 末尾 ``t`` 后缀）仅支持 Win10+：
    free-threaded build 依赖 mimalloc 与新调度器，上游官方未在 Win7 测试，
    且无 win7 重编译版 t 变体；注入 shim 无意义，直接返回 ``False``。
    """
    _, is_t = _split_t_suffix(py_version)
    if is_t:
        return False
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


def _trim_stdlib(
    runtime_dir: Path,
    py_version: str,
    target: Platform,
    stage: StageRecorder,
    *,
    aggressive: bool = False,
) -> None:
    """剥离标准库中运行时无用的模块（三平台分支，幂等）.

    - **Windows 标准版**（embed zip）：标准库在 ``python3XX.zip`` 内，走
      :func:`_rewrite_embed_stdlib_zip` 重写剥离（保守/激进两档）
    - **Windows 自由线程版**（``t`` 后缀）：standalone 路径，扁平化后标准库
      位于 ``runtime/Lib/``，按目录剥离
    - **Linux/macOS**：python-build-standalone，标准库在
      ``runtime/python/lib/pythonX.Y[t]/`` 下，按目录剥离 test/ensurepip 等

    :param aggressive: Windows embed zip 重写采用激进档剥离清单
    """
    is_t = py_version.endswith("t")
    if target is Platform.WINDOWS and not is_t:
        _rewrite_embed_stdlib_zip(runtime_dir, py_version, stage, aggressive=aggressive)
        return
    if target is Platform.WINDOWS and is_t:
        # python-build-standalone Windows freethreaded tarball 解压扁平化后
        # 标准库位于 runtime/Lib/（首字母大写、无版本后缀），与 Linux 嵌套结构不同
        stdlib = runtime_dir / "Lib"
    else:
        # Linux/macOS python-build-standalone：runtime/python/lib/pythonX.Y[t]/
        # free-threaded build 标准库目录名带 t 后缀（python3.13t）
        base = py_version[:-1] if is_t else py_version
        major, minor = base.split(".")[:2]
        suffix = "t" if is_t else ""
        stdlib = runtime_dir / "python" / "lib" / f"python{major}.{minor}{suffix}"
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


def _embed_entry_blacklisted(name: str, blacklist: frozenset[str]) -> bool:
    """判定 embed zip 条目是否命中剥离清单（目录按前缀、单文件按 stem 匹配）.

    embed zip 内条目为 ``.pyc`` 形态（3.11+ 官方全量冻结，``os.pyc``/
    ``pydoc_data/topics.pyc``），单文件黑名单存 stem（``pdb`` 匹配
    ``pdb.pyc``），目录黑名单按路径首段匹配（``xml/`` 命中 ``xml/`` 下
    全部条目）。
    """
    top = name.split("/", 1)[0]
    if top in blacklist:
        return True
    stem = top.rsplit(".", 1)[0] if "." in top else top
    return stem in blacklist


def _rewrite_embed_stdlib_zip(
    runtime_dir: Path,
    py_version: str,
    stage: StageRecorder,
    *,
    aggressive: bool = False,
) -> None:
    """重写 Windows embed ``python3XX.zip``：按剥离清单过滤条目后原子替换.

    embed zip 内标准库并非只读——读旧 zip → 过滤黑名单条目 → 写临时 zip →
    同目录 ``replace`` 原子替换。官方 embed 仅剥离 test/ensurepip/idlelib，
    ``pydoc_data``（~172KB）等文档数据与大块可选模块仍留在 zip 内。

    档位（:data:`_EMBED_TRIM_CONSERVATIVE` / :data:`_EMBED_TRIM_AGGRESSIVE`）：

    - 保守档（默认）：仅删纯文档/演示数据，任何正常运行代码不受影响
    - 激进档（``aggressive=True``）：再删 xml/email/http/unittest/asyncio 等
      大块模块，剥离后 ``import`` 即 ``ImportError``，面向确定不用的项目

    幂等：重写后黑名单条目已不在 zip 内，二次调用剥离数为 0 直接跳过。
    zip 畸形/读写失败时警告并保留原 zip，不中断构建。
    """
    pyxy = embed_dirname(py_version)
    zip_path = runtime_dir / f"{pyxy}.zip"
    if not zip_path.is_file():
        stage.set_detail("embed stdlib zip 不存在，跳过")
        return
    blacklist = _EMBED_TRIM_AGGRESSIVE if aggressive else _EMBED_TRIM_CONSERVATIVE
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            keep = [
                (info, zf.read(info.filename))
                for info in infos
                if not _embed_entry_blacklisted(info.filename, blacklist)
            ]
    except (zipfile.BadZipFile, OSError) as e:
        _logger.warning("读取 embed stdlib zip 失败，跳过精简: %s", e)
        stage.set_detail("zip 读取失败，跳过")
        return
    removed = len(infos) - len(keep)
    if removed == 0:
        stage.set_detail("已精简，跳过")
        return
    size_before = zip_path.stat().st_size
    # 临时文件 + 同目录替换保证原子性：写一半崩溃时原 zip 完整
    zip_tmp = zip_path.with_name(f"{zip_path.name}.tmp")
    try:
        with zipfile.ZipFile(zip_tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for info, data in keep:
                zf.writestr(info, data)
        zip_tmp.replace(zip_path)
    except OSError as e:
        _logger.warning("重写 embed stdlib zip 失败，保留原 zip: %s", e)
        with contextlib.suppress(OSError):
            zip_tmp.unlink(missing_ok=True)
        stage.set_detail("zip 写入失败，跳过")
        return
    saved = max(0, size_before - zip_path.stat().st_size)
    stage.skip(removed)
    stage.add_saved_bytes(saved)
    level = "激进" if aggressive else "保守"
    stage.set_detail(f"{level}档剥离 {removed} 条目")
    _logger.info("精简标准库: 重写 %s（%s档剥离 %d 条目，净省 %d 字节）", zip_path.name, level, removed, saved)


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

    剥离四类运行时无用文件（Linux/macOS 目标）：

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

    Windows 目标仅自由线程版走 standalone 路径（标准版 embed zip 已精简，
    函数内转 :func:`_trim_windows_standalone_runtime` 处理）。

    幂等：重复调用时已删除的文件不存在则跳过，``bytes_saved`` 仅累计首次删除大小。
    """
    if target is Platform.WINDOWS:
        if not py_version.endswith("t"):
            stage.set_detail("embed python 无调试符号，跳过")
            return
        _trim_windows_standalone_runtime(runtime_dir, py_version, stage, has_tkinter=has_tkinter)
        return

    python_dir = runtime_dir / "python"
    if not python_dir.is_dir():
        stage.set_detail("standalone runtime 不存在，跳过")
        return

    # free-threaded build 二进制与库文件名带 t 后缀（python3.13t / libpython3.13t.so）
    is_t = py_version.endswith("t")
    base = py_version[:-1] if is_t else py_version
    major, minor = base.split(".")[:2]
    suffix = "t" if is_t else ""
    saved_bytes = 0
    removed_files = 0
    removed_dirs = 0
    stripped_libs = 0

    if strip_symbols:
        lib_dir = python_dir / "lib"
        for lib in lib_dir.glob(f"libpython{major}.{minor}{suffix}.so*"):
            if lib.is_symlink() or not lib.is_file():
                continue
            ok, saved = _strip_elf_symbols(lib, "linux")
            stripped_libs += ok
            saved_bytes += saved
        for lib in lib_dir.glob(f"libpython{major}.{minor}{suffix}.dylib"):
            if lib.is_symlink() or not lib.is_file():
                continue
            ok, saved = _strip_elf_symbols(lib, "macos")
            stripped_libs += ok
            saved_bytes += saved

    bin_dir = python_dir / "bin"
    if bin_dir.is_dir():
        py_bin = bin_dir / f"python{major}.{minor}{suffix}"
        if py_bin.is_file():
            saved_bytes += py_bin.stat().st_size
            py_bin.unlink()
            removed_files += 1
            _logger.info("精简 runtime: 删除 python 二进制 %s", py_bin.name)
        # 清理别名链接与 *-config 脚本。freethreaded 版真实二进制为
        # python3.13t，bin/ 下另有别名链接 python3.13 → python3.13t 与
        # python3.13t-config 脚本：删真实二进制后必须一并删除别名，否则
        # 留下悬空符号链接（_find_bin_python 的 glob 回退会命中断链，
        # doctor 运行验证报 127）。
        link_names = [f"python{major}.{minor}{suffix}-config", "python3-config", "python3", "python"]
        if is_t:
            # 无 t 别名：python3.13（→ python3.13t）与 python3.13-config（→ python3.13t-config）
            link_names.extend((f"python{major}.{minor}", f"python{major}.{minor}-config"))
        for link_name in link_names:
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


def _trim_windows_standalone_runtime(
    runtime_dir: Path,
    py_version: str,
    stage: StageRecorder,
    *,
    has_tkinter: bool,
) -> None:
    """精简 Windows standalone runtime（自由线程版，扁平化布局）.

    python-build-standalone Windows tarball 自带开发期文件（约 75MB），运行时无用：

    - ``*.pdb`` 调试符号：runtime 根（``python314t.pdb`` 等）与 ``DLLs/`` 下
      各扩展模块的 pdb（DLLs 目录 80MB 中约七成是 pdb）
    - ``include/`` C 头文件（~2MB）、``libs/`` 静态链接库（~0.6MB）、``Scripts/``
      开发工具脚本
    - 版本别名二进制 ``python3.Xt.exe``/``pythonw3.Xt.exe``：与 ``python.exe``/
      ``pythonw.exe`` 等价，保留后者即可（``fsp r --debug`` 直接调 ``python.exe``）
    - 非 tkinter 项目剥离 ``tcl/`` 目录（~5MB；tkinter 项目 ``_tkinter.pyd``
      运行时需要 Tcl/Tk 脚本库）

    幂等：已删除的文件/目录不存在则跳过。
    """
    is_t = py_version.endswith("t")
    base = py_version[:-1] if is_t else py_version
    major, minor = base.split(".")[:2]
    suffix = "t" if is_t else ""
    saved_bytes = 0
    removed_files = 0
    removed_dirs = 0

    # pdb 调试符号：runtime 根 + DLLs/（避免 rglob 全树遍历 Lib/ 拖慢构建）
    pdb_dirs = [runtime_dir, runtime_dir / "DLLs"]
    for pdb_dir in pdb_dirs:
        if not pdb_dir.is_dir():
            continue
        for pdb in pdb_dir.glob("*.pdb"):
            try:
                saved_bytes += pdb.stat().st_size
                pdb.unlink()
                removed_files += 1
            except OSError as e:  # pragma: no cover - 杀软占用等文件系统异常容错
                _logger.warning("删除 %s 失败: %s", pdb, e)

    # 开发期目录：C 头文件 / 静态库 / 工具脚本
    for dirname in ("include", "libs", "Scripts"):
        d = runtime_dir / dirname
        if d.is_dir():
            saved_bytes += _dir_size(d)
            shutil.rmtree(d, ignore_errors=True)
            removed_dirs += 1
            _logger.info("精简 runtime: 删除 %s/", dirname)

    # 版本别名 exe（python.exe/pythonw.exe 保留）
    for name in (f"python{major}.{minor}{suffix}.exe", f"pythonw{major}.{minor}{suffix}.exe"):
        f = runtime_dir / name
        if f.is_file():
            try:
                saved_bytes += f.stat().st_size
                f.unlink()
                removed_files += 1
            except OSError as e:  # pragma: no cover - 文件占用容错
                _logger.warning("删除 %s 失败: %s", f, e)

    # 非 tkinter 项目剥离 Tcl/Tk 脚本运行时
    if not has_tkinter:
        tcl_dir = runtime_dir / "tcl"
        if tcl_dir.is_dir():
            saved_bytes += _dir_size(tcl_dir)
            shutil.rmtree(tcl_dir, ignore_errors=True)
            removed_dirs += 1
            _logger.info("精简 runtime: 删除 tcl/")

    stage.skip(removed_files + removed_dirs)
    stage.add_saved_bytes(saved_bytes)
    stage.set_detail(f"删 {removed_files + removed_dirs} 个文件/目录" if (removed_files or removed_dirs) else "无操作")


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
