"""Python 运行时 facade：download / extract / urls 子模块重导出.

拆自原 520 行单文件，子模块：

- :mod:`fspack.packaging.runtime_urls`：常量 + URL/文件命名辅助 + SHA256
- :mod:`fspack.packaging.runtime_extract`：条目安全校验 + 安全解压函数
- :mod:`fspack.packaging.runtime_download`：基类/子类（RuntimeDownloader、
  EmbedRuntime、StandaloneRuntime）+ ensure/download/extract 函数式 API

测试 patch 点（``monkeypatch.setattr("fspack.packaging.runtime.<name>", ...)``）：

- ``download_embed``：test_runtime L268/L282/L618
- ``download_standalone``：test_runtime L603/L630
- ``_validate_tar_member``、``STANDALONE_RELEASE_TAG``、``STANDALONE_BASE_URL``、
  ``standalone_tarball_name``：直接 import 使用
"""

from __future__ import annotations

from pathlib import Path

from fspack.packaging.runtime_download import (
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
from fspack.packaging.runtime_extract import (
    _safe_unlink_archive,
    _validate_tar_member,
    _validate_zip_member,
    extract_tar_safe,
    extract_zip_safe,
)
from fspack.packaging.runtime_urls import (
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
    "EmbedRuntime",
    "RuntimeDownloader",
    "StandaloneRuntime",
    "_safe_unlink_archive",
    "_sha256_file",
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


def write_pth(
    dist_dir: Path,
    version: str,
    extra_paths: tuple[str, ...] = (),
    *,
    enable_site: bool = True,
) -> Path:
    """在 runtime 目录生成 python3X._pth，控制 sys.path.

    _pth 必须与 python311.dll 同目录（dist/runtime/），路径相对 runtime 解析：
    python311.zip 标准库、..\\site-packages 第三方依赖（与 runtime 平级的
    dist/site-packages）、..\\src 用户源码。

    ``enable_site=False`` 时省略 ``import site`` 行，启动时跳过 ``site.py``
    执行（约节省 20-30ms）。wrapper 已显式 ``sys.path.insert`` site-packages，
    故禁用 site.py 不影响第三方依赖发现，但会丢失 ``user site`` 与
    ``.pth`` 文件处理——纯运行时场景无需这些功能。

    参考 rimsort 与 CPython 文档：``site.py`` 主要负责 site-packages 添加、
    ``.pth`` 文件扫描与 ``ENABLE_USER_SITE`` 处理，运行时无需重复执行。
    """
    pyxy = embed_dirname(version)
    runtime_dir = dist_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pth = runtime_dir / f"{pyxy}._pth"
    lines = [
        f"{pyxy}.zip",
        ".",
        "..\\site-packages",
        "..\\src",
        *extra_paths,
    ]
    if enable_site:
        lines.append("import site")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pth
