# Nuitka 编译后 Win7 兼容性修复

## 背景

启用 `nuitka = true` 编译 fspack 自身后，产物在 Win7 上无法运行。

根因有两层：

1. **MinGW 运行时 DLL 缺失**：fspack 用 `x86_64-w64-mingw32-gcc` 编译 loader.exe 与 .pyd，产物动态链接 `libgcc_s_seh-1.dll`/`libwinpthread-1.dll`/`libstdc++-6.dll`。这些 DLL 不随 Windows 分发，fspack 之前未注入到 `dist/runtime/`。Python 加载 .pyd 时 DLL 搜索路径含 `runtime/`（由 loader.exe 的 `SetDllDirectoryW` 设置），找不到这些 DLL 导致 `ImportError: DLL load failed`。

2. **MinGW 头文件默认 targeting Win10**：现代 MinGW-w64 头文件默认 `_WIN32_WINNT=0x0A00`（Win10），编译的 .pyd 可能调用 Win10+ API，Win7 上加载失败。

## 需求清单

- [x] 实现 `inject_mingw_runtime_dlls(target_dir)`：用 `gcc -print-file-name` 定位 MinGW 运行时 DLL，复制到 `dist/runtime/`
- [x] 在 `_prepare_runtime` 中对 Windows 目标调用注入（与 Win7 兼容 DLL 注入并列）
- [x] 在 `_build_compile_env` 中对 Windows 目标设置 `CFLAGS=-D_WIN32_WINNT=0x0601`（Win7）
- [x] 保留用户已有 CFLAGS（追加而非覆盖）
- [x] 已有 `_WIN32_WINNT=0x0601` 时不重复添加
- [x] Linux 目标不触发注入与 CFLAGS 设置
- [x] 测试覆盖所有新增场景
- [x] 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- Nuitka 编译后的 .pyd 在 Win7 上能被 Python 加载（MinGW 运行时 DLL 在 `dist/runtime/`）
- .pyd 不调用 Win10+ API（`_WIN32_WINNT=0x0601` 限制 MinGW 头文件 targeting）
- 不影响 Linux 目标构建
- 不破坏现有测试
- 覆盖率不下降
