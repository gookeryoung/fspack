"""Python 运行时下载与解压：embed python（Windows）与 python-build-standalone（Linux）。

提取 :class:`RuntimeDownloader` 基类封装 ``download → extract → ensure`` 三步流程的共性：

- 缓存检查（命中调 ``stage.hit_cache``）
- 进度条下载（:class:`fspack.packaging.net.Downloader`）
- 归档解压（zipfile/tarfile）
- marker 检查（重复构建跳过）
- 解压后钩子（``post_extract``，用于 embed 的 site-packages 创建）

子类通过实现钩子方法定制差异：归档文件名、下载 URL、marker 文件、解压格式等。
"""

from __future__ import annotations

import abc
import logging
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from fspack._compat import override
from fspack.config import MirrorConfig, is_offline
from fspack.exceptions import EmbedError

if TYPE_CHECKING:
    # StageRecorder 仅用于类型注解；顶部不导入 fspack.progress 避免连锁触发
    # rich.progress 加载（``import fspack.builder`` 热路径不下载运行时）。
    from fspack.progress import StageRecorder

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

# 阿里云 GitHub 镜像加速国内下载，路径与 GitHub releases 同构。
STANDALONE_BASE_URL = "https://mirrors.aliyun.com/github/releases/astral-sh/python-build-standalone"
# 20260718 release 包含 3.13.14；每个 release tag 只含该时间点的最新补丁版本，
# 故 KNOWN_STANDALONE_VERSIONS 中的版本号必须与本 tag 实际提供的版本号匹配。
STANDALONE_RELEASE_TAG = "20260718"


# ---- 辅助函数（子类与函数式 API 共用）----


def embed_dirname(version: str) -> str:
    """返回形如 python311 的版本前缀。"""
    major, minor = version.split(".")[:2]
    return f"python{major}{minor}"


def embed_zip_name(version: str) -> str:
    """返回 embed zip 文件名。"""
    return f"python-{version}-embed-amd64.zip"


def standalone_tarball_name(
    version: str,
    release_tag: str,
    *,
    windows: bool = False,
    macos_arch: str | None = None,
) -> str:
    """返回 python-build-standalone tarball 文件名.

    Args:
        version: Python 完整版本号（如 ``3.10.20``）。
        release_tag: astral-sh release tag（如 ``20260718``）。
        windows: True 返回 Windows (msvc) 平台 tarball，False 返回 Linux (gnu)。
        macos_arch: 非 None 时返回 macOS tarball，值为 ``"x86_64"`` 或 ``"arm64"``。
            macOS 与 Linux/Windows 互斥，``macos_arch`` 非 None 时忽略 ``windows``。
    """
    if macos_arch is not None:
        platform = f"{macos_arch}-apple-darwin"
    elif windows:
        platform = "x86_64-pc-windows-msvc"
    else:
        platform = "x86_64-unknown-linux-gnu"
    return f"cpython-{version}+{release_tag}-{platform}-install_only.tar.gz"


def standalone_url(
    version: str,
    release_tag: str,
    *,
    windows: bool = False,
    macos_arch: str | None = None,
) -> str:
    """返回完整下载 URL。"""
    return (
        f"{STANDALONE_BASE_URL}/{release_tag}/"
        f"{standalone_tarball_name(version, release_tag, windows=windows, macos_arch=macos_arch)}"
    )


def _sha256_file(path: Path, *, chunk_size: int = 64 * 1024) -> str:
    """计算文件 sha256 十六进制摘要（小写），用于下载归档完整性校验.

    分块读取避免大文件（如 python-build-standalone ~30MB）一次性占用内存。
    """
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _safe_unlink_archive(archive_path: Path, label: str) -> None:
    """删除损坏的归档文件，OSError 仅告警不抛.

    用于 ``extract_archive`` 解压失败时清理损坏归档，避免下次构建缓存命中
    损坏文件反复解压失败。删除失败不中断流程（仍抛 EmbedError 让上层处理）。
    """
    try:
        archive_path.unlink()
    except OSError as e:
        _logger.warning("删除损坏的 %s 失败: %s: %s", label, archive_path, e)


def _validate_tar_member(member: tarfile.TarInfo) -> None:
    """PEP 706 ``data`` filter 等价检查（用于 Python 3.11 及以下手动实现）.

    拒绝：绝对路径（Unix ``/`` 或 Windows 盘符 ``C:``）、路径穿越（``..`` 段）、
    符号链接、硬链接、设备文件（字符/块设备）。

    Python 3.12+ 使用内置 ``tarfile.data_filter``，本函数仅在低版本生效。
    tarball 来自网络下载（镜像站），预检防止恶意条目逃逸 ``runtime_dir``。
    """
    name = member.name.replace("\\", "/")
    if name.startswith("/"):
        raise EmbedError(f"python-build-standalone tarball 含绝对路径条目: {member.name}")
    if len(name) >= 2 and name[1] == ":":
        raise EmbedError(f"python-build-standalone tarball 含盘符条目: {member.name}")
    if ".." in name.split("/"):
        raise EmbedError(f"python-build-standalone tarball 含路径穿越条目: {member.name}")
    if member.issym() or member.islnk():
        raise EmbedError(f"python-build-standalone tarball 含链接条目: {member.name}")
    if member.isdev():
        raise EmbedError(f"python-build-standalone tarball 含设备文件条目: {member.name}")


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    """校验 zip 条目路径安全：拒绝绝对路径、路径穿越（``..``）、符号链接.

    zipfile 无内置安全过滤（与 tarfile 3.12+ ``filter='data'`` 不同），
    解压前必须手动校验，防止恶意 zip 路径穿越逃逸 ``runtime_dir``。
    embed zip 来自镜像下载，需防范镜像被篡改注入恶意条目。
    """
    name = info.filename.replace("\\", "/")
    if name.startswith("/"):
        raise EmbedError(f"embed zip 含绝对路径条目: {info.filename}")
    if len(name) >= 2 and name[1] == ":":
        raise EmbedError(f"embed zip 含盘符条目: {info.filename}")
    if ".." in name.split("/"):
        raise EmbedError(f"embed zip 含路径穿越条目: {info.filename}")
    # Unix 模式位在 external_attr 高 16 位（MS-DOS 兼容设计），0 表示未设置
    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise EmbedError(f"embed zip 含符号链接条目: {info.filename}")


# ---- 基类 ----


class RuntimeDownloader(abc.ABC):
    """Python 运行时下载与解压基类。

    封装 ``download → extract → ensure`` 三步流程的共性。子类通过实现钩子方法
    定制归档格式、URL、marker 检查等差异。

    通用流程：
    1. :meth:`download` —— 缓存检查 → 命中调 ``stage.hit_cache`` →
       未命中调 :meth:`Downloader.download`
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
        return None  # pragma: no cover # 默认钩子，所有子类均覆盖或 ensure_* 函数不调用

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
        """下载运行时归档到缓存目录，已存在则复用。

        缓存命中时调 ``stage.hit_cache()``；下载时用 :class:`Downloader` 显示
        实时进度条，并通过 ``stage.add_bytes`` 回写字节数。

        离线模式（``FSPACK_OFFLINE=1``）下缓存未命中时立即抛 :class:`EmbedError`，
        不尝试网络请求避免超时卡死。错误信息包含缺失文件名与缓存路径，
        便于用户预下载归档放入缓存。

        ``expected_hash`` 非 None 时下载后校验归档 sha256（hex 小写），不匹配则
        删除已下载文件并抛 :class:`EmbedError`，避免缓存损坏归档。校验失败不重试：
        hash 不匹配通常是源被篡改或 URL 指向错误版本，重试无意义。缓存命中时
        仍校验（检测缓存损坏或被替换的归档）。
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
            # 延迟导入：避免 ``import fspack.builder`` 触发 net 模块加载
            from fspack.packaging.net import Downloader

            downloader = Downloader(timeout=cls.download_timeout)
            downloader.download(url, archive_path, stage=stage, label=cls.download_label(version))
        except OSError as e:
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

        .. note::

            当前生产路径由模块级 ``ensure_embed``/``ensure_standalone`` 函数承担
            （便于测试 monkeypatch ``download_*`` 函数），本方法保留作为基类模板
            供未来子类复用。
        """
        marker = cls.marker_path(runtime_dir, version)  # pragma: no cover # 模板方法，当前未使用
        if marker.exists():  # pragma: no cover
            _logger.info("%s 已就绪: %s", cls.runtime_label, runtime_dir)  # pragma: no cover
            if stage is not None:  # pragma: no cover
                stage.hit_cache()  # pragma: no cover
        else:  # pragma: no cover
            archive_path = cls.download(
                version,
                cache_dir,
                stage=stage,
                **kwargs,  # pyrefly: ignore[bad-argument-type]
            )  # pragma: no cover
            cls.extract(archive_path, runtime_dir)  # pragma: no cover
        cls.post_extract(runtime_dir, version)  # pragma: no cover
        return runtime_dir  # pragma: no cover


# ---- 子类 ----


class EmbedRuntime(RuntimeDownloader):
    """Windows embed python 下载器。"""

    download_timeout = 180
    runtime_label = "embed python"

    @classmethod
    @override
    def archive_name(cls, version: str, **kwargs: object) -> str:  # noqa: ARG003 # 抽象方法签名要求
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
        """解压 embed zip 到 runtime_dir，损坏或含恶意条目时删除归档避免缓存污染."""
        try:
            with zipfile.ZipFile(archive_path) as zf:
                # zipfile 无内置安全过滤，解压前手动预检每个条目路径与类型，
                # 拒绝绝对路径/路径穿越/符号链接（防恶意 zip 逃逸 runtime_dir）。
                for info in zf.infolist():
                    _validate_zip_member(info)
                zf.extractall(runtime_dir)
        except zipfile.BadZipFile as e:
            # 删除损坏的归档：下次构建会重新下载，避免反复尝试解压损坏文件
            _safe_unlink_archive(archive_path, "embed zip")
            raise EmbedError(f"embed zip 损坏: {archive_path}") from e
        except EmbedError:
            # 预检发现的恶意条目：归档可能被篡改，删除避免下次构建再次使用
            _safe_unlink_archive(archive_path, "embed zip")
            raise

    @classmethod
    @override
    def post_extract(cls, runtime_dir: Path, version: str) -> None:  # noqa: ARG003 # 抽象方法签名要求，embed 不需要 version
        """创建 site-packages 目录。"""
        site_packages = runtime_dir / "Lib" / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)


class StandaloneRuntime(RuntimeDownloader):
    """python-build-standalone 下载器（Linux 与 macOS）。

    macOS 通过 ``macos_arch`` kwarg 区分 x86_64 与 arm64 架构，tarball 平台段
    为 ``{arch}-apple-darwin``；Linux 默认 ``x86_64-unknown-linux-gnu``。
    """

    download_timeout = 300
    runtime_label = "python-build-standalone"

    @classmethod
    @override
    def archive_name(cls, version: str, **kwargs: object) -> str:
        """返回 tarball 文件名。"""
        release_tag = kwargs["release_tag"]
        assert isinstance(release_tag, str)
        macos_arch = kwargs.get("macos_arch")
        assert isinstance(macos_arch, str) or macos_arch is None
        return standalone_tarball_name(version, release_tag, macos_arch=macos_arch)

    @classmethod
    @override
    def download_url(cls, version: str, **kwargs: object) -> str:
        """返回 GitHub 下载 URL。"""
        release_tag = kwargs["release_tag"]
        assert isinstance(release_tag, str)
        macos_arch = kwargs.get("macos_arch")
        assert isinstance(macos_arch, str) or macos_arch is None
        return standalone_url(version, release_tag, macos_arch=macos_arch)

    @classmethod
    @override
    def marker_path(cls, runtime_dir: Path, version: str) -> Path:
        """返回 python/bin/pythonX.Y marker 路径。"""
        major, minor = version.split(".")[:2]
        return runtime_dir / "python" / "bin" / f"python{major}.{minor}"

    @classmethod
    @override
    def extract_archive(cls, archive_path: Path, runtime_dir: Path) -> None:
        """解压 tar.gz 到 runtime_dir，损坏或含恶意条目时删除归档避免缓存污染."""
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                # Python 3.12+ 显式指定 data 过滤器（PEP 706）：消除 DeprecationWarning，
                # 并阻止绝对路径/路径穿越等恶意条目（tarball 来自网络下载）。
                # 低版本无 filter 参数，手动预检每个 member 等价实现 data filter。
                if sys.version_info >= (3, 12):
                    tf.extractall(runtime_dir, filter="data")  # pragma: no cover # 测试环境为 3.11，3.12+ 分支无法覆盖
                else:
                    for member in tf.getmembers():
                        _validate_tar_member(member)
                    tf.extractall(runtime_dir)
        except (tarfile.TarError, OSError) as e:
            # 删除损坏的归档：下次构建会重新下载，避免反复尝试解压损坏文件。
            # OSError 与 TarError 一并处理：tarfile.open 遇到非 gzip 文件抛 OSError
            # （"seeking back is not allowed"）或 ReadError，统一视为损坏。
            _safe_unlink_archive(archive_path, "python-build-standalone tarball")
            raise EmbedError(f"python-build-standalone tarball 损坏: {archive_path}") from e
        except EmbedError:
            # 预检发现的恶意条目：归档可能被篡改，删除避免下次构建再次使用
            _safe_unlink_archive(archive_path, "python-build-standalone tarball")
            raise


# ---- 函数式 API（委托给类，保持向后兼容）----
# ensure_* 函数内部调用 download_* 函数（而非类方法），便于测试 monkeypatch 拦截。


def download_embed(
    version: str,
    mirror: MirrorConfig,
    cache_dir: Path,
    *,
    stage: StageRecorder | None = None,
    expected_hash: str | None = None,
) -> Path:
    """从镜像下载 embed zip 到缓存目录，已存在则直接复用.

    ``expected_hash`` 非 None 时下载后校验 sha256（见 :meth:`RuntimeDownloader.download`）。
    """
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
    """确保 runtime_dir 内有可用 embed python，返回 runtime_dir。

    重复构建时若 python3X.dll 已存在则跳过下载与解压，但仍保证 site-packages 目录就绪。
    """
    dll_marker = EmbedRuntime.marker_path(runtime_dir, version)
    if dll_marker.is_file():
        _logger.info("embed python 已就绪: %s", runtime_dir)
        if stage is not None:
            stage.hit_cache()
    else:
        zip_path = download_embed(version, mirror, cache_dir, stage=stage, expected_hash=expected_hash)
        extract_embed(zip_path, runtime_dir)
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
    """下载 python-build-standalone tar.gz 到缓存目录，已存在则复用。

    Args:
        macos_arch: macOS 架构（``"x86_64"`` 或 ``"arm64"``），None 表示 Linux。
        expected_hash: 期望 sha256 hex，非 None 时下载后校验。
    """
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
    """确保 runtime_dir 内有可用 python-build-standalone，返回 runtime_dir。

    重复构建时若 runtime/python/bin/python3 已存在则跳过下载与解压。

    Args:
        macos_arch: macOS 架构（``"x86_64"`` 或 ``"arm64"``），None 表示 Linux。
        expected_hash: 期望 sha256 hex，非 None 时下载后校验。
    """
    python_bin = StandaloneRuntime.marker_path(runtime_dir, version)
    if python_bin.is_file():
        _logger.info("python-build-standalone 已就绪: %s", runtime_dir)
        if stage is not None:
            stage.hit_cache()
    else:
        tar_path = download_standalone(
            version, release_tag, cache_dir, stage=stage, macos_arch=macos_arch, expected_hash=expected_hash
        )
        extract_standalone(tar_path, runtime_dir)
    return runtime_dir


def write_pth(
    dist_dir: Path,
    version: str,
    extra_paths: tuple[str, ...] = (),
    *,
    enable_site: bool = True,
) -> Path:
    """在 runtime 目录生成 python3X._pth，控制 sys.path。

    _pth 必须与 python311.dll 同目录（dist/runtime/），路径相对 runtime 解析：
    python311.zip 标准库、Lib\\site-packages 第三方依赖、..\\src 用户源码。

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
        "Lib\\site-packages",
        "..\\src",
        *extra_paths,
    ]
    if enable_site:
        lines.append("import site")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pth
