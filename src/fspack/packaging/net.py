"""网络下载：SSL 上下文与 HTTP 进度下载.

:class:`Downloader` 整合 ``create_ssl_context`` 与 HTTP 下载两个职责：

- SSL 上下文创建（``SSL_CERT_FILE`` 环境变量 → certifi CA bundle → 系统默认 CA）
- HTTP 下载（``urllib.request`` + ``rich.progress`` 实时进度条）

供 :class:`fspack.packaging.runtime.RuntimeDownloader` 使用。
"""

from __future__ import annotations

import os
import ssl
import urllib.request
from pathlib import Path

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from fspack.console import console
from fspack.progress import StageRecorder

__all__ = ["Downloader"]

_BLOCK_SIZE = 64 * 1024


class Downloader:
    """HTTP 下载器，封装 SSL 上下文与进度条下载.

    用法::

        downloader = Downloader(timeout=180)
        written = downloader.download(url, dest, stage=stage, label="embed python")

    SSL 上下文默认通过 :meth:`create_ssl_context` 创建，也可经 ``ssl_ctx`` 参数
    注入（测试场景）。
    """

    def __init__(
        self,
        *,
        timeout: int = 180,
        ssl_ctx: ssl.SSLContext | None = None,
    ) -> None:
        self._timeout = timeout
        self._ssl_ctx = ssl_ctx or self.create_ssl_context()

    @staticmethod
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

    def download(
        self,
        url: str,
        dest: Path,
        *,
        stage: StageRecorder | None = None,
        label: str = "",
    ) -> int:
        """从 ``url`` 下载到 ``dest``，显示实时进度条，返回字节数。

        使用 ``urllib.request.urlopen`` + 分块读写 + ``rich.progress.Progress`` 显示下载进度。
        下载完成后若提供 ``stage``，调 ``stage.add_bytes`` 累加。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "fspack"})
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console.rich,
            transient=True,
        )
        with progress, urllib.request.urlopen(req, timeout=self._timeout, context=self._ssl_ctx) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            task_id = progress.add_task(label or url.rsplit("/", 1)[-1], total=total or None)
            written = 0
            with dest.open("wb") as f:
                while True:
                    chunk = resp.read(_BLOCK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    progress.update(task_id, advance=len(chunk))
        if stage:
            stage.add_bytes(written)
        return written
