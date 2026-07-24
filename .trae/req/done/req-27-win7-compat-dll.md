# req-27 Win7 兼容性 DLL 注入

## 需求

- [x] embed python 支持 Win7 等老旧系统兼容性
- [x] 避免每次构建下载完整 PythonVista embed zip
- [x] 本地准备兼容版二进制 DLL，随 fspack 分发

## 背景

用户提议使用 PythonVista（`https://gitcode.com/gh_mirrors/py/PythonVista`）的 embed python 替代 python.org 版本以保证 Win7 兼容性。经测试 gitcode 镜像不提供二进制文件直接下载，用户调整为本地准备兼容版 DLL 方案。

## 实现

从 `https://github.com/adang1345/api-ms-win-core-path` Release v1.0.0 提取 x64 `api-ms-win-core-path-l1-1-0.dll`（116KB），内置到 `src/fspack/assets/runtime/`。构建时对 Python 3.9+ Windows 目标注入到 `dist/runtime/`。

详见 `iter-33-win7-compat-dll.md`。
