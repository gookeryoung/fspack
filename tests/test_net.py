"""Downloader SSL 上下文与 HTTP 下载测试."""

from __future__ import annotations

import io
import ssl
from pathlib import Path

import pytest

from fspack.packaging.net import Downloader
from fspack.progress import StageRecorder


class _FakeResp:
    """模拟 urlopen 响应，支持分块 read(n)."""

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


class TestCreateSslContext:
    """Downloader.create_ssl_context CA 证书合并."""

    def test_default_creates_context_with_cert_required(self) -> None:
        """默认创建 SSL 上下文，应含 CA 证书."""
        ctx = Downloader.create_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_env_cert_file_takes_priority(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """SSL_CERT_FILE 环境变量指定自定义 CA bundle 时优先使用."""
        ca_file = tmp_path / "custom-ca.pem"
        ca_file.write_text("placeholder\n")
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))

        captured: dict[str, object] = {}
        real_cd = ssl.create_default_context

        def spy(**kwargs: object) -> ssl.SSLContext:
            captured.update(kwargs)
            return real_cd()

        monkeypatch.setattr(ssl, "create_default_context", spy)
        ctx = Downloader.create_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert captured.get("cafile") == str(ca_file)

    def test_env_nonexistent_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SSL_CERT_FILE 指向不存在的文件时回退到默认."""
        monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
        ctx = Downloader.create_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED


class TestDownloaderDownload:
    """Downloader.download 下载与指标回写."""

    def test_downloads_file_and_returns_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> _FakeResp:
            captured["url"] = req.full_url  # type: ignore[union-attr]
            return _FakeResp(b"hello world data")

        monkeypatch.setattr("fspack.packaging.net.urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "out" / "file.zip"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/test.zip", dest, label="测试下载")
        assert written == len(b"hello world data")
        assert dest.read_bytes() == b"hello world data"
        assert captured["url"] == "https://x/test.zip"

    def test_stage_receives_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "fspack.packaging.net.urllib.request.urlopen",
            lambda req, timeout, **kw: _FakeResp(b"abc" * 100),
        )
        rec = StageRecorder("download")
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/d", tmp_path / "f.zip", stage=rec)
        assert rec._bytes == written
        assert rec._bytes == 300

    def test_no_stage_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "fspack.packaging.net.urllib.request.urlopen",
            lambda req, timeout, **kw: _FakeResp(b"abc"),
        )
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/d", tmp_path / "f.zip")
        assert written == 3

    def test_propagates_network_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
            raise OSError("boom")

        monkeypatch.setattr("fspack.packaging.net.urllib.request.urlopen", fake_urlopen)
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(OSError, match="boom"):
            downloader.download("https://x/d", tmp_path / "f.zip")
