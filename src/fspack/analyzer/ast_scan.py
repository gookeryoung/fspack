"""AST 依赖扫描：从 Python 源码与 QML 文件提取 import.

:mod:`fspack.analyzer` 子包的 AST 解析模块，专注于"从代码文本提取 import
语句"——纯 AST 解析与正则匹配，无文件系统遍历（文件系统遍历见
:mod:`fspack.analyzer.fingerprint`）。

公开 API：

- :data:`STDLIB_FALLBACK`：Python 3.8/3.9 无 ``sys.stdlib_module_names`` 时的回退集合
- :func:`collect_imports`：收集 AST 顶层 import 模块名（去重保序）
- :func:`collect_imports_and_submodules`：单次 ``ast.walk`` 同时收集顶层与子模块
- :func:`collect_submodule_imports`：仅收集子模块级 import
- :func:`parse_qml_imports`：解析 QML 文件 import 提取 Qt 子模块名
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

__all__ = [
    "STDLIB_FALLBACK",
    "_qml_module_to_qt_sub",
    "collect_imports",
    "collect_imports_and_submodules",
    "collect_submodule_imports",
    "parse_qml_imports",
]

# Qt Python 绑定包名集合：QML 模块依赖需加入这些包的子模块集合。
# PySide2/PySide6/PyQt5/PyQt6 共享同一 QML 模块系统。
_QT_PYTHON_PACKAGES: frozenset[str] = frozenset({"PySide2", "PySide6", "PyQt5", "PyQt6"})

# QML 模块名 → Qt 子模块名（归一化名）显式映射表。
# 处理 QML 模块名与 DLL 子模块名不一致的情况：
# - ``QtQuick.Controls`` → ``QuickControls2``（Qt5/6 均为 Controls 2，DLL 名带 2 后缀）
# - ``QtQuick.Templates`` → ``QuickTemplates2``（同上）
# - ``QtQuick.Layouts`` → ``QuickLayouts``（去点号）
# - ``QtQuick.Shapes`` → ``QuickShapes``（去点号）
# - ``QtQuick.Window``/``QtQuick.Particles``/``QtQuick.LocalStorage`` 等 → ``Quick``
#   （这些是 QtQuick 的子模块，对应同一 ``Qt5Quick.dll``）
# - ``QtWebEngine`` → ``WebEngineCore``（QML 模块对应 WebEngineCore DLL）
_QML_MODULE_TO_QT_SUB: dict[str, str] = {
    "QtQuick.Controls": "QuickControls2",
    "QtQuick.Templates": "QuickTemplates2",
    "QtQuick.Layouts": "QuickLayouts",
    "QtQuick.Shapes": "QuickShapes",
    # 以下均为 QtQuick 子模块，对应 Qt5Quick.dll
    "QtQuick.Window": "Quick",
    "QtQuick.Particles": "Quick",
    "QtQuick.LocalStorage": "Quick",
    "QtQuick.Dialogs": "Quick",
    "QtQuick.Extras": "Quick",
    "QtQuick.PrivateWidgets": "Quick",
    "QtQuick.VirtualKeyboard": "Quick",
    "QtQuick.Timeline": "Quick",
    "QtQuick.Scene2D": "Quick",
    "QtQuick.Scene3D": "Quick",
    "QtQuick.Pdf": "Quick",
    # QML 模块对应不同 DLL 子模块名
    "QtWebEngine": "WebEngineCore",
}

# QML import 语句正则：``import QtXxx[.Yyy] [version]``
# 匹配 ``import QtQuick 2.15``/``import QtQuick.Controls 2.15``/``import QtQuick3D 2.15``
# 不匹配 ``import "."``/``import "scripts.js" as Scripts``（相对/JS 导入）
_QML_IMPORT_RE = re.compile(r"^\s*import\s+(Qt[\w.]+)(?:\s+\d+(?:\.\d+)*)?\s*$")


def _qml_module_to_qt_sub(qml_module: str) -> str | None:
    """QML 模块名 → Qt 子模块名（归一化名）.

    返回 None 表示非 Qt 模块（如 ``import "."`` 相对导入）。
    先查 :data:`_QML_MODULE_TO_QT_SUB` 显式映射，未命中时按默认规则
    去掉 ``Qt`` 前缀（如 ``QtQuick`` → ``Quick``、``QtCharts`` → ``Charts``、
    ``QtMultimedia`` → ``Multimedia``）。
    """
    # 仅 "Qt" 无后续字符或非 Qt 前缀返回 None
    if not qml_module.startswith("Qt") or qml_module == "Qt":
        return None
    if qml_module in _QML_MODULE_TO_QT_SUB:
        return _QML_MODULE_TO_QT_SUB[qml_module]
    return qml_module[2:]


def parse_qml_imports(qml_file: Path) -> set[str]:
    """解析 QML 文件中的 import 语句，返回 Qt 子模块名（归一化名）集合.

    QML import 语法：
    - ``import QtQuick 2.15`` → ``Quick``
    - ``import QtQuick.Controls 2.15`` → ``QuickControls2``
    - ``import QtQuick.Layouts 1.15`` → ``QuickLayouts``
    - ``import "."`` → 忽略（相对导入）
    - ``import "scripts.js" as Scripts`` → 忽略（JS 文件导入）

    文件读取失败返回空集合（不抛异常，避免阻塞依赖分析）。
    """
    subs: set[str] = set()
    try:
        text = qml_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return subs
    for line in text.splitlines():
        m = _QML_IMPORT_RE.match(line)
        if m:
            qml_module = m.group(1)
            qt_sub = _qml_module_to_qt_sub(qml_module)
            if qt_sub:
                subs.add(qt_sub)
    return subs


# Python 3.8/3.9 没有 sys.stdlib_module_names，用 curate 的集合回退
STDLIB_FALLBACK: frozenset[str] = frozenset(
    {
        "abc",
        "aifc",
        "argparse",
        "array",
        "ast",
        "asynchat",
        "asyncio",
        "asyncore",
        "atexit",
        "audioop",
        "base64",
        "bdb",
        "binascii",
        "binhex",
        "bisect",
        "builtins",
        "bz2",
        "calendar",
        "cgi",
        "cgitb",
        "chunk",
        "cmath",
        "cmd",
        "code",
        "codecs",
        "codeop",
        "collections",
        "colorsys",
        "compileall",
        "concurrent",
        "configparser",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "crypt",
        "csv",
        "ctypes",
        "curses",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "dis",
        "distutils",
        "doctest",
        "email",
        "encodings",
        "enum",
        "errno",
        "faulthandler",
        "fcntl",
        "filecmp",
        "fileinput",
        "fnmatch",
        "formatter",
        "fractions",
        "ftplib",
        "functools",
        "gc",
        "genericpath",
        "getopt",
        "getpass",
        "gettext",
        "glob",
        "graphlib",
        "grp",
        "gzip",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "http",
        "idlelib",
        "imaplib",
        "imghdr",
        "imp",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "keyword",
        "lib2to3",
        "linecache",
        "locale",
        "logging",
        "lzma",
        "mailbox",
        "mailcap",
        "marshal",
        "math",
        "mimetypes",
        "mmap",
        "modulefinder",
        "msilib",
        "msvcrt",
        "multiprocessing",
        "netrc",
        "nis",
        "nntplib",
        "ntpath",
        "numbers",
        "opcode",
        "operator",
        "optparse",
        "os",
        "ossaudiodev",
        "parser",
        "pathlib",
        "pdb",
        "pickle",
        "pickletools",
        "pipes",
        "pkgutil",
        "platform",
        "plistlib",
        "poplib",
        "posix",
        "posixpath",
        "pprint",
        "profile",
        "pstats",
        "pty",
        "pwd",
        "py_compile",
        "pyclbr",
        "pydoc",
        "pydoc_data",
        "pyexpat",
        "queue",
        "quopri",
        "random",
        "re",
        "readline",
        "reprlib",
        "resource",
        "rlcompleter",
        "runpy",
        "sched",
        "secrets",
        "select",
        "selectors",
        "shelve",
        "shlex",
        "shutil",
        "signal",
        "site",
        "smtpd",
        "smtplib",
        "sndhdr",
        "socket",
        "socketserver",
        "spwd",
        "sqlite3",
        "sre_compile",
        "sre_constants",
        "sre_parse",
        "ssl",
        "stat",
        "statistics",
        "string",
        "stringprep",
        "struct",
        "subprocess",
        "sunau",
        "symbol",
        "symtable",
        "sys",
        "sysconfig",
        "syslog",
        "tabnanny",
        "tarfile",
        "telnetlib",
        "tempfile",
        "termios",
        "test",
        "textwrap",
        "threading",
        "time",
        "timeit",
        "tkinter",
        "token",
        "tokenize",
        "trace",
        "traceback",
        "tracemalloc",
        "tty",
        "turtle",
        "types",
        "typing",
        "unicodedata",
        "unittest",
        "urllib",
        "uu",
        "uuid",
        "venv",
        "warnings",
        "wave",
        "weakref",
        "webbrowser",
        "winreg",
        "winsound",
        "wsgiref",
        "xdrlib",
        "xml",
        "xmlrpc",
        "zipapp",
        "zipfile",
        "zipimport",
        "zlib",
        "_thread",
        "__future__",
    }
)

_STDLIB: frozenset[str] = getattr(sys, "stdlib_module_names", STDLIB_FALLBACK)


def collect_imports_and_submodules(tree: ast.AST) -> tuple[list[str], dict[str, frozenset[str]]]:
    """单次 ``ast.walk`` 同时收集顶层导入与子模块导入。

    返回 ``(顶层导入列表, 子模块字典)``，语义分别与 :func:`collect_imports` /
    :func:`collect_submodule_imports` 一致。合并单次遍历避免对同一棵 AST
    走两遍的开销（大项目数百 .py 文件时收益明显）。

    只需顶层导入（如 :func:`infer_app_type`）或只需子模块的场景应直接用
    对应的独立函数，避免多余计算。
    """
    top_result: list[str] = []
    top_seen: set[str] = set()
    sub_result: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                _push(parts[0], top_result, top_seen)
                if len(parts) >= 2:
                    sub_result.setdefault(parts[0], set()).add(parts[1])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            parts = node.module.split(".")
            _push(parts[0], top_result, top_seen)
            if len(parts) >= 2:
                sub_result.setdefault(parts[0], set()).add(parts[1])
            elif len(parts) == 1:
                for alias in node.names:
                    if alias.name != "*":
                        sub_result.setdefault(parts[0], set()).add(alias.name)
    return top_result, {pkg: frozenset(subs) for pkg, subs in sub_result.items()}


def collect_imports(tree: ast.AST) -> list[str]:
    """收集 AST 中所有 import 的顶层模块名，去重保序."""
    result: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _push(alias.name.split(".")[0], result, seen)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            _push(node.module.split(".")[0], result, seen)
    return result


def _push(top: str, result: list[str], seen: set[str]) -> None:
    """将顶层模块名添加到结果列表，去重保序."""
    if top and top not in seen:
        seen.add(top)
        result.append(top)


def collect_submodule_imports(tree: ast.AST) -> dict[str, frozenset[str]]:
    """收集 AST 中子模块级 import，返回 {顶层包: frozenset[子模块名]}。

    处理三种形式：
    - ``import X.Y`` → ``{X: {Y}}``
    - ``from X.Y import Z`` → ``{X: {Y}}``
    - ``from X import Y`` → ``{X: {Y}}``（Y 可能是类/函数名，保留在集合中无害——
      不匹配任何 wheel 文件时自然忽略）

    相对导入（``level > 0``）与星号导入（``*``）跳过。
    """
    result: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) >= 2:
                    result.setdefault(parts[0], set()).add(parts[1])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            parts = node.module.split(".")
            if len(parts) >= 2:
                result.setdefault(parts[0], set()).add(parts[1])
            elif len(parts) == 1:
                for alias in node.names:
                    if alias.name != "*":
                        result.setdefault(parts[0], set()).add(alias.name)
    return {pkg: frozenset(subs) for pkg, subs in result.items()}
