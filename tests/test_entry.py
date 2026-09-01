"""入口包装器源码生成测试：EntryWrapper.dotted_module_name 与 generate_wrapper_source."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fspack.packaging.entry import EntryWrapper


def test_dotted_module_name_entry_outside_src_dir(tmp_path: Path) -> None:
    """入口脚本不在 src_dir 内时返回 None."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    entry = tmp_path / "other.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) is None


def test_dotted_module_name_entry_equals_src_dir(tmp_path: Path) -> None:
    """入口路径等于 src_dir 自身时返回 None（rel.parts 为空）."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    # entry_file 即 src_dir 本身（边界场景，relative_to 返回 "."）
    assert EntryWrapper.dotted_module_name(src_dir, src_dir) is None


def test_dotted_module_name_top_level_no_init(tmp_path: Path) -> None:
    """入口在 src_dir 顶层且 src_dir 无 __init__.py：返回 None（顶层模式）."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    entry = src_dir / "main.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) is None


def test_dotted_module_name_top_level_with_init(tmp_path: Path) -> None:
    """入口在 src_dir 顶层且 src_dir 有 __init__.py：返回 ('src.<stem>', '.')."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    entry = src_dir / "game.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) == ("src.game", ".")


def test_dotted_module_name_subdir_chain_with_init_prefix_src(tmp_path: Path) -> None:
    """入口在子目录且目录链都有 __init__.py，src_dir 也有：返回 ('src.<pkg>.<stem>', '.')."""
    src_dir = tmp_path / "src"
    pkg = src_dir / "pkg"
    pkg.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    entry = pkg / "main.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) == ("src.pkg.main", ".")


def test_dotted_module_name_subdir_chain_with_init_no_prefix(tmp_path: Path) -> None:
    """入口在子目录且目录链都有 __init__.py，src_dir 无：返回 ('<pkg>.<stem>', 'src')."""
    src_dir = tmp_path / "src"
    pkg = src_dir / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    entry = pkg / "main.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) == ("pkg.main", "src")


def test_dotted_module_name_subdir_chain_broken(tmp_path: Path) -> None:
    """入口在子目录但某级目录无 __init__.py（src_dir 是包）：返回 None."""
    src_dir = tmp_path / "src"
    pkg = src_dir / "pkg"
    pkg.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    # pkg 目录无 __init__.py，src_dir 是包时不允许容器跳过
    entry = pkg / "main.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) is None


def test_dotted_module_name_nested_subdir_chain(tmp_path: Path) -> None:
    """入口在多层嵌套子目录且目录链都为包：返回完整 dotted 路径."""
    src_dir = tmp_path / "src"
    nested = src_dir / "pkg" / "sub"
    nested.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    (src_dir / "pkg" / "__init__.py").write_text("")
    (nested / "__init__.py").write_text("")
    entry = nested / "main.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) == ("src.pkg.sub.main", ".")


def test_dotted_module_name_non_py_extension(tmp_path: Path) -> None:
    """入口文件无 .py 后缀时直接用文件名作为模块名."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    # 不以 .py 结尾的文件（理论上 fspack 入口都是 .py，此处覆盖分支）
    entry = src_dir / "main"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) == ("src.main", ".")


def test_dotted_module_name_src_layout(tmp_path: Path) -> None:
    """src-layout：src_dir 非包，src/ 容器无 __init__.py，其下 pkg 有。

    模拟 fuscan 项目结构：project_root/src/fuscan/__main__.py。
    返回 ("fuscan.__main__", "src/src")——包在 dist/src/src/fuscan/。
    """
    src_dir = tmp_path  # 项目根
    container = src_dir / "src"  # src-layout 容器（无 __init__.py）
    pkg = container / "fuscan"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    entry = pkg / "__main__.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) == ("fuscan.__main__", "src/src")


def test_dotted_module_name_src_layout_nested_entry(tmp_path: Path) -> None:
    """src-layout 下嵌套入口（如 fuscan.gui.__main__)."""
    src_dir = tmp_path  # 项目根
    container = src_dir / "src"
    pkg = container / "fuscan"
    sub = pkg / "gui"
    sub.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    entry = sub / "__main__.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) == ("fuscan.gui.__main__", "src/src")


def test_dotted_module_name_src_layout_multi_container(tmp_path: Path) -> None:
    """多层容器前缀：src_dir/outer/inner/pkg/main.py，outer/inner 均无 __init__.py."""
    src_dir = tmp_path
    pkg = src_dir / "outer" / "inner" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    entry = pkg / "main.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) == ("pkg.main", "src/outer/inner")


def test_dotted_module_name_no_pkg_after_container(tmp_path: Path) -> None:
    """src-layout 容器下无包目录：返回 None（全无 __init__.py）."""
    src_dir = tmp_path
    container = src_dir / "src"
    pkg = container / "pkg"  # pkg 也无 __init__.py
    pkg.mkdir(parents=True)
    entry = pkg / "main.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) is None


def test_dotted_module_name_pkg_after_container_broken(tmp_path: Path) -> None:
    """src-layout 容器后首个包之后某级目录无 __init__.py：返回 None."""
    src_dir = tmp_path
    container = src_dir / "src"
    pkg = container / "pkg"
    broken = pkg / "broken"  # broken 无 __init__.py，在 pkg 之后
    broken.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    entry = broken / "main.py"
    entry.write_text("")
    assert EntryWrapper.dotted_module_name(src_dir, entry) is None


def test_generate_wrapper_source_top_level_mode() -> None:
    """顶层模式（module_dotted=None）生成 run_path 分支.

    顶层模式须显式将 ``_SRC_DIR`` 加入 ``sys.path``：``runpy.run_path`` 对文件
    路径不自动把脚本目录加入 ``sys.path``（与 ``python script.py`` 不同），不
    显式注入会导致 ``import module_c`` 等本地绝对导入 ``ModuleNotFoundError``
    （cli_complex 模板回归根因）。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    assert "fspack 生成的入口包装器（app）" in source
    assert "_ENTRY_MODULE = None" in source
    assert "_ENTRY_REL = 'app.py'" in source
    # 模板里两个 runpy 调用都在，靠 if _ENTRY_MODULE 控制流；此处验证 None 字面量
    assert "runpy.run_path" in source
    # 顶层模式显式注入 _SRC_DIR 到 sys.path，使本地绝对导入可用
    assert "if _SRC_DIR not in sys.path:" in source
    assert "sys.path.insert(0, _SRC_DIR)" in source


def test_generate_wrapper_source_package_mode() -> None:
    """包模式（module_dotted='src.game'）生成 run_module 分支."""
    source = EntryWrapper.generate_wrapper_source("gktetris", "src.game", "game.py")
    assert "fspack 生成的入口包装器（gktetris）" in source
    assert "_ENTRY_MODULE = 'src.game'" in source
    assert "_ENTRY_REL = 'game.py'" in source
    assert "_PKG_ROOT_REL = '.'" in source
    # 模板里两个 runpy 调用都在，靠 if _ENTRY_MODULE 控制流；此处验证模块名已注入
    assert "runpy.run_module(_ENTRY_MODULE" in source


def test_generate_wrapper_source_src_layout_pkg_root() -> None:
    """src-layout 包模式：pkg_root_rel='src/src' 注入 _PKG_ROOT_REL."""
    source = EntryWrapper.generate_wrapper_source("fuscan", "fuscan.__main__", "src/fuscan/__main__.py", "src/src")
    assert "_ENTRY_MODULE = 'fuscan.__main__'" in source
    assert "_ENTRY_REL = 'src/fuscan/__main__.py'" in source
    assert "_PKG_ROOT_REL = 'src/src'" in source
    assert "runpy.run_module(_ENTRY_MODULE" in source


def test_generate_wrapper_source_qt_plugin_paths() -> None:
    """wrapper 源码含 Qt 插件路径设置代码（PySide2/PySide6/PyQt5/6）."""
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    for qt_pkg in ("PySide2", "PySide6", "PyQt5", "PyQt6"):
        assert qt_pkg in source
    assert "QT_PLUGIN_PATH" in source
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" in source


def test_generate_wrapper_source_qt_dll_directory() -> None:
    """wrapper 源码含 os.add_dll_directory 调用，使 QML 插件能找到 Qt5/6*.dll 依赖.

    QML 插件（qml/QtQuick.2/qtquick2plugin.dll）加载时依赖 Qt5Core.dll/
    Qt5Quick.dll 等，这些 DLL 在 site-packages/<qt_pkg>/ 目录下，默认 DLL
    搜索路径不含此目录，需显式 add_dll_directory。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    assert "os.add_dll_directory" in source
    assert "_qt_root" in source


def test_generate_wrapper_source_tkinter_disabled_by_default() -> None:
    """has_tkinter 默认 False：wrapper 注入 `if False:` 跳过 Tcl/Tk 环境变量."""
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    assert "if False:" in source
    assert "TCL_LIBRARY" in source  # 模板含代码但分支不执行
    assert "TK_LIBRARY" in source
    # glob 延迟到 tkinter 分支内：顶层无缩进的 import glob 意味着所有程序
    # （含无 tkinter 的绝大多数）都要付 glob 导入链（~7-9ms）的启动成本
    assert "\nimport glob\n" not in source


def test_generate_wrapper_source_tkinter_enabled() -> None:
    """has_tkinter=True：wrapper 注入 `if True:` 启用 Tcl/Tk 环境变量设置."""
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py", has_tkinter=True)
    assert "if True:" in source
    assert "TCL_LIBRARY" in source
    assert "TK_LIBRARY" in source
    assert "glob.glob" in source


def test_generate_wrapper_source_multiprocessing_spawn_dispatch() -> None:
    """wrapper 含 multiprocessing spawn 重入分发：环境就绪后执行引导码退出.

    Windows spawn 以 ``[exe, ..., '-c', 'from multiprocessing.spawn import
    spawn_main; ...']`` 重入应用，C loader 把入口脚本插到 argv[1] 后 ``-c``
    沦为入口 CLI 参数（argparse invalid choice → worker 全崩 exitcode=2）。
    wrapper 须在进入 runpy 之前检测该布局：执行引导码（spawn_main 内部完成
    worker 反序列化与执行）并 ``SystemExit(0)``，不再走用户入口。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    # 检测逻辑：'-c' 精确匹配 + 'from multiprocessing.' 前缀匹配三类引导码
    assert 'sys.argv[_mp_arg_i] == "-c"' in source
    assert 'startswith("from multiprocessing.")' in source
    # 命中后执行引导码并以 0 退出（不走 runpy 用户入口）
    assert "exec(sys.argv[_mp_arg_i + 1])" in source
    assert "raise SystemExit(0)" in source
    # 分发须在 runpy 进入用户入口之前（环境就绪但未被入口副作用污染）；
    # 用实际调用语句定位（模板 docstring/注释亦含 runpy 字样，不可作定位点）
    assert source.index('startswith("from multiprocessing.")') < source.index(
        "runpy.run_path(os.path.join(_SRC_DIR"
    )


def test_generate_wrapper_source_includes_site_packages_path() -> None:
    """wrapper 将 dist/site-packages 加入 sys.path.

    Linux standalone python 在 PYTHONHOME 模式下默认不启用 site-packages，
    wrapper 须显式将 dist/site-packages 加入 sys.path，否则 rich 等第三方依赖
    ModuleNotFoundError（CI v0.2.5 Linux 自打包失败根因）。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    # site-packages 平铺到 dist/site-packages（与 runtime 平级）
    assert 'os.path.join(_DIST_DIR, "site-packages")' in source
    # 不再有平台分支的 glob fallback
    assert "python3.*" not in source
    # 显式加入 sys.path
    assert "sys.path.insert(0, _SITE_PACKAGES)" in source


def test_generate_wrapper_source_gui_subsystem_null_streams() -> None:
    """GUI 子系统（pythonw.exe / -mwindows loader）下 sys.stdout/stderr/stdin 为 None.

    第三方库（如 loguru ``logger.add(sys.stderr)``）写 None 会触发 __fastfail
    崩溃（0xC0000409 STATUS_STACK_BUFFER_OVERRUN）。wrapper 须用 os.devnull
    替代 None，使日志写入静默丢弃而非崩溃。console 子系统下三者非 None 不受影响。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    assert "if sys.stdout is None:" in source
    assert "if sys.stderr is None:" in source
    assert "if sys.stdin is None:" in source
    assert "os.devnull" in source


def test_generate_wrapper_source_path_importer_cache_prepopulated() -> None:
    """wrapper 预填充 sys.path_importer_cache 避免 lazy FileFinder 创建开销.

    site-packages 是最高频搜索路径，预创建 FileFinder 注入 path_importer_cache
    使首次 import 直接命中缓存，跳过 path_hooks 迭代。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    assert "sys.path_importer_cache" in source
    assert "importlib.machinery.FileFinder" in source
    assert "ExtensionFileLoader" in source
    assert "SourceFileLoader" in source
    assert "SourcelessFileLoader" in source


def test_generate_wrapper_source_lazy_imports_disabled_by_default() -> None:
    """lazy_imports 默认空元组：wrapper 注入 ``_LAZY_MODULES = ()``，if 块不执行."""
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    assert "_LAZY_MODULES = ()" in source
    # _LazyImportFinder 类定义在 if 块内，空元组时不执行 if 块
    assert "if _LAZY_MODULES and _SITE_PACKAGES" in source


def test_generate_wrapper_source_lazy_imports_enabled() -> None:
    """lazy_imports 非空：wrapper 注入 _LazyImportFinder meta path finder.

    --lazy-import numpy,pandas 指定的模块由 LazyLoader 包装，首次属性访问时
    才执行 __init__.py，降低启动时间。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py", lazy_imports=("numpy", "pandas"))
    # 模块名以元组字面量注入
    assert "_LAZY_MODULES = ('numpy', 'pandas')" in source
    # _LazyImportFinder 类定义存在
    assert "class _LazyImportFinder:" in source
    assert "importlib.util.LazyLoader" in source
    # 注册到 sys.meta_path 前端
    assert "sys.meta_path.insert(0, _LazyImportFinder" in source


def test_generate_wrapper_source_lazy_imports_single_module() -> None:
    """单个 lazy-import 模块：元组字面量含一个元素."""
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py", lazy_imports=("numpy",))
    assert "_LAZY_MODULES = ('numpy',)" in source
    assert "importlib.util.LazyLoader" in source


def test_generate_wrapper_source_lazy_imports_empty_tuple() -> None:
    """显式传空元组：与默认行为一致，if 块不执行."""
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py", lazy_imports=())
    assert "_LAZY_MODULES = ()" in source
    # 类定义仍在源码中（模板静态包含），但 if 块不执行
    assert "_LazyImportFinder" in source


# --- iter-148 前后端分离 Web 打包：wrapper 注入静态文件 serve 与开浏览器 ---


def test_generate_wrapper_source_web_static_dirs_and_open_browser() -> None:
    """web_static_dirs + open_browser=True 时注入静态文件 serve 与开浏览器逻辑."""
    source = EntryWrapper.generate_wrapper_source(
        "app",
        None,
        "app.py",
        web_static_dirs=("dist",),
        open_browser=True,
    )
    assert "_WEB_STATIC_DIRS = ('dist',)" in source
    assert "_OPEN_BROWSER = True" in source
    # Flask monkey-patch 与 FastAPI monkey-patch 均注入
    assert "_patch_flask" in source
    assert "_patch_fastapi" in source
    assert "webbrowser" in source
    assert "threading.Timer" in source


def test_generate_wrapper_source_web_static_dirs_empty_no_injection() -> None:
    """web_static_dirs 为空时不注入静态文件 serve（if 块不执行）."""
    source = EntryWrapper.generate_wrapper_source(
        "app",
        None,
        "app.py",
        web_static_dirs=(),
        open_browser=True,
    )
    assert "_WEB_STATIC_DIRS = ()" in source
    # 模板静态包含 _patch_flask 等定义，但 if 块不执行
    assert "if _WEB_STATIC_DIRS and _OPEN_BROWSER:" in source


def test_generate_wrapper_source_open_browser_false_no_injection() -> None:
    """open_browser=False 时即使有 web_static_dirs 也不注入（if 块不执行）."""
    source = EntryWrapper.generate_wrapper_source(
        "app",
        None,
        "app.py",
        web_static_dirs=("dist",),
        open_browser=False,
    )
    assert "_WEB_STATIC_DIRS = ('dist',)" in source
    assert "_OPEN_BROWSER = False" in source
    # if 块条件 _WEB_STATIC_DIRS and _OPEN_BROWSER 不满足
    assert "if _WEB_STATIC_DIRS and _OPEN_BROWSER:" in source


def test_generate_wrapper_source_timing_ticks() -> None:
    """wrapper 注入 FSPACK_TIMING 打点：env_ready/entry_start/entry_done 三处调用.

    ``compile`` 验证生成源码语法（模板中 ``\\n`` 转义错误会导致跨行字符串
    语法错误，在此提前拦截）。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    compile(source, "_entry_app.py", "exec")
    assert '_FSPACK_TIMING = os.environ.get("FSPACK_TIMING") == "1"' in source
    assert '_fspack_tick("env_ready")' in source
    assert '_fspack_tick("entry_start")' in source
    assert '_fspack_tick("entry_done")' in source
    # 打点行格式：换行必须为字面反斜杠 n（模板普通字符串中 \n 会被解释为
    # 真实换行，破坏生成源码语法）
    assert '"[fspack timing] %s @%.1fms\\n"' in source


def test_generate_wrapper_source_py_init_gap() -> None:
    """wrapper 读 loader QPC 锚点计算 C 层初始化缝隙，输出 timing-gap 行.

    Windows loader 在 Py_Main 调用前写 FSPACK_LOADER_QPC_MS（perf_counter
    同源 QPC），wrapper 首语句相减得缝隙；负值（时钟源异常/旧 dist 无锚点）
    不打点。``compile`` 验证生成源码语法。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    compile(source, "_entry_app.py", "exec")
    assert 'os.environ.get("FSPACK_LOADER_QPC_MS")' in source
    assert '"[fspack timing-gap] py_init %.1fms\\n"' in source
    # 负值防御：时钟源异常时不输出打点
    assert "if _py_init_ms >= 0.0:" in source


def test_generate_wrapper_source_gui_ready_hook() -> None:
    """wrapper 注入 GUI 事件循环自终止钩子：import 拦截 + Qt/tkinter patch.

    ``FSPACK_TIMING=1`` 时经 builtins.__import__ 拦截框架首次导入，
    patch ``QApplication.exec``/``Tk.mainloop``：处理首帧事件后打点
    ``gui_ready`` 并直接返回，GUI 应用"进入界面后自行终止"。``compile``
    验证生成源码语法。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    compile(source, "_entry_app.py", "exec")
    # import 拦截器：仅 FSPACK_TIMING 时安装，patch 成功后保持原生 import
    assert "_fspack_orig_import = _builtins.__import__" in source
    assert "_builtins.__import__ = _fspack_import_hook" in source
    assert "builtins.__import__ = _fspack_orig_import" in source
    # 防递归关键：命中框架先卸载拦截器再执行本次导入（tkinter 模块体的
    # from tkinter.constants import * 嵌套导入不得再进入钩子）
    uninstall_pos = source.index("        _builtins.__import__ = _fspack_orig_import")
    import_pos = source.index("        mod = _fspack_orig_import(name, *args, **kwargs)")
    assert uninstall_pos < import_pos
    # patch 失败（ImportError）时重装拦截器待命重试
    assert source.count("_builtins.__import__ = _fspack_import_hook") >= 2
    # 框架清单与 patch 目标
    assert '_GUI_TOPS = ("PySide2", "PySide6", "PyQt5", "PyQt6", "tkinter")' in source
    assert "self.processEvents()" in source  # Qt：处理 show/paint 队列首帧上屏
    assert "self.update()" in source  # tkinter：处理 pending 事件
    assert "tkinter.Tk.mainloop = _fspack_mainloop" in source
    # gui_ready 打点复用 _fspack_tick（累计时刻行，runner 侧解析）
    assert '_fspack_tick("gui_ready")' in source
    # patch 后直接返回：Qt 返回 0（兼容 sys.exit(app.exec()) 模式）
    assert "return 0" in source


def test_generate_wrapper_source_gui_hook_no_brace_literals() -> None:
    """GUI 钩子块不含字面花括号（wrapper 模板经 str.format 填充）.

    钩子代码刻意避免 dict/set 字面量与 f-string，字面 ``{``/``}`` 会被
    format 误解析为占位符（KeyError）。此测试防止后续维护引入花括号。
    """
    source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    # 提取钩子块（从 _GUI_TOPS 到 tkinter 环境变量注释前）
    start = source.index("_GUI_TOPS")
    end = source.index("# tkinter 环境变量")
    hook_block = source[start:end]
    assert "{" not in hook_block and "}" not in hook_block


def _run_wrapper_with_timing(tmp_path: Path, site_packages: Path, entry_code: str) -> subprocess.CompletedProcess[str]:
    """生成 wrapper 并在子进程以 FSPACK_TIMING=1 执行，返回 CompletedProcess.

    site-packages 放置假 GUI 框架包，用户入口由 entry_code 给出；子进程
    环境继承当前进程并注入剖析开关。
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "app.py").write_text(entry_code, encoding="utf-8")
    assert site_packages.is_dir()

    wrapper_source = EntryWrapper.generate_wrapper_source("app", None, "app.py")
    wrapper_path = tmp_path / "_entry_app.py"
    wrapper_path.write_text(wrapper_source, encoding="utf-8")

    env = {**os.environ, "FSPACK_TIMING": "1"}
    return subprocess.run(
        [sys.executable, str(wrapper_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def test_generate_wrapper_gui_hook_tkinter_no_recursion(tmp_path: Path) -> None:
    """回归：剖析钩子拦截 tkinter 导入不再无限递归（RecursionError）.

    tkinter 模块体首行 ``from tkinter.constants import *`` 属同框架嵌套导入，
    旧钩子在该嵌套导入上重入安装逻辑，与 ``_fspack_patch_tkinter`` 自身的
    ``import tkinter`` 叠加成无限递归。用含同构嵌套导入的假 tkinter 包端到端
    执行 wrapper 验证：进程正常退出、输出 gui_ready 打点、mainloop 被 patch
    后立即返回。
    """
    tk_dir = tmp_path / "site-packages" / "tkinter"
    tk_dir.mkdir(parents=True)
    # 模块体嵌套同框架导入（复现真实 tkinter 的递归触发点）
    (tk_dir / "__init__.py").write_text(
        "from tkinter.constants import *\n"
        "\n"
        "\n"
        "class Tk:\n"
        "    def update(self):\n"
        "        pass\n"
        "\n"
        "    def mainloop(self):\n"
        "        raise SystemExit('mainloop not patched')\n",
        encoding="utf-8",
    )
    (tk_dir / "constants.py").write_text("X = 1\n", encoding="utf-8")

    result = _run_wrapper_with_timing(
        tmp_path,
        tmp_path / "site-packages",
        "import tkinter\ntkinter.Tk().mainloop()\nprint('app-exit')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "RecursionError" not in result.stderr
    assert "mainloop not patched" not in result.stderr
    assert "[fspack timing] gui_ready" in result.stderr
    assert "app-exit" in result.stdout


def test_generate_wrapper_gui_hook_qt_patch_and_native_import_restored(tmp_path: Path) -> None:
    """剖析钩子 patch Qt exec 并在安装后恢复原生 import（无递归/无残留拦截）.

    假 PySide6 包的 QtWidgets 模块体含 ``from PySide6.QtCore import ...``
    嵌套同框架导入（对应真实 QtWidgets 结构），验证命中框架先卸载拦截器
    的新时序下 Qt 路径同样无递归且 patch 生效。
    """
    qt_dir = tmp_path / "site-packages" / "PySide6"
    qt_dir.mkdir(parents=True)
    (qt_dir / "__init__.py").write_text("", encoding="utf-8")
    (qt_dir / "QtCore.py").write_text("Qt = object\n", encoding="utf-8")
    # 模块体嵌套同框架导入（对应真实 QtWidgets 的 from PySide6.QtCore import ...）
    (qt_dir / "QtWidgets.py").write_text(
        "from PySide6.QtCore import Qt\n"
        "\n"
        "\n"
        "class QApplication:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n"
        "\n"
        "    def processEvents(self):\n"
        "        pass\n"
        "\n"
        "    def exec(self):\n"
        "        raise SystemExit('exec not patched')\n",
        encoding="utf-8",
    )

    result = _run_wrapper_with_timing(
        tmp_path,
        tmp_path / "site-packages",
        "from PySide6.QtWidgets import QApplication\napp = QApplication([])\napp.exec()\nprint('app-exit')\n",
    )
    assert result.returncode == 0, result.stderr
    assert "RecursionError" not in result.stderr
    assert "exec not patched" not in result.stderr
    assert "[fspack timing] gui_ready" in result.stderr
    assert "app-exit" in result.stdout
