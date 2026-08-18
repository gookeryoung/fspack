"""Python 运行时 facade：download / extract / urls / trim / pth 子模块重导出.

子模块（按职责拆分）：

- :mod:`fspack.packaging.runtime.urls`：常量 + URL/文件命名辅助 + SHA256
- :mod:`fspack.packaging.runtime.extract`：条目安全校验 + 安全解压函数
- :mod:`fspack.packaging.runtime.download`：基类/子类（RuntimeDownloader、
  EmbedRuntime、StandaloneRuntime）+ ensure/download/extract 函数式 API
- :mod:`fspack.packaging.runtime.trim`：Win7 DLL 注入 + stdlib/standalone/Tcl/Tk 精简
- :mod:`fspack.packaging.runtime.pth`：python3X._pth 生成（sys.path 控制）

测试 patch 点（``monkeypatch.setattr("fspack.packaging.runtime.<name>", ...)``）：

- ``download_embed``：test_runtime L268/L282/L618
- ``download_standalone``：test_runtime L603/L630
- ``_validate_tar_member``、``STANDALONE_RELEASE_TAG``、``STANDALONE_BASE_URL``、
  ``standalone_tarball_name``：直接 import 使用
"""

from __future__ import annotations

from fspack.packaging.runtime.download import (
    EmbedRuntime,
    RuntimeDownloader,
    StandaloneRuntime,
    download_embed,
    download_standalone,
    ensure_embed,
    ensure_standalone,
    extract_embed,
    extract_standalone,
)
from fspack.packaging.runtime.extract import (
    _safe_unlink_archive,
    _validate_tar_member,
    _validate_zip_member,
    extract_tar_safe,
    extract_zip_safe,
)
from fspack.packaging.runtime.pth import write_pth
from fspack.packaging.runtime.trim import (
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
from fspack.packaging.runtime.urls import (
    STANDALONE_BASE_URL,
    STANDALONE_RELEASE_TAG,
    _sha256_file,
    embed_dirname,
    embed_zip_name,
    standalone_tarball_name,
    standalone_url,
)

__all__ = [
    "STANDALONE_BASE_URL",
    "STANDALONE_RELEASE_TAG",
    "_STANDALONE_DEV_BIN_FILES",
    "_STDLIB_TRIM_DIRS",
    "_WIN7_COMPAT_DLL_NAME",
    "EmbedRuntime",
    "RuntimeDownloader",
    "StandaloneRuntime",
    "_inject_win7_compat_dll",
    "_needs_win7_compat_dll",
    "_safe_unlink_archive",
    "_sha256_file",
    "_strip_elf_symbols",
    "_strip_tcl_tk_counted",
    "_trim_standalone_runtime",
    "_trim_stdlib",
    "_validate_tar_member",
    "_validate_zip_member",
    "download_embed",
    "download_standalone",
    "embed_dirname",
    "embed_zip_name",
    "ensure_embed",
    "ensure_standalone",
    "extract_embed",
    "extract_standalone",
    "extract_tar_safe",
    "extract_zip_safe",
    "standalone_tarball_name",
    "standalone_url",
    "write_pth",
]
