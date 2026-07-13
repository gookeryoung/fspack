# skill-03 Linux 平台支持（python-build-standalone）

## 核心决策

### 平台抽象
- `Platform` 枚举（WINDOWS/LINUX），`detect_platform()` 按 `sys.platform` 识别。
- `BuildConfig` 带 `target` 字段，流水线按 target 分发。
- 平台常量：wheel platform tag、编译器、libpython 后缀。

### python-build-standalone
- URL：`https://github.com/indygreg/python-build-standalone/releases/download/{tag}/cpython-{ver}+{tag}-x86_64-unknown-linux-gnu-install_only.tar.gz`。
- 缓存到 `~/.fspack/cache/standalone/`，解压到 `dist/runtime/python/`。
- `ensure_standalone` 幂等：`runtime/python/bin/python3` 存在则跳过。

### Linux C loader
- `dlopen(libpython.so, RTLD_NOW|RTLD_GLOBAL)` + `dlsym("Py_Main")` + `setenv("PYTHONHOME", "runtime/python")`。
- `exe_dir` 用 `readlink("/proc/self/exe")` 获取。
- gcc 编译：`gcc -O2 -o <exe> <c> -ldl`（Linux 无 GUI 子系统概念）。
- 后续演进（iter-10）：入口路径改为从 `dist/.entry` 文件读取（`fgets`），不再硬编码。

### 流水线分支
- WINDOWS：ensure_embed + write_pth + mingw loader + wheel `win_amd64`
- LINUX：ensure_standalone（不写 _pth）+ gcc loader + wheel `manylinux2014_x86_64`
- Linux 不写 _pth（PYTHONHOME 已定位标准库）。
