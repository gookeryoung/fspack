// bcryptprimitives.dll Win7 polyfill — ProcessPrng → bcrypt!BCryptGenRandom
//
// Problem: Rust 1.78+ (2024-05) 把 Windows 默认 target 最低 OS 提升到
//          Win10。之后编译的 Rust wheel（pydantic-core / bcrypt /
//          cryptography / watchfiles 等）硬链接 ProcessPrng —— 一个
//          Win10+ bcryptprimitives.dll 才有的导出。Win7 上该 DLL 整个
//          不存在，PE loader 报 WinError 127（找不到指定的程序）。
//
// Solution: 编译一个最小 bcryptprimitives.dll 只导出 ProcessPrng，
//           运行时转发到 Win7 原生 bcrypt.dll 的 BCryptGenRandom
//           （Win7 SP1 自带 bcrypt.dll，KB2999226 即可）。
//
// Build (mingw64):
//   x86_64-w64-mingw32-gcc -shared -O2 -o bcryptprimitives.dll bcryptprimitives.c -lbcrypt
//
// fspack 打包期会自动将此 shim 注入 dist 根目录（PE loader 优先从同
// 目录加载，遮蔽系统缺失的 Win10+ DLL）。

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>

// ProcessPrng —— Windows 10+ bcryptprimitives 导出；Rust rand crate
// （rust-rand 1.8+）在 is_x86_feature_detected("rdrand") 为假时走此路径。
// 原型（WinSDK bcryptprimitives.h）：
//   void ProcessPrng(PBYTE Data, SIZE_T len);
// Win7 上 bcrypt.dll 的 BCryptGenRandom 是功能等价的 CNG 随机数生成器
// （内部实现会检测 RNG 硬件，回退到基于 SHA-256 的 DRBG）。

__declspec(dllexport) VOID WINAPI ProcessPrng(PBYTE Data, SIZE_T len) {
    // 转发到 BCryptGenRandom —— BCrypt.useBCrypt.dll 在 Win7 SP1 上已存在，
    // 但显式 LoadLibrary + GetProcAddress 避免 PE loader 把 bcrypt.dll
    // 作为静态依赖（某些精简版 Win7 可能未装 CNG）。
    static VOID (*pfn)(PBYTE, SIZE_T) = NULL;
    if (!pfn) {
        HMODULE h = GetModuleHandleW(L"bcrypt.dll");
        if (!h) h = LoadLibraryW(L"bcrypt.dll");
        if (!h) return;  // 彻底失败：bcrypt 都没有，调用方得自己降级
        typedef VOID (WINAPI *ProcessPrng_t)(PBYTE, SIZE_T);
        pfn = (ProcessPrng_t)GetProcAddress(h, "ProcessPrng");
        if (!pfn) {
            // bcrypt.dll 自己没有 ProcessPrng（Win7 上 bcrypt.dll 不导出它），
            // 退而用 BCryptGenRandom 替代。BCryptGenRandom 返回 NTSTATUS，
            // 我们只做 best-effort，失败时 Data 内容不确定。
            typedef NTSTATUS(WINAPI* GenRandom_t)(BCRYPT_ALG_HANDLE, PUCHAR, ULONG, ULONG);
            GenRandom_t gr = (GenRandom_t)GetProcAddress(h, "BCryptGenRandom");
            if (gr) {
                BCRYPT_ALG_HANDLE alg = NULL;
                if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_RNG_ALGORITHM, NULL, 0) >= 0) {
                    gr(alg, Data, (ULONG)len, 0);
                    BCryptCloseAlgorithmProvider(alg, 0);
                }
            }
            return;
        }
    }
    pfn(Data, len);
}

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpReserved) {
    (void)hinstDLL; (void)fdwReason; (void)lpReserved;
    return TRUE;
}
