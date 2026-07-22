"""mirror 镜像源测试."""

from __future__ import annotations

import pytest

from fspack.mirror import DEFAULT_MIRROR, MIRRORS, get_mirror


def test_default_mirror_is_aliyun() -> None:
    assert DEFAULT_MIRROR == "aliyun"
    assert {"huawei", "aliyun", "tsinghua"} <= set(MIRRORS)


def test_get_mirror_default() -> None:
    assert get_mirror().name == "阿里云"


def test_get_mirror_by_name() -> None:
    assert get_mirror("aliyun").name == "阿里云"
    assert get_mirror("tsinghua").name == "清华"


def test_get_mirror_invalid() -> None:
    with pytest.raises(KeyError, match="未知镜像源"):
        get_mirror("nope")


def test_huawei_embed_url() -> None:
    m = get_mirror("huawei")
    assert m.embed_url("3.11.9") == ("https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip")


def test_huawei_pypi_index() -> None:
    assert get_mirror("huawei").pypi_index == "https://mirrors.huaweicloud.com/pypi/simple/"
