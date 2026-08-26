"""入口包装器源码生成。

fspack 在 dist 根目录为每个入口生成 ``_entry_<name>.py`` 包装器，由 C loader
通过 ``.entry`` 文件加载运行。包装器负责：

1. **设置 site-packages 到 sys.path**：Windows embed python 通过 ``python3XX._pth``
   控制 sys.path（含 site-packages），但 Linux standalone python 在 ``PYTHONHOME``
   模式下默认不启用 site-packages，需显式 ``sys.path.insert`` 才能找到 rich 等
   第三方依赖。
2. **设置 Qt 插件路径**：PySide2/PySide6/PyQt5/PyQt6 的 ``QT_PLUGIN_PATH``
   必须在 import 用户代码前设置，否则 ``QApplication`` 找不到平台插件。
3. **设置 Tcl/Tk 环境变量**：embed python 缺失 tkinter，打包补充的 Tcl/Tk
   脚本路径需通过 ``TCL_LIBRARY``/``TK_LIBRARY`` 显式指定。
4. **包式入口支持**：若入口脚本位于包内（所在目录链直至首个包目录都有
   ``__init__.py``），用 :func:`runpy.run_module` 以包上下文运行，使相对导入
   （``from .conf import ...``）可用；否则用 :func:`runpy.run_path` 直接运行
   顶层脚本。
5. **site-packages 缓存预填充**：预创建 ``FileFinder`` 注入
   ``sys.path_importer_cache``，使首次 import 直接命中缓存，跳过 ``path_hooks``
   迭代开销。
6. **延迟导入钩子**：``--lazy-import numpy,pandas`` 指定的模块由
   :class:`_LazyImportFinder` 拦截，用 :class:`importlib.util.LazyLoader` 包装，
   首次属性访问时才执行 ``__init__.py``，降低启动时间。
7. **启动耗时打点**：``FSPACK_TIMING=1``（由 ``fsp r --profile`` 注入）时输出
   各阶段累计时刻到 stderr，供 runner 侧汇总剖析；未启用时零开销。
8. **GUI 事件循环自终止**：``FSPACK_TIMING=1`` 时经 ``builtins.__import__``
   拦截 Qt 系（PySide2/6、PyQt5/6）与 tkinter 的首次导入，patch
   ``QApplication.exec``/``Tk.mainloop``：处理首帧事件后打点 ``gui_ready``
   并直接返回，GUI 应用"进入界面后自行终止"（退出码 0），剖析不挂起。

包模式下 wrapper 将 ``pkg_root`` 加入 ``sys.path`` 使首层包可 import。对于
src-layout 项目（包在 ``src/<pkg>/`` 下，``src/`` 是容器而非包），wrapper
加入 ``dist/src/src`` 使 ``<pkg>`` 可 import。

顶层模式下 wrapper 将 ``dist/src`` 加入 ``sys.path`` 使顶层绝对导入可用。
``runpy.run_path`` 对文件路径不自动把脚本目录加入 ``sys.path``（与
``python script.py`` 不同），需显式注入，否则 ``import module_c`` 等本地
绝对导入报 ``ModuleNotFoundError``。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["EntryWrapper"]

# wrapper 源码模板：{entry_name}/{module_dotted}/{pkg_root_rel}/{entry_rel}/{has_tkinter} 由 format 填入。
# module_dotted 为 None 时走顶层模式（run_path），否则走包模式（run_module）。
_WRAPPER_TEMPLATE = '''\
"""fspack 生成的入口包装器（{entry_name}）。

设置 site-packages 与 Qt 插件路径后以包上下文运行用户入口，使相对导入可用。
此文件由 fspack 构建时生成，不要手动编辑。
"""
import os
import runpy
import sys
import time

# fsp r --profile 启动耗时打点：FSPACK_TIMING=1 时输出各阶段累计时刻（ms）到
# stderr，由 fsp r 解析为启动耗时汇总；未启用时仅一次 environ.get，零开销。
# _T0 在 wrapper 首个可执行语句附近采样，近似解释器初始化完成时刻（进入
# Python 前的 loader 阶段耗时由 C loader 的 FSPACK_LOADER_VERBOSE 打点覆盖）。
_FSPACK_TIMING = os.environ.get("FSPACK_TIMING") == "1"
_T0 = time.perf_counter() if _FSPACK_TIMING else 0.0

# Windows loader 在 Py_Main 调用前写入 QPC 绝对毫秒锚点（perf_counter 底层
# 同为 QueryPerformanceCounter，同一单调时间线），差值即 Py_Main C 层初始化
# （runtime/io/codecs 等非 import 部分）+ wrapper 模块加载前段的实测耗时，
# 填补 loader 打点与 wrapper 打点之间的测量盲区。Linux/macOS loader 与旧
# dist 无此锚点，跳过（行为不变）。
_LOADER_QPC_MS = os.environ.get("FSPACK_LOADER_QPC_MS") if _FSPACK_TIMING else None
if _FSPACK_TIMING and _LOADER_QPC_MS:
    try:
        _py_init_ms = _T0 * 1000.0 - float(_LOADER_QPC_MS)
    except ValueError:
        _py_init_ms = -1.0
    if _py_init_ms >= 0.0:
        sys.stderr.write("[fspack timing-gap] py_init %.1fms\\n" % _py_init_ms)
        sys.stderr.flush()


def _fspack_tick(label):
    """输出累计时刻打点行 ``[fspack timing] <label> @<ms>ms``（未启用时无操作）."""
    if _FSPACK_TIMING:
        sys.stderr.write("[fspack timing] %s @%.1fms\\n" % (label, (time.perf_counter() - _T0) * 1000.0))
        sys.stderr.flush()

# GUI 子系统（pythonw.exe / -mwindows loader）下 sys.stdout/stderr/stdin 为 None，
# 第三方库（如 loguru logger.add(sys.stderr)）写 None 会触发 __fastfail 崩溃
# （0xC0000409 STATUS_STACK_BUFFER_OVERRUN）。用 os.devnull 替代 None，
# 使日志写入静默丢弃而非崩溃。console 子系统下三者非 None，不受影响。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")

_DIST_DIR = os.path.dirname(os.path.abspath(__file__))
_RUNTIME_DIR = os.path.join(_DIST_DIR, "runtime")

# site-packages 平铺到 dist/site-packages（与 runtime 平级，避免层级过深）。
# 显式加入 sys.path 是因为 Linux standalone 在 PYTHONHOME 模式下默认不启用
# site-packages（site.py 不会被自动调用），不显式添加会导致 rich 等
# 第三方依赖 ModuleNotFoundError。
_SITE_PACKAGES = os.path.join(_DIST_DIR, "site-packages")
if os.path.isdir(_SITE_PACKAGES) and _SITE_PACKAGES not in sys.path:
    sys.path.insert(0, _SITE_PACKAGES)

# 预填充 sys.path_importer_cache 避免 lazy FileFinder 创建开销：
# site-packages 是最高频搜索路径，首次 import 时 Python 会遍历 sys.path_hooks
# 创建 FileFinder。预创建并缓存使后续 import 直接命中 path_importer_cache，
# 跳过 path_hooks 迭代。等效于"sys.path_hooks 优先匹配 site-packages"——
# 缓存命中的 FileFinder 是最高优先级的 importer。
if _SITE_PACKAGES and os.path.isdir(_SITE_PACKAGES) and _SITE_PACKAGES not in sys.path_importer_cache:
    import importlib.machinery
    sys.path_importer_cache[_SITE_PACKAGES] = importlib.machinery.FileFinder(
        _SITE_PACKAGES,
        (importlib.machinery.ExtensionFileLoader, [".pyd", ".so"]),
        (importlib.machinery.SourceFileLoader, [".py"]),
        (importlib.machinery.SourcelessFileLoader, [".pyc"]),
    )

# 重量级模块延迟导入钩子：--lazy-import numpy,pandas 指定的模块
# 用 importlib.util.LazyLoader 包装，首次 import 时不执行模块 __init__.py，
# 首次属性访问时才真正加载。典型收益：numpy 启动省 ~80ms，pandas 省 ~150ms。
# C 扩展模块（.pyd/.so）无法延迟（C 初始化必须即时执行），返回 None 让默认
# finder 处理。仅拦截顶层模块名，子模块（numpy.array）通过 lazy 顶层触发加载。
_LAZY_MODULES = {lazy_imports!r}
if _LAZY_MODULES and _SITE_PACKAGES and os.path.isdir(_SITE_PACKAGES):
    import importlib.machinery
    import importlib.util

    class _LazyImportFinder:
        """延迟导入 meta path finder，拦截 _LAZY_MODULES 中的顶层模块."""

        def __init__(self, module_names, site_packages):
            self._lazy = frozenset(module_names)
            self._sp = site_packages

        def find_spec(self, name, path=None, target=None):
            top = name.split(".", 1)[0]
            if top not in self._lazy or name != top:
                return None
            # 包（目录 + __init__.py）：SourceFileLoader 可被 LazyLoader 包装
            pkg_init = os.path.join(self._sp, name, "__init__.py")
            if os.path.isfile(pkg_init):
                loader = importlib.machinery.SourceFileLoader(name, pkg_init)
                return importlib.util.spec_from_loader(
                    name, importlib.util.LazyLoader(loader)
                )
            # 纯 Python 模块（.py）
            mod_py = os.path.join(self._sp, name + ".py")
            if os.path.isfile(mod_py):
                loader = importlib.machinery.SourceFileLoader(name, mod_py)
                return importlib.util.spec_from_loader(
                    name, importlib.util.LazyLoader(loader)
                )
            # C 扩展（.pyd/.so）无法延迟，返回 None 让默认 finder 处理
            return None

    sys.meta_path.insert(0, _LazyImportFinder(_LAZY_MODULES, _SITE_PACKAGES))

# Qt 插件路径与 DLL 目录（PySide2/PySide6/PyQt5/PyQt6）——必须在 import 用户代码前设置，
# 否则 QApplication 启动时报 "Failed to load platform plugin windows"。
# 将 <qt_root> 加入 PATH：QPluginLoader 加载 QML 插件时依赖 DLL 搜索走 PATH
# （Qt 内部用 LoadLibrary 不传 LOAD_LIBRARY_SEARCH_USER_DIRS），不修改 PATH 会导致
# qtquick2plugin.dll 找到但 Qt5Quick.dll 加载失败。Qt C 扩展（.pyd）所在目录即
# <qt_root>，LoadLibrary 默认搜索加载模块目录，故 .pyd 依赖的 Qt DLL 无需额外设置。
#
# 不使用 os.add_dll_directory：PySide2 wheel 自带 VC++ 运行时 DLL（msvcp140.dll、
# vcruntime140.dll、ucrtbase.dll 等），add_dll_directory 把整个 <qt_root> 加入 DLL
# 搜索路径后，其他 C 扩展（如 onnxruntime）加载时会在 <qt_root> 找到这些运行时 DLL
# 的副本，与已加载的系统版本冲突，触发 "DLL 初始化例程失败"（DllMain 返回 FALSE）。
# 仅修改 PATH 不触发此冲突——PATH 用于 LoadLibrary 兜底搜索，但 add_dll_directory
# 添加的目录优先级更高且范围更广。PATH 修改足以让 QML 插件与 Qt C 扩展找到 Qt DLL。
for _qt_pkg in ("PySide2", "PySide6", "PyQt5", "PyQt6"):
    _qt_root = os.path.join(_SITE_PACKAGES, _qt_pkg)
    _qt_plugins = os.path.join(_qt_root, "plugins")
    if os.path.isdir(_qt_plugins):
        os.environ.setdefault("QT_PLUGIN_PATH", _qt_plugins)
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", _qt_plugins)
        if os.path.isdir(_qt_root):
            _path_sep = os.pathsep
            _old_path = os.environ.get("PATH", "")
            if _qt_root not in _old_path.split(_path_sep):
                os.environ["PATH"] = _qt_root + _path_sep + _old_path
        break

# splash 启动画面关闭通知（--splash 构建的 Windows exe 才有对应命名事件，
# 其余平台/构建为无操作）。GUI 应用由 loader C 侧 EnumWindows 检测首个可见
# 窗口自动关闭；WEB 应用无自有窗口，server 启动前经 ctypes SetEvent 通知。
def _close_splash():
    """通知 loader 关闭 splash 启动画面（事件不存在时无操作）."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        _kernel32 = ctypes.windll.kernel32
        _ev = _kernel32.OpenEventW(0x0002, False, f"Local\\\\fspack_splash_{{os.getpid()}}")
        if _ev:
            _kernel32.SetEvent(_ev)
            _kernel32.CloseHandle(_ev)
    except OSError:
        pass  # ctypes 异常等场景：画面由 loader C 侧 30s 超时兜底关闭

# GUI 事件循环自终止钩子（FSPACK_TIMING=1 剖析模式）：GUI 应用进入事件循环后
# 永不退出，剖析会永久挂起。经 builtins.__import__ 拦截主流 GUI 框架的首次
# 导入，patch QApplication.exec/exec_ 与 Tk.mainloop：先处理 pending 事件使
# 首帧上屏（"进入界面"），打点 gui_ready 后直接返回，程序自然退出（退出码
# 0），启动耗时得到有效评估。命中框架先卸载拦截器恢复原生 import 再执行
# 该次导入，导入完成后安装 patch；patch 所需子模块暂不可用时重装拦截器
# 待命重试。先卸载是防递归关键：框架模块体内部的嵌套同框架导入（tkinter
# 的 from tkinter.constants import *、QtWidgets 的 from PySide2.QtCore
# import *）若仍经拦截器会重入安装逻辑，与 patch 函数自身的 import 叠加成
# 无限递归（RecursionError）。未命中框架零开销（每次 import 仅一次元组
# 查询），未知框架由 runner 侧超时兜底。
# 注：本块代码不用 dict/set 字面量与 f-string——wrapper 模板经 str.format
# 填充，字面花括号会被误解析为占位符。
_GUI_TOPS = ("PySide2", "PySide6", "PyQt5", "PyQt6", "tkinter")


def _fspack_gui_ready():
    """输出界面就绪打点行（首帧上屏后、事件循环进入前的时刻）."""
    _fspack_tick("gui_ready")


def _fspack_patch_qt(qt_pkg):
    """patch QtWidgets.QApplication.exec/exec_：处理首帧后打点并返回 0."""
    if qt_pkg == "PySide2":
        from PySide2.QtWidgets import QApplication
    elif qt_pkg == "PySide6":
        from PySide6.QtWidgets import QApplication
    elif qt_pkg == "PyQt5":
        from PyQt5.QtWidgets import QApplication
    else:
        from PyQt6.QtWidgets import QApplication

    def _fspack_exec(self, *args, **kwargs):
        try:
            self.processEvents()  # 处理 show/paint 队列，首帧上屏
        except Exception:
            pass
        _fspack_gui_ready()
        return 0

    for _nm in ("exec", "exec_"):
        if hasattr(QApplication, _nm):
            setattr(QApplication, _nm, _fspack_exec)


def _fspack_patch_tkinter():
    """patch tkinter.Tk.mainloop：处理 pending 事件后打点并返回."""
    import tkinter

    def _fspack_mainloop(self, *args, **kwargs):
        try:
            self.update()  # 处理 pending 事件（含首帧重绘）
        except Exception:
            pass
        _fspack_gui_ready()
        return None

    tkinter.Tk.mainloop = _fspack_mainloop


def _fspack_try_install_gui_hook(top):
    """import 完成回调：patch 目标框架的事件循环入口（拦截器已由调用方卸载）.

    Qt 项目常先 import QtCore（此时 QtWidgets 尚不可用），ImportError 向上
    传播由调用方捕获并重装拦截器，待 QtWidgets 导入后重试。
    """
    if top == "tkinter":
        _fspack_patch_tkinter()
    else:
        _fspack_patch_qt(top)


_fspack_orig_import = None
if _FSPACK_TIMING:
    import builtins as _builtins

    _fspack_orig_import = _builtins.__import__

    def _fspack_import_hook(name, *args, **kwargs):
        top = name.split(".", 1)[0]
        if top not in _GUI_TOPS:
            return _fspack_orig_import(name, *args, **kwargs)
        # 命中 GUI 框架：先卸载拦截器恢复原生 import，再执行本次导入（防
        # 递归，见块首注释），导入完成后仅安装一次 patch
        _builtins.__import__ = _fspack_orig_import
        try:
            mod = _fspack_orig_import(name, *args, **kwargs)
        except ImportError:
            # 框架本体导入失败（未安装/依赖缺失）：重装拦截器待命，异常原样传播
            _builtins.__import__ = _fspack_import_hook
            raise
        try:
            _fspack_try_install_gui_hook(top)
        except ImportError:
            # patch 所需子模块暂不可用：重装拦截器待命，下次框架导入重试
            _builtins.__import__ = _fspack_import_hook
        return mod

    _builtins.__import__ = _fspack_import_hook


# tkinter 环境变量（embed python 缺失 Tcl/Tk 脚本路径，需手动指定）。
# Linux/macOS standalone 无需此块：Tcl/Tk 脚本库由 python-build-standalone
# 可重定位构建从 so 位置自动推导；共享库（libtcl9.0.so 等）由 loader exe
# 的 DT_RPATH 兜底解析（见 LinuxLoader._build_command）。
if {has_tkinter}:
    # glob 延迟导入：仅 tkinter 程序需要扫描 tcl/tk 脚本目录，无 tkinter 的
    # 程序（绝大多数）省去 wrapper 期的 glob 导入链（glob→fnmatch→re 等，
    # 实测约 7-9ms；后续用户代码经 pathlib._abc 导入 glob 时 re 多已被缓存）。
    import glob

    _tcl_lib = glob.glob(os.path.join(_RUNTIME_DIR, "tcl", "tcl*"))
    if _tcl_lib:
        os.environ.setdefault("TCL_LIBRARY", _tcl_lib[0])
    _tk_lib = glob.glob(os.path.join(_RUNTIME_DIR, "tcl", "tk*"))
    if _tk_lib:
        os.environ.setdefault("TK_LIBRARY", _tk_lib[0])

# Web 应用静态文件 serve 与自动开浏览器注入（仅 AppType.WEB 或显式 --open-browser）。
# 在 import 用户代码前 monkey-patch flask.Flask.run / uvicorn.run / uvicorn.Config.run：
# 用户代码调用 app.run() / uvicorn.run() 时挂载静态文件 serve（Flask static_folder /
# FastAPI StaticFiles）并启动 threading.Timer 调 webbrowser.open 打开浏览器。
# 静态目录在打包时由 fspack 解析为 dist 内绝对路径（web_static_dirs），运行时直接使用。
# 非 WEB 类型或未配置 web_static_dirs 时此块无操作（_WEB_STATIC_DIRS 为空）。
_WEB_STATIC_DIRS = {web_static_dirs!r}
_OPEN_BROWSER = {open_browser!r}
if _WEB_STATIC_DIRS and _OPEN_BROWSER:
    import threading
    import webbrowser

    # 解析静态目录为 dist 内绝对路径，过滤不存在的目录
    _resolved_static = [
        os.path.normpath(os.path.join(_DIST_DIR, _rel))
        for _rel in _WEB_STATIC_DIRS
        if os.path.isdir(os.path.normpath(os.path.join(_DIST_DIR, _rel)))
    ]

    def _open_browser_after_delay(url, delay=1.0):
        """延迟 delay 秒打开浏览器（等服务器监听端口就绪）."""
        def _open():
            try:
                webbrowser.open(url)
            except OSError:
                pass
        threading.Timer(delay, _open).start()

    def _patch_flask():
        """Monkey-patch flask.Flask.run 挂载静态目录与开浏览器."""
        try:
            from flask import Flask, send_from_directory
        except ImportError:
            return False
        _original_run = Flask.run
        _static_dir = _resolved_static[0] if _resolved_static else None

        def _patched_run(self, host=None, port=None, **kwargs):
            # 挂载静态文件 serve：首个静态目录作为 Flask static_folder
            if _static_dir and not getattr(self, "_fspack_static_mounted", False):
                # 路由 / 返回 index.html，/<path:path> 返回静态文件
                @self.route("/")
                def _fspack_index():
                    return send_from_directory(_static_dir, "index.html")

                @self.route("/<path:path>")
                def _fspack_static(path):
                    return send_from_directory(_static_dir, path)

                self._fspack_static_mounted = True
            _host = host or "127.0.0.1"
            _port = port or 5000
            _open_browser_after_delay(f"http://{{_host}}:{{_port}}/")
            _close_splash()  # WEB 无自有窗口，server 启动即关 splash
            return _original_run(self, host=host, port=port, **kwargs)

        Flask.run = _patched_run
        return True

    def _patch_fastapi():
        """Monkey-patch uvicorn.run 挂载 StaticFiles 与开浏览器.

        FastAPI 项目用 uvicorn.run(app, ...) 启动，无 app.run() 入口。拦截
        uvicorn.run：若 app 是 Starlette/FastAPI 实例且未挂载 StaticFiles，
        挂载到 / 路径（前端 SPA fallback 到 index.html 由 StaticFiles html=True
        处理）。再调原 uvicorn.run。
        """
        try:
            import uvicorn
        except ImportError:
            return False
        _original_uvicorn_run = uvicorn.run
        _static_dir = _resolved_static[0] if _resolved_static else None

        def _patched_uvicorn_run(app, host="127.0.0.1", port=8000, **kwargs):
            if _static_dir:
                try:
                    from starlette.staticfiles import StaticFiles
                    # 仅对 Starlette/FastAPI 实例挂载（duck-typing：有 mount 方法）
                    if hasattr(app, "mount") and not getattr(app, "_fspack_static_mounted", False):
                        app.mount("/", StaticFiles(directory=_static_dir, html=True), name="fspack_static")
                        app._fspack_static_mounted = True
                except ImportError:
                    pass
            _open_browser_after_delay(f"http://{{host}}:{{port}}/")
            _close_splash()  # WEB 无自有窗口，server 启动即关 splash
            return _original_uvicorn_run(app, host=host, port=port, **kwargs)

        uvicorn.run = _patched_uvicorn_run
        # uvicorn.Config.run 内部不调 uvicorn.run，但 uvicorn.run 是入口，
        # 多数 FastAPI 项目用 uvicorn.run(app, ...) 启动；少量用 uvicorn.Server
        # 直接构造，此场景不拦截（用户自行处理静态文件）
        return True

    # 尝试 patch（按 import 顺序，用户代码 import 时已生效）
    _patch_flask()
    _patch_fastapi()

# 环境准备完成打点：site-packages 注入/缓存预填充/lazy hooks/Qt/tkinter/web
# 补丁全部就绪，接下来是 sys.path 调整与 runpy 进入用户入口。
_fspack_tick("env_ready")

_SRC_DIR = os.path.join(_DIST_DIR, "src")
_ENTRY_MODULE = {module_dotted!r}
_ENTRY_REL = {entry_rel!r}
_PKG_ROOT_REL = {pkg_root_rel!r}
_PKG_ROOT = os.path.normpath(os.path.join(_DIST_DIR, _PKG_ROOT_REL))

# 进入用户入口打点：此后 runpy 会 import 用户代码（含入口包 __init__ 链）并执行
# __main__；entry_done 与 entry_start 的差值即用户入口总耗时（导入+执行）。
_fspack_tick("entry_start")
if _ENTRY_MODULE:
    # 包模式：加入包根让首层包可 import，run_module 保留包上下文（相对导入可用）
    if _PKG_ROOT not in sys.path:
        sys.path.insert(0, _PKG_ROOT)
    runpy.run_module(_ENTRY_MODULE, run_name="__main__", alter_sys=True)
else:
    # 顶层模式：加入 src 目录使绝对导入可用。
    # runpy.run_path 对文件路径不自动把脚本目录加入 sys.path
    # （_run_module_code 仅修改 sys.argv[0] 与 sys.modules），需显式注入
    # _SRC_DIR，否则 `import module_c`/`from pkg.mod import f` 等顶层绝对
    # 导入找不到本地模块（cli_complex 等模板的 ModuleNotFoundError 根因）。
    if _SRC_DIR not in sys.path:
        sys.path.insert(0, _SRC_DIR)
    runpy.run_path(os.path.join(_SRC_DIR, _ENTRY_REL), run_name="__main__")
# 用户入口执行完成打点：CLI 应用 main() 返回、GUI 事件循环退出后到达；
# 用户代码调用 os._exit() 时不会到达（runner 侧显示为未返回）。
_fspack_tick("entry_done")
'''


class EntryWrapper:
    """入口包装器生成器.

    封装入口脚本的 dotted 模块名计算与包装器源码生成。两个方法均为静态方法，
    无状态，通过类名直接调用：``EntryWrapper.dotted_module_name(...)``。
    """

    _TEMPLATE = _WRAPPER_TEMPLATE

    @staticmethod
    def dotted_module_name(src_dir: Path, entry_file: Path) -> tuple[str, str] | None:
        """计算入口脚本的 dotted 模块名与包根路径。

        fspack 把 ``src_dir`` 内容复制到 ``dist/src``。若入口在包内（目录链中
        首个包目录起直至入口都有 ``__init__.py``），返回 ``(module_dotted,
        pkg_root_rel)`` 供 :func:`runpy.run_module` 使用；否则返回 ``None``，
        wrapper 用 :func:`runpy.run_path` 运行顶层脚本。

        返回值 ``(module_dotted, pkg_root_rel)``：

        - ``module_dotted``：dotted 模块名，如 ``"src.game"`` 或 ``"fuscan.__main__"``。
        - ``pkg_root_rel``：包根相对 dist 的 POSIX 路径，wrapper 将其加入
          ``sys.path`` 使 ``module_dotted`` 的首层包可 import。

        返回值规则：

        - 入口在 ``src_dir`` 顶层且 ``src_dir`` 有 ``__init__.py``：返回
          ``("src.<stem>", ".")``——``dist/src`` 自身是名为 ``src`` 的包，
          ``sys.path`` 加入 dist 根即可 import。
        - 入口在 ``src_dir`` 顶层且 ``src_dir`` 无 ``__init__.py``：返回 ``None``
          （顶层模块，``sys.path`` 已含 ``src`` 自身）。
        - 入口在 ``src_dir`` 子目录且目录链从首个包起都有 ``__init__.py``：

          * ``src_dir`` 有 ``__init__.py``：返回 ``("src.<pkg>.<stem>", ".")``
          * ``src_dir`` 无 ``__init__.py``，无容器前缀：返回
            ``("<pkg>.<stem>", "src")``（包在 ``dist/src/<pkg>/``）
          * ``src_dir`` 无 ``__init__.py``，有容器前缀（src-layout，如
            ``src/`` 无 ``__init__.py`` 但其下 ``<pkg>/`` 有）：返回
            ``("<pkg>.<stem>", "src/<containers>")``（包在
            ``dist/src/<containers>/<pkg>/``）

        - 入口在 ``src_dir`` 子目录且首个包之后某级目录无 ``__init__.py``：
          返回 ``None``（退化为顶层，用 ``run_path``）。
        """
        try:
            rel = entry_file.relative_to(src_dir)
        except ValueError:
            return None
        parts = rel.parts
        if not parts:
            return None

        dir_parts = parts[:-1]
        last = parts[-1]
        module_stem = last[: -len(".py")] if last.endswith(".py") else last

        src_is_pkg = (src_dir / "__init__.py").is_file()

        # 入口在 src_dir 顶层：src_dir 是包则 'src.<stem>'，否则顶层模块
        if not dir_parts:
            return (f"src.{module_stem}", ".") if src_is_pkg else None

        # 入口在子目录，遍历目录链：
        # - src_dir 非包时，允许前缀无 __init__.py 的目录作为容器（src-layout 的 src/）
        # - 遇到首个包（有 __init__.py）后，后续目录必须都是包
        current = src_dir
        pkg_parts: list[str] = []
        container_parts: list[str] = []
        for part in dir_parts:
            current = current / part
            if not (current / "__init__.py").is_file():
                # 无 __init__.py：仅当 src_dir 非包且尚未遇到包时，视为容器目录
                if not src_is_pkg and not pkg_parts:
                    container_parts.append(part)
                    continue
                return None
            pkg_parts.append(part)

        if not pkg_parts:
            return None  # 目录链全无 __init__.py，退化为顶层

        # 构造模块名与包根路径
        if src_is_pkg:
            # src_dir 是包：module = src.<pkgs>.<stem>, pkg_root = dist
            module = ".".join(("src", *pkg_parts, module_stem))
            pkg_root = "."
        else:
            # src_dir 非包：module = <pkgs>.<stem>
            module = ".".join((*pkg_parts, module_stem))
            # 包根 = dist/src/<containers>（无容器时为 dist/src）
            pkg_root = "/".join(("src", *container_parts))

        return (module, pkg_root)

    @staticmethod
    def generate_wrapper_source(  # noqa: PLR0913
        entry_name: str,
        module_dotted: str | None,
        entry_rel: str,
        pkg_root_rel: str = ".",
        has_tkinter: bool = False,
        lazy_imports: tuple[str, ...] = (),
        web_static_dirs: tuple[str, ...] = (),
        open_browser: bool = False,
    ) -> str:
        """生成入口包装器源码。

        entry_name: 入口名（用于文档注释，便于区分多入口项目的不同 wrapper）。
        module_dotted: :meth:`dotted_module_name` 返回的 dotted 模块名；``None``
            表示顶层模式，wrapper 用 :func:`runpy.run_path`。
        entry_rel: 入口相对 ``src_dir`` 的 POSIX 路径（如 ``"game.py"``），
            顶层模式用其定位脚本。
        pkg_root_rel: 包根相对 dist 的 POSIX 路径（如 ``"."``、``"src"``、
            ``"src/src"``），包模式时 wrapper 将其加入 ``sys.path`` 使首层包
            可 import。顶层模式不使用此参数。
        has_tkinter: 是否注入 ``TCL_LIBRARY``/``TK_LIBRARY`` 环境变量设置。
            embed python 缺失 tkinter，打包补充后需在 wrapper 中显式指定
            Tcl/Tk 脚本路径，否则 ``_tkinter.pyd`` 找不到 ``tcl8.6/``。
        lazy_imports: 延迟导入的顶层模块名元组（如 ``("numpy", "pandas")``），
            wrapper 注入 :class:`_LazyImportFinder` meta path finder，首次 import
            时不执行模块 ``__init__.py``，首次属性访问时才真正加载。空元组时
            不注入 finder。典型收益：numpy 启动省 ~80ms，pandas 省 ~150ms。
        web_static_dirs: 前端构建产物目录（相对项目目录的 POSIX 路径元组），
            wrapper 解析为 dist 内绝对路径并注入静态文件 serve。仅 WEB 类型
            或显式启用 ``open_browser`` 时生效。空元组时不注入。
        open_browser: 是否在服务器启动后自动打开浏览器。``True`` 且
            ``web_static_dirs`` 非空时注入 Flask/FastAPI monkey-patch。
        """
        return EntryWrapper._TEMPLATE.format(
            entry_name=entry_name,
            module_dotted=module_dotted,
            entry_rel=entry_rel,
            pkg_root_rel=pkg_root_rel,
            has_tkinter=has_tkinter,
            lazy_imports=lazy_imports,
            web_static_dirs=web_static_dirs,
            open_browser=open_browser,
        )
