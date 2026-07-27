"""AST 依赖分析：扫描 import，分类标准库/本地/第三方."""

from __future__ import annotations

import ast
import hashlib
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Iterator

from fspack.config import DependencyReport

__all__ = [
    "STDLIB_FALLBACK",
    "analyze_dependencies",
    "collect_imports",
    "collect_imports_and_submodules",
    "collect_submodule_imports",
    "source_fingerprint",
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
        # 开发期目录：非运行时代码，扫描会导致误报依赖
        "examples",
        "tests",
        "docs",
        "templates",
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

    文件数超过 :data:`_PARALLEL_THRESHOLD` 时使用 :class:`ProcessPoolExecutor`
    并行解析（CPU 密集 ``ast.parse``），大项目显著提速。小项目走串行路径
    避免进程池启动开销（Windows spawn 约 100-200ms，需足够工作量摊销）。
    """
    py_files: list[Path] = [py for py in src_dir.rglob("*.py") if not _is_excluded(py, src_dir)]

    all_imports: list[str] = []
    all_submodules: dict[str, set[str]] = {}

    if len(py_files) >= _PARALLEL_THRESHOLD:
        _parse_parallel(py_files, all_imports, all_submodules)
    else:
        _parse_serial(py_files, all_imports, all_submodules)

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


# 并行解析阈值：低于此文件数走串行，避免进程池启动开销
# Windows spawn 启动 ~100-200ms，需足够工作量摊销；Linux fork 较快可更低
_PARALLEL_THRESHOLD = 200


def _parse_file_worker(py: str) -> tuple[list[str], dict[str, frozenset[str]]]:
    """进程池 worker：解析单个 .py 文件返回 ``(顶层导入, 子模块字典)``。

    错误文件返回空结果 ``([], {})``。模块级函数确保可 pickle 跨进程传递；
    接收 ``str`` 路径（比 ``Path`` 序列化更轻量）。
    """
    try:
        tree = ast.parse(Path(py).read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return [], {}
    return collect_imports_and_submodules(tree)


def _parse_serial(py_files: list[Path], all_imports: list[str], all_submodules: dict[str, set[str]]) -> None:
    """串行解析所有 .py 文件，结果合并到 ``all_imports`` / ``all_submodules``."""
    for py in py_files:
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        tops, subs = collect_imports_and_submodules(tree)
        all_imports.extend(tops)
        for pkg, sub_set in subs.items():
            all_submodules.setdefault(pkg, set()).update(sub_set)


def _parse_parallel(py_files: list[Path], all_imports: list[str], all_submodules: dict[str, set[str]]) -> None:
    """进程池并行解析 .py 文件（CPU 密集 ``ast.parse``）。

    ``chunksize`` 按 CPU 核心数与文件数自适应，减少 IPC 调度开销。
    """
    cpu_count = os.cpu_count() or 4
    chunksize = max(1, len(py_files) // (cpu_count * 4))
    with ProcessPoolExecutor(max_workers=cpu_count) as pool:
        for tops, subs in pool.map(_parse_file_worker, [str(p) for p in py_files], chunksize=chunksize):
            all_imports.extend(tops)
            for pkg, sub_set in subs.items():
                all_submodules.setdefault(pkg, set()).update(sub_set)


def source_fingerprint(src_dir: Path) -> str:
    """计算源码指纹用于依赖分析缓存键。

    遍历 ``src_dir`` 下所有不被排除的 ``.py`` 文件，以 ``相对路径|mtime_ns|size``
    拼接后求 SHA-256。与 :func:`analyze_dependencies` 使用相同的排除逻辑
    （``_EXCLUDED_DIRS``），保证指纹只反映被分析的源码变化。

    用 :func:`os.scandir` 递归遍历，利用 :meth:`os.DirEntry.stat` 缓存目录
    枚举时的 stat 信息（Windows ``WIN32_FIND_DATA`` / Linux ``d_ino``），
    避免对每个文件单独 ``stat`` 系统调用。同时按名称排序目录条目（含子目录），
    保证跨平台/文件系统的指纹确定性（``os.walk`` 不保证目录遍历顺序）。
    """
    h = hashlib.sha256()
    for rel, mtime_ns, size in _iter_py_entries(src_dir, src_dir):
        h.update(f"{rel}|{mtime_ns}|{size}\n".encode())
    return h.hexdigest()


def _iter_py_entries(current: Path, root: Path) -> Iterator[tuple[str, int, int]]:
    """递归遍历 ``.py`` 文件，返回 ``(相对路径, mtime_ns, size)`` 三元组。

    :func:`os.scandir` 返回的 :class:`os.DirEntry` 对象缓存了目录枚举时的
    stat 信息，``entry.stat(follow_symlinks=False)`` 直接复用缓存避免独立
    stat 调用。剪枝排除 ``_EXCLUDED_DIRS`` 与 ``*.egg-info`` 目录。

    条目按名称排序（含子目录），保证遍历顺序跨平台确定性——``os.walk``
    不保证目录遍历顺序，导致旧实现在不同文件系统上指纹不一致。
    """
    for entry in sorted(os.scandir(current), key=lambda e: e.name):
        if entry.is_dir(follow_symlinks=False):
            if entry.name in _EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                continue
            yield from _iter_py_entries(Path(entry.path), root)
        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".py"):
            rel = Path(entry.path).relative_to(root).as_posix()
            st = entry.stat(follow_symlinks=False)
            yield (rel, st.st_mtime_ns, st.st_size)
