"""embed python 下载/解压/_pth 测试."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from fspack.config import MirrorConfig
from fspack.exceptions import EmbedError
from fspack.packaging.runtime import (
    download_embed,
    embed_dirname,
    embed_zip_name,
    ensure_embed,
    extract_embed,
    write_pth,
)
from fspack.progress import StageRecorder

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
    """支持分块 read(n) 的 urlopen 响应 mock."""

    def __init__(self, data: bytes, block_size: int = 64) -> None:
        self._buf = io.BytesIO(data)
        self._block_size = block_size
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._buf.read(self._block_size)
        return self._buf.read(min(n, self._block_size))

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def test_download_embed_fetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(req: object, timeout: int, **kwargs: object) -> _FakeResp:
        captured["url"] = req.full_url  # type: ignore[union-attr]
        return _FakeResp(b"ZIPDATA")

    monkeypatch.setattr("fspack.progress.urllib.request.urlopen", fake_urlopen)
    path = download_embed("3.11.9", _MIRROR, tmp_path / "cache")
    assert path.read_bytes() == b"ZIPDATA"
    assert captured["url"].endswith("python-3.11.9-embed-amd64.zip")


def test_download_embed_network_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
        raise OSError("boom")

    monkeypatch.setattr("fspack.progress.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(EmbedError, match="下载 embed python 失败"):
        download_embed("3.11.9", _MIRROR, tmp_path / "cache")


def test_download_embed_cache_hit_calls_stage(tmp_path: Path) -> None:
    """缓存命中时调 stage.hit_cache()."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "python-3.11.9-embed-amd64.zip").write_bytes(b"old")
    rec = StageRecorder("test")
    download_embed("3.11.9", _MIRROR, cache, stage=rec)
    record = rec._finalize()
    assert record.cache_hit == 1
    assert record.bytes_downloaded == 0


def test_download_embed_fetches_records_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载成功时 stage.add_bytes 被调用."""
    monkeypatch.setattr(
        "fspack.progress.urllib.request.urlopen",
        lambda req, timeout, **kw: _FakeResp(b"ZIPDATA"),
    )
    rec = StageRecorder("test")
    download_embed("3.11.9", _MIRROR, tmp_path / "cache", stage=rec)
    record = rec._finalize()
    assert record.bytes_downloaded == 7  # len("ZIPDATA")
    assert record.cache_hit == 0


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
    monkeypatch.setattr("fspack.packaging.runtime.download_embed", lambda *a, **k: called.__setitem__("download", True))
    ensure_embed("3.11.9", _MIRROR, tmp_path / "cache", runtime)
    assert not called["download"]
    assert (runtime / "Lib" / "site-packages").is_dir()


def test_ensure_embed_downloads_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    zip_path = tmp_path / "fake.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("python311.dll", b"")
    monkeypatch.setattr("fspack.packaging.runtime.download_embed", lambda *a, **k: zip_path)
    ensure_embed("3.11.9", _MIRROR, tmp_path / "cache", runtime)
    assert (runtime / "python311.dll").is_file()
    assert (runtime / "Lib" / "site-packages").is_dir()
