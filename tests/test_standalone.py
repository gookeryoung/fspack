"""python-build-standalone 下载/解压测试。."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from fspack.exceptions import EmbedError
from fspack.standalone import (
    STANDALONE_BASE_URL,
    STANDALONE_RELEASE_TAG,
    download_standalone,
    ensure_standalone,
    extract_standalone,
    standalone_tarball_name,
    standalone_url,
)


def test_standalone_tarball_name() -> None:
    assert (
        standalone_tarball_name("3.11.9", "20241016")
        == "cpython-3.11.9+20241016-x86_64-unknown-linux-gnu-install_only.tar.gz"
    )


def test_standalone_url() -> None:
    url = standalone_url("3.11.9", "20241016")
    assert url.startswith(STANDALONE_BASE_URL)
    assert "20241016" in url
    assert "3.11.9" in url


def test_download_standalone_cache_hit(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    name = standalone_tarball_name("3.11.9", STANDALONE_RELEASE_TAG)
    (cache / name).write_bytes(b"old")
    path = download_standalone("3.11.9", STANDALONE_RELEASE_TAG, cache)
    assert path.read_bytes() == b"old"


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def test_download_standalone_fetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(req: object, timeout: int) -> _FakeResp:
        captured["url"] = req.full_url  # type: ignore[union-attr]
        return _FakeResp(b"TARDATA")

    monkeypatch.setattr("fspack.standalone.urllib.request.urlopen", fake_urlopen)
    path = download_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache")
    assert path.read_bytes() == b"TARDATA"
    assert "20241016" in captured["url"]


def test_download_standalone_network_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: int) -> object:
        raise OSError("boom")

    monkeypatch.setattr("fspack.standalone.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(EmbedError, match="下载 python-build-standalone 失败"):
        download_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache")


def _make_tar(path: Path, members: list[tuple[str, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_extract_standalone(tmp_path: Path) -> None:
    tar = tmp_path / "s.tar.gz"
    _make_tar(tar, [("python/bin/python3.11", b"#!/bin/sh"), ("python/lib/libpython3.11.so", b"so")])
    runtime = tmp_path / "runtime"
    extract_standalone(tar, runtime)
    assert (runtime / "python" / "bin" / "python3.11").is_file()
    assert (runtime / "python" / "lib" / "libpython3.11.so").is_file()


def test_extract_standalone_bad_tar(tmp_path: Path) -> None:
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tar")
    with pytest.raises(EmbedError, match="tarball 损坏"):
        extract_standalone(bad, tmp_path / "runtime")


def test_ensure_standalone_skips_when_python_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_text("")
    called = {"download": False}
    monkeypatch.setattr("fspack.standalone.download_standalone", lambda *a, **k: called.__setitem__("download", True))
    ensure_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache", runtime)
    assert not called["download"]


def test_ensure_standalone_downloads_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    tar_path = tmp_path / "fake.tar.gz"
    _make_tar(tar_path, [("python/bin/python3.11", b"")])
    monkeypatch.setattr("fspack.standalone.download_standalone", lambda *a, **k: tar_path)
    ensure_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache", runtime)
    assert (runtime / "python" / "bin" / "python3.11").is_file()
