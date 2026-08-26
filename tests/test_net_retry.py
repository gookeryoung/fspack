"""Downloader 重试逻辑与 RuntimeDownloader hash 校验测试.

覆盖 iter-126 新增的健壮性机制：

- :class:`fspack.packaging.net.Downloader` 指数退避重试：
  - 可重试错误（URLError/socket.timeout/HTTPError 502/503/504/读阶段连接中断）
    首次 + 2 次重试后成功返回结果
  - 不可重试错误（HTTPError 404/403）立即失败
  - 达到上限后抛出原始异常（reraise=True），并清理半成品 dest
- :class:`fspack.packaging.runtime.RuntimeDownloader` sha256 校验：
  - 下载后 hash 匹配返回路径
  - 下载后 hash 不匹配删除文件抛 :class:`EmbedError`
  - 缓存命中但 hash 不匹配删除重下
  - 缓存命中且 hash 匹配直接复用
  - 下载失败清理半成品归档再抛 :class:`EmbedError`
- :func:`fspack.packaging.net._is_retryable_network_error` 错误分类单元测试
- :func:`fspack.packaging.net._retry_wait_seconds` 指数退避等待时间单元测试

测试通过 ``monkeypatch.setattr("time.sleep", ...)`` 跳过实际 sleep，
避免指数退避等待（约 1s/2s）导致测试耗时。
"""

from __future__ import annotations

import email.message
import hashlib
import io
import socket
import ssl
import urllib.error
from pathlib import Path
from urllib.request import Request

import pytest

from fspack.config import MirrorConfig
from fspack.exceptions import EmbedError
from fspack.packaging.net import (
    _MAX_ATTEMPTS,
    _RETRY_INITIAL_WAIT,
    _RETRY_MAX_WAIT,
    _RETRYABLE_HTTP_STATUS,
    Downloader,
    _is_retryable_network_error,
    _retry_wait_seconds,
)
from fspack.packaging.runtime import (
    EmbedRuntime,
    _sha256_file,
    download_embed,
    download_standalone,
)
from fspack.progress import StageRecorder
from tests._stubs import FakeResp


def _make_http_error(code: int, msg: str = "Error") -> urllib.error.HTTPError:
    """构造 HTTPError 实例用于测试.

    集中处理 ``hdrs``/``fp`` 参数的类型要求（``email.message.Message`` 与文件对象），
    避免各测试重复构造与类型抑制注释。
    """
    return urllib.error.HTTPError(
        url="https://x/test",
        code=code,
        msg=msg,
        hdrs=email.message.Message(),
        fp=io.BytesIO(b""),
    )


@pytest.fixture(autouse=True)
def _patch_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """跳过重试等待，避免测试耗时（首次 + 2 次重试退避约 1s/2s）."""
    monkeypatch.setattr("time.sleep", lambda _: None)


# ---- 错误分类单元测试 ----


class TestIsRetryableNetworkError:
    """``_is_retryable_network_error`` 错误分类."""

    def test_url_error_is_retryable(self) -> None:
        """URLError（连接拒绝/DNS 失败/超时）可重试."""
        exc = urllib.error.URLError("connection refused")
        assert _is_retryable_network_error(exc) is True

    def test_socket_timeout_is_retryable(self) -> None:
        """socket.timeout（读超时）可重试."""
        exc = socket.timeout("read timed out")
        assert _is_retryable_network_error(exc) is True

    def test_connection_reset_error_is_retryable(self) -> None:
        """ConnectionResetError（分块读阶段连接被重置）可重试."""
        assert _is_retryable_network_error(ConnectionResetError("connection reset by peer")) is True

    def test_remote_disconnected_is_retryable(self) -> None:
        """http.client.RemoteDisconnected（服务端提前断开连接）可重试."""
        import http.client

        exc = http.client.RemoteDisconnected("Remote end closed connection without response")
        assert _is_retryable_network_error(exc) is True

    def test_incomplete_read_is_retryable(self) -> None:
        """http.client.IncompleteRead（响应体未读完即断开）可重试."""
        import http.client

        exc = http.client.IncompleteRead(b"partial", expected=1024)
        assert _is_retryable_network_error(exc) is True

    @pytest.mark.parametrize("code", [502, 503, 504])
    def test_http_5xx_server_error_is_retryable(self, code: int) -> None:
        """HTTP 502/503/504 服务端临时错误可重试."""
        exc = _make_http_error(code, "Server Error")
        assert _is_retryable_network_error(exc) is True

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 429])
    def test_http_4xx_client_error_not_retryable(self, code: int) -> None:
        """HTTP 4xx 客户端错误不可重试（重试无意义）."""
        exc = _make_http_error(code, "Client Error")
        assert _is_retryable_network_error(exc) is False

    def test_generic_oserror_not_retryable(self) -> None:
        """普通 OSError（如磁盘满）不在可重试列表."""
        exc = OSError("disk full")
        assert _is_retryable_network_error(exc) is False

    def test_value_error_not_retryable(self) -> None:
        """非网络异常不可重试."""
        assert _is_retryable_network_error(ValueError("boom")) is False

    def test_retryable_http_status_constant(self) -> None:
        """``_RETRYABLE_HTTP_STATUS`` 仅含 502/503/504."""
        assert frozenset({502, 503, 504}) == _RETRYABLE_HTTP_STATUS


# ---- 指数退避等待时间单元测试 ----


class TestRetryWaitSeconds:
    """``_retry_wait_seconds`` 指数退避 + 全抖动等待时间."""

    @pytest.mark.parametrize(
        ("failed_attempts", "upper"),
        [
            (1, _RETRY_INITIAL_WAIT),
            (2, _RETRY_INITIAL_WAIT * 2),
            (3, _RETRY_INITIAL_WAIT * 4),
        ],
    )
    def test_wait_within_exponential_upper_bound(self, failed_attempts: int, upper: float) -> None:
        """等待时间在 [0, min(initial * 2**(n-1), max)) 区间内（全抖动）."""
        wait = _retry_wait_seconds(failed_attempts)
        assert 0 <= wait < upper

    def test_wait_capped_at_max(self) -> None:
        """失败次数很多时等待上限被封顶为 _RETRY_MAX_WAIT."""
        wait = _retry_wait_seconds(100)
        assert 0 <= wait < _RETRY_MAX_WAIT + 1e-9
        assert wait <= _RETRY_MAX_WAIT

    def test_wait_is_randomized(self) -> None:
        """多次采样值不全相同（全抖动生效）."""
        samples = {_retry_wait_seconds(1) for _ in range(20)}
        assert len(samples) > 1

    def test_max_attempts_constant(self) -> None:
        """``_MAX_ATTEMPTS`` 为 3（首次 + 2 次重试）."""
        assert _MAX_ATTEMPTS == 3


# ---- Downloader 重试集成测试 ----


class TestDownloaderRetry:
    """``Downloader.download`` 重试行为."""

    @pytest.mark.slow
    def test_success_on_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """前 2 次 URLError，第 3 次成功，返回字节数."""
        calls: list[int] = []

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
            calls.append(len(calls) + 1)
            if len(calls) < 3:
                raise urllib.error.URLError("connection refused")
            return FakeResp(b"recovered data")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "out" / "file.zip"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/test.zip", dest, label="测试重试")
        assert written == len(b"recovered data")
        assert dest.read_bytes() == b"recovered data"
        assert len(calls) == 3

    @pytest.mark.slow
    def test_retry_exhausted_reraises_original(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """达到 3 次上限后抛出原始 URLError（reraise=True，非 RetryError）."""
        calls: list[int] = []

        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
            calls.append(len(calls) + 1)
            raise urllib.error.URLError("persistent failure")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(urllib.error.URLError, match="persistent failure"):
            downloader.download("https://x/d", tmp_path / "f.zip")
        assert len(calls) == 3

    @pytest.mark.slow
    def test_retry_exhausted_cleans_dest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """重试耗尽失败后清理半成品 dest，避免残缺文件污染缓存."""
        calls: list[int] = []

        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
            calls.append(len(calls) + 1)
            raise urllib.error.URLError("persistent failure")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "f.zip"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(urllib.error.URLError, match="persistent failure"):
            downloader.download("https://x/d", dest)
        assert len(calls) == 3
        assert not dest.exists()

    @pytest.mark.slow
    def test_cleanup_failure_does_not_mask_original(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """dest 清理自身失败（OSError）仅记 warning，仍抛出原始下载异常."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout, **kw: (_ for _ in ()).throw(urllib.error.URLError("persistent failure")),
        )

        def raise_unlink(self: Path, missing_ok: bool = False) -> None:
            raise OSError("unlink denied")

        monkeypatch.setattr(Path, "unlink", raise_unlink)
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(urllib.error.URLError, match="persistent failure"):
            downloader.download("https://x/d", tmp_path / "f.zip")

    @pytest.mark.slow
    def test_read_stage_connection_reset_retried(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """分块读阶段 ConnectionResetError 触发重试，恢复后下载成功."""
        calls: list[int] = []

        class _ResetResp:
            """read 阶段抛 ConnectionResetError 的响应桩，模拟弱网连接被重置."""

            headers: dict[str, str] = {"Content-Length": "16"}

            def read(self, n: int = -1) -> bytes:
                raise ConnectionResetError("connection reset by peer")

            def __enter__(self) -> _ResetResp:
                return self

            def __exit__(self, *a: object) -> bool:
                return False

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> object:
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return _ResetResp()
            return FakeResp(b"recovered data")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "f.zip"
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/flaky", dest)
        assert written == len(b"recovered data")
        assert dest.read_bytes() == b"recovered data"
        assert len(calls) == 2

    def test_http_404_not_retried(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 404 立即失败，不重试."""
        calls: list[int] = []

        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
            calls.append(len(calls) + 1)
            raise _make_http_error(404, "Not Found")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            downloader.download("https://x/missing", tmp_path / "f.zip")
        assert exc_info.value.code == 404
        assert len(calls) == 1

    @pytest.mark.slow
    def test_http_503_retried_then_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 503 重试 3 次后失败，抛出原始 HTTPError."""
        calls: list[int] = []

        def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
            calls.append(len(calls) + 1)
            raise _make_http_error(503, "Service Unavailable")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            downloader.download("https://x/busy", tmp_path / "f.zip")
        assert exc_info.value.code == 503
        assert len(calls) == 3

    @pytest.mark.slow
    def test_http_503_then_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 503 一次后恢复，重试成功."""
        calls: list[int] = []

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise _make_http_error(503, "Service Unavailable")
            return FakeResp(b"ok after flap")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/flap", tmp_path / "f.zip")
        assert written == len(b"ok after flap")
        assert len(calls) == 2

    @pytest.mark.slow
    def test_socket_timeout_retried(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """socket.timeout（读超时）触发重试."""
        calls: list[int] = []

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
            calls.append(len(calls) + 1)
            if len(calls) < 2:
                raise socket.timeout("read timed out")
            return FakeResp(b"recovered")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/slow", tmp_path / "f.zip")
        assert written == len(b"recovered")
        assert len(calls) == 2

    @pytest.mark.slow
    def test_retry_overwrites_partial_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """重试时 dest 以 wb 模式重新打开，覆盖上次部分写入."""
        calls: list[int] = []

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise urllib.error.URLError("transient")
            return FakeResp(b"full content")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        dest = tmp_path / "f.zip"
        dest.write_bytes(b"partial garbage from failed attempt")
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/d", dest)
        assert written == len(b"full content")
        assert dest.read_bytes() == b"full content"

    @pytest.mark.slow
    def test_stage_receives_bytes_after_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """重试成功后 stage.add_bytes 收到最终字节数."""
        calls: list[int] = []

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
            calls.append(len(calls) + 1)
            if len(calls) < 2:
                raise urllib.error.URLError("transient")
            return FakeResp(b"abc" * 100)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        rec = StageRecorder("download")
        downloader = Downloader(ssl_ctx=ssl.create_default_context())
        written = downloader.download("https://x/d", tmp_path / "f.zip", stage=rec)
        assert rec._bytes == written == 300


# ---- sha256 校验测试 ----


class TestSha256File:
    """``_sha256_file`` 辅助函数."""

    def test_known_hash(self, tmp_path: Path) -> None:
        """已知内容 sha256 与 hashlib 一致."""
        data = b"hello world"
        path = tmp_path / "f.bin"
        path.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert _sha256_file(path) == expected

    def test_large_file_chunked(self, tmp_path: Path) -> None:
        """大文件（>64KB 单块）分块读取结果与一次性一致."""
        data = b"x" * (200 * 1024)  # 200KB，触发多次分块
        path = tmp_path / "big.bin"
        path.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert _sha256_file(path) == expected

    def test_empty_file(self, tmp_path: Path) -> None:
        """空文件 sha256 与标准库一致."""
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        assert _sha256_file(path) == hashlib.sha256(b"").hexdigest()


# ---- RuntimeDownloader hash 校验集成测试 ----


class TestRuntimeDownloaderHashCheck:
    """``RuntimeDownloader.download`` 的 ``expected_hash`` 参数."""

    def test_download_hash_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig) -> None:
        """下载后 hash 匹配，返回路径."""
        data = b"ZIPDATA"
        expected_hash = hashlib.sha256(data).hexdigest()

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(data))
        path = download_embed("3.11.9", mirror, tmp_path / "cache", expected_hash=expected_hash)
        assert path.read_bytes() == data

    def test_download_hash_mismatch_raises_and_deletes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
    ) -> None:
        """下载后 hash 不匹配，抛 EmbedError 并删除已下载文件."""
        data = b"ZIPDATA"

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(data))
        with pytest.raises(EmbedError, match="sha256 校验失败"):
            download_embed("3.11.9", mirror, tmp_path / "cache", expected_hash="0" * 64)
        # 已下载文件应被删除，避免缓存损坏归档
        cache_file = tmp_path / "cache" / "python-3.11.9-embed-amd64.zip"
        assert not cache_file.exists()

    def test_cache_hit_hash_match_reuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
    ) -> None:
        """缓存命中且 hash 匹配，直接复用不下载."""
        data = b"cached data"
        expected_hash = hashlib.sha256(data).hexdigest()
        cache = tmp_path / "cache"
        cache.mkdir()
        cache_file = cache / "python-3.11.9-embed-amd64.zip"
        cache_file.write_bytes(data)

        # urlopen 不应被调用
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: pytest.fail("不应下载"))

        path = download_embed("3.11.9", mirror, cache, expected_hash=expected_hash)
        assert path.read_bytes() == data

    def test_cache_hit_hash_mismatch_redownloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
    ) -> None:
        """缓存命中但 hash 不匹配，删除缓存重新下载（且新下载 hash 匹配）."""
        new_data = b"fresh data"
        expected_hash = hashlib.sha256(new_data).hexdigest()
        cache = tmp_path / "cache"
        cache.mkdir()
        cache_file = cache / "python-3.11.9-embed-amd64.zip"
        cache_file.write_bytes(b"stale corrupted data")

        calls: list[int] = []

        def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
            calls.append(len(calls) + 1)
            return FakeResp(new_data)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        path = download_embed("3.11.9", mirror, cache, expected_hash=expected_hash)
        assert path.read_bytes() == new_data
        assert len(calls) == 1

    def test_cache_hit_hash_mismatch_redownload_also_bad_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
    ) -> None:
        """缓存 hash 不匹配重下，新下载 hash 仍不匹配，抛 EmbedError."""
        cache = tmp_path / "cache"
        cache.mkdir()
        cache_file = cache / "python-3.11.9-embed-amd64.zip"
        cache_file.write_bytes(b"stale")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(b"also bad"))
        with pytest.raises(EmbedError, match="sha256 校验失败"):
            download_embed("3.11.9", mirror, cache, expected_hash="0" * 64)
        assert not cache_file.exists()

    def test_no_hash_check_skips_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
    ) -> None:
        """expected_hash=None 时跳过 hash 校验（向后兼容）."""
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(b"any data"))
        path = download_embed("3.11.9", mirror, tmp_path / "cache")
        assert path.read_bytes() == b"any data"

    def test_standalone_hash_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """download_standalone 同样支持 expected_hash 校验."""
        data = b"TARDATA"
        expected_hash = hashlib.sha256(data).hexdigest()

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(data))
        path = download_standalone("3.11.9", "20260718", tmp_path / "cache", expected_hash=expected_hash)
        assert path.read_bytes() == data

    def test_embed_runtime_class_hash_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
    ) -> None:
        """EmbedRuntime.download 类方法直接调用支持 expected_hash."""
        data = b"CLASSDATA"
        expected_hash = hashlib.sha256(data).hexdigest()

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(data))
        path = EmbedRuntime.download("3.11.9", tmp_path / "cache", expected_hash=expected_hash, mirror=mirror)
        assert path.read_bytes() == data

    def test_download_failure_cleans_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
    ) -> None:
        """下载抛 OSError 时转 EmbedError，且半成品归档被清理不污染缓存."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout, **kw: (_ for _ in ()).throw(OSError("disk io error")),
        )
        cache = tmp_path / "cache"
        with pytest.raises(EmbedError, match="下载 embed python 失败"):
            download_embed("3.11.9", mirror, cache)
        assert not (cache / "python-3.11.9-embed-amd64.zip").exists()
