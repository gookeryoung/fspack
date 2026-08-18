"""Runtime 下载基类 + 子类 + ensure 函数式 API.

拆自 :mod:`fspack.packaging.runtime`。本模块：

- 定义抽象基类 :class:`RuntimeDownloader`：封装 ``download → extract`` 流程共性
- 定义两个具体子类：:class:`EmbedRuntime`（Windows embed）与
  :class:`StandaloneRuntime`（Linux/macOS python-build-standalone）
- 提供函数式 API：``download_*`` / ``extract_*`` / ``ensure_*``

``ensure_embed`` / ``ensure_standalone`` 通过 :func:`_R` 延迟从 runtime facade
解析 ``download_embed`` / ``download_standalone`` / ``extract_embed`` /
``extract_standalone`` 函数，确保 ``monkeypatch.setattr("runtime.download_embed", ...)``
等 patch 后的值能被感知（原代码 ensure_* 有意不用类方法直接调用，就是为了
留出测试 patch 拦截点）。
"""

from __future__ import annotations

import abc
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fspack._compat import override
from fspack.config import MirrorConfig, is_offline
from fspack.exceptions import EmbedError
from fspack.packaging.runtime.extract import extract_tar_safe, extract_zip_safe
from fspack.packaging.runtime.urls import (
    _sha256_file,
    embed_dirname,
    embed_zip_name,
    standalone_tarball_name,
    standalone_url,
)

if TYPE_CHECKING:
    from fspack.progress import StageRecorder

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# runtime facade 延迟 dispatch：兼容 monkeypatch.setattr("runtime.download_embed", ...)
# ---------------------------------------------------------------------------
_runtime_mod_holder: list[Any] = [None]


def _R(fn_name: str, fallback_fn: Any) -> Any:
    """从 ``fspack.packaging.runtime`` 取函数属性，取不到时回退 fallback_fn."""
    mod = _runtime_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging import runtime as _runtime_mod

            mod = _runtime_mod
            _runtime_mod_holder[0] = mod
        except ImportError:
            return fallback_fn
    return getattr(mod, fn_name, fallback_fn)


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class RuntimeDownloader(abc.ABC):
    """Python 运行时下载与解压基类.

    子类实现 ``archive_name`` / ``download_url`` / ``marker_path`` / ``extract_archive``
    四个钩子。通用 ``download`` 方法实现缓存检查 + 离线模式 + 进度条下载 +
    sha256 校验；通用 ``extract`` 方法仅 mkdir + 调钩子。
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
        """解压后钩子，默认无操作。EmbedRuntime 覆盖（创建 site-packages 历史职责占位）。"""
        return None  # pragma: no cover

    @classmethod
    def download(
        cls,
        version: str,
        cache_dir: Path,
        *,
        stage: StageRecorder | None = None,
        expected_hash: str | None = None,
        **kwargs: object,
    ) -> Path:
        """下载运行时归档到缓存目录，已存在则复用.

        缓存命中时 ``stage.hit_cache()``；下载进度回写 stage。离线模式下缓存未命中
        立即抛 :class:`EmbedError`。``expected_hash`` 非 None 时下载后校验 sha256
        （hex 小写），不匹配则删除重新下载（缓存命中时仍校验，检测损坏归档）。
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        archive_path = cache_dir / cls.archive_name(version, **kwargs)
        if archive_path.is_file():
            if expected_hash is not None:
                actual = _sha256_file(archive_path)
                if actual != expected_hash:
                    _logger.warning(
                        "%s 缓存 sha256 不匹配（期望 %s，实际 %s），删除重新下载",
                        cls.runtime_label,
                        expected_hash,
                        actual,
                    )
                    archive_path.unlink(missing_ok=True)
                else:
                    _logger.info("%s 已缓存且 hash 匹配: %s", cls.runtime_label, archive_path)
                    if stage is not None:
                        stage.hit_cache()
                    return archive_path
            else:
                _logger.info("%s 已缓存: %s", cls.runtime_label, archive_path)
                if stage is not None:
                    stage.hit_cache()
                return archive_path
        if is_offline():
            raise EmbedError(
                f"离线模式下 {cls.runtime_label} 缓存未命中: {archive_path.name}，"
                f"请预先下载放入 {cache_dir} 或取消 FSPACK_OFFLINE 环境变量"
            )
        url = cls.download_url(version, **kwargs)
        _logger.info("下载 %s: %s", cls.runtime_label, url)
        try:
            from fspack.packaging.net import Downloader

            downloader = Downloader(timeout=cls.download_timeout)
            downloader.download(url, archive_path, stage=stage, label=cls.download_label(version))
        except OSError as e:
            # 下载失败：best-effort 清理半成品归档，避免残缺文件污染缓存目录
            # （Downloader 内部已清理，此处兜底覆盖被 patch/其他实现绕过的场景）。
            # 清理自身失败仅记 warning，不掩盖原异常。
            try:
                archive_path.unlink(missing_ok=True)
            except OSError as unlink_err:  # pragma: no cover - 清理失败极罕见
                _logger.warning("清理下载失败的半成品归档失败 %s: %s", archive_path, unlink_err)
            raise EmbedError(f"下载 {cls.runtime_label} 失败: {url} -> {e}") from e
        if expected_hash is not None:
            actual = _sha256_file(archive_path)
            if actual != expected_hash:
                archive_path.unlink(missing_ok=True)
                raise EmbedError(
                    f"{cls.runtime_label} sha256 校验失败: 期望 {expected_hash}，实际 {actual}，"
                    f"已下载文件可能被篡改或 URL 指向错误版本"
                )
        return archive_path

    @classmethod
    def extract(cls, archive_path: Path, runtime_dir: Path) -> None:
        """解压运行时归档到 runtime_dir。"""
        runtime_dir.mkdir(parents=True, exist_ok=True)
        cls.extract_archive(archive_path, runtime_dir)


# ---------------------------------------------------------------------------
# 子类
# ---------------------------------------------------------------------------


class EmbedRuntime(RuntimeDownloader):
    """Windows embed python 下载器。"""

    download_timeout = 180
    runtime_label = "embed python"

    @classmethod
    @override
    def archive_name(cls, version: str, **kwargs: object) -> str:  # noqa: ARG003
        return embed_zip_name(version)

    @classmethod
    @override
    def download_url(cls, version: str, **kwargs: object) -> str:
        mirror = kwargs["mirror"]
        assert isinstance(mirror, MirrorConfig)
        return mirror.embed_url(version)

    @classmethod
    @override
    def marker_path(cls, runtime_dir: Path, version: str) -> Path:
        return runtime_dir / f"{embed_dirname(version)}.dll"

    @classmethod
    @override
    def extract_archive(cls, archive_path: Path, runtime_dir: Path) -> None:
        extract_zip_safe(archive_path, runtime_dir, label="embed zip")

    @classmethod
    @override
    def post_extract(cls, runtime_dir: Path, version: str) -> None:  # noqa: ARG003
        """解压后钩子（site-packages 已移至 dist 层级，无操作）."""
        return None


class StandaloneRuntime(RuntimeDownloader):
    """python-build-standalone 下载器（Linux 与 macOS）。

    macOS 通过 ``macos_arch`` kwarg 区分 x86_64 与 arm64。
    """

    download_timeout = 300
    runtime_label = "python-build-standalone"

    @classmethod
    @override
    def archive_name(cls, version: str, **kwargs: object) -> str:
        release_tag = kwargs["release_tag"]
        assert isinstance(release_tag, str)
        macos_arch = kwargs.get("macos_arch")
        assert isinstance(macos_arch, str) or macos_arch is None
        return standalone_tarball_name(version, release_tag, macos_arch=macos_arch)

    @classmethod
    @override
    def download_url(cls, version: str, **kwargs: object) -> str:
        release_tag = kwargs["release_tag"]
        assert isinstance(release_tag, str)
        macos_arch = kwargs.get("macos_arch")
        assert isinstance(macos_arch, str) or macos_arch is None
        return standalone_url(version, release_tag, macos_arch=macos_arch)

    @classmethod
    @override
    def marker_path(cls, runtime_dir: Path, version: str) -> Path:
        major, minor = version.split(".")[:2]
        return runtime_dir / "python" / "bin" / f"python{major}.{minor}"

    @classmethod
    @override
    def extract_archive(cls, archive_path: Path, runtime_dir: Path) -> None:
        extract_tar_safe(archive_path, runtime_dir, label="python-build-standalone tarball")


# ---------------------------------------------------------------------------
# 函数式 API（委托给类；ensure_* 通过 _R 解析 download_*/extract_* 便于测试 patch）
# ---------------------------------------------------------------------------


def download_embed(
    version: str,
    mirror: MirrorConfig,
    cache_dir: Path,
    *,
    stage: StageRecorder | None = None,
    expected_hash: str | None = None,
) -> Path:
    """从镜像下载 embed zip 到缓存目录，已存在则直接复用."""
    return EmbedRuntime.download(version, cache_dir, stage=stage, expected_hash=expected_hash, mirror=mirror)


def extract_embed(zip_path: Path, runtime_dir: Path) -> None:
    """解压 embed zip 到 runtime_dir。"""
    EmbedRuntime.extract(zip_path, runtime_dir)


def ensure_embed(  # noqa: PLR0913
    version: str,
    mirror: MirrorConfig,
    cache_dir: Path,
    runtime_dir: Path,
    *,
    stage: StageRecorder | None = None,
    expected_hash: str | None = None,
) -> Path:
    """确保 runtime_dir 内有可用 embed python，返回 runtime_dir.

    通过 :func:`_R` 延迟解析 ``download_embed`` / ``extract_embed``：测试 patch
    ``runtime.download_embed`` 时，此处会感知 patch 后的实现。
    """
    dll_marker = EmbedRuntime.marker_path(runtime_dir, version)
    if dll_marker.is_file():
        _logger.info("embed python 已就绪: %s", runtime_dir)
        if stage is not None:
            stage.hit_cache()
    else:
        download_embed_dispatch = _R("download_embed", download_embed)
        extract_embed_dispatch = _R("extract_embed", extract_embed)
        zip_path = download_embed_dispatch(version, mirror, cache_dir, stage=stage, expected_hash=expected_hash)
        extract_embed_dispatch(zip_path, runtime_dir)
    EmbedRuntime.post_extract(runtime_dir, version)
    return runtime_dir


def download_standalone(  # noqa: PLR0913
    version: str,
    release_tag: str,
    cache_dir: Path,
    *,
    stage: StageRecorder | None = None,
    macos_arch: str | None = None,
    expected_hash: str | None = None,
) -> Path:
    """下载 python-build-standalone tar.gz 到缓存目录，已存在则复用。"""
    return StandaloneRuntime.download(
        version,
        cache_dir,
        stage=stage,
        expected_hash=expected_hash,
        release_tag=release_tag,
        macos_arch=macos_arch,
    )


def extract_standalone(tar_path: Path, runtime_dir: Path) -> None:
    """解压 tar.gz 到 runtime_dir，解压后 runtime_dir/python/ 为 Python 根目录。"""
    StandaloneRuntime.extract(tar_path, runtime_dir)


def ensure_standalone(  # noqa: PLR0913
    version: str,
    release_tag: str,
    cache_dir: Path,
    runtime_dir: Path,
    *,
    stage: StageRecorder | None = None,
    macos_arch: str | None = None,
    expected_hash: str | None = None,
) -> Path:
    """确保 runtime_dir 内有可用 python-build-standalone，返回 runtime_dir.

    通过 :func:`_R` 延迟解析 ``download_standalone`` / ``extract_standalone``。
    """
    python_bin = StandaloneRuntime.marker_path(runtime_dir, version)
    if python_bin.is_file():
        _logger.info("python-build-standalone 已就绪: %s", runtime_dir)
        if stage is not None:
            stage.hit_cache()
    else:
        download_standalone_dispatch = _R("download_standalone", download_standalone)
        extract_standalone_dispatch = _R("extract_standalone", extract_standalone)
        tar_path = download_standalone_dispatch(
            version, release_tag, cache_dir, stage=stage, macos_arch=macos_arch, expected_hash=expected_hash
        )
        extract_standalone_dispatch(tar_path, runtime_dir)
    return runtime_dir
