"""C loader 源码模板.

从 :mod:`fspack.packaging.loader` 拆分而来，集中存放 Windows、Linux 与 macOS 的
C loader 源码模板。模板用 ``str.format`` 填充平台特定常量（DLL 名、
libpython 路径），由 :mod:`fspack.packaging.loader_compile` 的
``WindowsLoader.generate_source``/``LinuxLoader.generate_source``/
``MacLoader.generate_source`` 调用。

入口脚本路径在运行时从 ``<exe_dir>/<exe_basename>.entry`` 文件读取（多入口模式），
回退到 ``<exe_dir>/.entry``（单入口模式，向后兼容）。构建时为每个入口写对应
``<name>.entry`` 文件，使 loader 源码仅依赖 ``py_xy`` 与平台，可按
``(py_xy, app_type, platform)`` 缓存跨项目复用。
"""

from __future__ import annotations

__all__ = ["_LOADER_C_LINUX", "_LOADER_C_MACOS", "_LOADER_C_WINDOWS"]


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
    wchar_t runtime_dir[MAX_PATH];
    _snwprintf(dll, MAX_PATH, L"%s\\%s", dir, PYTHON_DLL);
    _snwprintf(runtime_dir, MAX_PATH, L"%s\\runtime", dir);

    /* Win7 兼容：SetDllDirectoryW 把 runtime\ 加入 DLL 搜索路径。
       python3X.dll 及其传递依赖（如 vcruntime140.dll → api-ms-win-core-path-l1-1-0.dll）
       位于 runtime\，但默认搜索路径只看 loader.exe 所在目录与 system32，
       找不到 runtime\ 中的依赖 DLL。SetDllDirectoryW 让 Windows 加载 DLL 及其
       所有层级依赖时都在 runtime\ 中查找。
       注：LOAD_WITH_ALTERED_SEARCH_PATH 仅影响第一级依赖，对传递依赖无效，
       故必须用 SetDllDirectoryW 兜底。 */
    SetDllDirectoryW(runtime_dir);

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


_LOADER_C_MACOS = r"""/* fspack 生成的 C loader —— 加载 python-build-standalone (macOS) 并运行用户入口脚本
   入口脚本路径从 <exe_basename>.entry 文件读取，回退 .entry（单入口兼容）
   与 Linux 版差异：用 _NSGetExecutablePath 取可执行路径（macOS 无 procfs），
   libpython 后缀为 .dylib，PATH_MAX 取自 sys/syslimits.h */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syslimits.h>
#include <mach-o/dyld.h>

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

static int get_exe_path(char *buf, size_t cap) {{
    /* macOS 用 _NSGetExecutablePath 获取当前可执行文件路径，返回绝对路径 */
    uint32_t size = (uint32_t)cap;
    if (_NSGetExecutablePath(buf, &size) != 0) {{
        fprintf(stderr, "无法获取可执行文件路径（buffer 不足，需 %u 字节）\n", size);
        return 1;
    }}
    return 0;
}}

int main(int argc, char **argv) {{
    char exe_path[PATH_MAX], dir[PATH_MAX];
    if (get_exe_path(exe_path, sizeof(exe_path)) != 0) {{
        return 1;
    }}
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
