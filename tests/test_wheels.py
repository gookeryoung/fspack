"""wheel 下载与依赖解析测试：pip/uv 调用、缓存管理、sdist 回退."""

from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path
from typing import Any, Sequence

import pytest

from fspack.exceptions import DependencyError
from fspack.packaging.wheels import (
    _PIP_PYTHON_NAMES,
    _build_pip_download_args,
    _build_sdist_wheels,
    _cleanup_partial_wheels,
    _convert_uv_output_to_pip_format,
    _deps_cache_key,
    _download_one_resolved,
    _download_one_with_uv,
    _download_online,
    _download_resolved_parallel,
    _eval_python_version_marker,
    _eval_single_marker,
    _filter_by_python_version,
    _find_pip_python,
    _find_uv,
    _load_deps_cache,
    _merge_parallel_results,
    _parse_missing_packages,
    _parse_pip_download_wheels,
    _resolve_with_uv,
    _run_pip,
    _save_deps_cache,
    _stream_subprocess,
    _uv_supports_download,
    download_wheels,
)
from fspack.packaging.wheels.resolver import DownloadContext
from fspack.progress import StageRecorder
from tests._stubs import CompletedStub


def _make_ctx(  # noqa: PLR0913
    base_args: list[str],
    cache_dir: Path | None = None,
    *,
    py: str = "/py/python",
    py_version: str = "3.11.9",
    platform_tags: Sequence[str] = ("win_amd64",),
    pypi_index: str = "https://idx/simple",
    extra_index_urls: Sequence[str] = (),
    find_links: Sequence[str] = (),
    uv_path: str | None = None,
) -> DownloadContext:
    """构造测试用 DownloadContext，默认值与 test_wheels 常用场景一致.

    ``cache_dir`` 仅在调用链触及缓存目录（pip -d/uv -d/临时 requirements）时
    需要真实路径，纯解析类测试可不传。
    """
    return DownloadContext(
        py=py,
        py_version=py_version,
        platform_tags=platform_tags,
        pypi_index=pypi_index,
        cache_dir=cache_dir or Path("./.test-cache"),
        base_args=base_args,
        extra_index_urls=extra_index_urls,
        find_links=find_links,
        uv_path=uv_path,
    )


@pytest.fixture(autouse=True)
def _clear_pip_python_cache() -> None:
    """每个测试前清空 ``_find_pip_python`` 的 lru_cache，避免跨测试缓存污染.

    ``_find_pip_python`` 成功结果进程内缓存（iter 修复），而本文件各测试通过
    monkeypatch 替换 ``sys.executable``/``PATH`` 构造不同候选集，若不清缓存，
    前一测试缓存的解释器路径会让后续测试命中旧值而不触发探测。
    """
    _find_pip_python.cache_clear()


def test_download_wheels_cmd_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-index 成功路径：命令含 --no-index，不含 -i index."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    cache = tmp_path / "cache"
    download_wheels(("numpy", "requests"), "3.11.9", "https://idx/simple", cache)
    cmd = captured["cmd"]
    assert cmd[0] == "/py/python"
    assert "download" in cmd
    assert "win_amd64" in cmd
    assert "3.11" in cmd
    assert "cp311" in cmd
    assert "--no-index" in cmd
    assert "https://idx/simple" not in cmd
    assert "numpy" in cmd and "requests" in cmd
    assert "--find-links" in cmd
    assert str(cache) in cmd
    assert "-d" in cmd


def test_download_wheels_fallback_cmd_has_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-index 失败且 uv 不可用时回退到带 -i index 的命令."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd)
        # --no-index 路径失败，触发回退
        raise subprocess.CalledProcessError(1, "pip", stderr="not in cache")

    def fake_stream(cmd: list[str]) -> CompletedStub:
        calls.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache")
    assert len(calls) == 2
    assert "--no-index" in calls[0]
    assert "https://idx/simple" in calls[1]
    assert "--no-index" not in calls[1]


def test_build_pip_download_args_standard() -> None:
    """标准版 py_version 解析为 cp311 abi + 3.11 python-version."""
    args = _build_pip_download_args("/py/python", "3.11.9", ("win_amd64",), Path("./.cache"))
    assert "--python-version" in args
    py_ver_idx = args.index("--python-version") + 1
    abi_idx = args.index("--abi") + 1
    assert args[py_ver_idx] == "3.11"
    assert args[abi_idx] == "cp311"


def test_build_pip_download_args_freethreaded_313t() -> None:
    """free-threaded 版本 3.13.14t 解析为 cp313t abi + 纯数字 3.13 python-version.

    pip 不识别 ``--python-version 3.13t``（报 "each version part must be an
    integer"），须剥离 t 后缀传 ``3.13``；``--abi cp313t`` 指定 free-threaded
    wheel（abi tag 与标准版 cp313 不互通），pip 按 abi 组合命中 freethreaded wheel。
    """
    args = _build_pip_download_args("/py/python", "3.13.14t", ("win_amd64",), Path("./.cache"))
    py_ver_idx = args.index("--python-version") + 1
    abi_idx = args.index("--abi") + 1
    assert args[py_ver_idx] == "3.13"
    assert args[abi_idx] == "cp313t"


def test_build_pip_download_args_freethreaded_314t() -> None:
    """free-threaded 版本 3.14.6t 解析为 cp314t abi + 纯数字 3.14 python-version."""
    args = _build_pip_download_args("/py/python", "3.14.6t", ("win_amd64",), Path("./.cache"))
    py_ver_idx = args.index("--python-version") + 1
    abi_idx = args.index("--abi") + 1
    assert args[py_ver_idx] == "3.14"
    assert args[abi_idx] == "cp314t"


def test_download_wheels_no_index_skips_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-index 成功时只调用 pip 一次，不查询网络 index."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache")
    assert len(calls) == 1
    assert "--no-index" in calls[0]
    assert "https://idx/simple" not in calls[0]


def test_download_wheels_multi_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多个 platform_tags 展开为多个 --platform 参数."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    download_wheels(
        ("PySide6",),
        "3.11.10",
        "https://idx/simple",
        tmp_path / "cache",
        platform_tags=("manylinux2014_x86_64", "manylinux_2_28_x86_64"),
    )
    cmd = captured["cmd"]
    platform_count = cmd.count("--platform")
    assert platform_count == 2, f"应有 2 个 --platform，实际 {platform_count}"
    assert "manylinux2014_x86_64" in cmd
    assert "manylinux_2_28_x86_64" in cmd


def test_download_wheels_pip_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_find_pip_python 抛 DependencyError 时 download_wheels 透传."""
    monkeypatch.setattr(
        "fspack.packaging.wheels.downloader._find_pip_python",
        lambda: (_ for _ in ()).throw(DependencyError("未找到可用的 pip")),
    )
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "cache")


def test_download_wheels_pip_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "pip", stderr="no wheel")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    def fake_stream(cmd: list[str]) -> CompletedStub:
        raise err

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    with pytest.raises(DependencyError, match="依赖下载失败"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "cache")


def test_download_wheels_pip_error_cleans_partial_wheels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """pip download 失败时清理本次部分下载的 wheel，保留下载前已存在的 wheel（iter-130）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    # 下载前已存在的 wheel（其他项目缓存），应保留
    existing = cache / "otherpkg-1.0-cp311-cp311-win_amd64.whl"
    existing.write_bytes(b"existing")
    # 本次部分下载的 wheel（pip 失败前已下载），应清理
    partial_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    err = subprocess.CalledProcessError(1, "pip", stderr="no wheel")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        # 模拟 pip 下载了部分 wheel 后失败
        (cache / partial_name).write_bytes(b"partial")
        raise err

    def fake_stream(cmd: list[str]) -> CompletedStub:
        raise err

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)

    with caplog.at_level("WARNING", logger="fspack.packaging.wheels.downloader"), pytest.raises(
        DependencyError, match="依赖下载失败"
    ):
        download_wheels(("numpy",), "3.11.9", "https://idx", cache)

    # 部分下载的 wheel 应被清理
    assert not (cache / partial_name).exists(), "部分下载的 wheel 应被清理"
    # 下载前已存在的 wheel 应保留
    assert existing.is_file(), "下载前已存在的 wheel 应保留"
    assert any("清理" in r.message and "wheel" in r.message for r in caplog.records)


def test_cleanup_partial_wheels_preserves_existing(tmp_path: Path) -> None:
    """_cleanup_partial_wheels 仅删除不在 before 集合中的 wheel."""
    cache = tmp_path / "cache"
    cache.mkdir()
    existing = cache / "existing-1.0.whl"
    partial1 = cache / "partial-1.0.whl"
    partial2 = cache / "partial-2.0.whl"
    existing.write_bytes(b"old")
    partial1.write_bytes(b"new1")
    partial2.write_bytes(b"new2")

    _cleanup_partial_wheels(cache, before={"existing-1.0.whl"})

    assert existing.is_file()
    assert not partial1.exists()
    assert not partial2.exists()


def test_cleanup_partial_wheels_no_partial(tmp_path: Path) -> None:
    """无部分下载 wheel 时 _cleanup_partial_wheels 无操作."""
    cache = tmp_path / "cache"
    cache.mkdir()
    existing = cache / "existing-1.0.whl"
    existing.write_bytes(b"old")

    # 无部分下载，before 与当前一致
    _cleanup_partial_wheels(cache, before={"existing-1.0.whl"})

    assert existing.is_file()


def test_cleanup_partial_wheels_unlink_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_cleanup_partial_wheels 删除失败时 warning 不中断."""
    cache = tmp_path / "cache"
    cache.mkdir()
    partial = cache / "partial-1.0.whl"
    partial.write_bytes(b"new")

    original_unlink = Path.unlink

    def fail_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == partial:
            raise OSError("permission denied")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with caplog.at_level("WARNING", logger="fspack.packaging.wheels.downloader"):
        _cleanup_partial_wheels(cache, before=set())
    assert any("清理部分下载的 wheel 失败" in r.message for r in caplog.records)


def test_download_wheels_python_disappeared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_find_pip_python 验证通过后 download 时 python 消失（FileNotFoundError）."""
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr(
        "fspack.packaging.wheels.subprocess.run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError())
    )
    with pytest.raises(DependencyError, match="未找到 pip"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "cache")


def test_download_wheels_records_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 回写新增 wheel 字节数到 stage."""
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    whl_content = b"x" * 100

    class _Result:
        returncode = 0
        stdout = f"Saved {whl_name}\n"
        stderr = ""

    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        (tmp_path / "cache" / whl_name).write_bytes(whl_content)
        return _Result()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    stage = StageRecorder("下载依赖")
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache", stage=stage)
    record = stage._finalize()
    assert record.bytes_downloaded == 100
    assert record.items == 1


def test_download_wheels_cache_hit_no_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache_dir 已存在的 wheel 不计入新增字节数，但计入缓存命中."""
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / whl_name).write_bytes(b"old" * 10)

    class _Result:
        returncode = 0
        stdout = f"File was already downloaded {whl_name}\n"
        stderr = ""

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", lambda cmd, **kw: _Result())
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    stage = StageRecorder("下载依赖")
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache, stage=stage)
    record = stage._finalize()
    assert record.bytes_downloaded == 0
    assert record.cache_hit == 1


def test_download_wheels_parses_stdout_for_wheels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 从 pip stdout 解析 wheel 列表（含传递依赖）."""
    whl1 = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    whl2 = "requests-2.31.0-py3-none-any.whl"

    class _Result:
        returncode = 0
        stdout = f"Collecting numpy\n  Saved {whl1}\nCollecting requests\n  File was already downloaded {whl2}\n"
        stderr = ""

    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        (tmp_path / "cache" / whl1).write_bytes(b"numpy")
        (tmp_path / "cache" / whl2).write_bytes(b"requests")
        return _Result()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    result = download_wheels(("numpy", "requests"), "3.11.9", "https://idx/simple", tmp_path / "cache")
    names = {p.name for p in result}
    assert whl1 in names
    assert whl2 in names


def test_download_wheels_fallback_to_dir_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout 无匹配行时回退到目录扫描."""
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"

    class _Result:
        returncode = 0
        stdout = "no wheel info here\n"
        stderr = ""

    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        (tmp_path / "cache" / whl_name).write_bytes(b"numpy")
        return _Result()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    result = download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache")
    assert len(result) == 1
    assert result[0].name == whl_name


def test_deps_cache_key_stable_and_distinct() -> None:
    """相同输入产生相同键；不同输入产生不同键."""
    k1 = _deps_cache_key(("numpy", "requests"), "3.11.9", ("win_amd64",))
    k2 = _deps_cache_key(("numpy", "requests"), "3.11.9", ("win_amd64",))
    k3 = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    k4 = _deps_cache_key(("numpy", "requests"), "3.10.11", ("win_amd64",))
    k5 = _deps_cache_key(("numpy", "requests"), "3.11.9", ("manylinux2014_x86_64",))
    assert k1 == k2
    assert k1 != k3
    assert k1 != k4
    assert k1 != k5


def test_deps_cache_key_order_independent() -> None:
    """包顺序不影响键（sorted 后哈希）."""
    k1 = _deps_cache_key(("numpy", "requests"), "3.11.9", ("win_amd64",))
    k2 = _deps_cache_key(("requests", "numpy"), "3.11.9", ("win_amd64",))
    assert k1 == k2


def test_load_deps_cache_miss_when_no_file(tmp_path: Path) -> None:
    """缓存文件不存在时返回 None."""
    assert _load_deps_cache(tmp_path / "cache", "abc123") is None


def test_load_deps_cache_hit_when_wheels_exist(tmp_path: Path) -> None:
    """缓存文件存在且 wheel 文件齐全时返回路径列表."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "numpy-1.0.whl").write_bytes(b"x")
    (cache / "requests-2.0.whl").write_bytes(b"y")
    _save_deps_cache(cache, "abc123", [cache / "numpy-1.0.whl", cache / "requests-2.0.whl"])
    loaded = _load_deps_cache(cache, "abc123")
    assert loaded is not None
    names = {p.name for p in loaded}
    assert names == {"numpy-1.0.whl", "requests-2.0.whl"}


def test_load_deps_cache_miss_when_wheel_deleted(tmp_path: Path) -> None:
    """缓存文件存在但 wheel 文件被删时返回 None（需重新解析）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _save_deps_cache(cache, "abc123", [cache / "numpy-1.0.whl"])
    # 不创建 wheel 文件
    assert _load_deps_cache(cache, "abc123") is None


def test_load_deps_cache_handles_corrupt_json(tmp_path: Path) -> None:
    """缓存文件 JSON 损坏时返回 None 不抛异常（iter-128 起删除损坏文件）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt_file = cache / ".deps-corrupt.json"
    corrupt_file.write_text("{bad json", encoding="utf-8")
    assert _load_deps_cache(cache, "corrupt") is None
    # iter-128：损坏的缓存文件被删除，避免下次构建重复告警
    assert not corrupt_file.is_file()


def test_load_deps_cache_deletes_corrupt_non_dict_json(tmp_path: Path) -> None:
    """缓存根对象非 dict（如 list/int）时删除文件（iter-128）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt_file = cache / ".deps-corrupt.json"
    # JSON 合法但结构不对：根对象是 list 而非 dict
    corrupt_file.write_text("[1, 2, 3]", encoding="utf-8")
    assert _load_deps_cache(cache, "corrupt") is None
    assert not corrupt_file.is_file()


def test_load_deps_cache_deletes_corrupt_wheels_field(tmp_path: Path) -> None:
    """wheels 字段类型错误（非 list）时删除文件（iter-128）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt_file = cache / ".deps-corrupt.json"
    # wheels 字段是字符串而非 list
    corrupt_file.write_text('{"wheels": "not-a-list"}', encoding="utf-8")
    assert _load_deps_cache(cache, "corrupt") is None
    assert not corrupt_file.is_file()


def test_load_deps_cache_deletes_non_str_wheel_element(tmp_path: Path) -> None:
    """wheels 列表含非 str 元素（如 int）时删除缓存文件并返回 None 重解析.

    回归场景：``cache_dir / name`` 遇非 str 元素触发未捕获 TypeError，导致构建
    崩溃。修复后与"非 list"同等视为损坏，走删除缓存返回 None 分支。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt_file = cache / ".deps-corrupt.json"
    # wheels 是 list 但第二个元素是 int（手工编辑/异常写入产生的脏数据）
    corrupt_file.write_text('{"wheels": ["numpy-1.0.whl", 123]}', encoding="utf-8")
    assert _load_deps_cache(cache, "corrupt") is None
    assert not corrupt_file.is_file()


def test_load_deps_cache_deletes_invalid_utf8(tmp_path: Path) -> None:
    """缓存文件含非法 UTF-8 字节时删除文件（iter-128）.

    UnicodeDecodeError 是 ValueError 子类，被 except (json.JSONDecodeError, ValueError) 捕获。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt_file = cache / ".deps-corrupt.json"
    # 写入非法 UTF-8 字节序列（0xff 不是合法 UTF-8 起始字节）
    corrupt_file.write_bytes(b"\xff\xfe{bad}")
    assert _load_deps_cache(cache, "corrupt") is None
    assert not corrupt_file.is_file()


def test_load_deps_cache_oserror_keeps_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """read_text 抛 OSError（文件系统错误）时不删除文件（iter-128）.

    OSError 可能是瞬时文件系统问题（权限/磁盘 I/O），删除反而误伤可恢复的缓存。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    cache_file = cache / ".deps-corrupt.json"
    cache_file.write_text('{"wheels": ["x.whl"]}', encoding="utf-8")

    def raise_oserror(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", raise_oserror)
    assert _load_deps_cache(cache, "corrupt") is None
    # OSError 不删除文件：可能是瞬时问题，下次构建重试
    assert cache_file.is_file()


def test_save_deps_cache_best_effort(tmp_path: Path) -> None:
    """写入失败仅 warning 不抛异常（best-effort）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _save_deps_cache(cache, "abc123", [cache / "numpy-1.0.whl"])
    cache_file = cache / ".deps-abc123.json"
    assert cache_file.is_file()
    import json as _json

    data = _json.loads(cache_file.read_text(encoding="utf-8"))
    assert data == {"wheels": ["numpy-1.0.whl"]}


def test_download_wheels_deps_cache_hit_skips_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖解析缓存命中时完全跳过 pip 调用."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    (cache / whl_name).write_bytes(b"numpy")

    # 预写依赖解析缓存
    key = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    _save_deps_cache(cache, key, [cache / whl_name])

    pip_called = False

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        nonlocal pip_called
        pip_called = True
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    stage = StageRecorder("下载依赖")
    result = download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache, stage=stage)
    record = stage._finalize()
    assert not pip_called
    assert len(result) == 1
    assert result[0].name == whl_name
    assert record.cache_hit == 1
    assert record.bytes_downloaded == 0


def test_download_wheels_writes_deps_cache_after_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip 解析成功后写入依赖解析缓存，下次调用命中."""
    cache = tmp_path / "cache"
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"

    class _Result:
        returncode = 0
        stdout = f"Saved {whl_name}\n"
        stderr = ""

    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        (cache / whl_name).write_bytes(b"numpy")
        return _Result()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    # 第一次调用：cache miss，走 pip，写缓存
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache)
    key = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    cache_file = cache / f".deps-{key}.json"
    assert cache_file.is_file()
    import json as _json

    data = _json.loads(cache_file.read_text(encoding="utf-8"))
    assert whl_name in data["wheels"]


def test_download_wheels_deps_cache_hit_no_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖解析缓存命中且 stage=None 时不报错."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    (cache / whl_name).write_bytes(b"numpy")
    key = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    _save_deps_cache(cache, key, [cache / whl_name])

    pip_called = False

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        nonlocal pip_called
        pip_called = True
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")

    result = download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache)
    assert not pip_called
    assert len(result) == 1


def test_save_deps_cache_oserror_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_text 抛 OSError 时仅 warning 不抛异常."""
    cache = tmp_path / "cache"
    cache.mkdir()

    def fake_write_text(self: Path, *a: object, **kw: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", fake_write_text)
    _save_deps_cache(cache, "abc123", [cache / "numpy-1.0.whl"])


def test_parse_pip_download_wheels_saved_and_cached() -> None:
    """解析 Saved 和 File was already downloaded 两种行."""
    stdout = (
        "Collecting numpy\n  Saved /path/to/numpy-1.0-cp311-win_amd64.whl\n"
        "Collecting requests\n  File was already downloaded /other/requests-2.0-py3-none-any.whl\n"
    )
    names = _parse_pip_download_wheels(stdout)
    assert names == ["numpy-1.0-cp311-win_amd64.whl", "requests-2.0-py3-none-any.whl"]


def test_parse_pip_download_wheels_dedup() -> None:
    """重复 wheel 文件名去重."""
    stdout = "Saved a-1.0.whl\nSaved a-1.0.whl\nSaved b-2.0.whl\n"
    names = _parse_pip_download_wheels(stdout)
    assert names == ["a-1.0.whl", "b-2.0.whl"]


def test_parse_pip_download_wheels_empty() -> None:
    """无匹配行返回空列表."""
    assert _parse_pip_download_wheels("nothing here\n") == []
    assert _parse_pip_download_wheels("") == []


def test_find_pip_python_uses_sys_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sys.executable 能跑 pip 时优先用它."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": ""})

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        if cmd[0] == str(venv_py):
            return CompletedStub()
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    assert _find_pip_python() == str(venv_py)


def test_find_pip_python_falls_back_to_system(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sys.executable 无 pip 时遍历 PATH 找系统 python."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    sys_py = sys_bin / _PIP_PYTHON_NAMES[0]
    sys_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": str(sys_bin)})

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        if cmd[0] == str(venv_py):
            raise subprocess.CalledProcessError(1, cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    assert _find_pip_python() == str(sys_py.resolve())


def test_find_pip_python_skips_venv_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PATH 中 venv 所在目录的系统 python 被跳过."""
    venv_bin = tmp_path / "venv"
    venv_bin.mkdir()
    venv_py = venv_bin / _PIP_PYTHON_NAMES[0]
    venv_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": str(venv_bin)})
    monkeypatch.setattr(
        "fspack.packaging.wheels.subprocess.run",
        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, cmd)),
    )
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        _find_pip_python()


def test_find_pip_python_all_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """所有候选都无 pip 时抛 DependencyError."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    (sys_bin / _PIP_PYTHON_NAMES[0]).write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": str(sys_bin)})

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        _find_pip_python()


def test_find_pip_python_empty_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PATH 为空时只检测 sys.executable."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": ""})
    monkeypatch.setattr(
        "fspack.packaging.wheels.subprocess.run",
        lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        _find_pip_python()


def test_find_pip_python_skips_dir_without_python3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PATH 中无系统 python 的目录被跳过，继续找下一个."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    empty_bin = tmp_path / "empty"
    empty_bin.mkdir()
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    sys_py = sys_bin / _PIP_PYTHON_NAMES[0]
    sys_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": f"{empty_bin}{os.pathsep}{sys_bin}"})

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        if cmd[0] == str(venv_py):
            raise subprocess.CalledProcessError(1, cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    assert _find_pip_python() == str(sys_py.resolve())


def test_find_pip_python_skips_unresolvable_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Path.resolve 抛 OSError 的目录被跳过."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    sys_py = sys_bin / _PIP_PYTHON_NAMES[0]
    sys_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": f"{bad_dir}{os.pathsep}{sys_bin}"})
    original_resolve = Path.resolve

    def fake_resolve(self: Path) -> Path:
        if self == bad_dir:
            raise OSError("mocked")
        return original_resolve(self)

    monkeypatch.setattr("fspack.packaging.wheels.Path.resolve", fake_resolve)

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        if cmd[0] == str(venv_py):
            raise subprocess.CalledProcessError(1, cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    assert _find_pip_python() == str(sys_py.resolve())


def test_find_pip_python_result_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """成功结果进程内缓存：第二次调用不再 spawn 子进程探测."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": ""})

    probe_count = {"n": 0}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        probe_count["n"] += 1
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    assert _find_pip_python() == str(venv_py)
    assert _find_pip_python() == str(venv_py)
    # 两次调用只触发一次 ``python -m pip --version`` 探测
    assert probe_count["n"] == 1


def test_find_pip_python_timeout_continues_to_next_candidate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """候选解释器探测超时（TimeoutExpired）时中断并继续下一个候选."""
    from fspack.packaging.wheels.downloader import _PIP_PROBE_TIMEOUT

    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    sys_py = sys_bin / _PIP_PYTHON_NAMES[0]
    sys_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": str(sys_bin)})

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        assert kw.get("timeout") == _PIP_PROBE_TIMEOUT, "探测命令必须带 timeout"
        if cmd[0] == str(venv_py):
            # 模拟网络盘/损坏解释器探测卡死：超时被中断
            raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 0))
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    assert _find_pip_python() == str(sys_py.resolve())


def test_find_pip_python_timeout_all_candidates_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """所有候选探测均超时时最终抛 DependencyError（而非 TimeoutExpired 逃逸）."""
    from fspack.packaging.wheels.downloader import _PIP_PROBE_TIMEOUT

    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    monkeypatch.setattr("fspack.packaging.wheels.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.packaging.wheels.os.environ", {"PATH": ""})

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise subprocess.TimeoutExpired(cmd, timeout=_PIP_PROBE_TIMEOUT)

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        _find_pip_python()


# ---------- _filter_by_python_version ----------


def test_filter_by_python_version_no_marker_kept() -> None:
    """无环境标记的依赖原样保留."""
    result = _filter_by_python_version(["numpy>=1.20", "requests"], "3.8.10")
    assert result == ["numpy>=1.20", "requests"]


def test_filter_by_python_version_skip_higher() -> None:
    """目标 3.8 时跳过 python_version >= '3.11' 的依赖."""
    pkgs = [
        "PySide2>=5.15.2.1; python_version <= '3.10'",
        "PySide6>=6.5.0; python_version >= '3.11'",
        "PyYAML>=6.0",
    ]
    result = _filter_by_python_version(pkgs, "3.8.10")
    assert result == ["PySide2>=5.15.2.1", "PyYAML>=6.0"]


def test_filter_by_python_version_keep_when_matches() -> None:
    """目标 3.11 时保留 python_version >= '3.11' 的依赖（去标记）."""
    pkgs = [
        "PySide2>=5.15.2.1; python_version <= '3.10'",
        "PySide6>=6.5.0; python_version >= '3.11'",
    ]
    result = _filter_by_python_version(pkgs, "3.11.9")
    assert result == ["PySide6>=6.5.0"]


def test_filter_by_python_version_keep_lower_bound_match() -> None:
    """边界值匹配：python_version <= '3.10' 在目标 3.10 时保留."""
    result = _filter_by_python_version(["PySide2>=5.15.2.1; python_version <= '3.10'"], "3.10.11")
    assert result == ["PySide2>=5.15.2.1"]


def test_filter_by_python_version_keep_non_python_marker() -> None:
    """非 python_version 标记保守保留（去标记）."""
    result = _filter_by_python_version(["foo>=1.0; platform_system == 'Windows'"], "3.8.10")
    assert result == ["foo>=1.0"]


def test_filter_by_python_version_and_combination() -> None:
    """and 组合：两个条件都满足才保留."""
    pkgs = ["bar>=1.0; python_version >= '3.8' and python_version < '3.12'"]
    assert _filter_by_python_version(pkgs, "3.10.11") == ["bar>=1.0"]
    assert _filter_by_python_version(pkgs, "3.12.0") == []


def test_filter_by_python_version_or_combination() -> None:
    """or 组合：任一条件满足即保留."""
    pkgs = ["baz>=1.0; python_version < '3.9' or python_version >= '3.12'"]
    assert _filter_by_python_version(pkgs, "3.8.10") == ["baz>=1.0"]
    assert _filter_by_python_version(pkgs, "3.11.9") == []
    assert _filter_by_python_version(pkgs, "3.12.0") == ["baz>=1.0"]


def test_filter_by_python_version_empty_input() -> None:
    """空列表输入返回空列表."""
    assert _filter_by_python_version([], "3.8.10") == []


def test_filter_by_python_version_all_filtered() -> None:
    """所有依赖都被标记过滤时返回空列表."""
    pkgs = ["PySide6>=6.5.0; python_version >= '3.11'"]
    assert _filter_by_python_version(pkgs, "3.8.10") == []


# ---------- _eval_single_marker / _eval_python_version_marker ----------


def test_eval_single_marker_ge() -> None:
    py = (3, 8)
    assert _eval_single_marker("python_version >= '3.8'", py) is True
    assert _eval_single_marker("python_version >= '3.9'", py) is False


def test_eval_single_marker_le() -> None:
    py = (3, 10)
    assert _eval_single_marker("python_version <= '3.10'", py) is True
    assert _eval_single_marker("python_version <= '3.9'", py) is False


def test_eval_single_marker_lt_gt() -> None:
    py = (3, 9)
    assert _eval_single_marker("python_version < '3.10'", py) is True
    assert _eval_single_marker("python_version > '3.8'", py) is True
    assert _eval_single_marker("python_version < '3.9'", py) is False
    assert _eval_single_marker("python_version > '3.9'", py) is False


def test_eval_single_marker_eq_ne() -> None:
    py = (3, 11)
    assert _eval_single_marker("python_version == '3.11'", py) is True
    assert _eval_single_marker("python_version != '3.10'", py) is True
    assert _eval_single_marker("python_version == '3.10'", py) is False


def test_eval_single_marker_non_python_returns_true() -> None:
    """非 python_version 标记保守返回 True."""
    assert _eval_single_marker("platform_system == 'Windows'", (3, 8)) is True


def test_eval_single_marker_double_quotes() -> None:
    """双引号标记值也能匹配."""
    assert _eval_single_marker('python_version >= "3.8"', (3, 9)) is True


def test_eval_python_version_marker_and() -> None:
    py = (3, 10)
    assert _eval_python_version_marker("python_version >= '3.8' and python_version <= '3.10'", py) is True
    assert _eval_python_version_marker("python_version >= '3.8' and python_version <= '3.9'", py) is False


def test_eval_python_version_marker_or() -> None:
    py = (3, 8)
    assert _eval_python_version_marker("python_version < '3.9' or python_version >= '3.12'", py) is True
    assert _eval_python_version_marker("python_version >= '3.9' or python_version >= '3.12'", py) is False


def test_eval_python_version_marker_case_insensitive() -> None:
    """and/or 大小写不敏感."""
    py = (3, 10)
    assert _eval_python_version_marker("python_version >= '3.8' AND python_version <= '3.10'", py) is True
    assert _eval_python_version_marker("python_version < '3.8' OR python_version >= '3.12'", py) is False


def test_eval_python_version_marker_non_python_returns_true() -> None:
    """纯非 python_version 标记保守返回 True."""
    assert _eval_python_version_marker("platform_system == 'Windows'", (3, 8)) is True


# ---------- _parse_missing_packages ----------


def test_parse_missing_packages_single() -> None:
    stderr = "ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
    assert _parse_missing_packages(stderr) == ["odfpy>=1.4.1"]


def test_parse_missing_packages_multiple() -> None:
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement PySide6>=6.5.0 (from versions: none)\n"
        "ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
    )
    assert _parse_missing_packages(stderr) == ["PySide6>=6.5.0", "odfpy>=1.4.1"]


def test_parse_missing_packages_dedup() -> None:
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
        "ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
    )
    assert _parse_missing_packages(stderr) == ["odfpy>=1.4.1"]


def test_parse_missing_packages_empty() -> None:
    """无匹配行返回空列表."""
    assert _parse_missing_packages("") == []
    assert _parse_missing_packages("no error here\n") == []


def test_parse_missing_packages_preserves_spec() -> None:
    """保留版本 specifier 供 pip wheel 使用."""
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement reportlab>=3.6.13,<4.0 (from versions: none)\n"
    )
    assert _parse_missing_packages(stderr) == ["reportlab>=3.6.13,<4.0"]


# ---------- _build_sdist_wheels ----------


def test_build_sdist_wheels_runs_pip_wheel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """对每个缺失包调用一次 pip wheel --no-deps."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    cache = tmp_path / "cache"
    cache.mkdir()
    _build_sdist_wheels(["odfpy>=1.4.1"], "/py/python", "https://idx/simple", cache)
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "/py/python"
    assert "wheel" in cmd
    assert "--no-deps" in cmd
    assert "-w" in cmd
    assert str(cache) in cmd
    assert "-i" in cmd
    assert "https://idx/simple" in cmd
    assert "odfpy>=1.4.1" in cmd


def test_build_sdist_wheels_multiple_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多个缺失包各调用一次 pip wheel."""
    calls: list[str] = []

    def fake_stream(cmd: list[str]) -> CompletedStub:
        calls.append(cmd[-1])
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    _build_sdist_wheels(["odfpy>=1.4.1", "foo>=1.0"], "/py/python", "https://idx", tmp_path / "cache")
    assert calls == ["odfpy>=1.4.1", "foo>=1.0"]


def test_build_sdist_wheels_failure_only_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip wheel 构建失败仅 warning 不抛异常（让后续重试失败时抛原始错误）."""

    def fake_stream(cmd: list[str]) -> CompletedStub:
        raise subprocess.CalledProcessError(1, cmd, stderr="build failed")

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    # 不抛异常即通过
    _build_sdist_wheels(["odfpy>=1.4.1"], "/py/python", "https://idx", tmp_path / "cache")


def test_build_sdist_wheels_pip_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FileNotFoundError 包装为 DependencyError（pip 解释器不存在）."""
    monkeypatch.setattr(
        "fspack.packaging.wheels.downloader._stream_subprocess", lambda cmd: (_ for _ in ()).throw(FileNotFoundError())
    )
    with pytest.raises(DependencyError, match="未找到 pip"):
        _build_sdist_wheels(["odfpy>=1.4.1"], "/missing/python", "https://idx", tmp_path / "cache")


# ---------- download_wheels 标记过滤 / sdist 回退分支 ----------


def test_download_wheels_filters_python_version_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """带 python_version >= '3.11' 的依赖在目标 3.8 时被剔除，不传给 pip."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    pkgs = (
        "PySide2>=5.15.2.1; python_version <= '3.10'",
        "PySide6>=6.5.0; python_version >= '3.11'",
        "PyYAML>=6.0",
    )
    download_wheels(pkgs, "3.8.10", "https://idx/simple", tmp_path / "cache")
    cmd = captured["cmd"]
    # PySide6 不应出现在命令中，PySide2 去掉标记后传入
    assert any(a == "PySide2>=5.15.2.1" for a in cmd)
    assert not any(a.startswith("PySide6") for a in cmd)
    assert "PyYAML>=6.0" in cmd
    # 标记部分不应作为独立参数传入
    assert not any("python_version" in a for a in cmd)


def test_download_wheels_all_filtered_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """所有依赖被标记过滤时返回空列表，不调用 pip."""
    pip_called = False

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        nonlocal pip_called
        pip_called = True
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    pkgs = ("PySide6>=6.5.0; python_version >= '3.11'",)
    result = download_wheels(pkgs, "3.8.10", "https://idx/simple", tmp_path / "cache")
    assert result == []
    assert not pip_called


def test_download_wheels_sdist_fallback_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-index 失败 → -i index 失败（含 missing 包）→ pip wheel 构建 → 重试成功."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "odfpy-1.4.1-py3-none-any.whl"
    call_count = {"index_download": 0, "pip_wheel": 0}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    # --no-index 路径走 subprocess.run（stream=False）
    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        raise subprocess.CalledProcessError(1, cmd, stderr="not in cache")

    # -i index 下载和 pip wheel 走 _stream_subprocess（stream=True）
    def fake_stream(cmd: list[str]) -> _Result:
        if "wheel" in cmd and "--no-deps" in cmd:
            call_count["pip_wheel"] += 1
            # 模拟从 sdist 构建 wheel 写入 cache_dir
            (cache / whl_name).write_bytes(b"odfpy")
            return _Result()
        # -i index 下载
        call_count["index_download"] += 1
        if call_count["index_download"] == 1:
            # 第一次 -i index 下载失败：报 odfpy 无 wheel
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
                "ERROR: No matching distribution found for odfpy>=1.4.1",
            )
        # 第二次重试成功
        r = _Result()
        r.stdout = f"Saved {whl_name}\n"
        return r

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    result = download_wheels(("odfpy>=1.4.1",), "3.8.10", "https://idx/simple", cache)
    assert call_count["index_download"] == 2
    assert call_count["pip_wheel"] == 1
    assert any(p.name == whl_name for p in result)


def test_download_wheels_sdist_fallback_no_missing_reraises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载失败但无 missing 包时直接抛出原始错误（不进入 sdist 回退）."""
    err = subprocess.CalledProcessError(1, "pip", stderr="network error, no missing pkg line")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        # --no-index 缓存解析失败
        raise subprocess.CalledProcessError(1, cmd, stderr="not in cache")

    def fake_stream(cmd: list[str]) -> CompletedStub:
        # -i index 下载失败（无 missing 包）
        raise err

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    with pytest.raises(DependencyError, match="依赖下载失败"):
        download_wheels(("numpy",), "3.8.10", "https://idx/simple", tmp_path / "cache")


# ---------- _find_uv / _resolve_with_uv / _download_online ----------


def test_find_uv_returns_path_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 可用时返回路径."""
    monkeypatch.setattr("fspack.packaging.wheels.shutil.which", lambda name: "/usr/local/bin/uv")
    assert _find_uv() == "/usr/local/bin/uv"


def test_find_uv_returns_none_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 不可用时返回 None."""
    monkeypatch.setattr("fspack.packaging.wheels.shutil.which", lambda name: None)
    assert _find_uv() is None


def test_resolve_with_uv_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip compile 成功时返回 name==version 列表."""
    # uv pip compile 输出格式：每行 "name==version"，含注释行（# 开头）
    fake_output = "numpy==1.24.0\n  # via -r -\nrequests==2.31.0\n  # via -r -\n"
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["input"] = kw.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_output, stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    ctx = _make_ctx([], uv_path="/usr/bin/uv")
    result = _resolve_with_uv(ctx, ("numpy>=1.0", "requests"))
    assert result == fake_output
    # 验证命令含 uv pip compile 和目标参数
    assert "pip" in captured["cmd"]
    assert "compile" in captured["cmd"]
    assert "--python-version" in captured["cmd"]
    assert "3.11" in captured["cmd"]
    assert "--python-platform" in captured["cmd"]
    assert "windows" in captured["cmd"]
    assert "--index-url" in captured["cmd"]
    # stdin 传入需求列表
    assert "numpy>=1.0" in captured["input"]
    assert "requests" in captured["input"]


def test_resolve_with_uv_linux_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台标签映射到 --python-platform linux."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="pkg==1.0\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    ctx = _make_ctx([], platform_tags=("manylinux2014_x86_64",), uv_path="/usr/bin/uv")
    _resolve_with_uv(ctx, ("pkg",))
    assert "linux" in captured["cmd"]


def test_uv_python_platform_mapping() -> None:
    """平台标签到 uv --python-platform 三值映射：windows/macos/linux."""
    from fspack.packaging.wheels.uv_bridge import _uv_python_platform

    # Windows：任一 tag 含 win
    assert _uv_python_platform(("win_amd64",)) == "windows"
    assert _uv_python_platform(("win32", "win_amd64")) == "windows"
    # macOS：任一 tag 以 macosx 开头（含 x86_64 与 arm64 变体）
    assert _uv_python_platform(("macosx_11_0_arm64",)) == "macos"
    assert _uv_python_platform(("macosx_10_15_x86_64", "macosx_11_0_arm64")) == "macos"
    # Linux：manylinux/musllinux 等
    assert _uv_python_platform(("manylinux2014_x86_64",)) == "linux"
    assert _uv_python_platform(("manylinux_2_28_x86_64", "musllinux_1_1_x86_64")) == "linux"
    # 空列表回退 linux（与旧行为一致）
    assert _uv_python_platform(()) == "linux"


def test_resolve_with_uv_macos_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """macosx 平台标签映射到 --python-platform macos（而非 linux）."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="pkg==1.0\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    ctx = _make_ctx([], platform_tags=("macosx_11_0_arm64",), uv_path="/usr/bin/uv")
    _resolve_with_uv(ctx, ("pkg",))
    cmd = captured["cmd"]
    assert "macos" in cmd
    assert "linux" not in cmd


def test_resolve_with_uv_freethreaded_pyversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """free-threaded py_version（3.13.14t）传纯数字 3.13 给 uv pip compile.

    uv 不识别 ``--python-version 3.13t``（报 "found t, which is not part of a
    valid version"），须剥离 t 后缀；compile 阶段仅解析版本号，freethreaded
    wheel（cp313t abi）的实际选择由后续 pip download 的 ``--abi cp313t`` 完成。
    """
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="pkg==1.0\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    ctx = _make_ctx([], py_version="3.13.14t", uv_path="/usr/bin/uv")
    _resolve_with_uv(ctx, ("pkg",))
    cmd = captured["cmd"]
    assert "--python-version" in cmd
    pv_idx = cmd.index("--python-version") + 1
    assert cmd[pv_idx] == "3.13"


def test_resolve_with_uv_no_uv_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 不可用时抛 DependencyError（ctx.uv_path 为 None）."""
    with pytest.raises(DependencyError, match="未找到 uv"):
        _resolve_with_uv(_make_ctx([], uv_path=None), ("numpy",))


def test_resolve_with_uv_empty_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 输出无匹配行时抛 DependencyError."""

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    with pytest.raises(DependencyError, match="未解析出任何依赖"):
        _resolve_with_uv(_make_ctx([], uv_path="/usr/bin/uv"), ("numpy",))


def test_resolve_with_uv_calledprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip compile 非零退出时抛 CalledProcessError（供 _download_online 捕获回退）."""

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="resolution failed")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        _resolve_with_uv(_make_ctx([], uv_path="/usr/bin/uv"), ("numpy",))


def test_download_online_uv_resolved_uses_no_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 解析成功时并行 pip download --no-deps 下载，每个包独立调用."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0\nrequests==2.31.0\n",
    )
    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured_cmds.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy>=1.0"], _make_ctx(base_args, cache))
    # 2 个包并行下载，触发 2 次 subprocess.run 调用
    assert len(captured_cmds) == 2
    for cmd in captured_cmds:
        assert "--no-deps" in cmd
    # 每个命令包含一个精确版本需求（而非 -r requirements.txt）
    all_args = {arg for cmd in captured_cmds for arg in cmd}
    assert "numpy==1.24.0" in all_args
    assert "requests==2.31.0" in all_args
    assert "-r" not in all_args
    assert "--progress-bar" not in all_args
    # 无临时 requirements 文件残留
    assert not (cache / ".requirements-resolved.txt").exists()


def test_download_online_uv_fails_falls_back_to_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 解析失败时回退到 pip 完整解析+下载."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "uv", stderr="fail")),
    )
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy"], _make_ctx(base_args, cache))
    cmd = captured["cmd"]
    assert "--no-deps" not in cmd
    assert "-i" in cmd
    assert "https://idx/simple" in cmd


def test_download_online_uv_empty_resolved_falls_back_to_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 解析输出无有效 name==version 行（空列表）时回退到 pip 完整解析.

    回归场景：``_extract_resolved_lines`` 返回 ``[]``（非 None）时旧实现继续走
    并行下载，``min(8, 0)`` 使 ``ThreadPoolExecutor(max_workers=0)`` 抛 ValueError
    且跳过 pip 回退。修复后空列表视为解析失败，赋 ``resolved=None`` 走 pip。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    # 输出非空（通过 _resolve_with_uv 的空输出检查）但无有效 name==version 行
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "  # via -r -\n# frozen requirements\n",
    )
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy"], _make_ctx(base_args, cache))
    # 走 pip 完整解析路径：含 -i index，不含并行 --no-deps
    cmd = captured["cmd"]
    assert "--no-deps" not in cmd
    assert "-i" in cmd
    assert "https://idx/simple" in cmd


def test_download_online_no_uv_uses_pip_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 不可用时直接用 pip 完整解析+下载."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy"], _make_ctx(base_args, cache))
    cmd = captured["cmd"]
    assert "--no-deps" not in cmd
    assert "-i" in cmd
    assert "https://idx/simple" in cmd


def test_download_online_sdist_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip 下载失败且含 missing 包时走 sdist 回退."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    call_count = {"index_download": 0, "pip_wheel": 0}
    whl_name = "odfpy-1.4.1-py3-none-any.whl"

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_stream(cmd: list[str]) -> _Result:
        if "wheel" in cmd and "--no-deps" in cmd:
            call_count["pip_wheel"] += 1
            (cache / whl_name).write_bytes(b"odfpy")
            return _Result()
        call_count["index_download"] += 1
        if call_count["index_download"] == 1:
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
                "ERROR: No matching distribution found for odfpy>=1.4.1",
            )
        r = _Result()
        r.stdout = f"Saved {whl_name}\n"
        return r

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["odfpy>=1.4.1"], _make_ctx(base_args, cache, py_version="3.8.10"))
    assert call_count["index_download"] == 2
    assert call_count["pip_wheel"] == 1


def test_download_wheels_uv_path_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 集成测试：--no-index 失败 → uv 解析 → 并行 pip --no-deps 下载."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0\n",
    )

    # --no-index 走 subprocess.run 失败（缓存未命中）；
    # 单包路径走 _stream_subprocess 成功（stream=True 流式输出 pip 进度条）
    download_calls = {"no_index": 0, "download": 0}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        if "--no-index" in cmd:
            download_calls["no_index"] += 1
            raise subprocess.CalledProcessError(1, cmd, stderr="not in cache")
        # pip download --no-deps <pkg>==<ver> 单包下载成功
        download_calls["download"] += 1
        (cache / whl_name).write_bytes(b"numpy")
        r = CompletedStub()
        r.stdout = f"Saved {whl_name}\n"
        return r

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_run)
    result = download_wheels(("numpy>=1.0",), "3.11.9", "https://idx/simple", cache)
    assert any(p.name == whl_name for p in result)
    assert download_calls["no_index"] == 1
    assert download_calls["download"] == 1


def test_download_online_uv_sdist_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 路径 sdist 回退：单包 pip download --no-deps 失败 → pip wheel 构建 → 重试成功."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "win-unicode-console-0.5-py3-none-any.whl"
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "win-unicode-console==0.5\n",
    )
    call_count = {"pip_download": 0, "pip_wheel": 0}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    # 单包路径调 _stream_subprocess（pip download --no-deps，stream=True）；
    # sdist 构建也走 _stream_subprocess（pip wheel --no-deps）
    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        call_count["pip_download"] += 1
        if call_count["pip_download"] == 1:
            # 第一次 pip download --no-deps 失败（无 wheel）
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="ERROR: Could not find a version that satisfies the requirement win-unicode-console==0.5 (from versions: none)\n"
                "ERROR: No matching distribution found for win-unicode-console==0.5",
            )
        # 第二次 pip download --no-deps -i index 重试成功（sdist 构建的 wheel 在缓存）
        r = _Result()
        r.stdout = f"Saved {whl_name}\n"
        return r

    def fake_stream(cmd: list[str]) -> _Result:
        # pip wheel --no-deps 构建路径（_build_sdist_wheels 中调用）
        if "wheel" in cmd and "--no-deps" in cmd and "-w" in cmd:
            call_count["pip_wheel"] += 1
            (cache / whl_name).write_bytes(b"wuc")
            return _Result()
        # pip download --no-deps 单包路径（stream=True 流式输出）
        return fake_run(cmd)

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache), "--only-binary=:all:"]
    result = _download_online(["win-unicode-console"], _make_ctx(base_args, cache, py_version="3.8.10"))
    assert call_count["pip_download"] == 2  # 第一次失败，第二次重试成功
    assert call_count["pip_wheel"] == 1  # sdist 构建一次
    assert f"Saved {whl_name}" in result.stdout


# ---------- _download_resolved_parallel / _download_one_resolved / _merge_parallel_results ----------


def test_download_resolved_parallel_multiple_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多包场景并行下载：每个包触发独立 subprocess.run 调用，结果合并 stdout."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    resolved_pkgs = ["numpy==1.24.0", "requests==2.31.0", "rich==13.5.0"]
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "\n".join(resolved_pkgs) + "\n",
    )
    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured_cmds.append(cmd)
        # 从 cmd 末尾取包名作为 wheel 文件名（模拟 pip download 输出）
        req = cmd[-1]
        pkg_name = req.split("==")[0]
        r = CompletedStub()
        r.stdout = f"Saved {pkg_name}-wheel.whl\n"
        return r

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    result = _download_online(["numpy>=1.0"], _make_ctx(base_args, cache))
    # 3 个包触发 3 次并行调用
    assert len(captured_cmds) == 3
    # 合并 stdout 包含所有包的 Saved 行
    assert "Saved numpy-wheel.whl" in result.stdout
    assert "Saved requests-wheel.whl" in result.stdout
    assert "Saved rich-wheel.whl" in result.stdout
    # 每个命令独立含 --no-deps 和精确版本需求
    for cmd in captured_cmds:
        assert "--no-deps" in cmd
        assert cmd[-1] in resolved_pkgs


def test_download_online_freethreaded_skips_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """free-threaded 版本跳过 uv 解析，直接 pip 完整解析按 --abi 选版本.

    uv 无 abi 参数且不识别 t 后缀，按标准 cp3XX 解析出的精确版本可能无
    cp3XXt wheel（如 numpy 2.5.x 仅发布 cp314t），须由 pip 完整解析按
    ``--abi cp3XXt`` 约束重新选版本（如回退到 2.4.6）。
    """

    def _uv_must_not_be_probed() -> str | None:
        raise AssertionError("free-threaded 版本不应探测/使用 uv")

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", _uv_must_not_be_probed)
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy"], _make_ctx(base_args, cache, py_version="3.13.14t"))
    cmd = captured["cmd"]
    # pip 完整解析：不带 --no-deps（精确版本由 pip 按 abi 约束自行解析）
    assert "--no-deps" not in cmd
    assert "-i" in cmd
    assert "https://idx/simple" in cmd


def test_download_wheels_sanitizes_pypi_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pypi_index 防御性清理：strip markdown 反引号/引号/空白包裹.

    从文档复制 URL 常带入 `` `https://...` `` 形式，pip/uv 不识别反引号导致
    "Invalid URL" 或 DNS 解析失败；入口处清理后所有下游命令（-i/--index-url）
    均为干净 URL。
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd)
        # --no-index 缓存解析失败，触发在线回退
        raise subprocess.CalledProcessError(1, "pip", stderr="not in cache")

    def fake_stream(cmd: list[str]) -> CompletedStub:
        calls.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    download_wheels(("numpy",), "3.11.9", "  `https://idx/simple`  ", tmp_path / "cache")
    assert len(calls) >= 2
    # 在线回退命令中的 -i 值为清理后的干净 URL
    online_cmd = calls[1]
    i_idx = online_cmd.index("-i") + 1
    assert online_cmd[i_idx] == "https://idx/simple"


def test_download_resolved_parallel_single_retry_error_converted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单包 sdist 回退后重试仍失败：转 DependencyError 而非裸 CalledProcessError.

    与 ``_run_pip`` 的异常约定一致（CalledProcessError 转 DependencyError 含
    stderr），避免裸 CalledProcessError 逃逸到 CLI 显示原始 traceback。
    """
    cache = tmp_path / "cache"
    cache.mkdir()

    def fake_stream(cmd: list[str]) -> CompletedStub:
        raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: No matching distribution found")

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr(
        "fspack.packaging.wheels.parallel._handle_sdist_fallback",
        lambda *a, **kw: None,
    )
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    ctx = _make_ctx(base_args, cache)
    ctx.uv_path = None
    with pytest.raises(DependencyError, match="No matching distribution"):
        _download_resolved_parallel(["numpy==2.5.2"], ctx)


def test_download_resolved_parallel_uses_configured_pypi_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并行下载路径必须传 ``-i <pypi_index>``，使用用户配置的镜像源而非 pip 默认 pypi.org.

    回归场景：``_download_worker`` 旧实现 ``with_index=False`` 导致 ``pip download``
    不传 ``-i``，pip 回退到默认 pypi.org（国内访问慢/超时，构建卡死）。
    修复后 ``with_index=True``，单包与多包路径均附加 ``-i <pypi_index>``。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    # 单包场景（用户的 pygame 卡死场景）
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "pygame==2.5.0\n",
    )
    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured_cmds.append(cmd)
        r = CompletedStub()
        r.stdout = "Saved pygame-wheel.whl\n"
        return r

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["pygame"], _make_ctx(base_args, cache, pypi_index="https://mirrors.aliyun.com/pypi/simple/"))
    # 单包路径触发 1 次调用，必须含 -i <pypi_index>
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert "-i" in cmd
    assert "https://mirrors.aliyun.com/pypi/simple/" in cmd


def test_download_resolved_parallel_partial_failure_sdist_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并行下载部分失败：失败包触发 sdist 回退，重试仅针对失败包."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    # numpy 成功，odfpy 失败（无 wheel，仅 sdist）
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0\nodfpy==1.4.1\n",
    )
    call_count = {"numpy_download": 0, "odfpy_download": 0, "pip_wheel": 0, "odfpy_retry": 0}
    # odfpy 调用计数：首次失败（无 wheel），sdist 构建后重试成功
    odfpy_calls = {"count": 0}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        req = cmd[-1]
        if "numpy==1.24.0" in req:
            call_count["numpy_download"] += 1
            r = CompletedStub()
            r.stdout = "Saved numpy-wheel.whl\n"
            return r
        # odfpy 路径：首次失败，重试成功（用计数器区分，因两条路径都带 -i）
        if "odfpy==1.4.1" in req:
            odfpy_calls["count"] += 1
            if odfpy_calls["count"] == 1:
                call_count["odfpy_download"] += 1
                raise subprocess.CalledProcessError(
                    1,
                    cmd,
                    stderr="ERROR: Could not find a version that satisfies the requirement odfpy==1.4.1 (from versions: none)\n"
                    "ERROR: No matching distribution found for odfpy==1.4.1",
                )
            call_count["odfpy_retry"] += 1
            r = CompletedStub()
            r.stdout = "Saved odfpy-wheel.whl\n"
            return r
        r = CompletedStub()
        return r

    def fake_stream(cmd: list[str]) -> CompletedStub:
        # pip wheel --no-deps 构建路径
        if "wheel" in cmd and "--no-deps" in cmd and "-w" in cmd:
            call_count["pip_wheel"] += 1
            return CompletedStub()
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache), "--only-binary=:all:"]
    result = _download_online(
        ["numpy>=1.0", "odfpy>=1.4.1"],
        _make_ctx(base_args, cache, py_version="3.8.10"),
    )
    # numpy 下载成功（1 次），odfpy 首次失败（1 次），sdist 构建（1 次），odfpy 重试成功（1 次）
    assert call_count["numpy_download"] == 1
    assert call_count["odfpy_download"] == 1
    assert call_count["pip_wheel"] == 1
    assert call_count["odfpy_retry"] == 1
    # 合并 stdout 包含 numpy 和 odfpy 的 Saved 行
    assert "Saved numpy-wheel.whl" in result.stdout
    assert "Saved odfpy-wheel.whl" in result.stdout


def test_download_resolved_parallel_multi_sdist_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并行下载多包失败：合并所有失败包 stderr 触发 sdist 回退，确保所有 sdist-only 包被构建.

    回归场景：win-unicode-console==0.5 等多个 sdist-only 包并行下载时，
    旧实现仅取 failed[0][1].stderr 解析缺失包，导致第二个失败包的 stderr
    被丢弃，sdist 构建遗漏，重试仍失败。修复后合并所有失败包 stderr。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    # 3 个包：numpy 成功，odfpy 与 win-unicode-console 均仅 sdist
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0\nodfpy==1.4.1\nwin-unicode-console==0.5\n",
    )
    call_count = {
        "numpy_download": 0,
        "odfpy_download": 0,
        "wuc_download": 0,
        "pip_wheel": 0,
        "odfpy_retry": 0,
        "wuc_retry": 0,
    }
    # 各包调用计数：首次失败（无 wheel），sdist 构建后重试成功
    # 用计数器区分首次/重试，因两条路径都带 -i（with_index=True）
    odfpy_calls = {"count": 0}
    wuc_calls = {"count": 0}
    # 记录 pip wheel 构建的包名，验证两个 sdist-only 包都被构建
    built_pkgs: list[str] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        req = cmd[-1]
        if "numpy==1.24.0" in req:
            call_count["numpy_download"] += 1
            r = CompletedStub()
            r.stdout = "Saved numpy-wheel.whl\n"
            return r
        if "odfpy==1.4.1" in req:
            odfpy_calls["count"] += 1
            if odfpy_calls["count"] == 1:
                call_count["odfpy_download"] += 1
                raise subprocess.CalledProcessError(
                    1,
                    cmd,
                    stderr="ERROR: Could not find a version that satisfies the requirement odfpy==1.4.1 (from versions: none)\n"
                    "ERROR: No matching distribution found for odfpy==1.4.1",
                )
            call_count["odfpy_retry"] += 1
            r = CompletedStub()
            r.stdout = "Saved odfpy-wheel.whl\n"
            return r
        if "win-unicode-console==0.5" in req:
            wuc_calls["count"] += 1
            if wuc_calls["count"] == 1:
                call_count["wuc_download"] += 1
                raise subprocess.CalledProcessError(
                    1,
                    cmd,
                    stderr="ERROR: Could not find a version that satisfies the requirement win-unicode-console==0.5 (from versions: none)\n"
                    "ERROR: No matching distribution found for win-unicode-console==0.5",
                )
            call_count["wuc_retry"] += 1
            r = CompletedStub()
            r.stdout = "Saved win_unicode_console-wheel.whl\n"
            return r
        r = CompletedStub()
        return r

    def fake_stream(cmd: list[str]) -> CompletedStub:
        # pip wheel --no-deps 构建路径：从 cmd 提取被构建的包名
        if "wheel" in cmd and "--no-deps" in cmd and "-w" in cmd:
            call_count["pip_wheel"] += 1
            # pip wheel 命令格式：... -w <dir> --no-deps <pkg>==<ver>
            # 取最后一个非版本参数作为包名
            for arg in reversed(cmd):
                if "==" in arg and not arg.startswith("-"):
                    built_pkgs.append(arg)
                    break
            return CompletedStub()
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache), "--only-binary=:all:"]
    result = _download_online(
        ["numpy>=1.0", "odfpy>=1.4.1", "win-unicode-console==0.5"],
        _make_ctx(base_args, cache, py_version="3.8.10"),
    )
    # numpy 成功，odfpy 与 wuc 首次下载均失败
    assert call_count["numpy_download"] == 1
    assert call_count["odfpy_download"] == 1
    assert call_count["wuc_download"] == 1
    # sdist 构建必须触发 2 次（odfpy + wuc），证明合并 stderr 解析出两个缺失包
    assert call_count["pip_wheel"] == 2
    # 两个失败包都被重试
    assert call_count["odfpy_retry"] == 1
    assert call_count["wuc_retry"] == 1
    # 验证两个 sdist-only 包都被 pip wheel 构建
    assert any("odfpy==1.4.1" in p for p in built_pkgs)
    assert any("win-unicode-console==0.5" in p for p in built_pkgs)
    # 合并 stdout 包含所有 3 个包的 Saved 行
    assert "Saved numpy-wheel.whl" in result.stdout
    assert "Saved odfpy-wheel.whl" in result.stdout
    assert "Saved win_unicode_console-wheel.whl" in result.stdout


def test_download_resolved_parallel_dependency_error_collected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 内 DependencyError（如 pip 消失）也收集进 failed 走 sdist 回退，不逃逸.

    回归场景：``_download_one_resolved`` 将 FileNotFoundError 转为
    :class:`DependencyError`，旧实现 ``except subprocess.CalledProcessError`` 捕不到，
    异常从 ``future.result()`` 逃逸直接崩溃，跳过 sdist 回退。修复后 except 元组
    扩为 ``(CalledProcessError, DependencyError)``，DependencyError 以 ``str(e)``
    参与合并 stderr。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0\nodfpy==1.4.1\n",
    )
    # numpy 成功，odfpy 首次抛 FileNotFoundError（→ DependencyError），重试成功
    odfpy_calls = {"count": 0}
    fallback_messages: list[str] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        req = cmd[-1]
        if "numpy==1.24.0" in req:
            r = CompletedStub()
            r.stdout = "Saved numpy-wheel.whl\n"
            return r
        if "odfpy==1.4.1" in req:
            odfpy_calls["count"] += 1
            if odfpy_calls["count"] == 1:
                # 模拟 pip 解释器消失：_download_one_resolved 转 DependencyError
                raise FileNotFoundError("no such file or directory: /py/python")
            r = CompletedStub()
            r.stdout = "Saved odfpy-wheel.whl\n"
            return r
        return CompletedStub()

    def fake_fallback(e: DependencyError, py: str, pypi_index: str, cache_dir: Path, **kw: object) -> list[str]:
        fallback_messages.append(str(e))
        return []

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    # 并行失败路径在 parallel 模块内触发 sdist 回退，patch 定义所在模块
    monkeypatch.setattr("fspack.packaging.wheels.parallel._handle_sdist_fallback", fake_fallback)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache), "--only-binary=:all:"]
    result = _download_online(["numpy>=1.0", "odfpy>=1.4.1"], _make_ctx(base_args, cache, py_version="3.8.10"))
    # DependencyError 未逃逸：触发 sdist 回退（合并 str(e) 作为 stderr 文本）
    assert len(fallback_messages) == 1
    assert "未找到 pip" in fallback_messages[0]
    # odfpy 首次失败（DependencyError 收集）+ sdist 后重试成功
    assert odfpy_calls["count"] == 2
    assert "Saved numpy-wheel.whl" in result.stdout
    assert "Saved odfpy-wheel.whl" in result.stdout


def test_download_one_resolved_with_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_download_one_resolved with_index=True 时附加 -i <pypi_index>."""
    cache = tmp_path / "cache"
    cache.mkdir()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    ctx = _make_ctx(base_args, cache, find_links=(str(cache),))
    _download_one_resolved("numpy==1.24.0", ctx, with_index=True)
    cmd = captured["cmd"]
    assert "-i" in cmd
    assert "https://idx/simple" in cmd
    assert "--no-deps" in cmd
    assert "numpy==1.24.0" in cmd
    assert "--find-links" in cmd


def test_download_one_resolved_without_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_download_one_resolved with_index=False 时不附加 -i."""
    cache = tmp_path / "cache"
    cache.mkdir()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_one_resolved("numpy==1.24.0", _make_ctx(base_args, cache), with_index=False)
    cmd = captured["cmd"]
    assert "-i" not in cmd
    assert "--no-deps" in cmd
    assert "numpy==1.24.0" in cmd


def test_download_one_resolved_pip_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_download_one_resolved 在 pip 消失时抛 DependencyError."""
    cache = tmp_path / "cache"
    cache.mkdir()

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        raise FileNotFoundError(2, "No such file", cmd[0])

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    with pytest.raises(DependencyError, match="未找到 pip"):
        _download_one_resolved("numpy==1.24.0", _make_ctx(base_args, cache), with_index=False)


def test_merge_parallel_results_concat_stdout() -> None:
    """_merge_parallel_results 拼接各任务 stdout，stderr 留空."""
    r1 = subprocess.CompletedProcess(args=[], returncode=0, stdout="Saved a.whl\n", stderr="err1")
    r2 = subprocess.CompletedProcess(args=[], returncode=0, stdout="Saved b.whl\n", stderr="err2")
    merged = _merge_parallel_results([("a==1.0", r1), ("b==2.0", r2)])
    assert "Saved a.whl" in merged.stdout
    assert "Saved b.whl" in merged.stdout
    assert merged.stderr == ""
    assert merged.returncode == 0


def test_merge_parallel_results_skip_empty_stdout() -> None:
    """_merge_parallel_results 跳过空 stdout 任务."""
    r1 = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    r2 = subprocess.CompletedProcess(args=[], returncode=0, stdout="Saved b.whl\n", stderr="")
    merged = _merge_parallel_results([("a==1.0", r1), ("b==2.0", r2)])
    assert merged.stdout == "Saved b.whl\n"


# ---------- _stream_subprocess ----------


_FAKE_STDOUT_FD = 3
_FAKE_STDERR_FD = 4


class _FakePipe:
    """模拟管道，提供 ``read()``、``fileno()`` 和分块读取."""

    def __init__(self, data: bytes, fd: int) -> None:
        self._data = data
        self._pos = 0
        self._fd = fd

    def read(self) -> bytes:
        result = self._data[self._pos :]
        self._pos = len(self._data)
        return result

    def fileno(self) -> int:
        return self._fd

    def read_chunk(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakePopen:
    """模拟 ``subprocess.Popen``，配合 ``_stream_subprocess`` 测试."""

    def __init__(self, cmd: list[str], stdout_bytes: bytes, stderr_bytes: bytes, returncode: int) -> None:
        self.args = cmd
        self.stdout = _FakePipe(stdout_bytes, _FAKE_STDOUT_FD)
        self.stderr = _FakePipe(stderr_bytes, _FAKE_STDERR_FD)
        self._returncode = returncode

    def wait(self) -> int:
        return self._returncode


def _patch_os_read_for(monkeypatch: pytest.MonkeyPatch, popen: _FakePopen) -> None:
    """mock ``os.read`` 按 fd 从 ``popen`` 的管道取数据."""
    pipes = {popen.stdout._fd: popen.stdout, popen.stderr._fd: popen.stderr}

    def fake_read(fd: int, n: int) -> bytes:
        pipe = pipes.get(fd)
        if pipe is None:
            return b""
        return pipe.read_chunk(n)

    monkeypatch.setattr("fspack.packaging.wheels.os.read", fake_read)


def _patch_stderr_buffer(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """替换 ``sys.stderr.buffer``，返回写入的字节块列表."""
    written: list[bytes] = []

    class _FakeBuffer:
        def write(self, data: bytes) -> int:
            written.append(data)
            return len(data)

        def flush(self) -> None:
            pass

    fake_stderr = types.SimpleNamespace(buffer=_FakeBuffer(), write=lambda s: None, flush=lambda: None)
    monkeypatch.setattr("fspack.packaging.wheels.sys.stderr", fake_stderr)
    return written


def test_stream_subprocess_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功时返回 CompletedProcess，stdout/stderr 正确捕获，stderr 实时写入终端."""
    written = _patch_stderr_buffer(monkeypatch)

    def fake_popen(cmd: list[str], **kw: Any) -> _FakePopen:
        popen = _FakePopen(cmd, stdout_bytes=b"saved wheel\n", stderr_bytes=b"Downloading pkg", returncode=0)
        _patch_os_read_for(monkeypatch, popen)
        return popen

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.Popen", fake_popen)
    result = _stream_subprocess(["pip", "download"])
    assert result.returncode == 0
    assert result.stdout == "saved wheel\n"
    assert result.stderr == "Downloading pkg"
    # stderr 被实时写入 sys.stderr.buffer
    assert b"".join(written) == b"Downloading pkg"


def test_stream_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败时抛出 CalledProcessError，含 stdout/stderr."""
    _patch_stderr_buffer(monkeypatch)

    def fake_popen(cmd: list[str], **kw: Any) -> _FakePopen:
        popen = _FakePopen(cmd, stdout_bytes=b"out", stderr_bytes=b"err msg", returncode=1)
        _patch_os_read_for(monkeypatch, popen)
        return popen

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.Popen", fake_popen)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _stream_subprocess(["pip"])
    assert exc_info.value.returncode == 1
    assert exc_info.value.stdout == "out"
    assert exc_info.value.stderr == "err msg"


def test_stream_subprocess_file_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Popen 抛 FileNotFoundError 时透传（pip 解释器不存在）."""
    monkeypatch.setattr(
        "fspack.packaging.wheels.subprocess.Popen", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError())
    )
    with pytest.raises(FileNotFoundError):
        _stream_subprocess(["/missing/cmd"])


def test_stream_subprocess_multibyte_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """多字节 stderr（中文）正确解码，不抛 UnicodeDecodeError."""
    _patch_stderr_buffer(monkeypatch)

    def fake_popen(cmd: list[str], **kw: Any) -> _FakePopen:
        popen = _FakePopen(cmd, stdout_bytes=b"", stderr_bytes="下载中\n".encode(), returncode=0)
        _patch_os_read_for(monkeypatch, popen)
        return popen

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.Popen", fake_popen)
    result = _stream_subprocess(["cmd"])
    assert result.stderr == "下载中\n"


# ---------- _run_pip stream 参数 ----------


def test_run_pip_stream_uses_stream_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=True 时调用 _stream_subprocess 而非 subprocess.run."""
    stream_called = False

    def fake_stream(cmd: list[str]) -> CompletedStub:
        nonlocal stream_called
        stream_called = True
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr(
        "fspack.packaging.wheels.subprocess.run",
        lambda cmd, **kw: (_ for _ in ()).throw(AssertionError("不应调用 subprocess.run")),
    )
    result = _run_pip(["pip"], "label", stream=True)
    assert stream_called is True
    assert result is not None
    assert result.returncode == 0


def test_run_pip_stream_false_uses_subprocess_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=False 时调用 subprocess.run 而非 _stream_subprocess."""
    run_called = False

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        nonlocal run_called
        run_called = True
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr(
        "fspack.packaging.wheels.downloader._stream_subprocess",
        lambda cmd: (_ for _ in ()).throw(AssertionError("不应调用 _stream_subprocess")),
    )
    _run_pip(["pip"], "label", stream=False)
    assert run_called is True


def test_run_pip_stream_suppress_error_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=True + suppress_error=True 时 CalledProcessError 返回 None."""

    def fake_stream(cmd: list[str]) -> CompletedStub:
        raise subprocess.CalledProcessError(1, cmd, stderr="fail")

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    result = _run_pip(["pip"], "label", stream=True, suppress_error=True)
    assert result is None


def test_run_pip_stream_failure_raises_dependency_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=True + suppress_error=False 时 CalledProcessError 转为 DependencyError."""

    def fake_stream(cmd: list[str]) -> CompletedStub:
        raise subprocess.CalledProcessError(1, cmd, stderr="download failed")

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    with pytest.raises(DependencyError, match="依赖下载失败"):
        _run_pip(["pip"], "label", stream=True)


def test_run_pip_stream_file_not_found_raises_dependency_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=True 时 FileNotFoundError 转为 DependencyError."""
    monkeypatch.setattr(
        "fspack.packaging.wheels.downloader._stream_subprocess", lambda cmd: (_ for _ in ()).throw(FileNotFoundError())
    )
    with pytest.raises(DependencyError, match="未找到 pip"):
        _run_pip(["/missing/pip"], "label", stream=True)


# ---------- 私有包源（extra_index_urls / find_links）----------


def test_deps_cache_key_includes_private_sources() -> None:
    """私有包源变化时缓存键不同，避免误命中旧缓存."""
    k1 = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    k2 = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",), ("https://pypi.company.com/simple/",))
    k3 = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",), (), ("./wheels",))
    k4 = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",), ("https://pypi.company.com/simple/",), ("./wheels",))
    assert k1 != k2, "extra_index_urls 变化应产生不同缓存键"
    assert k1 != k3, "find_links 变化应产生不同缓存键"
    assert k1 != k4, "私有包源变化应产生不同缓存键"
    assert k2 != k4, "find_links 增减应产生不同缓存键"


def test_deps_cache_key_private_sources_order_independent() -> None:
    """私有包源顺序不影响缓存键（list 序列化顺序敏感，但同顺序应稳定）."""
    k1 = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",), ("a", "b"), ("c",))
    k2 = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",), ("a", "b"), ("c",))
    assert k1 == k2


def test_resolve_with_uv_passes_extra_index_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip compile 命令含 --extra-index-url 参数."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="pkg==1.0\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    ctx = _make_ctx(
        [],
        uv_path="/usr/bin/uv",
        extra_index_urls=("https://pypi.company.com/simple/", "https://mirror.example.com/pypi"),
    )
    _resolve_with_uv(ctx, ("pkg",))
    cmd = captured["cmd"]
    assert cmd.count("--extra-index-url") == 2
    assert "https://pypi.company.com/simple/" in cmd
    assert "https://mirror.example.com/pypi" in cmd


def test_resolve_with_uv_passes_find_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip compile 命令含 --find-links 参数."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="pkg==1.0\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    ctx = _make_ctx([], uv_path="/usr/bin/uv", find_links=("./wheels", "https://example.com/wheels/"))
    _resolve_with_uv(ctx, ("pkg",))
    cmd = captured["cmd"]
    assert cmd.count("--find-links") == 2
    assert "./wheels" in cmd
    assert "https://example.com/wheels/" in cmd


def test_resolve_with_uv_no_private_sources_omits_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """无私有包源时 uv 命令不含 --extra-index-url/--find-links."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="pkg==1.0\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    _resolve_with_uv(_make_ctx([], uv_path="/usr/bin/uv"), ("pkg",))
    cmd = captured["cmd"]
    assert "--extra-index-url" not in cmd
    assert "--find-links" not in cmd


def test_download_online_uv_resolved_passes_extra_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 解析成功路径：并行 pip download --no-deps 命令含私有包源参数."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)

    def fake_resolve(ctx: DownloadContext, pkgs: Sequence[str], **kw: Any) -> str:
        # 验证 uv 解析也收到私有包源
        assert ctx.extra_index_urls == ("https://pypi.company.com/simple/",)
        assert ctx.find_links == ("./wheels",)
        return "numpy==1.24.0\n"

    monkeypatch.setattr("fspack.packaging.wheels.resolver._resolve_with_uv", fake_resolve)
    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured_cmds.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    ctx = _make_ctx(
        base_args,
        cache,
        extra_index_urls=("https://pypi.company.com/simple/",),
        find_links=("./wheels",),
    )
    _download_online(["numpy>=1.0"], ctx)
    # 单包场景触发 1 次 subprocess.run
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert "--no-deps" in cmd
    assert "--extra-index-url" in cmd
    assert "https://pypi.company.com/simple/" in cmd
    assert "--find-links" in cmd
    assert "./wheels" in cmd


def test_download_online_pip_full_passes_extra_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 不可用时 pip 完整解析+下载命令含私有包源参数."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    ctx = _make_ctx(
        base_args,
        cache,
        extra_index_urls=("https://pypi.company.com/simple/",),
        find_links=("./wheels",),
    )
    _download_online(["numpy"], ctx)
    cmd = captured["cmd"]
    assert "--extra-index-url" in cmd
    assert "https://pypi.company.com/simple/" in cmd
    assert "--find-links" in cmd
    assert "./wheels" in cmd


def test_download_online_sdist_fallback_passes_extra_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sdist 回退路径：pip wheel 命令含私有包源参数."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    call_count = {"index_download": 0, "pip_wheel": 0}
    whl_name = "odfpy-1.4.1-py3-none-any.whl"
    pip_wheel_cmds: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_stream(cmd: list[str]) -> _Result:
        if "wheel" in cmd and "--no-deps" in cmd:
            call_count["pip_wheel"] += 1
            pip_wheel_cmds.append(cmd)
            (cache / whl_name).write_bytes(b"odfpy")
            return _Result()
        call_count["index_download"] += 1
        if call_count["index_download"] == 1:
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
                "ERROR: No matching distribution found for odfpy>=1.4.1",
            )
        r = _Result()
        r.stdout = f"Saved {whl_name}\n"
        return r

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    ctx = _make_ctx(
        base_args,
        cache,
        py_version="3.8.10",
        extra_index_urls=("https://pypi.company.com/simple/",),
        find_links=("./wheels",),
    )
    _download_online(["odfpy>=1.4.1"], ctx)
    assert call_count["index_download"] == 2  # 第一次失败，第二次成功
    assert call_count["pip_wheel"] == 1  # sdist 构建一次
    assert len(pip_wheel_cmds) == 1
    wheel_cmd = pip_wheel_cmds[0]
    assert "--extra-index-url" in wheel_cmd
    assert "https://pypi.company.com/simple/" in wheel_cmd
    assert "--find-links" in wheel_cmd
    assert "./wheels" in wheel_cmd


def test_build_sdist_wheels_with_extra_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip wheel --no-deps 命令含私有包源参数."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    cache = tmp_path / "cache"
    cache.mkdir()
    _build_sdist_wheels(
        ["odfpy>=1.4.1"],
        "/py/python",
        "https://idx/simple",
        cache,
        extra_index_urls=("https://pypi.company.com/simple/",),
        find_links=("./wheels",),
    )
    assert len(captured) == 1
    cmd = captured[0]
    assert "--extra-index-url" in cmd
    assert "https://pypi.company.com/simple/" in cmd
    assert "--find-links" in cmd
    assert "./wheels" in cmd


def test_build_sdist_wheels_no_extra_sources_omits_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无私有包源时 pip wheel 命令不含 --extra-index-url/--find-links."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    _build_sdist_wheels(["odfpy>=1.4.1"], "/py/python", "https://idx/simple", tmp_path / "cache")
    cmd = captured[0]
    assert "--extra-index-url" not in cmd
    assert "--find-links" not in cmd


def test_download_wheels_passes_extra_sources_to_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 顶层接口透传私有包源给 pip download（回退路径）."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, "pip", stderr="not in cache")

    def fake_stream(cmd: list[str]) -> CompletedStub:
        calls.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    download_wheels(
        ("numpy",),
        "3.11.9",
        "https://idx/simple",
        tmp_path / "cache",
        extra_index_urls=("https://pypi.company.com/simple/",),
        find_links=("./wheels",),
    )
    # 回退路径的 pip download 命令应含私有包源参数
    pip_download_cmd = next(c for c in calls if "download" in c and "-i" in c)
    assert "--extra-index-url" in pip_download_cmd
    assert "https://pypi.company.com/simple/" in pip_download_cmd
    assert "--find-links" in pip_download_cmd
    assert "./wheels" in pip_download_cmd


def test_download_wheels_cache_miss_when_private_sources_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """私有包源变化时依赖解析缓存不命中，重新调用 pip."""
    call_count = {"pip_run": 0}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        call_count["pip_run"] += 1
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    cache = tmp_path / "cache"
    # 第一次：无私有包源
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache)
    first_count = call_count["pip_run"]
    assert first_count >= 1
    # 第二次：添加私有包源，缓存键不同，应重新调用 pip
    download_wheels(
        ("numpy",),
        "3.11.9",
        "https://idx/simple",
        cache,
        extra_index_urls=("https://pypi.company.com/simple/",),
    )
    assert call_count["pip_run"] > first_count, "私有包源变化时应跳过缓存重新调用 pip"


# ---------- require_hashes 路径（iter-126 补覆盖）----------


def test_download_online_require_hashes_uses_hashes_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """require_hashes=True 且 uv 可用时走 _download_with_hashes：命令含 --require-hashes -r."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0 \\\n  --hash=sha256:abc\n",
    )
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy>=1.0"], _make_ctx(base_args, cache), require_hashes=True)
    cmd = captured["cmd"]
    assert "--require-hashes" in cmd
    assert "-r" in cmd
    # 临时 requirements 文件路径（-r 后跟文件名）
    r_idx = cmd.index("-r")
    assert r_idx + 1 < len(cmd), "-r 后应有文件路径"
    req_file = cmd[r_idx + 1]
    assert "requirements" in req_file or req_file.endswith(".txt")
    # 调用完成后临时文件应被删除
    assert not Path(req_file).exists(), "临时 requirements.txt 应被删除"


def test_download_online_require_hashes_no_uv_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """require_hashes=True 但 uv 不可用时降级为不校验（warning），走 pip 完整解析下载."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    with caplog.at_level("WARNING"):
        _download_online(["numpy>=1.0"], _make_ctx(base_args, cache), require_hashes=True)
    # 降级后走 pip 完整解析（stream=True），命令含 -i index 但不含 --require-hashes
    cmd = captured["cmd"]
    assert "-i" in cmd
    assert "https://idx/simple" in cmd
    assert "--require-hashes" not in cmd
    # warning 记录了降级
    assert any("require_hashes" in rec.message and "降级" in rec.message for rec in caplog.records)


def test_download_wheels_require_hashes_cache_hit_skips_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """require_hashes=True 缓存命中时跳过校验（缓存 wheel 已首次校验）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    # 预置依赖解析缓存
    wheel_file = cache / "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    wheel_file.write_bytes(b"fake wheel")
    _save_deps_cache(cache, _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",), (), ()), [wheel_file])

    # pip 不应被调用
    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        raise AssertionError("缓存命中不应调用 pip")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    wheels = download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache, require_hashes=True)
    assert wheels == [wheel_file]


def test_download_with_hashes_cleanup_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_download_with_hashes 路径：pip 失败时临时 requirements.txt 仍被删除.

    `_run_pip` 把 `CalledProcessError` 转为 `DependencyError`（suppress_error=False），
    但 `finally` 块仍执行清理。本测试断言临时文件不残留。
    """
    from fspack.packaging.wheels.resolver import _download_with_hashes

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0 \\\n  --hash=sha256:abc\n",
    )

    def fake_stream(cmd: list[str]) -> CompletedStub:
        # 模拟 pip 校验失败（hash mismatch）
        raise subprocess.CalledProcessError(1, cmd, stderr="hash mismatch")

    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    # _run_pip 把 CalledProcessError 转为 DependencyError
    with pytest.raises(DependencyError, match="hash mismatch"):
        _download_with_hashes(["numpy>=1.0"], _make_ctx(base_args, cache))
    # 临时 requirements 文件应被清理（finally 块）
    leftover = list(cache.glob("*requirements*.txt"))
    assert not leftover, "失败后临时 requirements.txt 应被 finally 清理"


# ---------- iter-132 uv 下载路径测试 ----------


def test_uv_supports_download_returns_true_when_help_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip download --help 退出码 0 时返回 True."""
    monkeypatch.setattr(
        "fspack.packaging.wheels.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=0),
    )
    assert _uv_supports_download("/usr/bin/uv") is True


def test_uv_supports_download_returns_false_when_help_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip download --help 非零退出时返回 False（uv 0.1.9+ 移除该子命令）."""
    monkeypatch.setattr(
        "fspack.packaging.wheels.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=2),
    )
    assert _uv_supports_download("/usr/bin/uv") is False


def test_uv_supports_download_returns_false_when_uv_path_is_none() -> None:
    """uv_path 为 None 时直接返回 False，不调 subprocess."""
    assert _uv_supports_download(None) is False


def test_uv_supports_download_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip download --help 超时时返回 False."""

    def raise_timeout(*a: object, **kw: object) -> object:
        raise subprocess.TimeoutExpired(cmd=["uv"], timeout=5.0)

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", raise_timeout)
    assert _uv_supports_download("/usr/bin/uv") is False


def test_convert_uv_output_downloaded_to_saved() -> None:
    """uv 'Downloaded <name>.whl' 转为 'Saved <name>.whl'."""
    uv_output = "Resolved 1 package in 10ms\nDownloaded numpy-1.24.0-cp311-cp311-win_amd64.whl\n"
    result = _convert_uv_output_to_pip_format(uv_output)
    assert "Saved numpy-1.24.0-cp311-cp311-win_amd64.whl" in result


def test_convert_uv_output_cached_to_saved() -> None:
    """uv 'Cached <name>.whl' 也转为 'Saved <name>.whl'."""
    uv_output = "Cached requests-2.31.0-py3-none-any.whl\n"
    result = _convert_uv_output_to_pip_format(uv_output)
    assert "Saved requests-2.31.0-py3-none-any.whl" in result


def test_convert_uv_output_empty_returns_empty() -> None:
    """空输入返回空字符串."""
    assert _convert_uv_output_to_pip_format("") == ""
    assert _convert_uv_output_to_pip_format("No wheels here\n") == ""


def test_download_one_with_uv_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip download 成功时返回 stdout 含 'Saved <name>.whl'（兼容 pip 格式）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="Downloaded numpy-1.24.0-cp311-cp311-win_amd64.whl\n",
            stderr="",
        )

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    result = _download_one_with_uv(
        "numpy==1.24.0",
        _make_ctx([], cache, uv_path="/usr/bin/uv"),
        with_index=False,
    )
    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/uv"
    assert "pip" in cmd and "download" in cmd
    assert "--no-deps" in cmd
    assert "-d" in cmd and str(cache) in cmd
    assert "--python-version" in cmd and "3.11" in cmd
    assert "--python-platform" in cmd and "windows" in cmd
    assert "numpy==1.24.0" in cmd
    assert "-i" not in cmd  # with_index=False
    # stdout 转换为 pip 兼容格式
    assert "Saved numpy-1.24.0-cp311-cp311-win_amd64.whl" in result.stdout


def test_download_one_with_uv_with_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """with_index=True 时附加 --index-url."""
    cache = tmp_path / "cache"
    cache.mkdir()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Downloaded a.whl\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    _download_one_with_uv(
        "numpy==1.24.0",
        _make_ctx([], cache, uv_path="/usr/bin/uv"),
        with_index=True,
    )
    cmd = captured["cmd"]
    assert "--index-url" in cmd
    assert "https://idx/simple" in cmd


def test_download_one_with_uv_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 非零退出时抛 CalledProcessError（由调用方捕获回退 pip）."""
    cache = tmp_path / "cache"
    cache.mkdir()

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd, stderr="uv error")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        _download_one_with_uv(
            "numpy==1.24.0",
            _make_ctx([], cache, uv_path="/usr/bin/uv"),
            with_index=False,
        )


def test_download_one_with_uv_macos_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macosx 平台标签映射到 --python-platform macos（而非 linux）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Downloaded a.whl\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    _download_one_with_uv(
        "numpy==1.24.0",
        _make_ctx([], cache, uv_path="/usr/bin/uv", platform_tags=("macosx_11_0_arm64",)),
        with_index=False,
    )
    cmd = captured["cmd"]
    assert "--python-platform" in cmd
    assert "macos" in cmd
    assert "linux" not in cmd


def test_download_one_with_uv_freethreaded_pyversion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """free-threaded py_version（3.14.6t）传纯数字 3.14 给 uv pip download.

    uv 不识别 ``--python-version 3.14t``，须剥离 t 后缀；freethreaded wheel
    （cp314t abi）的实际选择由回退的 pip download ``--abi cp314t`` 完成。
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Downloaded a.whl\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    _download_one_with_uv(
        "numpy==1.24.0",
        _make_ctx([], cache, py_version="3.14.6t", uv_path="/usr/bin/uv"),
        with_index=False,
    )
    cmd = captured["cmd"]
    assert "--python-version" in cmd
    pv_idx = cmd.index("--python-version") + 1
    assert cmd[pv_idx] == "3.14"


def test_download_online_shares_uv_path_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_download_online 顶部调 _find_uv 一次，共享给 require_hashes 检查与 uv 解析."""
    cache = tmp_path / "cache"
    cache.mkdir()
    call_count = {"find_uv": 0}

    def counting_find_uv() -> str:
        call_count["find_uv"] += 1
        return "/usr/bin/uv"

    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", counting_find_uv)
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: False)
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0\n",
    )
    monkeypatch.setattr(
        "fspack.packaging.wheels.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=0, stdout="Saved numpy.whl\n"),
    )
    monkeypatch.setattr(
        "fspack.packaging.wheels.downloader._stream_subprocess",
        lambda cmd: subprocess.CompletedProcess(args=[], returncode=0, stdout="Saved numpy.whl\n"),
    )
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy>=1.0"], _make_ctx(base_args, cache))
    # _find_uv 只调一次（共享），不是两次（require_hashes 检查 + uv 解析检查）
    assert call_count["find_uv"] == 1


def test_download_online_uses_uv_download_when_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 可用且支持 pip download 时，并行下载用 uv pip download 而非 pip download."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: True)
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0\nrequests==2.31.0\n",
    )
    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured_cmds.append(cmd)
        # uv 输出格式
        pkg = cmd[-1].split("==")[0]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=f"Downloaded {pkg}-wheel.whl\n",
            stderr="",
        )

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy>=1.0"], _make_ctx(base_args, cache))
    # 2 个包用 uv 下载，触发 2 次 subprocess.run（不含 --help 检测，因 _uv_supports_download 已 mock）
    assert len(captured_cmds) == 2
    for cmd in captured_cmds:
        assert cmd[0] == "/usr/bin/uv"
        assert "download" in cmd
        assert "--no-deps" in cmd
    # stdout 转换后含 Saved 行
    all_args = {arg for cmd in captured_cmds for arg in cmd}
    assert "numpy==1.24.0" in all_args
    assert "requests==2.31.0" in all_args


def test_download_online_uv_download_fails_falls_back_to_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 下载失败时单包回退到 pip download."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._uv_supports_download", lambda uv_path: True)
    monkeypatch.setattr(
        "fspack.packaging.wheels.resolver._resolve_with_uv",
        lambda ctx, pkgs, **kw: "numpy==1.24.0\n",
    )
    call_log: list[str] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "/usr/bin/uv":
            call_log.append("uv")
            raise subprocess.CalledProcessError(1, cmd, stderr="uv fail")
        call_log.append("pip")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Saved numpy.whl\n", stderr="")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_run)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    result = _download_online(["numpy>=1.0"], _make_ctx(base_args, cache))
    # uv 失败后回退到 pip
    assert "uv" in call_log
    assert "pip" in call_log
    assert "Saved numpy.whl" in result.stdout
