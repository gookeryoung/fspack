"""测试共享桩与守卫函数.

集中存放跨多个测试文件重复定义的桩类与守卫函数，减少冗余：

- :class:`CompletedStub`：``subprocess.run`` 成功返回值桩（替换 5 处 ``_Completed``）
- :class:`FakeResp`：``urlopen`` 响应桩，支持分块 ``read(n)``（替换 2 处 ``_FakeResp``）
- :func:`fail_urlopen`：离线模式守卫，断言不应触发网络请求

仅提取重复 ≥ 2 处且完全一致的符号；带额外字段的嵌套桩（如 ``test_runner.py``
中带 ``args`` 的 ``_Completed``）保留本地定义。``_make_info``/``_make_tar``/
``_copy_example`` 等仅 1-2 处且签名差异较大，不提取。
"""

from __future__ import annotations

import io

__all__ = [
    "CompletedStub",
    "FakeResp",
    "fail_urlopen",
]


class CompletedStub:
    """``subprocess.run`` 成功返回值桩.

    提供 ``returncode``/``stdout``/``stderr`` 三属性，用于 mock ``subprocess.run``
    的成功路径。带额外字段或自定义行为的测试应本地定义专属桩。
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeResp:
    """``urlopen`` 响应桩，支持分块 ``read(n)``.

    模拟 ``http.client.HTTPResponse`` 的 ``read(n)`` 行为：``n=-1`` 时返回
    ``block_size`` 字节，``n>0`` 时返回 ``min(n, block_size)`` 字节。配合
    :class:`fspack.packaging.net.Downloader` 的分块下载循环使用。

    上下文管理器协议（``__enter__``/``__exit__``）兼容 ``with urlopen(...) as resp``
    语法，与真实 ``HTTPResponse`` 一致。
    """

    def __init__(self, data: bytes, block_size: int = 64) -> None:
        self._buf = io.BytesIO(data)
        self._block_size = block_size
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._buf.read(self._block_size)
        return self._buf.read(min(n, self._block_size))

    def __enter__(self) -> FakeResp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def fail_urlopen(*a: object, **kw: object) -> object:
    """离线模式守卫：被 mock 的 ``urlopen`` 不应被调用.

    用法::

        monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    一旦被调用即抛 :class:`AssertionError`，确保离线模式测试不误触发网络请求。
    """
    raise AssertionError("离线模式不应触发网络请求")
