# iter-03 Linux 平台支持（python-build-standalone）

## 迭代目标

P3 增加 Linux 打包：用 indygreg python-build-standalone 作为便携式 CPython 运行时，C loader 加载 libpython 调用 Py_Main，思路与 Windows embed 一致。复用 P1/P2 的解析/AST/依赖下载/流水线框架，加平台抽象层。

## 关键决策

### 平台抽象
- 新增 `platform.py`：`Platform` 枚举（WINDOWS/LINUX）、`detect_platform()` 按 `sys.platform` 识别、平台常量（wheel platform tag、编译器、libpython 后缀）。
- `BuildConfig` 加 `target: Platform` 字段，流水线按 target 分发。

### python-build-standalone 下载（standalone.py）
- URL：`https://github.com/indygreg/python-build-standalone/releases/download/{release_tag}/cpython-{ver}+{release_tag}-x86_64-unknown-linux-gnu-install_only.tar.gz`。
- 缓存到 `~/.fspack/cache/standalone/`，命中跳过下载。
- 解压到 `dist/runtime/`，解压后顶层 `python/` 目录为 Python 根（含 bin/python3.X、lib/libpython3.X.so、lib/python3.X/）。
- `ensure_standalone` 幂等：`runtime/python/bin/python3` 存在则跳过。

### Linux C loader
- 模板用 `dlopen(libpython.so, RTLD_NOW|RTLD_GLOBAL)` + `dlsym("Py_Main")` + `setenv("PYTHONHOME", "runtime/python")`。
- 入口路径烧入：`ENTRY_FILE`（如 `src/helloworld.py`）、`LIBPYTHON`（如 `runtime/python/lib/libpython3.11.so`）、`PYTHONHOME`（`runtime/python`）。
- `exe_dir` 用 `readlink("/proc/self/exe")` 获取 loader 所在目录。
- gcc 编译：`gcc -O2 -o <exe> <c> -ldl`（Linux 无 GUI 子系统，AppType.GUI 仅影响快捷方式/安装包，loader 不区分）。

### 流水线分支
- `builder.build(target=...)`：
  - WINDOWS：ensure_embed + write_pth + mingw loader + wheel `win_amd64`
  - LINUX：ensure_standalone（不写 _pth，用 PYTHONHOME）+ gcc loader + wheel `manylinux2014_x86_64`
- Linux 不写 _pth（PYTHONHOME 已定位标准库，site-packages 在 `runtime/python/lib/python3.X/site-packages`）。

### CLI
- `fsp b --target linux|windows`（默认当前平台）。
- `fsp p --target` 同理。

## 改动文件清单

新增：
- src/fspack/platform.py
- src/fspack/standalone.py
- tests/test_standalone.py
- tests/test_platform.py

修改：
- src/fspack/config.py（BuildConfig 加 target）
- src/fspack/loader.py（generate_loader_source/compile_loader 加 platform 分支 + Linux 模板）
- src/fspack/builder.py（build 加 target 参数，按平台分支）
- src/fspack/commands/build.py、package.py（透传 target）
- src/fspack/cli.py（--target 选项）

## 验证结果

四项门禁全过（2026-07-12）：

- `ruff check src tests`：All checks passed
- `ruff format --check src tests`：34 files already formatted
- `pyrefly check`：0 errors（2 suppressed）
- `pytest -m "not slow" --cov=fspack`：118 passed, 1 deselected, cov 99.08%

修复要点：
- `loader.generate_loader_source` Linux 分支 libpython 路径漏 "python" 前缀（`lib3.11.so` → `libpython3.11.so`），与 `platform.libpython_so` 对齐。
- `test_loader.py` 缺 `Platform`/`gcc_available` 导入。
- `test_builder.py` Windows 测试的 `fake_compile` 缺 `platform` 参数、`download_wheels` lambda 缺 `platform_tag` 关键字参数（builder.build 现按 target 透传）。

未覆盖行（非新增）：
- `platform.py:21`（detect_platform 的 Windows 分支，Linux dev 不可达）
- `project.py:83`（防御性 `mod in seen` continue）
- `cli.py:66->exit`/`82`（argparse 错误分支）

## 遗留事项

- python-build-standalone 真实下载/gcc 编译标 slow，待环境验证。
- P4：macOS 支持；Linux 安装包（.deb/.rpm/AppImage）。
