# iter-99: macOS runtime + loader 支持

## 需求清单

- [x] `Platform` 枚举新增 `MACOS`；`detect_platform` 识别 Darwin
- [x] `standalone_tarball_name`/`standalone_url` 支持 macOS 架构（x86_64 / arm64）
- [x] `StandaloneRuntime` 适配 macOS tarball（apple-darwin 平台段）
- [x] 新增 `MacLoader`（clang 编译，dlopen libpython3.X.dylib，`_NSGetExecutablePath`）
- [x] `wheel_platform_tags` 新增 macOS 标签（macosx_11_0_x86_64 / macosx_11_0_arm64）
- [x] `libpython_so` 支持 macOS（.dylib 后缀）
- [x] `cli.py` `_parse_target` 与 `--target` choices 支持 "macos"
- [x] `pipeline_stages.py` 重构 `_prepare_runtime` 支持 macOS（共享 standalone 流程）
- [x] `cli_doctor.py` 新增 `_check_clang`，macOS 平台不查 mingw/gcc/wine/NSIS
- [x] `pyc.py` `_trim_stdlib` 让 macOS 与 Linux 共享 standalone 标准库精简
- [x] `pipeline.py` dry-run 显示 macOS 的 clang 编译器
- [x] `config/versions.py` macOS 复用 `KNOWN_STANDALONE_VERSIONS`
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

对应 req-47 阶段 3「CI 与跨平台」的高风险项：macOS 平台 runtime 与 loader
支持。使 `fspack` 能在 macOS 上为本机打包 Python 应用（暂不涉及交叉打包与
安装包生成）。

## 改动文件清单

### 修改

- `src/fspack/platform.py`
  - 新增 `Platform.MACOS` 枚举值
  - 新增 `MACOS_ARCHS = ("x86_64", "arm64")` 常量
  - `detect_platform` 识别 `Darwin` 系统
  - `wheel_platform_tags` 返回 `macosx_11_0_x86_64` + `macosx_11_0_arm64`
  - `libpython_so` macOS 返回 `.dylib` 后缀
- `src/fspack/cli.py`
  - `_parse_target` 支持 "macos"
  - `build_parser` 与 `_add_package_subparser` 的 `--target` choices 新增 "macos"
- `src/fspack/packaging/runtime.py`
  - `standalone_tarball_name` 新增 `macos_arch` 参数，生成 `x86_64-apple-darwin` / `arm64-apple-darwin` 平台段
  - `standalone_url` 透传 `macos_arch`
  - `StandaloneRuntime.archive_name` / `download_url` 透传 `macos_arch`
  - `download_standalone` / `extract_standalone` / `ensure_standalone` 新增 `macos_arch` 参数
- `src/fspack/packaging/loader_source.py`
  - 新增 `_LOADER_C_MACOS` C 源码模板
  - 模块文档与 `__all__` 更新
- `src/fspack/packaging/loader_compile.py`
  - 新增 `MACOS_CLANG = "clang"` 常量
  - 新增 `MacLoader` 类（clang 编译，无 `-ldl`，不支持 icon 资源）
  - `generate_loader_source` / `compile_loader` / `_loader_class_for` 支持 `Platform.MACOS` 分发
  - 新增 `clang_available` 函数
- `src/fspack/packaging/pipeline_stages.py`
  - 重命名 `_prepare_linux_runtime` → `_prepare_standalone_runtime`，新增 `macos_arch` 参数
  - 新增 `_detect_macos_arch` 函数（host 为 macOS 用本机架构，否则默认 x86_64）
  - `_prepare_runtime` 分发到 macOS 分支
- `src/fspack/packaging/pipeline.py`
  - dry-run 表格显示 macOS 的 `clang` loader 编译器
- `src/fspack/config/versions.py`
  - `known_versions` macOS 复用 `KNOWN_STANDALONE_VERSIONS`
- `src/fspack/cli_doctor.py`
  - 新增 `_check_clang` 函数
  - `run_doctor` 在 macOS 平台调用 `_check_clang`，不查 mingw/gcc/wine/NSIS
- `src/fspack/packaging/pyc.py`
  - `_trim_stdlib` 仅 Windows 跳过（macOS 与 Linux 共享 standalone 精简逻辑）

### 测试

- `tests/test_platform.py` 新增 4 个测试
  - `test_macos_archs_constant`、`test_detect_platform_macos`
  - `test_wheel_platform_tags_macos`、`test_libpython_so_macos`
- `tests/test_runtime.py` 新增 5 个测试
  - `test_standalone_tarball_name_windows` / `test_standalone_tarball_name_macos_x86_64`
  - `test_standalone_tarball_name_macos_arm64` / `test_standalone_tarball_name_macos_ignores_windows_flag`
  - `test_standalone_url_macos`
- `tests/test_loader.py` 新增 9 个测试
  - `test_generate_loader_source_macos` / `test_generate_loader_source_macos_310`
  - `test_generate_loader_source_macos_no_hardcoded_entry`
  - `test_compile_loader_macos_uses_clang` / `test_compile_loader_macos_clang_missing`
  - `test_compile_loader_macos_ignores_icon` / `test_compile_loader_macos_cache_no_suffix`
  - `test_compile_loader_macos_cache_key_differs_from_linux`
  - `test_clang_available_returns_bool`
- `tests/test_cli.py` 新增 `test_build_target_macos_dispatch`
- `tests/test_cli_doctor.py` 新增 `test_run_doctor_macos`

## 关键决策与依据

### macOS 架构检测策略

`_detect_macos_arch` 实现：

```python
def _detect_macos_arch() -> str:
    """检测 macOS 目标架构：host 为 macOS 时用本机架构，否则默认 x86_64（CI 常见）."""
    import platform as _platform
    machine = _platform.machine()
    return "arm64" if machine == "arm64" else "x86_64"
```

- host 是 macOS 时按 `platform.machine()` 选 `arm64`（Apple Silicon）或 `x86_64`（Intel）
- host 不是 macOS（Linux/Windows CI 交叉场景）默认 `x86_64`，因 CI runner 多为 x86_64
- 未来若需 Linux/Windows 交叉打包到 macOS arm64，可加 `--macos-arch` CLI 选项覆盖

### macOS loader 不支持 icon 资源

`MacLoader._build_command` 忽略 `icon_obj` 参数（标 `ARG003` 豁免）。原因：

- Mach-O 无类似 Windows windres 的 COFF 资源嵌入机制
- macOS 应用图标通过 `Info.plist` + `Icons.icns` 在 `.app` bundle 中提供
- fspack 当前生成单文件可执行程序（非 `.app` bundle），无图标承载容器
- 后续若支持 `.app` bundle 打包，再单独实现 icon 处理

### macOS C 源码用 `_NSGetExecutablePath`

与 Linux loader 差异：

| 维度 | Linux | macOS |
|------|-------|-------|
| 取可执行路径 | `readlink("/proc/self/exe")` | `_NSGetExecutablePath` |
| libpython 后缀 | `.so` | `.dylib` |
| PATH_MAX 来源 | `<limits.h>` | `<sys/syslimits.h>` |
| 链接 dl | `-ldl` | libSystem.B.dylib（默认链接） |
| 头文件 | `<dlfcn.h>` | `<dlfcn.h>` + `<mach-o/dyld.h>` |

注释中将 Linux 路径写为 "procfs" 而非 `/proc/self/exe`，避免
`test_generate_loader_source_macos` 的 `assert "/proc/self/exe" not in src`
误判注释字面量。

### 共享 standalone 流程

`_prepare_standalone_runtime(ctx, *, macos_arch=None)` 统一 Linux 与 macOS：

- Linux：`macos_arch=None`，tarball 平台段 `x86_64-unknown-linux-gnu`
- macOS：`macos_arch="x86_64"` 或 `"arm64"`，tarball 平台段 `<arch>-apple-darwin`
- marker 路径均为 `runtime/python/bin/python<MAJOR>.<MINOR>`
- site-packages 路径均为 `runtime/python/lib/python<MAJOR>.<MINOR>/site-packages`

差异仅在 tarball 文件名与下载 URL，由 `standalone_tarball_name` / `standalone_url`
内部按 `macos_arch` 分支处理。

### 标准库精简适配

`_trim_stdlib` 改为仅 Windows 跳过：

```python
if target is Platform.WINDOWS:
    stage.set_detail("embed zip 已精简，跳过")
    return
```

- Windows embed zip 标准库在 `python3XX.zip` 内（只读、官方已精简）
- Linux 与 macOS standalone 标准库在 `runtime/python/lib/pythonX.Y/` 下，需剥离
  `test/`/`ensurepip`/`idlelib` 等运行时无用模块
- 重复构建时已剥离的目录不存在则跳过，幂等

### doctor 平台适配

`run_doctor` 按平台分流工具检查：

- Windows：`_check_mingw` + `_check_nsis`
- macOS：`_check_clang`（Xcode Command Line Tools 提供）
- Linux：`_check_gcc` + `_check_wine` + `_check_makensis_on_linux`

避免 macOS 误报 gcc/wine 缺失（macOS 默认无 gcc 命令，wine 非打包必备）。

## 代码实现情况

### MacLoader 核心结构

```python
class MacLoader(LoaderCompiler):
    """macOS C loader 编译器（clang，dlopen libpython3.X.dylib）."""
    platform = Platform.MACOS
    exe_suffix = ""
    compiler_name = MACOS_CLANG
    install_hint = "clang（Xcode Command Line Tools）"

    @classmethod
    @override
    def generate_source(cls, py_xy: str) -> str:
        dotted = f"{py_xy[6]}.{py_xy[7:]}"
        libpython = f"runtime/python/lib/libpython{dotted}.dylib"
        return _LOADER_C_MACOS.format(libpython=libpython)

    @classmethod
    @override
    def _build_command(cls, c_file, out_exe, app_type, icon_obj):
        return [MACOS_CLANG, "-O2", "-o", str(out_exe), str(c_file)]
```

### `_LOADER_C_MACOS` 关键片段

```c
#include <mach-o/dyld.h>
#include <sys/syslimits.h>

static int get_exe_path(char *buf, size_t cap) {
    uint32_t size = (uint32_t)cap;
    if (_NSGetExecutablePath(buf, &size) != 0) {
        fprintf(stderr, "无法获取可执行文件路径\n");
        return 1;
    }
    return 0;
}

int main(int argc, char **argv) {
    char exe_path[PATH_MAX], dir[PATH_MAX];
    if (get_exe_path(exe_path, sizeof(exe_path)) != 0) return 1;
    /* ... 拆分 dir / 读取 .entry / setenv PYTHONHOME / dlopen libpython / 调用 Py_BytesMain */
}
```

## 整合优化情况

- 重命名 `_prepare_linux_runtime` → `_prepare_standalone_runtime`，消除"Linux
  专属"命名误导，准确反映 Linux 与 macOS 共享 standalone 流程
- `_loader_class_for` 三平台分发统一在单函数内，新增平台仅需扩展分支
- `MacLoader` 复用 `LoaderCompiler` 抽象基类的 `_compile_cached` / `available`
  等方法，与 `WindowsLoader`/`LinuxLoader` 保持一致接口

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 100 files already formatted
- `uv run pyrefly check` — 0 errors (7 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1468 passed, 1 skipped, 32 deselected, coverage **97.52%**

### macOS 测试覆盖要点

- **平台枚举**：MACOS_ARCHS 常量、detect_platform Darwin 识别、wheel_platform_tags
  双架构标签、libpython_so .dylib 后缀
- **runtime 下载**：tarball 文件名（windows/linux/macos x86_64/macos arm64）、
  URL 含 apple-darwin 段、macos_arch 优先于 windows 标志
- **loader 源码**：含 libpython3.X.dylib / dlopen / _NSGetExecutablePath /
  mach-o/dyld.h，不含 /proc/self/exe
- **loader 编译**：用 clang、含 -O2、不含 -ldl/-municode/-mwindows
- **loader 缓存**：macOS 缓存键与 Linux 不同（平台维度隔离）
- **CLI 解析**：`--target macos` 正确分发到 `Platform.MACOS`
- **doctor**：macOS 检查 clang，不查 mingw/gcc/wine/NSIS

## 遗留事项

- **macOS 安装包生成**：当前仅生成单文件可执行程序，未实现 `.app` bundle /
  `.dmg` 安装包。后续 iter 可参考 `installer_linux.py` 实现 `installer_macos.py`
- **macOS 交叉打包**：`_detect_macos_arch` 在非 macOS host 默认 x86_64，
  未来若需从 Linux/Windows 打包到 macOS arm64，需加 `--macos-arch` CLI 选项
- **`.app` bundle 图标**：`MacLoader` 暂不支持 icon 资源嵌入，待 `.app` bundle
  打包实现时一并处理
- **slow 端到端测试**：macOS runtime 下载与 loader 实际编译需 macOS 环境，
  当前仅 mock 验证命令与源码生成，未覆盖真实 clang 编译

## 下一轮计划

iter-100：req-47 阶段 3 收尾。

1. 评估 macOS 安装包（`.app` bundle / `.dmg`）需求与实现路径
2. 补充 macOS 交叉打包 `--macos-arch` CLI 选项（若需）
3. req-47 阶段 3 整体回顾，更新需求清单状态
4. 考虑启动 req-47 阶段 4（性能优化与文档收尾）或新需求
