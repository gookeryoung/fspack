"""doctor/win7.py 测试：_check_win7_compat 缓存 zip/manifest/shim 自检."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from fspack.console import console
from fspack.doctor import (
    CheckStatus,
)


@pytest.fixture(autouse=True)
def _fixed_rich_width(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """固定 Rich Console 宽度，避免窄终端环境下 word wrap 导致断言失败.

    多个测试渲染含 8 列的 Rich Table（bench 模式汇总表），窄终端（width<80）下
    Rich 会截断长文本（如 ``ModuleNotFoundError`` → ``ModuleNot…``）或丢弃列，
    导致断言偶发失败。固定 width=200 确保所有环境渲染一致。

    必须直接 patch ``_width`` 而非走 ``width`` 属性往返：rich 的 ``width`` getter
    在 ``_width``/``_height`` 均已设置时（shell 导出 ``COLUMNS``/``LINES`` 环境变量
    即如此）返回 ``_width - legacy_windows``，而 setter 存原始值——往返一次宽度净
    减 1（legacy Windows 控制台）。本文件数百个测试逐个缩水，跑完后宽度变负数，
    rich 会把后续所有文本裁剪为空，殃及后续文件 27 个 capsys 断言。
    ``monkeypatch`` 记录的是原始 ``_width`` 值，恢复无损。
    """
    monkeypatch.setattr(console.rich, "_width", 200)
    yield


# --- Win7 兼容自检（doctor.win7）---


def _patch_win7_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_dir: Path) -> None:
    """隔离 win7 自检环境：缓存目录与 shim 资产均指向临时路径（故障注入访问私有常量）."""
    import fspack.doctor.win7 as doctor_win7

    monkeypatch.setattr(doctor_win7, "win7_dll_cache_dir", lambda: cache_dir)
    shim = tmp_path / "api-ms-win-core-path-l1-1-0.dll"
    shim.write_bytes(b"shim")
    monkeypatch.setattr(doctor_win7, "WIN7_SHIM_DLL_PATH", shim)


def test_check_win7_compat_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无缓存时 OK：清单对齐、shim 就绪、提示首次打包自动下载."""
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    _patch_win7_env(monkeypatch, tmp_path, cache)

    result = _check_win7_compat()

    assert result.status is CheckStatus.OK
    assert "暂无缓存" in result.detail


def test_check_win7_compat_cached_zip_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存 zip 哈希与清单一致时 OK，detail 报告校验通过数."""
    import hashlib

    import fspack.doctor.win7 as doctor_win7
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    _patch_win7_env(monkeypatch, tmp_path, cache)
    version = next(iter(doctor_win7.WIN7_EMBED_SHA256))
    data = b"win7-embed-zip"
    (cache / doctor_win7.win7_zip_cache_name(version)).write_bytes(data)
    monkeypatch.setitem(doctor_win7.WIN7_EMBED_SHA256, version, hashlib.sha256(data).hexdigest())

    result = _check_win7_compat()

    assert result.status is CheckStatus.OK
    assert "缓存 1 个 zip 校验通过" in result.detail


def test_check_win7_compat_cached_zip_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存 zip 哈希不匹配时 WARN，建议删除后重新构建自动重下."""
    import fspack.doctor.win7 as doctor_win7
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    _patch_win7_env(monkeypatch, tmp_path, cache)
    version = next(iter(doctor_win7.WIN7_EMBED_SHA256))
    (cache / doctor_win7.win7_zip_cache_name(version)).write_bytes(b"corrupted-bytes")

    result = _check_win7_compat()

    assert result.status is CheckStatus.WARN
    assert "哈希不匹配" in result.detail
    assert "删除" in result.suggestion


def test_check_win7_compat_manifest_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """清单缺失 3.12+ 版本时 ERROR（版本升级遗漏）."""
    import fspack.doctor.win7 as doctor_win7
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    _patch_win7_env(monkeypatch, tmp_path, cache)
    # 删掉一个 3.12+ 版本条目模拟升级 KNOWN_EMBED_VERSIONS 后忘同步清单
    # delitem 在 teardown 自动恢复，避免污染全局清单 dict
    monkeypatch.delitem(doctor_win7.WIN7_EMBED_SHA256, "3.12.10")

    result = _check_win7_compat()

    assert result.status is CheckStatus.ERROR
    assert "3.12.10" in result.detail


def test_check_win7_compat_shim_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内置 shim 资产缺失时 ERROR（3.9+ 打包必需）."""
    import fspack.doctor.win7 as doctor_win7
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(doctor_win7, "win7_dll_cache_dir", lambda: cache)
    monkeypatch.setattr(doctor_win7, "WIN7_SHIM_DLL_PATH", tmp_path / "not-exist.dll")

    result = _check_win7_compat()

    assert result.status is CheckStatus.ERROR
    assert "shim 缺失" in result.detail
