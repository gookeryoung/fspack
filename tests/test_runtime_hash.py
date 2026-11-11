"""运行时归档 sha256 清单与校验（embed zip / standalone tarball）."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fspack.config import MirrorConfig
from fspack.config.versions import KNOWN_EMBED_VERSIONS, KNOWN_STANDALONE_VERSIONS
from fspack.exceptions import EmbedError
from fspack.packaging.runtime import (
    STANDALONE_RELEASE_TAG,
    download_embed,
    download_standalone,
    embed_zip_name,
    ensure_embed,
    standalone_release_tag,
    standalone_tarball_name,
)
from fspack.packaging.runtime.hashes import (
    EMBED_SHA256,
    RUNTIME_SHA256,
    SKIP_RUNTIME_HASH_ENV,
    STANDALONE_SHA256,
    expected_runtime_hash,
    runtime_hash_enabled,
    runtime_sha256,
)


def test_manifest_covers_known_embed_versions() -> None:
    """KNOWN_EMBED_VERSIONS 中每个可下载版本都有 embed zip 的 sha256."""
    for version in KNOWN_EMBED_VERSIONS.values():
        if version.endswith("t"):
            # freethreaded 无 python.org embed zip，走 standalone tarball
            continue
        name = embed_zip_name(version)
        assert name in EMBED_SHA256, f"缺少 {name} 的 sha256，升级版本时请同步 hashes.py"
        assert len(EMBED_SHA256[name]) == 64


def test_manifest_covers_known_standalone_versions() -> None:
    """KNOWN_STANDALONE_VERSIONS 在 Linux/Windows/macOS 双架构上都有 sha256.

    macOS 侧直接使用生产中真实传入的值 ``"arm64"``/``"x86_64"``（而非上游资产名
    ``aarch64``），以验证命名归一化后能命中清单。
    """
    for version in KNOWN_STANDALONE_VERSIONS.values():
        names = [
            standalone_tarball_name(version, STANDALONE_RELEASE_TAG),
            standalone_tarball_name(version, STANDALONE_RELEASE_TAG, windows=True),
            standalone_tarball_name(version, STANDALONE_RELEASE_TAG, macos_arch="x86_64"),
            standalone_tarball_name(version, STANDALONE_RELEASE_TAG, macos_arch="arm64"),
        ]
        for name in names:
            assert name in STANDALONE_SHA256, f"缺少 {name} 的 sha256，升级版本/tag 时请同步 hashes.py"
            assert len(STANDALONE_SHA256[name]) == 64


def test_runtime_sha256_lookup() -> None:
    """已知归档返回清单值，未知归档返回 None."""
    name = embed_zip_name("3.11.9")
    assert runtime_sha256(name) == EMBED_SHA256[name] == RUNTIME_SHA256[name]
    assert runtime_sha256("python-9.9.9-embed-amd64.zip") is None


def test_runtime_hash_enabled_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """FSPACK_SKIP_RUNTIME_HASH=1 关闭校验，其他值保持开启."""
    monkeypatch.delenv(SKIP_RUNTIME_HASH_ENV, raising=False)
    assert runtime_hash_enabled() is True
    monkeypatch.setenv(SKIP_RUNTIME_HASH_ENV, "1")
    assert runtime_hash_enabled() is False
    monkeypatch.setenv(SKIP_RUNTIME_HASH_ENV, "0")
    assert runtime_hash_enabled() is True


def test_expected_runtime_hash_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """清单收录时返回期望 sha256."""
    monkeypatch.delenv(SKIP_RUNTIME_HASH_ENV, raising=False)
    name = embed_zip_name("3.11.9")
    assert expected_runtime_hash(name, "embed python 3.11.9") == EMBED_SHA256[name]


def test_expected_runtime_hash_unknown_warns(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """未收录时告警并返回 None（不阻断自定义运行时版本）."""
    monkeypatch.delenv(SKIP_RUNTIME_HASH_ENV, raising=False)
    assert expected_runtime_hash("python-9.9.9-embed-amd64.zip", "embed python 9.9.9") is None
    assert "未收录 sha256 清单" in caplog.text


def test_expected_runtime_hash_skip_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式跳过时不查表也不告警."""
    monkeypatch.setenv(SKIP_RUNTIME_HASH_ENV, "1")
    assert expected_runtime_hash(embed_zip_name("3.11.9"), "embed python 3.11.9") is None


def test_standalone_release_tag_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """release tag 可通过环境变量覆盖，未设置/空值时回退默认常量."""
    monkeypatch.delenv("FSPACK_STANDALONE_RELEASE_TAG", raising=False)
    assert standalone_release_tag() == STANDALONE_RELEASE_TAG
    monkeypatch.setenv("FSPACK_STANDALONE_RELEASE_TAG", "  20250101 ")
    assert standalone_release_tag() == "20250101"
    monkeypatch.setenv("FSPACK_STANDALONE_RELEASE_TAG", "   ")
    assert standalone_release_tag() == STANDALONE_RELEASE_TAG


def test_download_embed_accepts_manifest_hash_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """缓存命中时按清单 sha256 校验通过，直接复用（不联网）."""
    monkeypatch.delenv(SKIP_RUNTIME_HASH_ENV, raising=False)
    data = b"embed-archive-bytes"
    monkeypatch.setitem(RUNTIME_SHA256, embed_zip_name("3.11.9"), hashlib.sha256(data).hexdigest())
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / embed_zip_name("3.11.9")).write_bytes(data)
    assert download_embed("3.11.9", mirror, cache).read_bytes() == data


def test_download_embed_manifest_hash_mismatch_redownload_blocked_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """缓存文件与清单 sha256 不符：删除后离线模式下报错，不静默复用损坏归档."""
    monkeypatch.delenv(SKIP_RUNTIME_HASH_ENV, raising=False)
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setitem(RUNTIME_SHA256, embed_zip_name("3.11.9"), hashlib.sha256(b"expected").hexdigest())
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / embed_zip_name("3.11.9")
    archive.write_bytes(b"corrupted")
    with pytest.raises(EmbedError, match="缓存未命中"):
        download_embed("3.11.9", mirror, cache)
    assert not archive.exists()


def test_ensure_embed_passes_manifest_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """ensure_embed 经 download_embed 走清单校验：哈希不符时重新下载并失败."""
    monkeypatch.delenv(SKIP_RUNTIME_HASH_ENV, raising=False)
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setitem(RUNTIME_SHA256, embed_zip_name("3.11.9"), hashlib.sha256(b"expected").hexdigest())
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / embed_zip_name("3.11.9")).write_bytes(b"corrupted")
    with pytest.raises(EmbedError, match="缓存未命中"):
        ensure_embed("3.11.9", mirror, cache, tmp_path / "runtime")


def test_download_standalone_accepts_manifest_hash_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """standalone tarball 缓存命中时按清单 sha256 校验通过，直接复用."""
    monkeypatch.delenv(SKIP_RUNTIME_HASH_ENV, raising=False)
    data = b"standalone-archive-bytes"
    name = standalone_tarball_name("3.11.15", STANDALONE_RELEASE_TAG)
    monkeypatch.setitem(RUNTIME_SHA256, name, hashlib.sha256(data).hexdigest())
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / name).write_bytes(data)
    assert download_standalone("3.11.15", STANDALONE_RELEASE_TAG, cache).read_bytes() == data


def test_download_standalone_manifest_hash_mismatch_blocked_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """standalone 缓存与清单不符：删除后离线模式报错."""
    monkeypatch.delenv(SKIP_RUNTIME_HASH_ENV, raising=False)
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    name = standalone_tarball_name("3.11.15", STANDALONE_RELEASE_TAG, windows=True)
    monkeypatch.setitem(RUNTIME_SHA256, name, hashlib.sha256(b"expected").hexdigest())
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / name
    archive.write_bytes(b"corrupted")
    with pytest.raises(EmbedError, match="缓存未命中"):
        download_standalone("3.11.15", STANDALONE_RELEASE_TAG, cache, windows=True)
    assert not archive.exists()
