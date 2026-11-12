"""M1 本地 wheel 缓存快速解析（``wheels/local_cache.py``）单元测试.

覆盖三类返回值语义（命中 / 必然不足 / 不确定回退）与 resolver 接入点，
保证快速路径只在完全确定时生效，其余情形仍走原 pip 预检。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.exceptions import DependencyError
from fspack.packaging.wheels import local_cache
from fspack.packaging.wheels.local_cache import (
    _copy_into_cache,
    _local_dirs,
    _match_wheels,
    _requirement_name,
    _try_local_resolution,
    _wheel_compatible,
)
from fspack.packaging.wheels.resolver import DownloadContext, _run_pip_download
from fspack.slim.spec import WheelInfo


def _ctx(cache_dir: Path, *, uv_path: str | None = "/uv", **kw: Any) -> DownloadContext:
    """构造目标 3.11 / win_amd64 的下载上下文."""
    return DownloadContext(
        py="/py/python",
        py_version=kw.pop("py_version", "3.11.9"),
        platform_tags=kw.pop("platform_tags", ("win_amd64",)),
        pypi_index="https://idx/simple",
        cache_dir=cache_dir,
        base_args=["/py/python", "-m", "pip", "download"],
        uv_path=uv_path,
        **kw,
    )


def _touch_wheel(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(b"PK\x03\x04")
    return path


@pytest.mark.parametrize(
    ("req", "expected"),
    [
        ("numpy>=1.0", "numpy"),
        ("PySide6==6.8.0; sys_platform == 'win32'", "pyside6"),
        ("pkg[extra]>=1", "pkg"),
        ("pkg @ https://example.com/pkg.whl", None),
        ("", None),
    ],
)
def test_requirement_name(req: str, expected: str | None) -> None:
    """从依赖声明提取规范化包名；direct URL 无法判定返回 None."""
    assert _requirement_name(req) == expected


def test_local_dirs_remote_find_links(tmp_path: Path) -> None:
    """find-links 含远程 URL 时返回 None（无法离线扫描）."""
    ctx = _ctx(tmp_path, find_links=["https://example.com/wheels"])
    assert _local_dirs(ctx) is None

    local = tmp_path / "links"
    ctx = _ctx(tmp_path, find_links=[str(local)])
    assert _local_dirs(ctx) == [tmp_path, local]


@pytest.mark.parametrize(
    ("filename", "py_version", "tags", "expected"),
    [
        ("numpy-2.1.0-cp311-cp311-win_amd64.whl", "3.11.9", ("win_amd64",), True),
        ("numpy-2.1.0-cp311-cp311-win_amd64.whl", "3.12.1", ("win_amd64",), False),
        ("numpy-2.1.0-cp311-cp311-win_amd64.whl", "3.11.9", ("manylinux2014_x86_64",), False),
        ("six-1.16.0-py2.py3-none-any.whl", "3.11.9", ("win_amd64",), True),
        ("foo-1.0-cp38-abi3-win_amd64.whl", "3.11.9", ("win_amd64",), True),
        ("foo-1.0-cp38-abi3-win_amd64.whl", "3.7.9", ("win_amd64",), False),
        ("numpy-2.1.0-cp311-cp311-win_amd64.whl", "3.11.9t", ("win_amd64",), False),
        ("numpy-2.1.0-cp311-cp311-win_amd64.whl", "3", ("win_amd64",), False),
    ],
)
def test_wheel_compatible(filename: str, py_version: str, tags: tuple[str, ...], expected: bool) -> None:
    """wheel tag 兼容判定：cpXY / py3-none-any / abi3 / 平台 / free-threaded."""
    info = WheelInfo.from_filename(filename)
    assert info is not None
    assert _wheel_compatible(info, py_version, tags) is expected


def test_match_wheels_unique(tmp_path: Path) -> None:
    """唯一兼容候选时返回其路径."""
    path = _touch_wheel(tmp_path, "numpy-2.1.0-cp311-cp311-win_amd64.whl")
    by_name = {"numpy": [path]}
    assert _match_wheels(["numpy==2.1.0"], by_name, "3.11.9", ("win_amd64",)) == [path]


@pytest.mark.parametrize(
    "pins",
    [
        ["numpy==2.0.0"],  # 版本不符
        ["flask==3.0.0"],  # 本地不存在
    ],
)
def test_match_wheels_missing(tmp_path: Path, pins: list[str]) -> None:
    """版本不符或包缺失时无法确定，返回 None."""
    path = _touch_wheel(tmp_path, "numpy-2.1.0-cp311-cp311-win_amd64.whl")
    by_name = {"numpy": [path]}
    assert _match_wheels(pins, by_name, "3.11.9", ("win_amd64",)) is None


def test_match_wheels_multiple_candidates(tmp_path: Path) -> None:
    """同版本有多个兼容 wheel（cp311 + 纯 Python）时无法确定选哪个，返回 None."""
    a = _touch_wheel(tmp_path, "numpy-2.1.0-cp311-cp311-win_amd64.whl")
    b = _touch_wheel(tmp_path, "numpy-2.1.0-py3-none-any.whl")
    by_name = {"numpy": [a, b]}
    assert _match_wheels(["numpy==2.1.0"], by_name, "3.11.9", ("win_amd64",)) is None


def test_copy_into_cache_from_find_links(tmp_path: Path) -> None:
    """find-links 目录的 wheel 复制进 cache_dir，保持下游「结果都在缓存目录」约定."""
    cache = tmp_path / "cache"
    cache.mkdir()
    src = _touch_wheel(tmp_path / "links", "numpy-2.1.0-cp311-cp311-win_amd64.whl")
    out = _copy_into_cache([src], cache)
    assert out == [cache / src.name]
    assert (cache / src.name).is_file()


def test_copy_into_cache_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """复制失败（权限/占用）时返回 None，交回 pip 预检处理."""
    cache = tmp_path / "cache"
    cache.mkdir()
    src = _touch_wheel(tmp_path / "links", "numpy-2.1.0-cp311-cp311-win_amd64.whl")

    def boom(*a: Any) -> None:
        raise OSError("denied")

    monkeypatch.setattr(local_cache.shutil, "copy2", boom)
    assert _copy_into_cache([src], cache) is None


def test_try_local_resolution_without_uv(tmp_path: Path) -> None:
    """uv 不可用时不打快路径."""
    assert _try_local_resolution(["numpy"], _ctx(tmp_path, uv_path=None)) is None


def test_try_local_resolution_remote_find_links(tmp_path: Path) -> None:
    """find-links 含远程 URL 时不打快路径."""
    ctx = _ctx(tmp_path, find_links=["https://example.com/wheels"])
    assert _try_local_resolution(["numpy"], ctx) is None


def test_try_local_resolution_direct_url(tmp_path: Path) -> None:
    """依赖含 direct URL 时无法用 name==version 映射，回退."""
    assert _try_local_resolution(["pkg @ https://example.com/pkg.whl"], _ctx(tmp_path)) is None


def test_try_local_resolution_empty_cache(tmp_path: Path) -> None:
    """空缓存且顶层包全缺 → 返回 []（pip 预检必然失败）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    assert _try_local_resolution(["numpy", "requests"], _ctx(cache)) == []


def test_try_local_resolution_sdist_blocks_short_circuit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录内有 sdist 时不能断言缓存不足，仍交给 uv 判断（这里 uv 无输出 → 回退）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "numpy-2.1.0.tar.gz").write_bytes(b"x")
    monkeypatch.setattr(local_cache, "_resolve_with_uv_offline", lambda *a, **k: "")
    assert _try_local_resolution(["numpy"], _ctx(cache)) is None


def test_try_local_resolution_uv_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 解析失败时回退 pip 预检（返回 None）."""
    cache = tmp_path / "cache"
    _touch_wheel(cache, "numpy-2.1.0-cp311-cp311-win_amd64.whl")

    def boom(*a: Any, **k: Any) -> str:
        raise DependencyError("uv boom")

    monkeypatch.setattr(local_cache, "_resolve_with_uv_offline", boom)
    assert _try_local_resolution(["numpy"], _ctx(cache)) is None


def test_try_local_resolution_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 解析出精确版本且映射唯一 → 返回 wheel 路径列表."""
    cache = tmp_path / "cache"
    wheel = _touch_wheel(cache, "numpy-2.1.0-cp311-cp311-win_amd64.whl")
    monkeypatch.setattr(local_cache, "_resolve_with_uv_offline", lambda *a, **k: "numpy==2.1.0\n")
    assert _try_local_resolution(["numpy"], _ctx(cache)) == [wheel]


def test_try_local_resolution_hit_from_find_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命中 find-links 中的 wheel 时复制进 cache_dir 再返回."""
    cache = tmp_path / "cache"
    cache.mkdir()
    links = tmp_path / "links"
    wheel = _touch_wheel(links, "numpy-2.1.0-cp311-cp311-win_amd64.whl")
    monkeypatch.setattr(local_cache, "_resolve_with_uv_offline", lambda *a, **k: "numpy==2.1.0\n")
    out = _try_local_resolution(["numpy"], _ctx(cache, find_links=[str(links)]))
    assert out == [cache / wheel.name]


def test_run_pip_download_uses_local_fast_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地命中时直接返回 pip 兼容输出，不启动 pip 进程."""
    cache = tmp_path / "cache"
    _touch_wheel(cache, "numpy-2.1.0-cp311-cp311-win_amd64.whl")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/uv")
    monkeypatch.setattr(local_cache, "_resolve_with_uv_offline", lambda *a, **k: "numpy==2.1.0\n")

    def no_pip(*a: Any, **k: Any) -> None:
        raise AssertionError("命中本地快速解析时不应调用 pip")

    monkeypatch.setattr("fspack.packaging.wheels.process._run_pip", no_pip)
    result = _run_pip_download(["numpy"], _ctx(cache))
    assert result.returncode == 0
    assert result.stdout == "Saved numpy-2.1.0-cp311-cp311-win_amd64.whl\n"


def test_run_pip_download_empty_cache_offline_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空缓存 + 离线：跳过注定失败的 pip 预检，直接给出缓存未命中错误."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/uv")

    def no_pip(*a: Any, **k: Any) -> None:
        raise AssertionError("空缓存离线时不应调用 pip 预检")

    monkeypatch.setattr("fspack.packaging.wheels.process._run_pip", no_pip)
    with pytest.raises(DependencyError, match="离线模式下依赖缓存未命中"):
        _run_pip_download(["numpy"], _ctx(cache))


def test_run_pip_download_empty_cache_online_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空缓存 + 在线：跳过预检直接走在线下载."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/uv")
    called: list[list[str]] = []

    def fake_online(filtered: list[str], ctx: DownloadContext, *, require_hashes: bool = False) -> Any:
        called.append(filtered)
        return subprocess.CompletedProcess(args=["online"], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.resolver._download_online", fake_online)
    result = _run_pip_download(["numpy"], _ctx(cache))
    assert called == [["numpy"]]
    assert result.args == ["online"]


def test_run_pip_download_require_hashes_skips_fast_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """require_hashes=True 不走快路径（哈希校验路径需 uv 生成锁定文件）."""
    cache = tmp_path / "cache"
    cache.mkdir()

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("require_hashes 路径不应尝试本地快速解析")

    monkeypatch.setattr("fspack.packaging.wheels.resolver._try_local_resolution", boom)
    monkeypatch.setattr("fspack.packaging.wheels.process._run_pip", lambda *a, **k: None)
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._download_online",
        lambda *a, **k: subprocess.CompletedProcess(args=["online"], returncode=0, stdout="", stderr=""),
    )
    result = _run_pip_download(["numpy"], _ctx(cache), require_hashes=True)
    assert result.args == ["online"]
