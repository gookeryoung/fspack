"""Downloader SSL 上下文与 HTTP 下载测试."""

from __future__ import annotations

import http.client
import ssl
from pathlib import Path
from urllib.request import Request

import pytest

from fspack.packaging.net import Downloader
from fspack.progress import StageRecorder
from tests._stubs import FakeResp


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

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
            captured["url"] = req.full_url
            return FakeResp(b"hello world data")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "out" / "file.zip"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/test.zip", dest, label="测试下载")
        assert written == len(b"hello world data")
        assert dest.read_bytes() == b"hello world data"
        assert captured["url"] == "https://x/test.zip"

    def test_stage_receives_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout, **kw: FakeResp(b"abc" * 100),
        )
        rec = StageRecorder("download")
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/d", tmp_path / "f.zip", stage=rec)
        assert rec._bytes == written
        assert rec._bytes == 300

    def test_no_stage_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout, **kw: FakeResp(b"abc"),
        )
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/d", tmp_path / "f.zip")
        assert written == 3

    def test_propagates_network_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
            raise OSError("boom")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(OSError, match="boom"):
            downloader.download("https://x/d", tmp_path / "f.zip")

    def test_content_length_non_numeric_treated_as_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Content-Length 非数字时不抛 ValueError，按未知大小完成下载."""

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
            resp = FakeResp(b"chunked body")
            # 模拟部分镜像/代理返回非法 Content-Length
            resp.headers = {"Content-Length": "not-a-number"}
            return resp

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "f.zip"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/d", dest)
        assert written == len(b"chunked body")
        assert dest.read_bytes() == b"chunked body"

    def test_failed_download_cleans_dest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """不可重试异常失败后清理半成品 dest，避免污染缓存."""

        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
            raise OSError("boom")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "f.zip"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(OSError, match="boom"):
            downloader.download("https://x/d", dest)
        assert not dest.exists()

    def test_truncated_body_raises_incomplete_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """响应体截断（written < Content-Length 且连接正常关闭）重试耗尽后失败.

        弱网/代理截断场景 urllib 读循环可能无异常结束，仅靠字节数对账发现；
        最终失败后不产生 dest（.part 半成品被清理），缓存不被截断文件污染。
        """

        class TruncResp(FakeResp):
            """Content-Length 声明完整长度但只提供一半数据（模拟代理截断）."""

            def __init__(self) -> None:
                super().__init__(b"x" * 100)
                self.headers = {"Content-Length": "200"}

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: TruncResp())
        dest = tmp_path / "f.tar.gz"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(http.client.IncompleteRead):
            downloader.download("https://x/d", dest)
        assert not dest.exists(), "截断文件不应写入 dest"
        assert not (tmp_path / "f.tar.gz.part").exists(), ".part 半成品应被清理"

    def test_atomic_part_rename(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """下载经 .part 临时文件写入，成功后原子 replace 到 dest（不留 .part）."""

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout, **kw: FakeResp(b"full body"),
        )
        dest = tmp_path / "f.zip"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/d", dest)
        assert written == len(b"full body")
        assert dest.read_bytes() == b"full body"
        assert not (tmp_path / "f.zip.part").exists(), "成功后 .part 应被 rename 消费"
