"""网络下载：SSL 上下文与 HTTP 进度下载（含指数退避重试）.

:class:`Downloader` 整合 ``create_ssl_context`` 与 HTTP 下载两个职责：

- SSL 上下文创建（``SSL_CERT_FILE`` 环境变量 → certifi CA bundle → 系统默认 CA）
- HTTP 下载（``urllib.request`` + ``rich.progress`` 实时进度条）
- 指数退避重试（``tenacity``，首次 + 2 次重试，退避约 1s/2s + 抖动，仅对可重试错误）

供 :class:`fspack.packaging.runtime.RuntimeDownloader` 使用。

重试策略：
- 可重试：``URLError``（连接超时/DNS 失败/拒绝连接）、``socket.timeout``（读超时）、
  ``HTTPError`` 502/503/504（服务端临时错误）、弱网读阶段中断（``ConnectionResetError``、
  ``http.client.RemoteDisconnected``、``http.client.IncompleteRead``）
- 不可重试：``HTTPError`` 4xx（客户端错误，如 404/403，重试无意义）、其他 ``OSError``
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import tenacity

if TYPE_CHECKING:
    # SSLContext / StageRecorder 仅用于类型注解（``from __future__ import
    # annotations`` 使注解不在运行时求值），顶部不导入 ssl / fspack.progress
    # 避免连锁触发 rich.progress 加载。
    from ssl import SSLContext

    from fspack.progress import StageRecorder

__all__ = ["Downloader"]

_logger = logging.getLogger(__name__)

_BLOCK_SIZE = 64 * 1024

# 重试配置：首次 + 2 次重试（共 3 次尝试），指数退避约 1s/2s + 抖动
_MAX_ATTEMPTS = 3
_RETRY_INITIAL_WAIT = 1.0
_RETRY_MAX_WAIT = 4.0
# 可重试 HTTP 状态码：服务端临时错误（网关错误、服务不可用、网关超时）
_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})


def _is_retryable_network_error(exc: BaseException) -> bool:
    """判断异常是否可重试.

    可重试：连接超时/DNS 失败/拒绝连接（``URLError``）、读超时（``socket.timeout``）、
    HTTP 502/503/504（服务端临时错误）、弱网分块读阶段中断（``ConnectionResetError``
    连接被重置、``http.client.RemoteDisconnected`` 服务端提前断开、
    ``http.client.IncompleteRead`` 响应体不完整）。
    不可重试：HTTP 4xx（客户端错误，如 404/403）、其他 ``OSError``（如磁盘满）。

    ``urllib.error`` 与 ``socket`` 在函数内延迟导入，避免顶部触发 ``urllib.request``
    加载（守护测试 :func:`tests.test_cli.test_builder_import_does_not_load_urllib_request`）。
    """
    import http.client
    import socket
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_HTTP_STATUS
    if isinstance(exc, urllib.error.URLError):
        # URLError 含连接拒绝、DNS 失败、超时等，均值得重试
        return True
    # 弱网分块读阶段的连接中断均为瞬时故障，重试可恢复：
    # ConnectionResetError（连接被重置）、RemoteDisconnected（服务端提前断开）、
    # IncompleteRead（响应体未读完即断开）
    if isinstance(exc, (ConnectionResetError, http.client.RemoteDisconnected, http.client.IncompleteRead)):
        return True
    return isinstance(exc, socket.timeout)


def _build_download_retryer() -> tenacity.Retrying:
    """构造下载重试器：首次 + 2 次重试，退避约 1s/2s + 抖动，仅对可重试错误.

    返回 :class:`tenacity.Retrying` 实例，调用方用 ``for attempt in retryer: with attempt:``
    模式包装可能失败的网络操作。``reraise=True`` 确保最终失败时抛出原始异常（而非
    :class:`tenacity.RetryError`），便于调用方按异常类型处理。

    ``before_sleep`` 在每次重试前记录 WARNING 日志（含异常与下次等待时间），便于诊断。
    """
    return tenacity.Retrying(
        stop=tenacity.stop_after_attempt(_MAX_ATTEMPTS),
        wait=tenacity.wait_exponential_jitter(initial=_RETRY_INITIAL_WAIT, max=_RETRY_MAX_WAIT),
        retry=tenacity.retry_if_exception(_is_retryable_network_error),
        reraise=True,
        before_sleep=tenacity.before_sleep_log(_logger, logging.WARNING),  # type: ignore[arg-type]
    )


class Downloader:
    """HTTP 下载器，封装 SSL 上下文、进度条与指数退避重试.

    用法::

        downloader = Downloader(timeout=180)
        written = downloader.download(url, dest, stage=stage, label="embed python")

    SSL 上下文默认通过 :meth:`create_ssl_context` 创建，也可经 ``ssl_ctx`` 参数
    注入（测试场景）。

    下载失败时按 :func:`_is_retryable_network_error` 分类重试：连接超时/DNS 失败/
    读阶段连接中断/HTTP 502/503/504 重试（首次 + 2 次重试，退避约 1s/2s + 抖动），
    HTTP 4xx 等客户端错误立即失败。失败后清理 ``dest`` 半成品文件避免污染缓存。
    """

    def __init__(
        self,
        *,
        timeout: int = 180,
        ssl_ctx: SSLContext | None = None,
    ) -> None:
        self._timeout = timeout
        self._ssl_ctx = ssl_ctx or self.create_ssl_context()

    @staticmethod
    def create_ssl_context() -> SSLContext:
        """创建 SSL 上下文，按优先级合并 CA 证书源。

        优先级：
        1. ``SSL_CERT_FILE`` 环境变量（用户显式指定，如 FastGithub 代理环境）
        2. certifi CA bundle + 系统默认 CA（certifi 更新更及时）
        3. 系统默认 CA
        """
        import os
        import ssl

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

        网络错误按 :func:`_is_retryable_network_error` 分类重试：可重试错误（连接超时、
        DNS 失败、读阶段连接中断、HTTP 502/503/504）重试（首次 + 2 次重试，退避约
        1s/2s + 抖动），不可重试错误（HTTP 4xx、磁盘满）立即失败。重试时 ``dest``
        以 ``wb`` 模式重新打开覆盖上次部分写入，progress 任务重建（transient=True
        不留痕）。最终失败（重试耗尽或不可重试异常 reraise）后 best-effort 清理
        ``dest`` 半成品文件，避免残缺归档污染缓存。

        ``urllib.request`` / ``rich.progress`` / ``fspack.console`` 在方法内延迟导入：
        ``import fspack.builder`` 热路径不触发 urllib.request（省 ~15ms）与
        rich.progress 多 column 类加载（省 ~8ms），仅在实际下载时才加载。
        测试通过 ``monkeypatch.setattr("urllib.request.urlopen", ...)`` 全局 patch
        ``urllib.request.urlopen``，方法内 ``import urllib.request`` 拿到的就是
        被 patch 后的同一模块对象。

        重试等待时间测试通过 ``monkeypatch.setattr("tenacity.nap.sleep", ...)`` 跳过
        实际 sleep，避免测试耗时。
        """
        # 延迟导入：urllib.request + rich.progress 多 column 类 + console 单例。
        # 仅在真正下载时加载，避免 import fspack.builder 热路径触发 ~23ms 重模块加载。
        import urllib.request

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

        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "fspack"})

        retryer = _build_download_retryer()
        written = 0
        # tenacity Retrying 用 ``for attempt in retryer: with attempt:`` 模式：
        # 可重试异常触发下次循环，不可重试异常或达上限后 reraise 抛出原始异常。
        # progress 在循环内创建：每次重试新建 Progress（transient=True 退出时清除显示），
        # 避免重复进入 with 块的状态复用问题。
        try:
            for attempt in retryer:
                with attempt:
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
                        try:
                            total = int(resp.headers.get("Content-Length") or 0)
                        except ValueError:
                            # Content-Length 非数字（部分镜像/代理返回非法值）：按未知
                            # 大小处理（total=0），进度条无 total 仅显示已下载量
                            total = 0
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
        except Exception:
            # 下载失败（重试耗尽或不可重试异常 reraise）：best-effort 清理半成品
            # 文件，避免残缺归档留在缓存目录被下次构建误用。清理自身失败仅记
            # warning，不掩盖原异常。
            try:
                dest.unlink(missing_ok=True)
            except OSError as unlink_err:  # pragma: no cover - 清理失败极罕见
                _logger.warning("清理下载失败的半成品文件失败 %s: %s", dest, unlink_err)
            raise
        if stage:
            stage.add_bytes(written)
        return written
