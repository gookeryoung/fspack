"""AST 依赖分析：扫描 import，分类标准库/本地/第三方."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from fspack.config import DependencyReport

__all__ = [
    "STDLIB_FALLBACK",
    "analyze_dependencies",
    "collect_imports",
    "collect_imports_and_submodules",
    "collect_submodule_imports",
]

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


def _local_packages(src_dir: Path, project_name: str) -> set[str]:
    """识别项目本地包/模块名（顶层 .py 与含 __init__.py 的目录）."""
    local: set[str] = {project_name}
    for entry in src_dir.iterdir():
        if entry.is_file() and entry.suffix == ".py":
            local.add(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            local.add(entry.name)
    return local


_EXCLUDED_DIRS = frozenset(
    {
        "dist",
        "build",
        ".git",
        "__pycache__",
        ".venv",
        ".tox",
        ".fspack",
        ".trae",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)


def _is_excluded(path: Path, src_dir: Path) -> bool:
    """判断 .py 文件是否位于构建产物或缓存目录下，应跳过扫描."""
    parts = path.relative_to(src_dir).parts[:-1]
    return any(part in _EXCLUDED_DIRS or part.endswith(".egg-info") for part in parts)


def analyze_dependencies(src_dir: Path, project_name: str, declared: tuple[str, ...]) -> DependencyReport:
    """扫描 src_dir 下所有 .py，分类 import 为标准库/本地/第三方。

    自动排除 dist/build/.venv 等构建产物与缓存目录，避免扫描到已解包的
    embed python 或 python-build-standalone 标准库源码导致误报依赖。
    """
    all_imports: list[str] = []
    all_submodules: dict[str, set[str]] = {}
    for py in src_dir.rglob("*.py"):
        if _is_excluded(py, src_dir):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        tops, subs = collect_imports_and_submodules(tree)
        all_imports.extend(tops)
        for pkg, sub_set in subs.items():
            all_submodules.setdefault(pkg, set()).update(sub_set)

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
    ast_submodules = {
        pkg: frozenset(subs) for pkg, subs in all_submodules.items() if pkg not in local and pkg not in _STDLIB
    }
    return DependencyReport(
        declared=declared,
        ast_third_party=tuple(third),
        ast_stdlib=tuple(stdlib),
        ast_local=tuple(local_imports),
        ast_submodules=ast_submodules,
    )
