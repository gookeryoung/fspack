"""C loader 源码生成与 mingw 交叉编译。

loader.exe 与 python3X._pth 同目录（dist/），动态加载 dist/runtime/python3X.dll，
解析 ``Py_Main`` 符号后以 ``[loader.exe, dist/src/<entry>, ...用户参数]`` 调用。
sys.path 由 _pth 文件控制，loader 不再设置环境变量。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fspack.config import AppType
from fspack.exceptions import LoaderError

__all__ = [
    "MINGW_GCC",
    "compile_loader",
    "generate_loader_source",
    "mingw_available",
]

_logger = logging.getLogger(__name__)
MINGW_GCC = "x86_64-w64-mingw32-gcc"

_LOADER_C_TEMPLATE = r"""/* fspack 生成的 C loader —— 加载 embed python 并运行用户入口脚本 */
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <stdlib.h>

#define ENTRY_FILE L"{entry_file}"
#define PYTHON_DLL L"{python_dll}"

typedef int (*Py_Main_t)(int argc, wchar_t **argv);

static void exe_dir(wchar_t *buf, size_t cap) {{
    GetModuleFileNameW(NULL, buf, (DWORD)cap);
    wchar_t *slash = wcsrchr(buf, L'\\');
    if (slash) *slash = L'\0';
}}

int wmain(int argc, wchar_t **argv) {{
    wchar_t dir[MAX_PATH];
    exe_dir(dir, MAX_PATH);

    wchar_t dll[MAX_PATH], entry[MAX_PATH];
    _snwprintf(dll, MAX_PATH, L"%s\\%s", dir, PYTHON_DLL);
    _snwprintf(entry, MAX_PATH, L"%s\\%s", dir, ENTRY_FILE);

    HMODULE h = LoadLibraryW(dll);
    if (!h) {{
        fwprintf(stderr, L"加载 Python DLL 失败: %s\n", dll);
        return 1;
    }}
    Py_Main_t py_main = (Py_Main_t)GetProcAddress(h, "Py_Main");
    if (!py_main) {{
        fwprintf(stderr, L"未找到 Py_Main 符号: %s\n", dll);
        return 1;
    }}

    wchar_t **new_argv = (wchar_t **)malloc(sizeof(wchar_t *) * (argc + 1));
    if (!new_argv) {{
        return 1;
    }}
    new_argv[0] = argv[0];
    new_argv[1] = entry;
    for (int i = 1; i < argc; i++) {{
        new_argv[1 + i] = argv[i];
    }}
    return py_main(argc + 1, new_argv);
}}
"""


def generate_loader_source(entry_rel_from_dist: str, py_xy: str) -> str:
    """生成 C loader 源码。

    entry_rel_from_dist: 入口脚本相对 dist 的 posix 路径（如 src/helloworld.py）。
    py_xy: 形如 python311 的版本前缀。
    """
    entry_win = entry_rel_from_dist.replace("/", "\\")
    python_dll = f"runtime\\{py_xy}.dll"
    return _LOADER_C_TEMPLATE.format(entry_file=entry_win, python_dll=python_dll)


def mingw_available() -> bool:
    """检测 mingw 交叉编译器是否可用。."""
    import shutil

    return shutil.which(MINGW_GCC) is not None


def compile_loader(source: str, out_exe: Path, app_type: AppType, work_dir: Path) -> Path:
    """用 mingw 交叉编译 loader 源码为 Windows .exe，返回可执行文件路径。."""
    work_dir.mkdir(parents=True, exist_ok=True)
    c_file = work_dir / "loader.c"
    c_file.write_text(source, encoding="utf-8")
    out_exe.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [MINGW_GCC, "-O2", "-municode", "-o", str(out_exe), str(c_file)]
    if app_type is AppType.GUI:
        cmd.insert(1, "-mwindows")
    _logger.info("编译 loader: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise LoaderError(f"未找到 mingw 编译器 {MINGW_GCC}，请安装 mingw-w64") from e
    except subprocess.CalledProcessError as e:
        raise LoaderError(f"loader 编译失败:\n{e.stderr}") from e
    return out_exe
