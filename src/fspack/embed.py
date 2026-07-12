"""embed python 下载、解压与 _pth 配置。."""

from __future__ import annotations

import logging
import urllib.request
import zipfile
from pathlib import Path

from fspack.config import MirrorConfig
from fspack.exceptions import EmbedError

__all__ = [
    "download_embed",
    "embed_dirname",
    "embed_zip_name",
    "ensure_embed",
    "extract_embed",
    "write_pth",
]

_logger = logging.getLogger(__name__)


def embed_dirname(version: str) -> str:
    """返回形如 python311 的版本前缀。."""
    major, minor = version.split(".")[:2]
    return f"python{major}{minor}"


def embed_zip_name(version: str) -> str:
    """返回 embed zip 文件名。."""
    return f"python-{version}-embed-amd64.zip"


def download_embed(version: str, mirror: MirrorConfig, cache_dir: Path) -> Path:
    """从镜像下载 embed zip 到缓存目录，已存在则直接复用。."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / embed_zip_name(version)
    if zip_path.is_file():
        _logger.info("embed python 已缓存: %s", zip_path)
        return zip_path
    url = mirror.embed_url(version)
    _logger.info("下载 embed python: %s", url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fspack"})
        with urllib.request.urlopen(req, timeout=180) as resp, zip_path.open("wb") as f:
            f.write(resp.read())
    except OSError as e:
        raise EmbedError(f"下载 embed python 失败: {url} -> {e}") from e
    return zip_path


def extract_embed(zip_path: Path, runtime_dir: Path) -> None:
    """解压 embed zip 到 runtime_dir。."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(runtime_dir)
    except zipfile.BadZipFile as e:
        raise EmbedError(f"embed zip 损坏: {zip_path}") from e


def write_pth(dist_dir: Path, version: str, extra_paths: tuple[str, ...] = ()) -> Path:
    """在 dist 根目录生成 python3X._pth，控制 sys.path。

    _pth 与 loader.exe 同目录（dist/），路径相对 dist 解析：
    runtime\\python311.zip 标准库、runtime\\Lib\\site-packages 第三方依赖、src 用户源码。
    """
    pyxy = embed_dirname(version)
    pth = dist_dir / f"{pyxy}._pth"
    lines = [
        f"runtime\\{pyxy}.zip",
        "runtime",
        "runtime\\Lib\\site-packages",
        "src",
        *extra_paths,
        "import site",
    ]
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pth


def ensure_embed(
    version: str,
    mirror: MirrorConfig,
    cache_dir: Path,
    runtime_dir: Path,
) -> Path:
    """确保 runtime_dir 内有可用 embed python，返回 runtime_dir。

    重复构建时若 python3X.dll 已存在则跳过下载与解压，但仍保证 site-packages 目录就绪。
    """
    dll_marker = runtime_dir / f"{embed_dirname(version)}.dll"
    if dll_marker.is_file():
        _logger.info("embed python 已就绪: %s", runtime_dir)
    else:
        zip_path = download_embed(version, mirror, cache_dir)
        extract_embed(zip_path, runtime_dir)
    site_packages = runtime_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    return runtime_dir
