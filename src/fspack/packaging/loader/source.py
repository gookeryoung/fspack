"""C loader 源码模板.

从 :mod:`fspack.packaging.loader` 拆分而来，集中存放 Windows、Linux 与 macOS 的
C loader 源码模板。模板用 ``str.format`` 填充平台特定常量（DLL 名、
libpython 路径），由 :mod:`fspack.packaging.loader.compile` 的
``WindowsLoader.generate_source``/``LinuxLoader.generate_source``/
``MacLoader.generate_source`` 调用。

入口脚本路径在运行时从 ``<exe_dir>/<exe_basename>.entry`` 文件读取（多入口模式），
回退到 ``<exe_dir>/.entry``（单入口模式，向后兼容）。构建时为每个入口写对应
``<name>.entry`` 文件，使 loader 源码仅依赖 ``py_xy`` 与平台，可按
``(py_xy, app_type, platform)`` 缓存跨项目复用。

Windows 模板含三个 ``FSPACK:SPLASH*`` 标记，由 :func:`apply_splash` 注入/
剥离 splash 启动画面代码（``--splash`` 构建选项，默认关闭）。
"""

from __future__ import annotations

__all__ = ["_LOADER_C_LINUX", "_LOADER_C_MACOS", "_LOADER_C_WINDOWS", "_SPLASH_C_WINDOWS", "apply_splash"]


_LOADER_C_WINDOWS = r"""/* fspack 生成的 C loader —— 加载 embed python 并运行用户入口脚本
   入口脚本路径从 <exe_basename>.entry 文件读取，回退 .entry（单入口兼容） */
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <stdlib.h>
#include <io.h>

#define PYTHON_DLL L"{python_dll}"
#define MAX_ENTRY 512

/* loader 退出码规范（用户/CI 可据此精确诊断，勿与 C 运行时错误码段冲突）：
   100=入口读取失败 101=Python 运行时加载失败 102=符号缺失 103=内存不足 */
#define ERR_ENTRY 100
#define ERR_DLL 101
#define ERR_SYM 102
#define ERR_MEM 103
/* FSPACK:SPLASH */

typedef int (*Py_Main_t)(int argc, wchar_t **argv);

static void write_log(const wchar_t *dir, const wchar_t *msg) {{
    /* best-effort 追加写 <dir>\\logs\\loader.log：GUI 子系统无控制台，
       stderr 输出用户不可见，日志文件是唯一持久诊断通道；任何失败静默忽略。
       消息经 WideCharToMultiByte 转 UTF-8 后按字节写入——msvcrt 的 fopen
       不保证支持 ", ccs=UTF-8" 模式串，直接写字节最稳。 */
    wchar_t log_dir[MAX_PATH], log_path[MAX_PATH];
    _snwprintf(log_dir, MAX_PATH, L"%s\\logs", dir);
    CreateDirectoryW(log_dir, NULL); /* 已存在时失败，忽略 */
    _snwprintf(log_path, MAX_PATH, L"%s\\loader.log", log_dir);
    char path_a[MAX_PATH * 3];
    if (WideCharToMultiByte(CP_UTF8, 0, log_path, -1, path_a, sizeof(path_a), NULL, NULL) == 0) return;
    FILE *f = fopen(path_a, "ab");
    if (!f) return;
    char msg_a[MAX_ENTRY * 4];
    if (WideCharToMultiByte(CP_UTF8, 0, msg, -1, msg_a, sizeof(msg_a), NULL, NULL) > 0) {{
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(f, "[%04d-%02d-%02d %02d:%02d:%02d] %s\n",
                st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, msg_a);
    }}
    fclose(f);
}}

static void stderr_write(const wchar_t *msg) {{
    /* stderr 输出适配：控制台直连时 fwprintf 走宽字符 API 正确显示中文；
       重定向（文件/管道，如 CI 收集日志）时宽字符流在默认 locale 下中文
       丢失（首字符后截断），转 UTF-8 字节写入保证完整保留 */
    if (_isatty(_fileno(stderr))) {{
        fwprintf(stderr, L"%s\n", msg);
    }} else {{
        char msg_a[MAX_ENTRY * 4];
        if (WideCharToMultiByte(CP_UTF8, 0, msg, -1, msg_a, sizeof(msg_a), NULL, NULL) > 0) {{
            fprintf(stderr, "%s\n", msg_a);
        }}
    }}
}}

static void report_error(const wchar_t *dir, const wchar_t *msg) {{
    /* 三通道输出：stderr（有控制台）→ MessageBox（GUI 无控制台弹窗）→
       日志文件（持久兜底）。文案统一格式：
       [fspack loader] <阶段>: <原因>\n建议: <修复动作> */
    /* FSPACK:SPLASH_CLOSE */
    stderr_write(msg);
    if (GetConsoleWindow() == NULL) {{
        MessageBoxW(NULL, msg, L"启动失败", MB_ICONERROR | MB_OK);
    }}
    write_log(dir, msg);
}}

static const wchar_t *describe_load_error(DWORD err) {{
    /* GetLastError 翻译为人话，指明修复方向（杀软隔离/位数/权限是高频根因） */
    switch (err) {{
    case 2:   return L"文件不存在，发布包不完整";
    case 5:   return L"访问被拒绝，杀毒软件拦截或权限不足";
    case 126: return L"找不到依赖 DLL，runtime 目录损坏或被杀毒软件隔离";
    case 193: return L"DLL 位数不匹配（32/64 位混装）";
    default:  return L"未知错误";
    }}
}}

static int verbose_enabled(void) {{
    /* FSPACK_LOADER_VERBOSE=1 时输出各阶段耗时到 stderr，诊断启动性能；
       默认关闭，正常启动零开销（仅一次 getenv） */
    wchar_t v[8];
    return GetEnvironmentVariableW(L"FSPACK_LOADER_VERBOSE", v, 8) == 1 && v[0] == L'1';
}}

static void set_dont_write_bytecode(void) {{
    /* 发行目录常被杀软实时监控或挂载为只读，运行时 pyc 回写既慢又可能失败。
       禁用后每次 import 省去 __pycache__ 写尝试；用户已显式设置时不覆盖。
       GetEnvironmentVariableW 返回 0 表示变量不存在（含已删除场景）。 */
    wchar_t v[2];
    if (GetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", v, 2) == 0) {{
        SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", L"1");
    }}
}}

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
    wchar_t dir[MAX_PATH], base[MAX_PATH], path[MAX_PATH], msg[MAX_PATH * 3];
    split_exe(exe_path, dir, MAX_PATH, base, MAX_PATH);

    /* 多入口模式：<dir>\<base>.entry */
    _snwprintf(path, MAX_PATH, L"%s\\%s.entry", dir, base);
    FILE *f = _wfopen(path, L"rb");
    if (!f) {{
        /* 单入口模式回退：<dir>\.entry */
        _snwprintf(path, MAX_PATH, L"%s\\.entry", dir);
        f = _wfopen(path, L"rb");
        if (!f) {{
            _snwprintf(msg, MAX_PATH * 3,
                       L"[fspack loader] 读取入口失败: 找不到 %s\\%s.entry 或 %s\\.entry\n"
                       L"建议: 发布包不完整，请重新安装或联系发布者", dir, base, dir);
            report_error(dir, msg);
            return ERR_ENTRY;
        }}
    }}
    char buf[MAX_ENTRY];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) {{
        buf[--n] = '\0';
    }}
    if (n == 0) {{
        report_error(dir, L"[fspack loader] 读取入口失败: 入口文件内容为空\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }}
    /* UTF-8 → UTF-16 转换：.entry 文件以 UTF-8 编码写入（构建侧
       atomic_write_text 默认 utf-8），中文入口名若逐字节强转
       (wchar_t)(unsigned char)buf[i] 会产生 mojibake 导致找不到入口文件，
       必须经 MultiByteToWideChar 按代码页转换；返回 0 表示失败
       （含 entry_out 缓冲区不足，覆盖原 n >= cap 字节级检查） */
    if (MultiByteToWideChar(CP_UTF8, 0, buf, -1, entry_out, (int)cap) == 0) {{
        report_error(dir, L"[fspack loader] 读取入口失败: 入口路径编码转换失败\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }}
    return 0;
}}

int wmain(int argc, wchar_t **argv) {{
    wchar_t exe_path[MAX_PATH], dir[MAX_PATH];
    GetModuleFileNameW(NULL, exe_path, MAX_PATH);
    wcscpy_s(dir, MAX_PATH, exe_path);
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = L'\0';

    int verbose = verbose_enabled();
    ULONGLONG t_start = GetTickCount64();

    wchar_t dll[MAX_PATH], entry[MAX_ENTRY], entry_full[MAX_PATH + MAX_ENTRY];
    wchar_t runtime_dir[MAX_PATH], msg[MAX_PATH * 3];
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

    set_dont_write_bytecode();

    /* FSPACK:SPLASH_SHOW */
    if (read_entry(exe_path, entry, MAX_ENTRY) != 0) {{
        return ERR_ENTRY;
    }}
    if (verbose) {{
        wchar_t tmsg[128];
        _snwprintf(tmsg, 128, L"[fspack loader] read_entry 耗时 %lums", (unsigned long)(GetTickCount64() - t_start));
        stderr_write(tmsg);
    }}
    _snwprintf(entry_full, sizeof(entry_full)/sizeof(entry_full[0]), L"%s\\%s", dir, entry);

    ULONGLONG t_dll = GetTickCount64();
    HMODULE h = LoadLibraryW(dll);
    if (!h) {{
        DWORD err = GetLastError();
        _snwprintf(msg, MAX_PATH * 3,
                   L"[fspack loader] 加载 Python 运行时失败: %s\n"
                   L"原因: %s（错误码 %lu）\n"
                   L"建议: 检查 runtime 目录是否完整；若被杀毒软件隔离请恢复并加入白名单",
                   dll, describe_load_error(err), (unsigned long)err);
        report_error(dir, msg);
        return ERR_DLL;
    }}
    if (verbose) {{
        wchar_t tmsg[160];
        _snwprintf(tmsg, 160, L"[fspack loader] 加载 %s 耗时 %lums", PYTHON_DLL, (unsigned long)(GetTickCount64() - t_dll));
        stderr_write(tmsg);
    }}
    Py_Main_t py_main = (Py_Main_t)GetProcAddress(h, "Py_Main");
    if (!py_main) {{
        _snwprintf(msg, MAX_PATH * 3,
                   L"[fspack loader] Python 运行时异常: 未找到 Py_Main 符号: %s\n"
                   L"建议: runtime 目录与发布工具版本不匹配，请重新安装", dll);
        report_error(dir, msg);
        return ERR_SYM;
    }}

    wchar_t **new_argv = (wchar_t **)malloc(sizeof(wchar_t *) * (argc + 2));
    if (!new_argv) {{
        report_error(dir, L"[fspack loader] 内存不足: 分配参数缓冲区失败\n建议: 关闭其他程序后重试");
        return ERR_MEM;
    }}
    new_argv[0] = argv[0];
    new_argv[1] = entry_full;
    for (int i = 1; i < argc; i++) {{
        new_argv[1 + i] = argv[i];
    }}
    new_argv[argc + 1] = NULL;
    if (verbose) {{
        wchar_t tmsg[128];
        _snwprintf(tmsg, 128, L"[fspack loader] loader 总耗时 %lums（进入 Python）", (unsigned long)(GetTickCount64() - t_start));
        stderr_write(tmsg);
    }}
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
#include <time.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/syslimits.h>
#include <mach-o/dyld.h>

#define LIBPYTHON "{libpython}"
#define PYTHONHOME "runtime/python"

/* loader 退出码规范（与 Windows/Linux 版一致，用户/CI 可据此精确诊断）：
   100=入口读取失败 101=Python 运行时加载失败 102=符号缺失 103=内存不足 */
#define ERR_ENTRY 100
#define ERR_DLL 101
#define ERR_SYM 102
#define ERR_MEM 103

typedef int (*Py_BytesMain_t)(int argc, char **argv);

static void write_log(const char *dir, const char *msg) {{
    /* best-effort 追加写 <dir>/logs/loader.log：无终端（Finder 启动的 GUI
       会话）场景 stderr 不可见，日志文件是持久诊断通道；任何失败静默忽略 */
    char log_dir[PATH_MAX], log_path[PATH_MAX];
    snprintf(log_dir, sizeof(log_dir), "%s/logs", dir);
    mkdir(log_dir, 0755); /* 已存在时 EEXIST，忽略 */
    snprintf(log_path, sizeof(log_path), "%s/loader.log", log_dir);
    FILE *f = fopen(log_path, "a");
    if (!f) return;
    time_t t = time(NULL);
    struct tm tm_buf;
    if (localtime_r(&t, &tm_buf)) {{
        char ts[32];
        strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm_buf);
        fprintf(f, "[%s] %s\n", ts, msg);
    }}
    fclose(f);
}}

static void report_error(const char *dir, const char *msg) {{
    /* 双通道输出：stderr（有终端时可见）+ 日志文件（持久兜底）。
       文案统一格式：[fspack loader] <阶段>: <原因>\n建议: <修复动作> */
    fprintf(stderr, "%s\n", msg);
    write_log(dir, msg);
}}

static int verbose_enabled(void) {{
    /* FSPACK_LOADER_VERBOSE=1 时输出各阶段耗时到 stderr，诊断启动性能；
       默认关闭，正常启动零开销（仅一次 getenv） */
    const char *v = getenv("FSPACK_LOADER_VERBOSE");
    return v != NULL && v[0] == '1' && v[1] == '\0';
}}

static double now_ms(void) {{
    /* 单调时钟毫秒：打点用，不受系统时间调整影响 */
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1000000.0;
}}

static void set_dont_write_bytecode(void) {{
    /* 发行目录常被杀软实时监控或挂载为只读，运行时 pyc 回写既慢又可能失败。
       禁用后每次 import 省去 __pycache__ 写尝试；用户已显式设置时不覆盖。 */
    if (getenv("PYTHONDONTWRITEBYTECODE") == NULL) {{
        setenv("PYTHONDONTWRITEBYTECODE", "1", 0);
    }}
}}

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
    char dir[PATH_MAX], base[PATH_MAX], path[PATH_MAX], msg[PATH_MAX * 2];
    split_exe(exe_path, dir, sizeof(dir), base, sizeof(base));

    /* 多入口模式：<dir>/<base>.entry */
    snprintf(path, sizeof(path), "%s/%s.entry", dir, base);
    FILE *f = fopen(path, "r");
    if (!f) {{
        /* 单入口模式回退：<dir>/.entry */
        snprintf(path, sizeof(path), "%s/.entry", dir);
        f = fopen(path, "r");
        if (!f) {{
            snprintf(msg, sizeof(msg),
                     "[fspack loader] 读取入口失败: 找不到 %s/%s.entry 或 %s/.entry\n"
                     "建议: 发布包不完整，请重新安装或联系发布者", dir, base, dir);
            report_error(dir, msg);
            return ERR_ENTRY;
        }}
    }}
    if (!fgets(entry_out, (int)cap, f)) {{
        fclose(f);
        report_error(dir, "[fspack loader] 读取入口失败: 入口文件内容为空\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }}
    fclose(f);
    size_t n = strlen(entry_out);
    while (n > 0 && (entry_out[n-1] == '\n' || entry_out[n-1] == '\r')) {{
        entry_out[--n] = '\0';
    }}
    if (n == 0) {{
        report_error(dir, "[fspack loader] 读取入口失败: 入口路径无效\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
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
        report_error(".", "[fspack loader] 定位可执行文件失败: 无法获取可执行文件路径\n建议: 请从正常路径启动");
        return ERR_ENTRY;
    }}
    strncpy(dir, exe_path, sizeof(dir) - 1);
    dir[sizeof(dir) - 1] = '\0';
    char *slash = strrchr(dir, '/');
    if (slash) *slash = '\0';

    int verbose = verbose_enabled();
    double t_start = now_ms();

    char lib[PATH_MAX], entry[PATH_MAX], home[PATH_MAX], entry_full[PATH_MAX * 2], msg[PATH_MAX * 2];
    snprintf(lib, sizeof(lib), "%s/%s", dir, LIBPYTHON);
    snprintf(home, sizeof(home), "%s/%s", dir, PYTHONHOME);

    set_dont_write_bytecode();

    if (read_entry(exe_path, entry, sizeof(entry)) != 0) {{
        return ERR_ENTRY;
    }}
    if (verbose) {{
        fprintf(stderr, "[fspack loader] read_entry 耗时 %.1fms\n", now_ms() - t_start);
    }}
    snprintf(entry_full, sizeof(entry_full), "%s/%s", dir, entry);

    setenv("PYTHONHOME", home, 1);

    /* RTLD_LAZY：libpython 符号表庞大，惰性绑定让启动期跳过全量重定位
       （CPython 官方可执行文件同款加载方式），首次调用各符号时才解析 */
    double t_dll = now_ms();
    void *h = dlopen(lib, RTLD_LAZY | RTLD_GLOBAL);
    if (!h) {{
        snprintf(msg, sizeof(msg),
                 "[fspack loader] 加载 Python 运行时失败: %s\n"
                 "原因: %s\n"
                 "建议: 检查 runtime 目录是否完整，或联系发布者", lib, dlerror());
        report_error(dir, msg);
        return ERR_DLL;
    }}
    if (verbose) {{
        fprintf(stderr, "[fspack loader] dlopen %s 耗时 %.1fms\n", LIBPYTHON, now_ms() - t_dll);
    }}
    Py_BytesMain_t py_main = (Py_BytesMain_t)dlsym(h, "Py_BytesMain");
    if (!py_main) {{
        report_error(dir, "[fspack loader] Python 运行时异常: 未找到 Py_BytesMain 符号\n建议: runtime 目录与发布工具版本不匹配，请重新安装");
        return ERR_SYM;
    }}

    char **new_argv = (char **)malloc(sizeof(char *) * (argc + 2));
    if (!new_argv) {{
        report_error(dir, "[fspack loader] 内存不足: 分配参数缓冲区失败\n建议: 关闭其他程序后重试");
        return ERR_MEM;
    }}
    new_argv[0] = argv[0];
    new_argv[1] = entry_full;
    for (int i = 1; i < argc; i++) new_argv[1 + i] = argv[i];
    new_argv[argc + 1] = NULL;
    if (verbose) {{
        fprintf(stderr, "[fspack loader] loader 总耗时 %.1fms（进入 Python）\n", now_ms() - t_start);
    }}
    return py_main(argc + 1, new_argv);
}}
"""


_LOADER_C_LINUX = r"""/* fspack 生成的 C loader —— 加载 python-build-standalone 并运行用户入口脚本
   入口脚本路径从 <exe_basename>.entry 文件读取，回退 .entry（单入口兼容）
   注：exe 链接参数含 -Wl,--disable-new-dtags,-rpath,$ORIGIN/runtime/python/lib
   （老式 DT_RPATH），使进程内所有 C 扩展的 NEEDED 解析搜索 runtime/python/lib
   （如 _tkinter.so 依赖的 libtcl9.0.so），详见 LinuxLoader._build_command */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <linux/limits.h>

#define LIBPYTHON "{libpython}"
#define PYTHONHOME "runtime/python"

/* loader 退出码规范（与 Windows 版一致，用户/CI 可据此精确诊断）：
   100=入口读取失败 101=Python 运行时加载失败 102=符号缺失 103=内存不足 */
#define ERR_ENTRY 100
#define ERR_DLL 101
#define ERR_SYM 102
#define ERR_MEM 103

typedef int (*Py_BytesMain_t)(int argc, char **argv);

static void write_log(const char *dir, const char *msg) {{
    /* best-effort 追加写 <dir>/logs/loader.log：无终端（GUI 会话）场景
       stderr 不可见，日志文件是持久诊断通道；任何失败静默忽略 */
    char log_dir[PATH_MAX], log_path[PATH_MAX];
    snprintf(log_dir, sizeof(log_dir), "%s/logs", dir);
    mkdir(log_dir, 0755); /* 已存在时 EEXIST，忽略 */
    snprintf(log_path, sizeof(log_path), "%s/loader.log", log_dir);
    FILE *f = fopen(log_path, "a");
    if (!f) return;
    time_t t = time(NULL);
    struct tm tm_buf;
    if (localtime_r(&t, &tm_buf)) {{
        char ts[32];
        strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm_buf);
        fprintf(f, "[%s] %s\n", ts, msg);
    }}
    fclose(f);
}}

static void report_error(const char *dir, const char *msg) {{
    /* 双通道输出：stderr（有终端时可见）+ 日志文件（持久兜底）。
       文案统一格式：[fspack loader] <阶段>: <原因>\n建议: <修复动作> */
    fprintf(stderr, "%s\n", msg);
    write_log(dir, msg);
}}

static int verbose_enabled(void) {{
    /* FSPACK_LOADER_VERBOSE=1 时输出各阶段耗时到 stderr，诊断启动性能；
       默认关闭，正常启动零开销（仅一次 getenv） */
    const char *v = getenv("FSPACK_LOADER_VERBOSE");
    return v != NULL && v[0] == '1' && v[1] == '\0';
}}

static double now_ms(void) {{
    /* 单调时钟毫秒：打点用，不受系统时间调整影响 */
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1000000.0;
}}

static void set_dont_write_bytecode(void) {{
    /* 发行目录常被杀软实时监控或挂载为只读，运行时 pyc 回写既慢又可能失败。
       禁用后每次 import 省去 __pycache__ 写尝试；用户已显式设置时不覆盖。 */
    if (getenv("PYTHONDONTWRITEBYTECODE") == NULL) {{
        setenv("PYTHONDONTWRITEBYTECODE", "1", 0);
    }}
}}

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
    char dir[PATH_MAX], base[PATH_MAX], path[PATH_MAX], msg[PATH_MAX * 2];
    split_exe(exe_path, dir, sizeof(dir), base, sizeof(base));

    /* 多入口模式：<dir>/<base>.entry */
    snprintf(path, sizeof(path), "%s/%s.entry", dir, base);
    FILE *f = fopen(path, "r");
    if (!f) {{
        /* 单入口模式回退：<dir>/.entry */
        snprintf(path, sizeof(path), "%s/.entry", dir);
        f = fopen(path, "r");
        if (!f) {{
            snprintf(msg, sizeof(msg),
                     "[fspack loader] 读取入口失败: 找不到 %s/%s.entry 或 %s/.entry\n"
                     "建议: 发布包不完整，请重新安装或联系发布者", dir, base, dir);
            report_error(dir, msg);
            return ERR_ENTRY;
        }}
    }}
    if (!fgets(entry_out, (int)cap, f)) {{
        fclose(f);
        report_error(dir, "[fspack loader] 读取入口失败: 入口文件内容为空\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }}
    fclose(f);
    size_t n = strlen(entry_out);
    while (n > 0 && (entry_out[n-1] == '\n' || entry_out[n-1] == '\r')) {{
        entry_out[--n] = '\0';
    }}
    if (n == 0) {{
        report_error(dir, "[fspack loader] 读取入口失败: 入口路径无效\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }}
    return 0;
}}

int main(int argc, char **argv) {{
    char exe_path[PATH_MAX], dir[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (n < 0) {{
        report_error(".", "[fspack loader] 定位可执行文件失败: 无法读取 /proc/self/exe\n建议: 请从正常路径启动");
        return ERR_ENTRY;
    }}
    exe_path[n] = '\0';
    strncpy(dir, exe_path, sizeof(dir) - 1);
    dir[sizeof(dir) - 1] = '\0';
    char *slash = strrchr(dir, '/');
    if (slash) *slash = '\0';

    int verbose = verbose_enabled();
    double t_start = now_ms();

    char lib[PATH_MAX], entry[PATH_MAX], home[PATH_MAX], entry_full[PATH_MAX * 2], msg[PATH_MAX * 2];
    snprintf(lib, sizeof(lib), "%s/%s", dir, LIBPYTHON);
    snprintf(home, sizeof(home), "%s/%s", dir, PYTHONHOME);

    set_dont_write_bytecode();

    if (read_entry(exe_path, entry, sizeof(entry)) != 0) {{
        return ERR_ENTRY;
    }}
    if (verbose) {{
        fprintf(stderr, "[fspack loader] read_entry 耗时 %.1fms\n", now_ms() - t_start);
    }}
    snprintf(entry_full, sizeof(entry_full), "%s/%s", dir, entry);

    setenv("PYTHONHOME", home, 1);

    /* RTLD_LAZY：libpython 符号表庞大，惰性绑定让启动期跳过全量重定位
       （CPython 官方可执行文件同款加载方式），首次调用各符号时才解析 */
    double t_dll = now_ms();
    void *h = dlopen(lib, RTLD_LAZY | RTLD_GLOBAL);
    if (!h) {{
        snprintf(msg, sizeof(msg),
                 "[fspack loader] 加载 Python 运行时失败: %s\n"
                 "原因: %s\n"
                 "建议: 检查 runtime 目录是否完整，或联系发布者", lib, dlerror());
        report_error(dir, msg);
        return ERR_DLL;
    }}
    if (verbose) {{
        fprintf(stderr, "[fspack loader] dlopen %s 耗时 %.1fms\n", LIBPYTHON, now_ms() - t_dll);
    }}
    Py_BytesMain_t py_main = (Py_BytesMain_t)dlsym(h, "Py_BytesMain");
    if (!py_main) {{
        report_error(dir, "[fspack loader] Python 运行时异常: 未找到 Py_BytesMain 符号\n建议: runtime 目录与发布工具版本不匹配，请重新安装");
        return ERR_SYM;
    }}

    char **new_argv = (char **)malloc(sizeof(char *) * (argc + 2));
    if (!new_argv) {{
        report_error(dir, "[fspack loader] 内存不足: 分配参数缓冲区失败\n建议: 关闭其他程序后重试");
        return ERR_MEM;
    }}
    new_argv[0] = argv[0];
    new_argv[1] = entry_full;
    for (int i = 1; i < argc; i++) new_argv[1 + i] = argv[i];
    new_argv[argc + 1] = NULL;
    if (verbose) {{
        fprintf(stderr, "[fspack loader] loader 总耗时 %.1fms（进入 Python）\n", now_ms() - t_start);
    }}
    return py_main(argc + 1, new_argv);
}}
"""


# ---- splash 启动画面（Windows，--splash 构建选项注入）----

# 独立的 splash C 代码块：经标记替换（非 str.format）拼接进 Windows loader 源码，
# 块内 C 花括号无需 {{}} 转义。__FSPACK_SPLASH_TITLE__ 为应用名占位符。
#
# 设计要点：
# - 独立线程 + 消息循环：主线程继续加载 python3X.dll 并进入 Py_Main，互不阻塞
# - 三个关闭条件（任一满足即关）：
#   1. 进程内出现首个可见应用窗口（GUI 工具包通用，EnumWindows 每 150ms 轮询，
#      跨 Qt/tkinter/wx/WebView 全覆盖，无需逐工具包适配）
#   2. Python wrapper SetEvent 通知（WEB 应用无自有窗口，server 启动时通知）
#   3. 30s 超时兜底（异常场景防永久停留）
# - 事件手动复位（manual reset）：重复 SetEvent 幂等，splash_close 可安全重入
# - WS_EX_TOOLWINDOW：不占任务栏位置；EnumWindows 轮询时也按此标志排除自身
_SPLASH_C_WINDOWS = r"""
/* ---- splash 启动画面（--splash 构建选项注入，默认构建不含此段）---- */
#define SPLASH_W 480
#define SPLASH_H 280
#define SPLASH_TIMEOUT_MS 30000
#define SPLASH_POLL_MS 150

static const wchar_t *g_splash_title = L"__FSPACK_SPLASH_TITLE__";
static HANDLE g_splash_event = NULL;

struct splash_enum_ctx { DWORD pid; HWND found; };

static BOOL CALLBACK splash_enum_cb(HWND hwnd, LPARAM lp) {
    /* 查找本进程内首个可见非工具窗口（GUI 应用主窗口首帧检测） */
    struct splash_enum_ctx *ctx = (struct splash_enum_ctx *)lp;
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid != ctx->pid) return TRUE;
    wchar_t cls[64];
    if (GetClassNameW(hwnd, cls, 64) == 0) return TRUE;
    if (wcscmp(cls, L"FspackSplash") == 0) return TRUE;
    if (!IsWindowVisible(hwnd)) return TRUE;
    if (GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW) return TRUE;
    ctx->found = hwnd;
    return FALSE;
}

static LRESULT CALLBACK splash_wndproc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);
        RECT rc;
        GetClientRect(hwnd, &rc);
        HBRUSH bg = CreateSolidBrush(RGB(24, 26, 32));
        FillRect(hdc, &rc, bg);
        DeleteObject(bg);
        SetTextColor(hdc, RGB(235, 237, 240));
        SetBkMode(hdc, TRANSPARENT);
        HFONT old_font = (HFONT)SelectObject(hdc, GetStockObject(DEFAULT_GUI_FONT));
        RECT rc_title = rc;
        DrawTextW(hdc, g_splash_title, -1, &rc_title,
                  DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
        RECT rc_hint = rc;
        rc_hint.top = rc.bottom - 52;
        DrawTextW(hdc, L"正在启动，请稍候...", -1, &rc_hint,
                  DT_CENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
        SelectObject(hdc, old_font);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_ERASEBKGND:
        return 1; /* 背景在 WM_PAINT 统一绘制，避免闪烁 */
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static DWORD WINAPI splash_thread(LPVOID param) {
    (void)param;
    HANDLE ev = g_splash_event;
    WNDCLASSW wc;
    ZeroMemory(&wc, sizeof(wc));
    wc.lpfnWndProc = splash_wndproc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_APPSTARTING);
    wc.lpszClassName = L"FspackSplash";
    if (!RegisterClassW(&wc)) return 0;
    int sx = GetSystemMetrics(SM_CXSCREEN), sy = GetSystemMetrics(SM_CYSCREEN);
    HWND hwnd = CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
        wc.lpszClassName, g_splash_title, WS_POPUP | WS_VISIBLE,
        (sx - SPLASH_W) / 2, (sy - SPLASH_H) / 2, SPLASH_W, SPLASH_H,
        NULL, NULL, wc.hInstance, NULL);
    if (!hwnd) return 0;
    UpdateWindow(hwnd);

    ULONGLONG t_start = GetTickCount64();
    for (;;) {
        DWORD wait = MsgWaitForMultipleObjects(1, &ev, FALSE, SPLASH_POLL_MS, QS_ALLINPUT);
        if (wait == WAIT_OBJECT_0) break; /* wrapper 通知关闭（WEB server 启动） */
        if (wait == WAIT_TIMEOUT) {
            /* 轮询进程内首个可见应用窗口（GUI 工具包通用首帧检测） */
            struct splash_enum_ctx ctx;
            ctx.pid = GetCurrentProcessId();
            ctx.found = NULL;
            EnumWindows(splash_enum_cb, (LPARAM)&ctx);
            if (ctx.found) break;
            if (GetTickCount64() - t_start >= SPLASH_TIMEOUT_MS) break; /* 超时兜底 */
            continue;
        }
        MSG msg;
        while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) return 0;
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
    DestroyWindow(hwnd);
    return 0;
}

static void splash_show(void) {
    /* 命名事件 Local\\fspack_splash_<pid>：Python wrapper 经 ctypes
       OpenEventW+SetEvent 通知关闭（WEB 应用无自有窗口场景） */
    wchar_t name[64];
    _snwprintf(name, 64, L"Local\\fspack_splash_%lu", GetCurrentProcessId());
    g_splash_event = CreateEventW(NULL, TRUE, FALSE, name);
    if (!g_splash_event) return;
    HANDLE th = CreateThread(NULL, 0, splash_thread, NULL, 0, NULL);
    if (th) CloseHandle(th);
}

static void splash_close(void) {
    if (g_splash_event) SetEvent(g_splash_event);
}
"""


def _c_escape(text: str) -> str:
    """转义文本为 C 字符串字面量内容（反斜杠与双引号，宽/窄字面量通用）."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def apply_splash(source: str, title: str | None) -> str:
    """向 Windows loader 源码注入或剥离 splash 启动画面代码.

    ``title`` 为 None 时剥离全部 ``FSPACK:SPLASH*`` 标记（默认构建，不含
    splash）；非 None 时注入 :data:`_SPLASH_C_WINDOWS` 代码块（标题经
    :func:`_c_escape` 转义为 C 宽字符串字面量）。

    用标记替换而非 ``str.format`` 拼接：splash 块内 C 花括号无需 ``{{}}``
    转义，且主模板的 ``format`` 先行完成，互不干扰。
    """
    if title is None:
        return (
            source.replace("/* FSPACK:SPLASH */\n", "")
            .replace("    /* FSPACK:SPLASH_CLOSE */\n", "")
            .replace("    /* FSPACK:SPLASH_SHOW */\n", "")
        )
    block = _SPLASH_C_WINDOWS.replace("__FSPACK_SPLASH_TITLE__", _c_escape(title))
    return (
        source.replace("/* FSPACK:SPLASH */", block)
        .replace("    /* FSPACK:SPLASH_CLOSE */", "    splash_close();")
        .replace("    /* FSPACK:SPLASH_SHOW */", "    splash_show();")
    )
