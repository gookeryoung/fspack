"""net.py SSL 上下文测试."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from fspack.net import create_ssl_context


def test_create_ssl_context_default() -> None:
    """默认创建 SSL 上下文，应含 CA 证书."""
    ctx = create_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_create_ssl_context_with_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    ctx = create_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert captured.get("cafile") == str(ca_file)


def test_create_ssl_context_env_nonexistent(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSL_CERT_FILE 指向不存在的文件时回退到默认."""
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    ctx = create_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
