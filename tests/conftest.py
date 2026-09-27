"""测试共享 fixture.

集中存放跨多个测试文件重复定义的 fixture，减少冗余。

辅助桩类与守卫函数（:class:`CompletedStub` / :class:`FakeResp` /
:func:`fail_urlopen`）放在 :mod:`tests._stubs`，测试文件用
``from tests._stubs import CompletedStub`` 显式导入。
"""

from __future__ import annotations

import pytest

__all__ = ["mirror"]


@pytest.fixture
def mirror():
    """测试用 :class:`MirrorConfig` 常量 fixture.

    所有测试共用同一镜像配置（``name="t"``、``python_base="https://x/py"``、
    ``pypi_index="https://x/s"``），避免每个测试文件重复定义 ``_MIRROR`` 常量。

    fspack 延迟到函数内导入：xdist worker 中 conftest 加载早于 pytest-cov
    启动 coverage，顶层导入会触发 module-not-measured 警告。
    """
    from fspack.config import MirrorConfig

    return MirrorConfig(name="t", python_base="https://x/py", pypi_index="https://x/s")
