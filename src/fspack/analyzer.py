"""AST 依赖分析：扫描 import，分类标准库/本地/第三方。."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from fspack.config import DependencyReport

__all__ = ["STDLIB_FALLBACK", "analyze_dependencies", "collect_imports"]

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


def collect_imports(tree: ast.AST) -> list[str]:
    """收集 AST 中所有 import 的顶层模块名，去重保序。."""
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
    if top and top not in seen:
        seen.add(top)
        result.append(top)


def _local_packages(src_dir: Path, project_name: str) -> set[str]:
    """识别项目本地包/模块名（顶层 .py 与含 __init__.py 的目录）。."""
    local: set[str] = {project_name}
    for entry in src_dir.iterdir():
        if entry.is_file() and entry.suffix == ".py":
            local.add(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            local.add(entry.name)
    return local


def analyze_dependencies(src_dir: Path, project_name: str, declared: tuple[str, ...]) -> DependencyReport:
    """扫描 src_dir 下所有 .py，分类 import 为标准库/本地/第三方。."""
    all_imports: list[str] = []
    for py in src_dir.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        all_imports.extend(collect_imports(tree))

    local = _local_packages(src_dir, project_name)
    stdlib: list[str] = []
    third: list[str] = []
    local_imports: list[str] = []
    seen: set[str] = set()
    for imp in all_imports:
        if imp in seen:
            continue
        seen.add(imp)
        if imp in local:
            local_imports.append(imp)
        elif imp in _STDLIB:
            stdlib.append(imp)
        else:
            third.append(imp)
    return DependencyReport(
        declared=declared,
        ast_third_party=tuple(third),
        ast_stdlib=tuple(stdlib),
        ast_local=tuple(local_imports),
    )
