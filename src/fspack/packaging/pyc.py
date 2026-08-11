"""字节码预编译 facade：stamp / compile / source_strip / runtime_trim 重导出.

拆自原 609 行单文件，子模块：

- :mod:`fspack.packaging.pyc_stamp`：.pyc_stamp 路径与键值计算
- :mod:`fspack.packaging.pyc_compile`：compileall 执行与 _precompile_pyc 主入口
- :mod:`fspack.packaging.source_strip`：.py 源码剥离 + PEP 3147 legacy 布局迁移
- :mod:`fspack.packaging.runtime_trim`：Win7 DLL 注入 + stdlib/standalone/Tcl/Tk 精简

测试 patch 点（monkeypatch.setattr("fspack.packaging.pyc.<name>", ...)）：

- ``subprocess``（大量 strip 测试替换 subprocess.run）
- ``_WIN7_COMPAT_DLL_NAME``（L912 test_builder）
- ``_COMPILEALL_TIMEOUT``（test_nuitka L4136/L4154）
"""

from __future__ import annotations

import subprocess

from fspack.packaging.pyc_compile import (
    _COMPILEALL_TIMEOUT,
    _precompile_pyc,
    _run_compileall,
)
from fspack.packaging.pyc_stamp import _pyc_stamp_key, _pyc_stamp_path
from fspack.packaging.runtime_trim import (
    _STANDALONE_DEV_BIN_FILES,
    _STDLIB_TRIM_DIRS,
    _WIN7_COMPAT_DLL_NAME,
    _inject_win7_compat_dll,
    _needs_win7_compat_dll,
    _strip_elf_symbols,
    _strip_tcl_tk_counted,
    _trim_standalone_runtime,
    _trim_stdlib,
)
from fspack.packaging.source_strip import (
    _is_in_data_dirs,
    _strip_compiled_py,
    _strip_py_sources,
)

__all__ = [
    "_COMPILEALL_TIMEOUT",
    "_STANDALONE_DEV_BIN_FILES",
    "_STDLIB_TRIM_DIRS",
    "_WIN7_COMPAT_DLL_NAME",
    "_inject_win7_compat_dll",
    "_is_in_data_dirs",
    "_needs_win7_compat_dll",
    "_precompile_pyc",
    "_pyc_stamp_key",
    "_pyc_stamp_path",
    "_run_compileall",
    "_strip_compiled_py",
    "_strip_elf_symbols",
    "_strip_py_sources",
    "_strip_tcl_tk_counted",
    "_trim_standalone_runtime",
    "_trim_stdlib",
    "subprocess",
]
