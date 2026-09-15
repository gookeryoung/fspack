"""Win7 兼容性 facade：check / dll / patch / scan / shim_build 子模块重导出.

子模块（按职责拆分）：

- :mod:`fspack.packaging.win7.check`：PE 导入表 Win8+ API 黑名单静态检查
  （CLI：``python -m fspack.packaging.win7.check``）
- :mod:`fspack.packaging.win7.dll`：Win7 重编译版 python3XX.dll 清单驱动
  下载与双重校验（sha256 + 导入表）
- :mod:`fspack.packaging.win7.patch`：PE 导入表原地改名——把 kernel32 的
  Win8+ API（如 GetSystemTimePreciseAsFileTime）改写为同签名 Win7 原生
  等价函数，IAT 槽位不变（CLI：``python -m fspack.packaging.win7.patch``）
- :mod:`fspack.packaging.win7.scan`：dist 产物 Win7 兼容门禁（loader exe
  硬门禁 + dist 全量扫描报告）
- :mod:`fspack.packaging.win7.shim_build`：就地编译 Win7 C shim DLL
  （synch / bcryptprimitives），构建期兜底缺失二进制
"""

from __future__ import annotations

from fspack.packaging.win7.check import (
    PeParseError,
    Win7ApiViolation,
    Win7CheckResult,
    check_win7_imports,
    main,
)
from fspack.packaging.win7.dll import (
    WIN7_EMBED_SHA256,
    WIN7_SHIM_DLL_PATH,
    Win7DllError,
    Win7EmbedRuntime,
    download_win7_embed,
    ensure_win7_dll,
    extract_win7_dll,
    is_win7_runtime,
    needs_win7_dll,
    win7_dll_name,
    win7_zip_cache_name,
    win7_zip_url,
)
from fspack.packaging.win7.patch import (
    RENAMED_IMPORTS,
    PatchResult,
    PEPatchError,
    RenameRule,
    patch_bytes,
    patch_dist_win7,
    patch_file,
)
from fspack.packaging.win7.scan import (
    Win7ScanError,
    Win7ScanReport,
    enforce_win7_loaders,
    inject_win7_shims,
    iter_pe_files,
    render_win7_report,
    scan_dist_win7,
    write_win7_report,
)
from fspack.packaging.win7.shim_build import (
    ALL_SHIMS,
    SHIM_SRC_DIR,
    ShimBuildError,
    ShimSpec,
    build_all_shims,
    build_shim,
    ensure_all_shims,
    is_shim_missing,
)

__all__ = [
    "ALL_SHIMS",
    "RENAMED_IMPORTS",
    "SHIM_SRC_DIR",
    "WIN7_EMBED_SHA256",
    "WIN7_SHIM_DLL_PATH",
    "PEPatchError",
    "PatchResult",
    "PeParseError",
    "RenameRule",
    "ShimBuildError",
    "ShimSpec",
    "Win7ApiViolation",
    "Win7CheckResult",
    "Win7DllError",
    "Win7EmbedRuntime",
    "Win7ScanError",
    "Win7ScanReport",
    "build_all_shims",
    "build_shim",
    "check_win7_imports",
    "download_win7_embed",
    "enforce_win7_loaders",
    "ensure_all_shims",
    "ensure_win7_dll",
    "extract_win7_dll",
    "inject_win7_shims",
    "is_shim_missing",
    "is_win7_runtime",
    "iter_pe_files",
    "main",
    "needs_win7_dll",
    "patch_bytes",
    "patch_dist_win7",
    "patch_file",
    "render_win7_report",
    "scan_dist_win7",
    "win7_dll_name",
    "win7_zip_cache_name",
    "win7_zip_url",
    "write_win7_report",
]
