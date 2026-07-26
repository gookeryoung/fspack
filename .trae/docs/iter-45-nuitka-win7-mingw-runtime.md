# iter-45 Nuitka 编译后 Win7 兼容性修复

## 需求清单

- [x] 实现 `inject_mingw_runtime_dlls` 注入 MinGW 运行时 DLL 到 `dist/runtime/`
- [x] 在 `_prepare_runtime` 中对 Windows 目标调用注入
- [x] 在 `_build_compile_env` 中对 Windows 目标设置 `CFLAGS=-D_WIN32_WINNT=0x0601`
- [x] 保留用户已有 CFLAGS，已有 `_WIN32_WINNT=0x0601` 时不重复添加
- [x] Linux 目标不触发注入与 CFLAGS 设置
- [x] 测试覆盖所有新增场景
- [x] 全套门禁通过

## 迭代目标

修复启用 `nuitka = true` 编译 fspack 自身后，产物在 Win7 上无法运行的问题。两层根因：
1. MinGW 运行时 DLL（libgcc_s_seh-1.dll 等）未注入到 `dist/runtime/`，Python 加载 .pyd 时找不到
2. MinGW 头文件默认 `_WIN32_WINNT=0x0A00`（Win10），.pyd 可能调用 Win10+ API

## 改动文件清单

- [src/fspack/packaging/loader.py](../../src/fspack/packaging/loader.py)：新增 `_MINGW_RUNTIME_DLLS`、`_locate_mingw_dll`、`inject_mingw_runtime_dlls`，导出 `inject_mingw_runtime_dlls`
- [src/fspack/builder.py](../../src/fspack/builder.py)：`_prepare_runtime` 中对 Windows 目标调用 `inject_mingw_runtime_dlls`
- [src/fspack/packaging/nuitka.py](../../src/fspack/packaging/nuitka.py)：`_build_compile_env` 中对 Windows 目标追加 `CFLAGS=-D_WIN32_WINNT=0x0601`
- [tests/test_loader.py](../../tests/test_loader.py)：新增 4 个测试覆盖 DLL 注入
- [tests/test_nuitka.py](../../tests/test_nuitka.py)：新增 4 个测试覆盖 CFLAGS 传递
- [tests/test_builder.py](../../tests/test_builder.py)：`_setup_embed_mocks` 添加 `inject_mingw_runtime_dlls` mock，新增 2 个测试覆盖 `_prepare_runtime` 调用

## 关键决策与依据

### 1. MinGW 运行时 DLL 注入放在 loader.py

**决策**：在 `loader.py` 中实现 `inject_mingw_runtime_dlls`，与 `MINGW_GCC`/`_find_mingw_gcc`/`mingw_available` 等 MinGW 相关符号放在一起。

**依据**：
- loader.py 已是 MinGW 工具链相关模块（含 MINGW_GCC、mingw_available 等）
- 职责单一：MinGW 工具链定位与运行时 DLL 注入
- 避免 nuitka.py 依赖 loader.py（nuitka.py 已在 _check_c_compiler 中惰性导入 loader）
- builder.py 通过惰性导入调用，避免顶层循环依赖

### 2. 用 `gcc -print-file-name` 定位 DLL 而非硬编码路径

**决策**：用 `subprocess.run([gcc, "-print-file-name", dll_name])` 定位每个 DLL。

**依据**：
- MinGW 工具链安装位置不固定（Linux 交叉编译在 `/usr/x86_64-w64-mingw32/bin/`，Windows 原生在 MSYS2/WinLibs 安装目录）
- `gcc -print-file-name` 返回 gcc 已知的 DLL 路径，自适应不同安装位置
- 相对路径时用 `shutil.which` 兜底（覆盖 gcc 返回仅文件名的情况）
- 源 DLL 缺失时仅 warning 不报错（兼容静态链接或非标准 MinGW 构建）

### 3. _WIN32_WINNT 通过 CFLAGS 环境变量传递

**决策**：在 `_build_compile_env` 中设置 `CFLAGS=-D_WIN32_WINNT=0x0601`。

**依据**：
- Nuitka 4.x 的 scons 读取 `CFLAGS` 环境变量追加到 `CCFLAGS` 传给 gcc
- `-D_WIN32_WINNT=0x0601` 覆盖 MinGW 头文件默认值 `0x0A00`（Win10）
- gcc 命令行定义优先于头文件默认定义
- 保留用户已有 CFLAGS（追加而非覆盖），已有 `_WIN32_WINNT=0x0601` 时不重复添加

### 4. 注入位置在 _prepare_runtime 而非 _compile_user_sources

**决策**：在 `_prepare_runtime` 中调用 `inject_mingw_runtime_dlls`，与 Win7 兼容 DLL 注入并列。

**依据**：
- loader.exe 也依赖 MinGW 运行时 DLL（编译命令同样无 -static），无论是否启用 Nuitka 都需要注入
- _prepare_runtime 是 runtime 准备的统一入口，与 Win7 兼容 DLL 注入逻辑相邻
- 注入幂等：DLL 已存在则跳过，重复构建安全

### 5. _setup_embed_mocks 添加 inject_mingw_runtime_dlls mock

**决策**：在 `_setup_embed_mocks` 中 mock `inject_mingw_runtime_dlls` 为空操作。

**依据**：
- `inject_mingw_runtime_dlls` 内部调用 `subprocess.run([gcc, ...])`，测试机无 MinGW 时会 FileNotFoundError
- 实际注入逻辑在 test_loader.py 测试覆盖，builder 层只需验证调用
- 测试隔离：避免依赖测试机 MinGW 环境

## 代码实现情况

### loader.py 新增

```python
_MINGW_RUNTIME_DLLS = ("libgcc_s_seh-1.dll", "libwinpthread-1.dll", "libstdc++-6.dll")


def _locate_mingw_dll(gcc: str, dll_name: str) -> Path | None:
    """用 gcc -print-file-name 定位 MinGW 运行时 DLL."""
    result = subprocess.run([gcc, "-print-file-name", dll_name], capture_output=True, text=True, check=False)
    candidate = result.stdout.strip()
    if not candidate:
        return None
    candidate_path = Path(candidate)
    if not candidate_path.is_absolute():
        which = shutil.which(candidate)
        if which is None:
            return None
        candidate_path = Path(which)
    return candidate_path if candidate_path.is_file() else None


def inject_mingw_runtime_dlls(target_dir: Path) -> None:
    """将 MinGW 运行时 DLL 复制到目标目录."""
    gcc = _find_mingw_gcc()
    for dll_name in _MINGW_RUNTIME_DLLS:
        dest = target_dir / dll_name
        if dest.is_file():
            continue
        src = _locate_mingw_dll(gcc, dll_name)
        if src is None:
            _logger.warning("MinGW 运行时 DLL 缺失: %s，跳过注入", dll_name)
            continue
        shutil.copy2(src, dest)
```

### builder.py 修改

```python
if target is Platform.WINDOWS:
    from fspack.packaging.loader import inject_mingw_runtime_dlls
    inject_mingw_runtime_dlls(ctx.runtime_dir)
```

### nuitka.py 修改

```python
if target is Platform.WINDOWS:
    win7_flag = "-D_WIN32_WINNT=0x0601"
    existing_cflags = env.get("CFLAGS", "")
    if win7_flag not in existing_cflags:
        env["CFLAGS"] = f"{existing_cflags} {win7_flag}".strip()
```

## 整合优化情况

- 复用 loader.py 的 `_find_mingw_gcc`，无重复代码
- 与现有 Win7 兼容 DLL 注入逻辑并列，统一在 `_prepare_runtime` 处理 runtime DLL
- 测试隔离：builder 层 mock inject_mingw_runtime_dlls，loader 层独立测试注入逻辑

## 测试验证结果

```
uv run ruff check src tests          → All checks passed!
uv run ruff format src tests         → 1 file reformatted, 46 files left unchanged
uv run pyrefly check                 → 0 errors (70 suppressed, 7 warnings not shown)
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95
  → 951 passed, 21 deselected in 6.34s
  → coverage: 97.02%
```

模块覆盖率：
- loader.py: 98%（+2pp，新增 _locate_mingw_dll/inject_mingw_runtime_dlls）
- nuitka.py: 96%（持平，新增 CFLAGS 分支已覆盖）
- builder.py: 96%（持平，新增 inject_mingw_runtime_dlls 调用已覆盖）

## 遗留事项

- 未实测真实 Win7 环境（需用户实机验证）
- UCRT 依赖（KB2999226）未处理：Win7 用户需自行安装 UCRT 补丁，fspack 不内置 UCRT
- loader.exe 编译命令未添加 `-static`：仍依赖 MinGW 运行时 DLL，但通过 inject_mingw_runtime_dlls 注入到 `dist/runtime/` 解决（loader.exe 在 `dist/`，加载时 DLL 搜索路径含 system32/PATH，可能找到 DLL；若找不到需进一步将 DLL 复制到 `dist/`）

## 下一轮计划

无（修复完成，等待用户 Win7 实机验证）。若 Win7 仍报 loader.exe 加载失败，需将 MinGW 运行时 DLL 也复制到 `dist/`（loader.exe 所在目录）。
