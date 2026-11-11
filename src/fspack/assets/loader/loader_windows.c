/* fspack 生成的 C loader —— 加载 embed python 并运行用户入口脚本
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

static void write_log(const wchar_t *dir, const wchar_t *msg) {
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
    if (WideCharToMultiByte(CP_UTF8, 0, msg, -1, msg_a, sizeof(msg_a), NULL, NULL) > 0) {
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(f, "[%04d-%02d-%02d %02d:%02d:%02d] %s\n",
                st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, msg_a);
    }
    fclose(f);
}

static void stderr_write(const wchar_t *msg) {
    /* stderr 输出适配：控制台直连时 fwprintf 走宽字符 API 正确显示中文；
       重定向（文件/管道，如 CI 收集日志）时宽字符流在默认 locale 下中文
       丢失（首字符后截断），转 UTF-8 字节写入保证完整保留 */
    if (_isatty(_fileno(stderr))) {
        fwprintf(stderr, L"%s\n", msg);
    } else {
        char msg_a[MAX_ENTRY * 4];
        if (WideCharToMultiByte(CP_UTF8, 0, msg, -1, msg_a, sizeof(msg_a), NULL, NULL) > 0) {
            fprintf(stderr, "%s\n", msg_a);
        }
    }
}

static void report_error(const wchar_t *dir, const wchar_t *msg) {
    /* 三通道输出：stderr（有控制台）→ MessageBox（GUI 无控制台弹窗）→
       日志文件（持久兜底）。文案统一格式：
       [fspack loader] <阶段>: <原因>\n建议: <修复动作> */
    /* FSPACK:SPLASH_CLOSE */
    stderr_write(msg);
    if (GetConsoleWindow() == NULL) {
        MessageBoxW(NULL, msg, L"启动失败", MB_ICONERROR | MB_OK);
    }
    write_log(dir, msg);
}

static const wchar_t *describe_load_error(DWORD err) {
    /* GetLastError 翻译为人话，指明修复方向（杀软隔离/位数/权限是高频根因） */
    switch (err) {
    case 2:   return L"文件不存在，发布包不完整";
    case 5:   return L"访问被拒绝，杀毒软件拦截或权限不足";
    case 126: return L"找不到依赖 DLL，runtime 目录损坏或被杀毒软件隔离";
    case 193: return L"DLL 位数不匹配（32/64 位混装）";
    default:  return L"未知错误";
    }
}

static int verbose_enabled(void) {
    /* FSPACK_LOADER_VERBOSE=1 时输出各阶段耗时到 stderr，诊断启动性能；
       默认关闭，正常启动零开销（仅一次 getenv） */
    wchar_t v[8];
    return GetEnvironmentVariableW(L"FSPACK_LOADER_VERBOSE", v, 8) == 1 && v[0] == L'1';
}

static int timing_enabled(void) {
    /* FSPACK_TIMING=1（fsp r --profile 注入）时向子 Python 传递 QPC 锚点，
       供 wrapper 计算 Py_Main C 层初始化缝隙；默认关闭零开销 */
    wchar_t v[8];
    return GetEnvironmentVariableW(L"FSPACK_TIMING", v, 8) == 1 && v[0] == L'1';
}

static double now_ms(void) {
    /* QueryPerformanceCounter 高精度计时（~100ns 精度）。GetTickCount64
       精度仅 15.6ms，read_entry/LoadLibrary 等快速阶段会被量化成 0ms，
       使启动剖析的 loader 段失真（真实耗时被并入"未细分"）。频率查询
       结果恒定，static 缓存仅执行一次。 */
    static LARGE_INTEGER freq = {0};
    LARGE_INTEGER counter;
    if (freq.QuadPart == 0) {
        QueryPerformanceFrequency(&freq);
    }
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart * 1000.0 / (double)freq.QuadPart;
}

static void set_dont_write_bytecode(void) {
    /* 发行目录常被杀软实时监控或挂载为只读，运行时 pyc 回写既慢又可能失败。
       禁用后每次 import 省去 __pycache__ 写尝试；用户已显式设置时不覆盖。
       GetEnvironmentVariableW 返回 0 表示变量不存在（含已删除场景）。 */
    wchar_t v[2];
    if (GetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", v, 2) == 0) {
        SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", L"1");
    }
}

static void split_exe(const wchar_t *exe_path, wchar_t *dir, size_t dir_cap, wchar_t *base, size_t base_cap) {
    wchar_t tmp[MAX_PATH];
    wcscpy_s(tmp, MAX_PATH, exe_path);
    wchar_t *slash = wcsrchr(tmp, L'\\');
    if (slash) {
        wcscpy_s(base, base_cap, slash + 1);
        *slash = L'\0';
        wcscpy_s(dir, dir_cap, tmp);
    } else {
        dir[0] = L'\0';
        wcscpy_s(base, base_cap, tmp);
    }
    /* 去除 .exe 后缀 */
    wchar_t *dot = wcsrchr(base, L'.');
    if (dot && wcscmp(dot, L".exe") == 0) *dot = L'\0';
}

static int read_entry(const wchar_t *exe_path, wchar_t *entry_out, size_t cap) {
    wchar_t dir[MAX_PATH], base[MAX_PATH], path[MAX_PATH], msg[MAX_PATH * 3];
    split_exe(exe_path, dir, MAX_PATH, base, MAX_PATH);

    /* 多入口模式：<dir>\<base>.entry */
    _snwprintf(path, MAX_PATH, L"%s\\%s.entry", dir, base);
    FILE *f = _wfopen(path, L"rb");
    if (!f) {
        /* 单入口模式回退：<dir>\.entry */
        _snwprintf(path, MAX_PATH, L"%s\\.entry", dir);
        f = _wfopen(path, L"rb");
        if (!f) {
            _snwprintf(msg, MAX_PATH * 3,
                       L"[fspack loader] 读取入口失败: 找不到 %s\\%s.entry 或 %s\\.entry\n"
                       L"建议: 发布包不完整，请重新安装或联系发布者", dir, base, dir);
            report_error(dir, msg);
            return ERR_ENTRY;
        }
    }
    char buf[MAX_ENTRY];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = '\0';
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) {
        buf[--n] = '\0';
    }
    if (n == 0) {
        report_error(dir, L"[fspack loader] 读取入口失败: 入口文件内容为空\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }
    /* UTF-8 → UTF-16 转换：.entry 文件以 UTF-8 编码写入（构建侧
       atomic_write_text 默认 utf-8），中文入口名若逐字节强转
       (wchar_t)(unsigned char)buf[i] 会产生 mojibake 导致找不到入口文件，
       必须经 MultiByteToWideChar 按代码页转换；返回 0 表示失败
       （含 entry_out 缓冲区不足，覆盖原 n >= cap 字节级检查） */
    if (MultiByteToWideChar(CP_UTF8, 0, buf, -1, entry_out, (int)cap) == 0) {
        report_error(dir, L"[fspack loader] 读取入口失败: 入口路径编码转换失败\n建议: 发布包损坏，请重新安装");
        return ERR_ENTRY;
    }
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    wchar_t exe_path[MAX_PATH], dir[MAX_PATH];
    GetModuleFileNameW(NULL, exe_path, MAX_PATH);
    wcscpy_s(dir, MAX_PATH, exe_path);
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = L'\0';

    int verbose = verbose_enabled();
    double t_start = now_ms();

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
    if (read_entry(exe_path, entry, MAX_ENTRY) != 0) {
        return ERR_ENTRY;
    }
    if (verbose) {
        wchar_t tmsg[128];
        _snwprintf(tmsg, 128, L"[fspack loader] read_entry 耗时 %.1fms", now_ms() - t_start);
        stderr_write(tmsg);
    }
    _snwprintf(entry_full, sizeof(entry_full)/sizeof(entry_full[0]), L"%s\\%s", dir, entry);

    double t_dll = now_ms();
    HMODULE h = LoadLibraryW(dll);
    if (!h) {
        DWORD err = GetLastError();
        _snwprintf(msg, MAX_PATH * 3,
                   L"[fspack loader] 加载 Python 运行时失败: %s\n"
                   L"原因: %s（错误码 %lu）\n"
                   L"建议: 检查 runtime 目录是否完整；若被杀毒软件隔离请恢复并加入白名单",
                   dll, describe_load_error(err), (unsigned long)err);
        report_error(dir, msg);
        return ERR_DLL;
    }
    if (verbose) {
        wchar_t tmsg[160];
        _snwprintf(tmsg, 160, L"[fspack loader] 加载 %s 耗时 %.1fms", PYTHON_DLL, now_ms() - t_dll);
        stderr_write(tmsg);
    }
    Py_Main_t py_main = (Py_Main_t)GetProcAddress(h, "Py_Main");
    if (!py_main) {
        _snwprintf(msg, MAX_PATH * 3,
                   L"[fspack loader] Python 运行时异常: 未找到 Py_Main 符号: %s\n"
                   L"建议: runtime 目录与发布工具版本不匹配，请重新安装", dll);
        report_error(dir, msg);
        return ERR_SYM;
    }

    wchar_t **new_argv = (wchar_t **)malloc(sizeof(wchar_t *) * (argc + 2));
    if (!new_argv) {
        report_error(dir, L"[fspack loader] 内存不足: 分配参数缓冲区失败\n建议: 关闭其他程序后重试");
        return ERR_MEM;
    }
    new_argv[0] = argv[0];
    new_argv[1] = entry_full;
    for (int i = 1; i < argc; i++) {
        new_argv[1 + i] = argv[i];
    }
    new_argv[argc + 1] = NULL;
    if (verbose) {
        wchar_t tmsg[128];
        _snwprintf(tmsg, 128, L"[fspack loader] loader 总耗时 %.1fms（进入 Python）", now_ms() - t_start);
        stderr_write(tmsg);
    }
    /* Py_Main 调用前写入 QPC 绝对毫秒锚点：CPython 的 perf_counter 底层
       同为 QueryPerformanceCounter（同一单调时间线、同零点），wrapper 首语句
       相减即得 Py_Main C 层初始化（runtime/io/codecs 等非 import 部分）
       实测耗时，填补 loader 打点与 wrapper 打点之间的测量盲区 */
    if (timing_enabled()) {
        char qpc_buf[32];
        _snprintf(qpc_buf, 32, "%.6f", now_ms());
        SetEnvironmentVariableA("FSPACK_LOADER_QPC_MS", qpc_buf);
    }
    return py_main(argc + 1, new_argv);
}
