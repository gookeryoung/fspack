// api-ms-win-core-synch-l1-2-0.dll Win7 polyfill
//
// Problem: pydantic-core Rust code statically imports WaitOnAddress /
//          WakeByAddress* from api-ms-win-core-synch-l1-2-0.dll (a Win8+
//          API set). PE loader on Win7 cannot find that DLL at all.
//
// Solution: Build a stub api-ms-win-core-synch-l1-2-0.dll that exports:
//   - WaitOnAddress / WakeByAddress*  -> CONDITION_VARIABLE polyfill
//   - Sleep / SleepEx                 -> kernel32 forwarding (Python
//     embed runtime resolves Sleep via this DLL too!)
//
// Build (MSVC):  cl /LD api-ms-win-core-synch-l1-2-0.c /Fe:api-ms-win-core-synch-l1-2-0.dll kernel32.lib
// Build (mingw): x86_64-w64-mingw32-gcc -shared -O2 -o api-ms-win-core-synch-l1-2-0.dll api-ms-win-core-synch-l1-2-0.c

// Shadow SDK declarations BEFORE including windows.h, so that our own
// __declspec(dllexport) definitions below do not collide with the SDK's
// __declspec(dllimport) prototypes. Also hides Sleep/SleepEx — mingw headers
// declare SleepEx as returning DWORD, which disagrees with the Win10 SDK's
// BOOL prototype; we neutralize both by shadowing the names entirely.
#define WaitOnAddress          WaitOnAddress_sdk_shadow
#define WakeByAddressSingle    WakeByAddressSingle_sdk_shadow
#define WakeByAddressAll       WakeByAddressAll_sdk_shadow
#define Sleep                  Sleep_sdk_shadow
#define SleepEx                SleepEx_sdk_shadow

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>

// -- 无 CRT 依赖的 memcmp 内联实现 ---------------------------------------
// 使用 -nostdlib 编译时 libmingwex 不参与链接，必须自带 memcmp；
// 仅 WaitOnAddress 一处调用，性能不敏感。
static int _memcmp_impl(const void *s1, const void *s2, size_t n) {
    const unsigned char *a = (const unsigned char *)s1;
    const unsigned char *b = (const unsigned char *)s2;
    for (size_t i = 0; i < n; i++) {
        if (a[i] != b[i]) return (int)a[i] - (int)b[i];
    }
    return 0;
}
#define memcmp _memcmp_impl

#undef WaitOnAddress
#undef WakeByAddressSingle
#undef WakeByAddressAll
#undef Sleep
#undef SleepEx

// -- polyfill state -------------------------------------------------
static CRITICAL_SECTION g_cs;
static CONDITION_VARIABLE g_cv;

// kernel32 function pointers (lazy bind at first call)
typedef VOID (WINAPI *SleepFn)(DWORD);
typedef DWORD (WINAPI *SleepExFn)(DWORD, BOOL);  // mingw SDK 用 DWORD 返回
static SleepFn   g_Sleep   = NULL;
static SleepExFn g_SleepEx = NULL;

static void _load_kernel32_funcs(void) {
    if (g_Sleep) return;
    HMODULE hk = GetModuleHandleW(L"kernel32.dll");
    if (!hk) hk = LoadLibraryW(L"kernel32.dll");
    if (hk) {
        g_Sleep   = (SleepFn)  GetProcAddress(hk, "Sleep");
        g_SleepEx = (SleepExFn)GetProcAddress(hk, "SleepEx");
    }
}

// -- Win8+ API polyfill --------------------------------------------

__declspec(dllexport) BOOL WINAPI WaitOnAddress(
    volatile VOID *Address,
    PVOID CompareAddress,
    SIZE_T AddressSize,
    DWORD dwMilliseconds
) {
    EnterCriticalSection(&g_cs);
    BOOL result = TRUE;
    // 显式 const 转换消掉 volatile 传递到 memcmp 的警告
    while (memcmp((const void *)Address, CompareAddress, AddressSize) == 0) {
        result = SleepConditionVariableCS(&g_cv, &g_cs, dwMilliseconds);
        if (result == FALSE && GetLastError() == ERROR_TIMEOUT) {
            LeaveCriticalSection(&g_cs);
            return FALSE;
        }
    }
    LeaveCriticalSection(&g_cs);
    return TRUE;
}

__declspec(dllexport) VOID WINAPI WakeByAddressSingle(PVOID Address) {
    WakeConditionVariable(&g_cv);
}

__declspec(dllexport) VOID WINAPI WakeByAddressAll(PVOID Address) {
    WakeAllConditionVariable(&g_cv);
}

// -- Win7-native functions -> forward to kernel32 -----------------
// 这些在 Win10+ 被重新导出到 api-ms-win-core-synch-l1-2-0，但 Win7 没有
// API Set 层。必须由 shim 再导出，Python embed runtime 也经此 DLL 解析
// Sleep，缺失会导致加载崩溃。

__declspec(dllexport) VOID WINAPI Sleep(DWORD dwMilliseconds) {
    _load_kernel32_funcs();
    if (g_Sleep) g_Sleep(dwMilliseconds);
}

__declspec(dllexport) DWORD WINAPI SleepEx(DWORD dwMilliseconds, BOOL bAlertable) {
    _load_kernel32_funcs();
    if (g_SleepEx) return g_SleepEx(dwMilliseconds, bAlertable);
    return 0;
}

// -- DLL entry -----------------------------------------------------

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpReserved) {
    if (fdwReason == DLL_PROCESS_ATTACH) {
        InitializeCriticalSection(&g_cs);
        InitializeConditionVariable(&g_cv);
    } else if (fdwReason == DLL_PROCESS_DETACH) {
        DeleteCriticalSection(&g_cs);
    }
    return TRUE;
}
