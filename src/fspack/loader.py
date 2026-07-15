"""C loader 源码生成与交叉编译。

Windows：loader.exe 在 dist/，动态加载 dist/runtime/python3X.dll，
解析 ``Py_Main`` 符号后以 ``[loader.exe, dist/src/<entry>, ...用户参数]`` 调用。
sys.path 由 dist/runtime/python3X._pth 文件控制（与 DLL 同目录），loader 不再设置环境变量。

Linux：loader 与 runtime/python/ 同目录（dist/），dlopen dist/runtime/python/lib/libpython3.X.so，
setenv PYTHONHOME 指向 runtime/python，调用 ``Py_BytesMain`` 运行入口脚本。

入口脚本路径在运行时从 ``<exe_dir>/<exe_basename>.entry`` 文件读取（多入口模式），
回退到 ``<exe_dir>/.entry``（单入口模式，向后兼容）。构建时为每个入口写对应
``<name>.entry`` 文件，使 loader 源码仅依赖 ``py_xy`` 与平台，可按
``(py_xy, app_type, platform)`` 缓存跨项目复用。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path

from fspack.config import AppType
from fspack.exceptions import LoaderError
from fspack.platform import Platform
from fspack.progress import StageRecorder, spinner

__all__ = [
    "LINUX_GCC",
    "MINGW_GCC",
    "compile_loader",
    "gcc_available",
    "generate_loader_source",
    "loader_cache_dir",
    "mingw_available",
]

_logger = logging.getLogger(__name__)
MINGW_GCC = "x86_64-w64-mingw32-gcc"
LINUX_GCC = "gcc"


def loader_cache_dir() -> Path:
    """返回 fspack loader 缓存目录 ``~/.fspack/cache/loaders/``。."""
    return Path.home() / ".fspack" / "cache" / "loaders"


def _loader_cache_key(source: str, app_type: AppType, platform: Platform) -> str:
    """计算 loader 缓存键：sha256(source + app_type + platform) 前 16 字符 hex。

    源码仅依赖 ``py_xy`` 与平台（入口路径运行时从 ``<exe_basename>.entry``
    或回退 ``.entry`` 读取），应用类型影响 ``-mwindows`` 编译选项，三者组合哈希
    作为缓存文件名，保证同配置命中、改配置失效。
    """
    h = hashlib.sha256()
    h.update(source.encode("utf-8"))
    h.update(app_type.value.encode("utf-8"))
    h.update(platform.value.encode("utf-8"))
    return h.hexdigest()[:16]


_LOADER_C_WINDOWS = r"""/* fspack 生成的 C loader —— 加载 embed python 并运行用户入口脚本
   入口脚本路径从 <exe_basename>.entry 文件读取，回退 .entry（单入口兼容） */
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <stdlib.h>

#define PYTHON_DLL L"{python_dll}"
#define MAX_ENTRY 512

typedef int (*Py_Main_t)(int argc, wchar_t **argv);

static void split_exe(const wchar_t *exe_path, wchar_t *dir, size_t dir_cap, wchar_t *base, size_t base_cap) {{
    wchar_t tmp[MAX_PATH];
    wcscpy_s(tmp, MAX_PATH, exe_path);
    wchar_t *slash = wcsrchr(tmp, L'\\');
    if (slash) {{
        wcscpy_s(base, base_cap, slash + 1);
        *slash = L'\0';
        wcscpy_s(dir, dir_cap, tmp);
    }} else {{
        dir[0] = L'\0';
        wcscpy_s(base, base_cap, tmp);
    }}
    /* 去除 .exe 后缀 */
    wchar_t *dot = wcsrchr(base, L'.');
    if (dot && wcscmp(dot, L".exe") == 0) *dot = L'\0';
}}

static int read_entry(const wchar_t *exe_path, wchar_t *entry_out, size_t cap) {{
    wchar_t dir[MAX_PATH], base[MAX_PATH], path[MAX_PATH];
    split_exe(exe_path, dir, MAX_PATH, base, MAX_PATH);

    /* 多入口模式：<dir>\<base>.entry */
    _snwprintf(path, MAX_PATH, L"%s\\%s.entry", dir, base);
    FILE *f = _wfopen(path, L"rb");
    if (!f) {{
        /* 单入口模式回退：<dir>\.entry */
        _snwprintf(path, MAX_PATH, L"%s\\.entry", dir);
        f = _wfopen(path, L"rb");
        if (!f) {{
            fwprintf(stderr, L"无法读取入口文件: %s\\%s.entry 或 %s\\.entry\n", dir, base, dir);
            return 1;
        }}
    }}
    char buf[MAX_ENTRY];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) {{
        buf[--n] = '\0';
    }}
    if (n == 0 || n >= cap) {{
        fwprintf(stderr, L"入口路径无效\n");
        return 1;
    }}
    for (size_t i = 0; i <= n; i++) {{
        entry_out[i] = (wchar_t)(unsigned char)buf[i];
    }}
    return 0;
}}

int wmain(int argc, wchar_t **argv) {{
    wchar_t exe_path[MAX_PATH], dir[MAX_PATH];
    GetModuleFileNameW(NULL, exe_path, MAX_PATH);
    wcscpy_s(dir, MAX_PATH, exe_path);
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = L'\0';

    wchar_t dll[MAX_PATH], entry[MAX_ENTRY], entry_full[MAX_PATH + MAX_ENTRY];
    _snwprintf(dll, MAX_PATH, L"%s\\%s", dir, PYTHON_DLL);

    if (read_entry(exe_path, entry, MAX_ENTRY) != 0) {{
        return 1;
    }}
    _snwprintf(entry_full, sizeof(entry_full)/sizeof(entry_full[0]), L"%s\\%s", dir, entry);

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

    wchar_t **new_argv = (wchar_t **)malloc(sizeof(wchar_t *) * (argc + 2));
    if (!new_argv) {{
        return 1;
    }}
    new_argv[0] = argv[0];
    new_argv[1] = entry_full;
    for (int i = 1; i < argc; i++) {{
        new_argv[1 + i] = argv[i];
    }}
    new_argv[argc + 1] = NULL;
    return py_main(argc + 1, new_argv);
}}
"""

_LOADER_C_LINUX = r"""/* fspack 生成的 C loader —— 加载 python-build-standalone 并运行用户入口脚本
   入口脚本路径从 <exe_basename>.entry 文件读取，回退 .entry（单入口兼容） */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <linux/limits.h>

#define LIBPYTHON "{libpython}"
#define PYTHONHOME "runtime/python"

typedef int (*Py_BytesMain_t)(int argc, char **argv);

static void split_exe(const char *exe_path, char *dir, size_t dir_cap, char *base, size_t base_cap) {{
    char tmp[PATH_MAX];
    strncpy(tmp, exe_path, sizeof(tmp) - 1);
    tmp[sizeof(tmp) - 1] = '\0';
    char *slash = strrchr(tmp, '/');
    if (slash) {{
        strncpy(base, slash + 1, base_cap - 1);
        base[base_cap - 1] = '\0';
        *slash = '\0';
        strncpy(dir, tmp, dir_cap - 1);
        dir[dir_cap - 1] = '\0';
    }} else {{
        dir[0] = '\0';
        strncpy(base, tmp, base_cap - 1);
        base[base_cap - 1] = '\0';
    }}
}}

static int read_entry(const char *exe_path, char *entry_out, size_t cap) {{
    char dir[PATH_MAX], base[PATH_MAX], path[PATH_MAX];
    split_exe(exe_path, dir, sizeof(dir), base, sizeof(base));

    /* 多入口模式：<dir>/<base>.entry */
    snprintf(path, sizeof(path), "%s/%s.entry", dir, base);
    FILE *f = fopen(path, "r");
    if (!f) {{
        /* 单入口模式回退：<dir>/.entry */
        snprintf(path, sizeof(path), "%s/.entry", dir);
        f = fopen(path, "r");
        if (!f) {{
            fprintf(stderr, "无法读取入口文件: %s/%s.entry 或 %s/.entry\n", dir, base, dir);
            return 1;
        }}
    }}
    if (!fgets(entry_out, (int)cap, f)) {{
        fclose(f);
        fprintf(stderr, "入口文件为空: %s\n", path);
        return 1;
    }}
    fclose(f);
    size_t n = strlen(entry_out);
    while (n > 0 && (entry_out[n-1] == '\n' || entry_out[n-1] == '\r')) {{
        entry_out[--n] = '\0';
    }}
    if (n == 0) {{
        fprintf(stderr, "入口路径无效\n");
        return 1;
    }}
    return 0;
}}

int main(int argc, char **argv) {{
    char exe_path[PATH_MAX], dir[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (n < 0) {{
        fprintf(stderr, "无法读取 /proc/self/exe\n");
        return 1;
    }}
    exe_path[n] = '\0';
    strncpy(dir, exe_path, sizeof(dir) - 1);
    dir[sizeof(dir) - 1] = '\0';
    char *slash = strrchr(dir, '/');
    if (slash) *slash = '\0';

    char lib[PATH_MAX], entry[PATH_MAX], home[PATH_MAX], entry_full[PATH_MAX * 2];
    snprintf(lib, sizeof(lib), "%s/%s", dir, LIBPYTHON);
    snprintf(home, sizeof(home), "%s/%s", dir, PYTHONHOME);

    if (read_entry(exe_path, entry, sizeof(entry)) != 0) {{
        return 1;
    }}
    snprintf(entry_full, sizeof(entry_full), "%s/%s", dir, entry);

    setenv("PYTHONHOME", home, 1);

    void *h = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    if (!h) {{
        fprintf(stderr, "加载 libpython 失败: %s\n%s\n", lib, dlerror());
        return 1;
    }}
    Py_BytesMain_t py_main = (Py_BytesMain_t)dlsym(h, "Py_BytesMain");
    if (!py_main) {{
        fprintf(stderr, "未找到 Py_BytesMain 符号\n");
        return 1;
    }}

    char **new_argv = (char **)malloc(sizeof(char *) * (argc + 2));
    if (!new_argv) return 1;
    new_argv[0] = argv[0];
    new_argv[1] = entry_full;
    for (int i = 1; i < argc; i++) new_argv[1 + i] = argv[i];
    new_argv[argc + 1] = NULL;
    return py_main(argc + 1, new_argv);
}}
"""


def generate_loader_source(
    py_xy: str,
    platform: Platform = Platform.WINDOWS,
) -> str:
    """生成 C loader 源码。

    py_xy: 形如 python311 的版本前缀。
    platform: 目标平台，决定加载 DLL（Windows）或 .so（Linux）。

    入口脚本路径在运行时从 ``<exe_dir>/<exe_basename>.entry`` 读取（多入口），
    回退 ``<exe_dir>/.entry``（单入口）；构建时由 build 写入对应入口文件。
    loader 源码仅依赖 ``py_xy`` 与平台，可按 ``(py_xy, app_type, platform)`` 缓存复用。
    """
    if platform is Platform.LINUX:
        dotted = f"{py_xy[6]}.{py_xy[7:]}"
        libpython = f"runtime/python/lib/libpython{dotted}.so"
        return _LOADER_C_LINUX.format(libpython=libpython)
    python_dll = f"runtime\\\\{py_xy}.dll"
    return _LOADER_C_WINDOWS.format(python_dll=python_dll)


def mingw_available() -> bool:
    """检测 mingw 交叉编译器是否可用。."""
    return shutil.which(MINGW_GCC) is not None


def gcc_available() -> bool:
    """检测 gcc 编译器是否可用。."""
    return shutil.which(LINUX_GCC) is not None


def compile_loader(  # noqa: PLR0913
    source: str,
    out_exe: Path,
    app_type: AppType,
    work_dir: Path,
    platform: Platform = Platform.WINDOWS,
    *,
    cache_dir: Path | None = None,
    stage: StageRecorder | None = None,
) -> Path:
    """编译 loader 源码为可执行文件，返回路径。

    Windows 用 mingw 交叉编译（GUI 加 -mwindows），Linux 用 gcc（链接 libdl）。

    缓存命中时直接复制到 ``out_exe`` 并调 ``stage.hit_cache()``；
    未命中时编译并 best-effort 回写缓存供后续复用。缓存键为
    ``sha256(source + app_type + platform)`` 前 16 字符，保证同配置命中、
    改配置失效。``cache_dir`` 默认 ``~/.fspack/cache/loaders/``。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    out_exe.parent.mkdir(parents=True, exist_ok=True)

    cache = cache_dir or loader_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    key = _loader_cache_key(source, app_type, platform)
    suffix = ".exe" if platform is Platform.WINDOWS else ""
    cached_exe = cache / f"{key}{suffix}"

    if cached_exe.is_file():
        _logger.info("loader 缓存命中: %s", cached_exe.name)
        shutil.copy2(cached_exe, out_exe)
        if stage is not None:
            stage.hit_cache()
            stage.set_detail("缓存命中")
        return out_exe

    c_file = work_dir / "loader.c"
    c_file.write_text(source, encoding="utf-8")

    if platform is Platform.LINUX:
        cmd: list[str] = [LINUX_GCC, "-O2", "-o", str(out_exe), str(c_file), "-ldl"]
        compiler = LINUX_GCC
        install_hint = "gcc"
    else:
        cmd = [MINGW_GCC, "-O2", "-municode", "-o", str(out_exe), str(c_file)]
        if app_type is AppType.GUI:
            cmd.insert(1, "-mwindows")
        compiler = MINGW_GCC
        install_hint = "mingw-w64"
    _logger.info("编译 loader: %s", " ".join(cmd))
    try:
        with spinner(f"编译 loader ({compiler})"):
            subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise LoaderError(f"未找到编译器 {compiler}，请安装 {install_hint}") from e
    except subprocess.CalledProcessError as e:
        raise LoaderError(f"loader 编译失败:\n{e.stderr}") from e
    try:
        shutil.copy2(out_exe, cached_exe)
    except OSError as e:
        _logger.warning("loader 缓存回写失败: %s", e)
    if stage is not None:
        stage.set_detail(compiler)
    return out_exe
