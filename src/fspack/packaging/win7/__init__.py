"""Win7 兼容性 facade：check / dll / scan 子模块重导出.

子模块（按职责拆分）：

- :mod:`fspack.packaging.win7.check`：PE 导入表 Win8+ API 黑名单静态检查
  （CLI：``python -m fspack.packaging.win7.check``）
- :mod:`fspack.packaging.win7.dll`：Win7 重编译版 python3XX.dll 清单驱动
  下载与双重校验（sha256 + 导入表）
- :mod:`fspack.packaging.win7.scan`：dist 产物 Win7 兼容门禁（loader exe
  硬门禁 + dist 全量扫描报告）
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
    needs_win7_dll,
    win7_dll_name,
    win7_zip_cache_name,
    win7_zip_url,
)
from fspack.packaging.win7.scan import (
    Win7ScanError,
    Win7ScanReport,
    enforce_win7_loaders,
    iter_pe_files,
    render_win7_report,
    scan_dist_win7,
    write_win7_report,
)

__all__ = [
    "WIN7_EMBED_SHA256",
    "WIN7_SHIM_DLL_PATH",
    "PeParseError",
    "Win7ApiViolation",
    "Win7CheckResult",
    "Win7DllError",
    "Win7EmbedRuntime",
    "Win7ScanError",
    "Win7ScanReport",
    "check_win7_imports",
    "download_win7_embed",
    "enforce_win7_loaders",
    "ensure_win7_dll",
    "extract_win7_dll",
    "iter_pe_files",
    "main",
    "needs_win7_dll",
    "render_win7_report",
    "scan_dist_win7",
    "win7_dll_name",
    "win7_zip_cache_name",
    "win7_zip_url",
    "write_win7_report",
]
