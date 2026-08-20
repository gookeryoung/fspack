"""Runtime 常量、URL/文件名辅助、SHA256 校验.

拆自 :mod:`fspack.packaging.runtime`。本模块无依赖链（仅标准库 + fspack 顶层配置），
可被 download / extract / facade 模块安全 import。
"""

from __future__ import annotations

from pathlib import Path

from fspack.config.versions import _split_t_suffix

# 阿里云 GitHub 镜像加速国内下载，路径与 GitHub releases 同构。
STANDALONE_BASE_URL = "https://mirrors.aliyun.com/github/releases/astral-sh/python-build-standalone"
# 20260718 release 包含 3.13.14；每个 release tag 只含该时间点的最新补丁版本，
# 故 KNOWN_STANDALONE_VERSIONS 中的版本号必须与本 tag 实际提供的版本号匹配。
STANDALONE_RELEASE_TAG = "20260718"


def embed_dirname(version: str) -> str:
    """返回形如 ``python311`` / ``python313t`` 的版本前缀.

    支持自由线程版本（PEP 703/779）：``version`` 末尾 ``t`` 后缀时返回
    ``python313t``（对应 ``python313t.dll`` 文件名，与标准版 ``python313.dll``
    不互通）。剥离 ``t`` 后再 split 取 major.minor，避免 ``int("13t")`` 失败。
    """
    base, is_t = _split_t_suffix(version)
    major, minor = base.split(".")[:2]
    return f"python{major}{minor}{'t' if is_t else ''}"


def embed_zip_name(version: str) -> str:
    """返回 embed zip 文件名.

    仅用于 Windows 标准版（``version`` 末尾无 ``t`` 后缀），如
    ``python-3.13.14-embed-amd64.zip``。

    注意：python.org 官方**不**提供 freethreaded embed zip（即便 ``version``
    含 ``t`` 后缀，此函数仅返回文件名文本，下载会 404）。Windows 自由线程版本
    的运行时下载走 :func:`standalone_tarball_name` 路径（python-build-standalone
    的 ``-freethreaded-install_only`` tarball）。
    """
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
        version: Python 完整版本号（如 ``3.10.20`` 或自由线程 ``3.13.14t``）。
        release_tag: astral-sh release tag（如 ``20260718``）。
        windows: True 返回 Windows (msvc) 平台 tarball，False 返回 Linux (gnu)。
        macos_arch: 非 None 时返回 macOS tarball，值为 ``"x86_64"`` 或 ``"arm64"``。
            macOS 与 Linux/Windows 互斥，``macos_arch`` 非 None 时忽略 ``windows``。

    自由线程版本（``version`` 末尾 ``t`` 后缀）：astral-sh python-build-standalone
    在平台三元组后插入 ``-freethreaded-`` 标识 t 变体（**版本号无 t 后缀**），
    如
    ``cpython-3.13.14+20260718-x86_64-pc-windows-msvc-freethreaded-install_only.tar.gz``。
    标准版无此段，如 ``cpython-3.13.14+20260718-x86_64-pc-windows-msvc-install_only.tar.gz``。
    """
    if macos_arch is not None:
        platform = f"{macos_arch}-apple-darwin"
    elif windows:
        platform = "x86_64-pc-windows-msvc"
    else:
        platform = "x86_64-unknown-linux-gnu"
    # astral-sh tarball 命名：版本号无 t 后缀，freethreaded 作为 build variant
    # 插入 platform 与 install_only 之间（-freethreaded-install_only）
    base, is_t = _split_t_suffix(version)
    variant = "-freethreaded" if is_t else ""
    return f"cpython-{base}+{release_tag}-{platform}{variant}-install_only.tar.gz"


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
    """计算文件 sha256 十六进制摘要（小写），分块读取避免大文件一次性占内存."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
