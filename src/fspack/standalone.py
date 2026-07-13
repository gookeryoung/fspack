"""python-build-standalone 便携式 CPython 下载与解压（Linux 运行时）。."""

from __future__ import annotations

import logging
import tarfile
from pathlib import Path

from fspack.exceptions import EmbedError
from fspack.net import create_ssl_context
from fspack.progress import StageRecorder, download_with_progress

__all__ = [
    "STANDALONE_BASE_URL",
    "STANDALONE_RELEASE_TAG",
    "download_standalone",
    "ensure_standalone",
    "extract_standalone",
    "standalone_tarball_name",
    "standalone_url",
]

_logger = logging.getLogger(__name__)
STANDALONE_BASE_URL = "https://github.com/indygreg/python-build-standalone/releases/download"
STANDALONE_RELEASE_TAG = "20241016"


def standalone_tarball_name(version: str, release_tag: str) -> str:
    """返回 python-build-standalone tarball 文件名。."""
    return f"cpython-{version}+{release_tag}-x86_64-unknown-linux-gnu-install_only.tar.gz"


def standalone_url(version: str, release_tag: str) -> str:
    """返回完整下载 URL。."""
    return f"{STANDALONE_BASE_URL}/{release_tag}/{standalone_tarball_name(version, release_tag)}"


def download_standalone(
    version: str,
    release_tag: str,
    cache_dir: Path,
    *,
    stage: StageRecorder | None = None,
) -> Path:
    """下载 python-build-standalone tar.gz 到缓存目录，已存在则复用。

    缓存命中时调 ``stage.hit_cache()``；下载时用 ``download_with_progress`` 显示
    实时进度条，并通过 ``stage.add_bytes`` 回写字节数。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    tar_path = cache_dir / standalone_tarball_name(version, release_tag)
    if tar_path.is_file():
        _logger.info("python-build-standalone 已缓存: %s", tar_path)
        if stage is not None:
            stage.hit_cache()
        return tar_path
    url = standalone_url(version, release_tag)
    _logger.info("下载 python-build-standalone: %s", url)
    try:
        download_with_progress(
            url,
            tar_path,
            ssl_ctx=create_ssl_context(),
            stage=stage,
            timeout=300,
            label=f"python-build-standalone {version}",
        )
    except OSError as e:
        raise EmbedError(f"下载 python-build-standalone 失败: {url} -> {e}") from e
    return tar_path


def extract_standalone(tar_path: Path, runtime_dir: Path) -> None:
    """解压 tar.gz 到 runtime_dir，解压后 runtime_dir/python/ 为 Python 根目录。."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(runtime_dir)
    except (tarfile.TarError, OSError) as e:
        raise EmbedError(f"python-build-standalone tarball 损坏: {tar_path}") from e


def ensure_standalone(
    version: str,
    release_tag: str,
    cache_dir: Path,
    runtime_dir: Path,
    *,
    stage: StageRecorder | None = None,
) -> Path:
    """确保 runtime_dir 内有可用 python-build-standalone，返回 runtime_dir。

    重复构建时若 runtime/python/bin/python3 已存在则跳过下载与解压。
    """
    major, minor = version.split(".")[:2]
    python_bin = runtime_dir / "python" / "bin" / f"python{major}.{minor}"
    if python_bin.is_file():
        _logger.info("python-build-standalone 已就绪: %s", runtime_dir)
        if stage is not None:
            stage.hit_cache()
        return runtime_dir
    tar_path = download_standalone(version, release_tag, cache_dir, stage=stage)
    extract_standalone(tar_path, runtime_dir)
    return runtime_dir
