"""C loader 源码生成与交叉编译 facade.

本模块为 facade，从子模块 re-export 全部公开 API：

- :mod:`fspack.packaging.loader.source`：C 源码模板（Windows/Linux/macOS）
- :mod:`fspack.packaging.loader.compile`：编译器基类、平台子类、编译流程、
  icon 资源处理

保留 ``import subprocess``/``import shutil`` 供测试 monkeypatch 通过
``fspack.packaging.loader.subprocess.run``/``shutil.which`` 等路径访问
（``subprocess``/``shutil`` 为全局模块对象，patch 影响所有子模块）。

Windows：loader.exe 在 dist/，动态加载 dist/runtime/python3X.dll，
解析 ``Py_Main`` 符号后以 ``[loader.exe, dist/src/<entry>, ...用户参数]`` 调用。
sys.path 由 dist/runtime/python3X._pth 文件控制（与 DLL 同目录），loader 不再设置环境变量。

Linux：loader 与 runtime/python/ 同目录（dist/），dlopen dist/runtime/python/lib/libpython3.X.so，
setenv PYTHONHOME 指向 runtime/python，调用 ``Py_BytesMain`` 运行入口脚本。

macOS：loader 与 runtime/python/ 同目录（dist/），dlopen
dist/runtime/python/lib/libpython3.X.dylib，setenv PYTHONHOME 指向 runtime/python，
调用 ``Py_BytesMain`` 运行入口脚本。可执行路径用 ``_NSGetExecutablePath`` 获取
（macOS 无 ``/proc/self/exe``）。

入口脚本路径在运行时从 ``<exe_dir>/<exe_basename>.entry`` 文件读取（多入口模式），
回退到 ``<exe_dir>/.entry``（单入口模式，向后兼容）。构建时为每个入口写对应
``<name>.entry`` 文件，使 loader 源码仅依赖 ``py_xy`` 与平台，可按
``(py_xy, app_type, platform)`` 缓存跨项目复用。
"""

from __future__ import annotations

import shutil  # noqa: F401 # 测试 monkeypatch 通过 fspack.packaging.loader.shutil.which 访问
import subprocess  # noqa: F401 # 测试 monkeypatch 通过 fspack.packaging.loader.subprocess.run 访问

from fspack.packaging.loader.compile import (
    LINUX_GCC,
    MACOS_CLANG,
    MINGW_GCC,
    MINGW_WINDRES,
    LinuxLoader,
    LoaderCompiler,
    LoaderVersionInfo,
    MacLoader,
    WindowsLoader,
    _compile_resource_obj,  # noqa: F401 # 测试通过 fspack.packaging.loader._compile_resource_obj 访问
    _find_mingw_gcc,  # noqa: F401 # 测试 monkeypatch 通过 fspack.packaging.loader._find_mingw_gcc 访问
    _find_windres,  # noqa: F401 # 测试通过 fspack.packaging.loader._find_windres 访问
    _icon_hash,  # noqa: F401 # 测试通过 fspack.packaging.loader._icon_hash 访问
    _loader_cache_key,  # noqa: F401 # 测试通过 fspack.packaging.loader._loader_cache_key 访问
    _version_info_hash,  # noqa: F401 # 测试通过 fspack.packaging.loader._version_info_hash 访问
    clang_available,
    compile_loader,
    gcc_available,
    generate_loader_source,
    loader_cache_dir,
    mingw_available,
)

__all__ = [
    "LINUX_GCC",
    "MACOS_CLANG",
    "MINGW_GCC",
    "MINGW_WINDRES",
    "LinuxLoader",
    "LoaderCompiler",
    "LoaderVersionInfo",
    "MacLoader",
    "WindowsLoader",
    "clang_available",
    "compile_loader",
    "gcc_available",
    "generate_loader_source",
    "loader_cache_dir",
    "mingw_available",
]
