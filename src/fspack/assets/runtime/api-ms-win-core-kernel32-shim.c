// api-ms-win-core-kernel32-shim.dll — Win7 GetSystemTimePreciseAsFileTime polyfill
//
// 问题：Python 3.13 起官方 python313.dll 及 pydantic-core Rust wheel
//       静态导入 kernel32!GetSystemTimePreciseAsFileTime（Win8+ 引入）。
//       kernel32 是 KnownDLL 无法遮蔽，但 .pyd 扩展名不是 KnownDLL——
//       打包期用 patch.py 把目标函数的导入表项重定向到本 DLL。
//
// 本 DLL 仅导出 GetSystemTimePreciseAsFileTime，内部实现：
//   - 冷启动：QueryPerformanceFrequency 计算 QPC 到 FILETIME 100ns tick 的换算比
//   - 每次调用：QueryPerformanceCounter 取高精度计数器，按换算比加到
//               GetSystemTimeAsFileTime 返回的 FILETIME 上得到亚微秒精度
//
// 依赖：仅 kernel32.dll 的 QPC / QPF / GetSystemTimeAsFileTime —— 全部
//       Win7 原生导出，用 -nostdlib 编译零 CRT 依赖。
//
// 编译（已纳入 shim_build.py 注册表）：
//   x86_64-w64-mingw32-gcc -shared -O2 -nostdlib -e DllMain \
//     -o api-ms-win-core-kernel32-shim.dll \
//     api-ms-win-core-kernel32-shim.c -lkernel32

// Shadow SDK 声明，避免 windows.h 里的 __declspec(dllimport) 与我们自己的
// __declspec(dllexport) 定义冲突（否则 gcc 会报 "redeclared without dllimport"）。
#define GetSystemTimePreciseAsFileTime  GetSystemTimePreciseAsFileTime_sdk_shadow

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#undef GetSystemTimePreciseAsFileTime

// -- QPC→FILETIME 换算缓存 ---------------------------------------------
// FILETIME 单位 100ns；QPF 是每秒 QPC tick 数；ratio = QPF / 10000 即每个
// FILETIME tick 对应的 QPC tick 数（取整数部分，截断误差对 GetSystemTime
// PrecisionAsFileTime 亚微秒精度可接受）。
static ULONGLONG g_qpc_per_100ns = 0;  // 0 表示尚未初始化
static ULONGLONG g_last_qpc = 0;       // 上次调用时的 QPC
static ULONGLONG g_last_filetime = 0;  // 上次调用时的 FILETIME

// -- Win8+ API polyfill -------------------------------------------------

__declspec(dllexport) VOID WINAPI GetSystemTimePreciseAsFileTime(LPFILETIME lpSystemTimeAsFileTime) {
    if (!lpSystemTimeAsFileTime) return;

    LARGE_INTEGER qpc;
    QueryPerformanceCounter(&qpc);

    if (g_qpc_per_100ns == 0) {
        // 第一次调用：计算换算比 + 用 GetSystemTimeAsFileTime 作为 base
        LARGE_INTEGER freq;
        QueryPerformanceFrequency(&freq);
        // freq.QuadPart 是每秒 QPC tick；除以 10000 得到每 100ns 对应 QPC 数
        // 截断到整数——误差 < 1 tick / 10000 ≈ 纳秒级，可忽略
        g_qpc_per_100ns = (ULONGLONG)(freq.QuadPart / 10000);
        if (g_qpc_per_100ns == 0) {
            // 极端情况（QPF 非常低，Win7 不会出现）：退化到 GetSystemTimeAsFileTime
            GetSystemTimeAsFileTime(lpSystemTimeAsFileTime);
            return;
        }

        FILETIME base_ft;
        GetSystemTimeAsFileTime(&base_ft);
        g_last_qpc = (ULONGLONG)qpc.QuadPart;
        g_last_filetime = (ULONGLONG)base_ft.dwLowDateTime
                        | ((ULONGLONG)base_ft.dwHighDateTime << 32);
        *lpSystemTimeAsFileTime = base_ft;
        return;
    }

    ULONGLONG now_qpc = (ULONGLONG)qpc.QuadPart;
    ULONGLONG delta_qpc = now_qpc - g_last_qpc;  // 无符号，依赖 QPC 单调递增

    // delta_qpc → delta_100ns：向下取整；若 QPC 重置（如 CPU 频率变化）或首次调用
    // 差值为负（无符号溢出），跳过插值直接返回 GetSystemTimeAsFileTime
    if (now_qpc < g_last_qpc) {
        FILETIME base_ft;
        GetSystemTimeAsFileTime(&base_ft);
        g_last_qpc = now_qpc;
        g_last_filetime = (ULONGLONG)base_ft.dwLowDateTime
                        | ((ULONGLONG)base_ft.dwHighDateTime << 32);
        *lpSystemTimeAsFileTime = base_ft;
        return;
    }

    ULONGLONG delta_100ns = delta_qpc / g_qpc_per_100ns;
    ULONGLONG result = g_last_filetime + delta_100ns;

    lpSystemTimeAsFileTime->dwLowDateTime  = (DWORD)(result & 0xFFFFFFFF);
    lpSystemTimeAsFileTime->dwHighDateTime = (DWORD)(result >> 32);

    g_last_qpc = now_qpc;
    g_last_filetime = result;
}

// -- DLL entry ----------------------------------------------------------
// -nostdlib 编译，无 CRT startup；DllMain 足够，不需要任何 process attach 初始化。
BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpReserved) {
    (void)hinstDLL;
    (void)fdwReason;
    (void)lpReserved;
    return TRUE;
}
