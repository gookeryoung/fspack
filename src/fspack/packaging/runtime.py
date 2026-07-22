"""Python 运行时下载与解压：embed python（Windows）与 python-build-standalone（Linux）。

提取 :class:`RuntimeDownloader` 基类封装 ``download → extract → ensure`` 三步流程的共性：

- 缓存检查（命中调 ``stage.hit_cache``）
- 进度条下载（``download_with_progress``）
- 归档解压（zipfile/tarfile）
- marker 检查（重复构建跳过）
- 解压后钩子（``post_extract``，用于 embed 的 site-packages 创建）

子类通过实现钩子方法定制差异：归档文件名、下载 URL、marker 文件、解压格式等。
"""

from __future__ import annotations

import abc
import logging
import sys
import tarfile
import zipfile
from pathlib import Path

from fspack.config import MirrorConfig
from fspack.exceptions import EmbedError
from fspack.packaging.net import Downloader
from fspack.progress import StageRecorder

if sys.version_info >= (3, 12):  # pragma: no cover
    from typing import override
else:
    from typing_extensions import override  # type: ignore[import-not-found,unused-ignore]

__all__ = [
    "STANDALONE_BASE_URL",
    "STANDALONE_RELEASE_TAG",
    "EmbedRuntime",
    "RuntimeDownloader",
    "StandaloneRuntime",
    "download_embed",
    "download_standalone",
    "embed_dirname",
    "embed_zip_name",
    "ensure_embed",
    "ensure_standalone",
    "extract_embed",
    "extract_standalone",
    "standalone_tarball_name",
    "standalone_url",
    "write_pth",
]

_logger = logging.getLogger(__name__)

STANDALONE_BASE_URL = "https://github.com/indygreg/python-build-standalone/releases/download"
STANDALONE_RELEASE_TAG = "20241016"


# ---- 辅助函数（子类与函数式 API 共用）----


def embed_dirname(version: str) -> str:
    """返回形如 python311 的版本前缀。"""
    major, minor = version.split(".")[:2]
    return f"python{major}{minor}"


def embed_zip_name(version: str) -> str:
    """返回 embed zip 文件名。"""
    return f"python-{version}-embed-amd64.zip"


def standalone_tarball_name(version: str, release_tag: str) -> str:
    """返回 python-build-standalone tarball 文件名。"""
    return f"cpython-{version}+{release_tag}-x86_64-unknown-linux-gnu-install_only.tar.gz"


def standalone_url(version: str, release_tag: str) -> str:
    """返回完整下载 URL。"""
    return f"{STANDALONE_BASE_URL}/{release_tag}/{standalone_tarball_name(version, release_tag)}"


# ---- 基类 ----


class RuntimeDownloader(abc.ABC):
    """Python 运行时下载与解压基类。

    封装 ``download → extract → ensure`` 三步流程的共性。子类通过实现钩子方法
    定制归档格式、URL、marker 检查等差异。

    通用流程：
    1. :meth:`download` —— 缓存检查 → 命中调 ``stage.hit_cache`` →
       未命中 ``download_with_progress``
    2. :meth:`extract` —— ``mkdir runtime_dir`` → 调 :meth:`extract_archive` 钩子
    3. :meth:`ensure` —— marker 检查 → 命中跳过 → 未命中 download+extract →
       :meth:`post_extract`

    类属性：
    - ``download_timeout``：下载超时秒数
    - ``runtime_label``：运行时名称，用于日志与错误消息
    """

    download_timeout: int = 180
    runtime_label: str = "运行时"

    @classmethod
    @abc.abstractmethod
    def archive_name(cls, version: str, **kwargs: object) -> str:
        """返回运行时归档文件名。"""

    @classmethod
    @abc.abstractmethod
    def download_url(cls, version: str, **kwargs: object) -> str:
        """返回下载 URL。"""

    @classmethod
    @abc.abstractmethod
    def marker_path(cls, runtime_dir: Path, version: str) -> Path:
        """返回就绪检查的 marker 文件路径。"""

    @classmethod
    @abc.abstractmethod
    def extract_archive(cls, archive_path: Path, runtime_dir: Path) -> None:
        """解压归档到 runtime_dir，损坏时抛 :class:`EmbedError`。"""

    @classmethod
    def download_label(cls, version: str) -> str:
        """进度条标签，默认 ``"{runtime_label} {version}"``。"""
        return f"{cls.runtime_label} {version}"

    @classmethod
    def post_extract(cls, runtime_dir: Path, version: str) -> None:  # noqa: ARG003
        """解压后额外步骤，默认无操作。子类可覆盖（如 embed 创建 site-packages）。"""
        return None

    @classmethod
    def download(
        cls,
        version: str,
        cache_dir: Path,
        *,
        stage: StageRecorder | None = None,
        **kwargs: object,
    ) -> Path:
        """下载运行时归档到缓存目录，已存在则复用。

        缓存命中时调 ``stage.hit_cache()``；下载时用 ``download_with_progress`` 显示
        实时进度条，并通过 ``stage.add_bytes`` 回写字节数。
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        archive_path = cache_dir / cls.archive_name(version, **kwargs)
        if archive_path.is_file():
            _logger.info("%s 已缓存: %s", cls.runtime_label, archive_path)
            if stage is not None:
                stage.hit_cache()
            return archive_path
        url = cls.download_url(version, **kwargs)
        _logger.info("下载 %s: %s", cls.runtime_label, url)
        try:
            downloader = Downloader(timeout=cls.download_timeout)
            downloader.download(url, archive_path, stage=stage, label=cls.download_label(version))
        except OSError as e:
            raise EmbedError(f"下载 {cls.runtime_label} 失败: {url} -> {e}") from e
        return archive_path

    @classmethod
    def extract(cls, archive_path: Path, runtime_dir: Path) -> None:
        """解压运行时归档到 runtime_dir。"""
        runtime_dir.mkdir(parents=True, exist_ok=True)
        cls.extract_archive(archive_path, runtime_dir)

    @classmethod
    def ensure(
        cls,
        version: str,
        cache_dir: Path,
        runtime_dir: Path,
        *,
        stage: StageRecorder | None = None,
        **kwargs: object,
    ) -> Path:
        """确保 runtime_dir 内有可用运行时，返回 runtime_dir。

        重复构建时若 marker 文件已存在则跳过下载与解压，但仍执行 :meth:`post_extract`。
        """
        marker = cls.marker_path(runtime_dir, version)
        if marker.exists():
            _logger.info("%s 已就绪: %s", cls.runtime_label, runtime_dir)
            if stage is not None:
                stage.hit_cache()
        else:
            archive_path = cls.download(version, cache_dir, stage=stage, **kwargs)
            cls.extract(archive_path, runtime_dir)
        cls.post_extract(runtime_dir, version)
        return runtime_dir


# ---- 子类 ----


class EmbedRuntime(RuntimeDownloader):
    """Windows embed python 下载器。"""

    download_timeout = 180
    runtime_label = "embed python"

    @classmethod
    @override
    def archive_name(cls, version: str, **kwargs: object) -> str:
        """返回 embed zip 文件名。"""
        return embed_zip_name(version)

    @classmethod
    @override
    def download_url(cls, version: str, **kwargs: object) -> str:
        """返回镜像下载 URL。"""
        mirror = kwargs["mirror"]
        assert isinstance(mirror, MirrorConfig)
        return mirror.embed_url(version)

    @classmethod
    @override
    def marker_path(cls, runtime_dir: Path, version: str) -> Path:
        """返回 python3X.dll marker 路径。"""
        return runtime_dir / f"{embed_dirname(version)}.dll"

    @classmethod
    @override
    def extract_archive(cls, archive_path: Path, runtime_dir: Path) -> None:
        """解压 embed zip 到 runtime_dir。"""
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(runtime_dir)
        except zipfile.BadZipFile as e:
            raise EmbedError(f"embed zip 损坏: {archive_path}") from e

    @classmethod
    @override
    def post_extract(cls, runtime_dir: Path, version: str) -> None:
        """创建 site-packages 目录。"""
        site_packages = runtime_dir / "Lib" / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)


class StandaloneRuntime(RuntimeDownloader):
    """python-build-standalone 下载器（Linux）。"""

    download_timeout = 300
    runtime_label = "python-build-standalone"

    @classmethod
    @override
    def archive_name(cls, version: str, **kwargs: object) -> str:
        """返回 tarball 文件名。"""
        release_tag = kwargs["release_tag"]
        assert isinstance(release_tag, str)
        return standalone_tarball_name(version, release_tag)

    @classmethod
    @override
    def download_url(cls, version: str, **kwargs: object) -> str:
        """返回 GitHub 下载 URL。"""
        release_tag = kwargs["release_tag"]
        assert isinstance(release_tag, str)
        return standalone_url(version, release_tag)

    @classmethod
    @override
    def marker_path(cls, runtime_dir: Path, version: str) -> Path:
        """返回 python/bin/pythonX.Y marker 路径。"""
        major, minor = version.split(".")[:2]
        return runtime_dir / "python" / "bin" / f"python{major}.{minor}"

    @classmethod
    @override
    def extract_archive(cls, archive_path: Path, runtime_dir: Path) -> None:
        """解压 tar.gz 到 runtime_dir。"""
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(runtime_dir)
        except (tarfile.TarError, OSError) as e:
            raise EmbedError(f"python-build-standalone tarball 损坏: {archive_path}") from e


# ---- 函数式 API（委托给类，保持向后兼容）----
# ensure_* 函数内部调用 download_* 函数（而非类方法），便于测试 monkeypatch 拦截。


def download_embed(
    version: str,
    mirror: MirrorConfig,
    cache_dir: Path,
    *,
    stage: StageRecorder | None = None,
) -> Path:
    """从镜像下载 embed zip 到缓存目录，已存在则直接复用。"""
    return EmbedRuntime.download(version, cache_dir, stage=stage, mirror=mirror)


def extract_embed(zip_path: Path, runtime_dir: Path) -> None:
    """解压 embed zip 到 runtime_dir。"""
    EmbedRuntime.extract(zip_path, runtime_dir)


def ensure_embed(
    version: str,
    mirror: MirrorConfig,
    cache_dir: Path,
    runtime_dir: Path,
    *,
    stage: StageRecorder | None = None,
) -> Path:
    """确保 runtime_dir 内有可用 embed python，返回 runtime_dir。

    重复构建时若 python3X.dll 已存在则跳过下载与解压，但仍保证 site-packages 目录就绪。
    """
    dll_marker = EmbedRuntime.marker_path(runtime_dir, version)
    if dll_marker.is_file():
        _logger.info("embed python 已就绪: %s", runtime_dir)
        if stage is not None:
            stage.hit_cache()
    else:
        zip_path = download_embed(version, mirror, cache_dir, stage=stage)
        extract_embed(zip_path, runtime_dir)
    EmbedRuntime.post_extract(runtime_dir, version)
    return runtime_dir


def download_standalone(
    version: str,
    release_tag: str,
    cache_dir: Path,
    *,
    stage: StageRecorder | None = None,
) -> Path:
    """下载 python-build-standalone tar.gz 到缓存目录，已存在则复用。"""
    return StandaloneRuntime.download(version, cache_dir, stage=stage, release_tag=release_tag)


def extract_standalone(tar_path: Path, runtime_dir: Path) -> None:
    """解压 tar.gz 到 runtime_dir，解压后 runtime_dir/python/ 为 Python 根目录。"""
    StandaloneRuntime.extract(tar_path, runtime_dir)


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
    python_bin = StandaloneRuntime.marker_path(runtime_dir, version)
    if python_bin.is_file():
        _logger.info("python-build-standalone 已就绪: %s", runtime_dir)
        if stage is not None:
            stage.hit_cache()
    else:
        tar_path = download_standalone(version, release_tag, cache_dir, stage=stage)
        extract_standalone(tar_path, runtime_dir)
    return runtime_dir


def write_pth(dist_dir: Path, version: str, extra_paths: tuple[str, ...] = ()) -> Path:
    """在 runtime 目录生成 python3X._pth，控制 sys.path。

    _pth 必须与 python311.dll 同目录（dist/runtime/），路径相对 runtime 解析：
    python311.zip 标准库、Lib\\site-packages 第三方依赖、..\\src 用户源码。
    """
    pyxy = embed_dirname(version)
    runtime_dir = dist_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pth = runtime_dir / f"{pyxy}._pth"
    lines = [
        f"{pyxy}.zip",
        ".",
        "Lib\\site-packages",
        "..\\src",
        *extra_paths,
        "import site",
    ]
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pth
