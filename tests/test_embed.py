"""embed python 下载/解压/_pth 测试。."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from fspack.config import MirrorConfig
from fspack.embed import (
    download_embed,
    embed_dirname,
    embed_zip_name,
    ensure_embed,
    extract_embed,
    write_pth,
)
from fspack.exceptions import EmbedError

_MIRROR = MirrorConfig(name="t", python_base="https://x/py", pypi_index="https://x/s")


def test_embed_dirname_and_zipname() -> None:
    assert embed_dirname("3.11.9") == "python311"
    assert embed_zip_name("3.11.9") == "python-3.11.9-embed-amd64.zip"


def test_download_embed_cache_hit(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "python-3.11.9-embed-amd64.zip").write_bytes(b"old")
    path = download_embed("3.11.9", _MIRROR, cache)
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


def test_download_embed_fetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(req: object, timeout: int, **kwargs: object) -> _FakeResp:
        captured["url"] = req.full_url  # type: ignore[union-attr]
        return _FakeResp(b"ZIPDATA")

    monkeypatch.setattr("fspack.embed.urllib.request.urlopen", fake_urlopen)
    path = download_embed("3.11.9", _MIRROR, tmp_path / "cache")
    assert path.read_bytes() == b"ZIPDATA"
    assert captured["url"].endswith("python-3.11.9-embed-amd64.zip")


def test_download_embed_network_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
        raise OSError("boom")

    monkeypatch.setattr("fspack.embed.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(EmbedError, match="下载 embed python 失败"):
        download_embed("3.11.9", _MIRROR, tmp_path / "cache")


def test_extract_embed(tmp_path: Path) -> None:
    zip_path = tmp_path / "e.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("python311.dll", b"dll")
        zf.writestr("python311._pth", "python311.zip\n")
    dest = tmp_path / "runtime"
    extract_embed(zip_path, dest)
    assert (dest / "python311.dll").is_file()
    assert (dest / "python311._pth").read_text().startswith("python311.zip")


def test_extract_embed_bad_zip(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(EmbedError, match="embed zip 损坏"):
        extract_embed(bad, tmp_path / "runtime")


def test_write_pth_content(tmp_path: Path) -> None:
    pth = write_pth(tmp_path, "3.11.9")
    assert pth == tmp_path / "runtime" / "python311._pth"
    content = pth.read_text(encoding="utf-8")
    assert "python311.zip" in content
    assert "Lib\\site-packages" in content
    assert "..\\src" in content
    assert "import site" in content


def test_write_pth_extra_paths(tmp_path: Path) -> None:
    pth = write_pth(tmp_path, "3.11.9", extra_paths=("assets",))
    assert "assets" in pth.read_text()


def test_ensure_embed_skips_when_dll_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python311.dll").write_bytes(b"")
    called = {"download": False}
    monkeypatch.setattr("fspack.embed.download_embed", lambda *a, **k: called.__setitem__("download", True))
    ensure_embed("3.11.9", _MIRROR, tmp_path / "cache", runtime)
    assert not called["download"]
    assert (runtime / "Lib" / "site-packages").is_dir()


def test_ensure_embed_downloads_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    zip_path = tmp_path / "fake.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("python311.dll", b"")
    monkeypatch.setattr("fspack.embed.download_embed", lambda *a, **k: zip_path)
    ensure_embed("3.11.9", _MIRROR, tmp_path / "cache", runtime)
    assert (runtime / "python311.dll").is_file()
    assert (runtime / "Lib" / "site-packages").is_dir()
