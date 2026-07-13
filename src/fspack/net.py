"""网络下载的 SSL 上下文与公共配置。.

合并 certifi CA bundle 与系统 CA 证书，支持 SSL_CERT_FILE 环境变量覆盖。
用于 embed.py 与 standalone.py 的 HTTPS 下载。
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path

__all__ = ["create_ssl_context"]


def create_ssl_context() -> ssl.SSLContext:
    """创建 SSL 上下文，按优先级合并 CA 证书源。

    优先级：
    1. ``SSL_CERT_FILE`` 环境变量（用户显式指定，如 FastGithub 代理环境）
    2. certifi CA bundle + 系统默认 CA（certifi 更新更及时）
    3. 系统默认 CA
    """
    env_ca = os.environ.get("SSL_CERT_FILE")
    if env_ca and Path(env_ca).is_file():
        return ssl.create_default_context(cafile=env_ca)
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.load_default_certs()
        return ctx
    except ImportError:  # pragma: no cover
        return ssl.create_default_context()
