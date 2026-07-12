# fspack P3 需求清单

P1/P2 已交付 Windows 优先的完整链路（CLI + 解析 + embed + loader + NSIS）。P3 增加 Linux 平台支持，运行时用 indygreg 的 python-build-standalone 便携式 CPython，C loader 加载 libpython 调用 Py_Main，思路与 Windows embed 一致。

## P3 范围

[x] 1. 平台抽象：新增 `Platform` 枚举（WINDOWS/LINUX），`BuildConfig` 带 target 字段，`detect_platform()` 识别当前系统。
[x] 2. python-build-standalone 下载与解压：新增 `standalone.py`，从 GitHub releases 下载 `cpython-{ver}+{date}-x86_64-unknown-linux-gnu-install_only.tar.gz`，缓存到 `~/.fspack/cache/standalone/`，解压到 `dist/runtime/python/`。
[x] 3. Linux C loader：扩展 `loader.py` 生成 Linux C 源码（dlopen libpython.so + dlsym Py_Main + setenv PYTHONHOME），用 gcc 编译为 ELF 可执行文件（`-ldl`）。
[x] 4. Linux 依赖下载：扩展 `builder.download_wheels` 支持 `--platform manylinux2014_x86_64` 拉 Linux wheel。
[x] 5. 流水线平台分支：`builder.build` 加 `target` 参数，按平台调 standalone/embed + 对应 loader + 对应 wheel 平台。
[x] 6. CLI `--target`：`fsp b`/`fsp p` 加 `--target windows|linux`，默认当前平台。
[x] 7. 单测：standalone 下载/解压 mock、Linux loader 源码内容断言、gcc 编译 mock、build target 分支、CLI --target 分发。
[] 8. 端到端验证（slow）：真实下载 python-build-standalone + gcc 编译 + 运行 hello world。

## 不在 P3 范围

- macOS 支持（P4+）。
- Linux 安装包（.deb/.rpm/AppImage），P3 仅产出可运行目录。
- Linux GUI（无 windows 子系统概念，Tk/Qt 直接运行）。

## 验收标准

- `fsp b --target linux` 在 Linux 上产出 `dist/<name>`（ELF 可执行文件）+ `dist/runtime/python/` + `dist/src/` + `dist/runtime/python/lib/python3.X/site-packages/`。
- `dist/<name>` 直接运行输出预期内容（无需 wine）。
- 全套门禁通过：`ruff check`、`ruff format --check`、`pyrefly check`、`pytest --cov≥95%`（standalone 下载/gcc 编译标 slow）。
- Python 3.8 兼容（dev 环境）。

## 约束

- python-build-standalone 从 GitHub 下载（无稳定国内镜像），标 slow；单测 mock urllib。
- Linux loader 用 gcc（系统自带），不需 mingw。
- 遵循 rule-11 Python 规范，平台分支用枚举分发，避免字符串判断。
