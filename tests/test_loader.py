"""C loader 源码生成与编译测试。."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType
from fspack.exceptions import LoaderError
from fspack.loader import (
    MINGW_GCC,
    _loader_cache_key,
    compile_loader,
    gcc_available,
    generate_loader_source,
    loader_cache_dir,
    mingw_available,
)
from fspack.platform import Platform
from fspack.progress import StageRecorder


def test_generate_loader_source_contains_entry_and_dll() -> None:
    src = generate_loader_source("src/helloworld.py", "python311")
    assert r"src\\helloworld.py" in src
    assert r"runtime\\python311.dll" in src
    assert "Py_Main" in src
    assert "wmain" in src


def test_generate_loader_source_backslash_conversion() -> None:
    src = generate_loader_source("src/a/b/c.py", "python312")
    assert r"src\\a\\b\\c.py" in src
    assert r"runtime\\python312.dll" in src


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def _touch_out(cmd: list[str]) -> None:
    Path(cmd[cmd.index("-o") + 1]).touch()


def test_compile_loader_cli_invokes_mingw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return _Completed()

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    compile_loader("int wmain(){return 0;}", out, AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")
    assert out.is_file()
    assert "-municode" in captured["cmd"]
    assert "-mwindows" not in captured["cmd"]
    assert captured["cmd"][0] == "x86_64-w64-mingw32-gcc"


def test_compile_loader_gui_adds_mwindows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return _Completed()

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    compile_loader("x", out, AppType.GUI, tmp_path / "w", cache_dir=tmp_path / "cache")
    assert "-mwindows" in captured["cmd"]


def test_compile_loader_mingw_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError("no mingw")

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match=r"请安装 mingw-w64"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")


def test_compile_loader_compile_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "gcc", stderr="syntax error")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match="loader 编译失败"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")


def test_mingw_available_returns_bool() -> None:
    assert isinstance(mingw_available(), bool)


def test_gcc_available_returns_bool() -> None:
    assert isinstance(gcc_available(), bool)


def test_generate_loader_source_linux() -> None:
    src = generate_loader_source("src/helloworld.py", "python311", Platform.LINUX)
    assert "src/helloworld.py" in src
    assert "runtime/python/lib/libpython3.11.so" in src
    assert "dlopen" in src
    assert "dlsym" in src
    assert "Py_BytesMain" in src
    assert "setenv" in src
    assert "PYTHONHOME" in src


def test_generate_loader_source_linux_310() -> None:
    src = generate_loader_source("src/app.py", "python310", Platform.LINUX)
    assert "libpython3.10.so" in src


def test_compile_loader_linux_uses_gcc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return _Completed()

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    out = tmp_path / "app"
    compile_loader(
        "int main(){return 0;}", out, AppType.CLI, tmp_path / "w", Platform.LINUX, cache_dir=tmp_path / "cache"
    )
    assert out.is_file()
    assert captured["cmd"][0] == "gcc"
    assert "-ldl" in captured["cmd"]
    assert "-municode" not in captured["cmd"]
    assert "-mwindows" not in captured["cmd"]


def test_compile_loader_linux_gcc_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match=r"请安装 gcc"):
        compile_loader("x", tmp_path / "app", AppType.CLI, tmp_path / "w", Platform.LINUX, cache_dir=tmp_path / "cache")


def test_compile_loader_cache_hit_copies_without_compiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存命中时直接复制，不调用编译器。."""
    source = "int wmain(){return 0;}"
    cache = tmp_path / "cache"
    cache.mkdir()
    key = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS)
    cached = cache / f"{key}.exe"
    cached.write_bytes(b"cached-exe")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise AssertionError("缓存命中不应调用编译器")

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    stage = StageRecorder("生成 C loader")
    compile_loader(source, out, AppType.CLI, tmp_path / "w", Platform.WINDOWS, cache_dir=cache, stage=stage)
    assert out.read_bytes() == b"cached-exe"
    record = stage._finalize()
    assert record.cache_hit == 1
    assert record.detail == "缓存命中"


def test_compile_loader_cache_miss_writes_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存未命中时编译并回写缓存。."""
    source = "int wmain(){return 0;}"
    cache = tmp_path / "cache"

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"compiled-exe")
        return _Completed()

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    compile_loader(source, out, AppType.CLI, tmp_path / "w", Platform.WINDOWS, cache_dir=cache)
    assert out.read_bytes() == b"compiled-exe"
    key = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS)
    cached = cache / f"{key}.exe"
    assert cached.is_file()
    assert cached.read_bytes() == b"compiled-exe"


def test_compile_loader_second_call_hits_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """相同配置第二次调用命中缓存，只编译一次。."""
    call_count = 0

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        nonlocal call_count
        call_count += 1
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"compiled")
        return _Completed()

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    cache = tmp_path / "cache"
    source = "int wmain(){return 0;}"
    out1 = tmp_path / "app1.exe"
    compile_loader(source, out1, AppType.CLI, tmp_path / "w1", Platform.WINDOWS, cache_dir=cache)
    out2 = tmp_path / "app2.exe"
    compile_loader(source, out2, AppType.CLI, tmp_path / "w2", Platform.WINDOWS, cache_dir=cache)
    assert call_count == 1
    assert out2.read_bytes() == b"compiled"


def test_compile_loader_cache_key_differs_by_app_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """相同源码不同 app_type 产生不同缓存键，互不命中。."""
    source = "int wmain(){return 0;}"
    cache = tmp_path / "cache"
    calls: list[str] = []

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        calls.append(cmd[0])
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return _Completed()

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    compile_loader(source, tmp_path / "cli.exe", AppType.CLI, tmp_path / "w1", Platform.WINDOWS, cache_dir=cache)
    compile_loader(source, tmp_path / "gui.exe", AppType.GUI, tmp_path / "w2", Platform.WINDOWS, cache_dir=cache)
    assert len(calls) == 2


def test_compile_loader_cache_linux_no_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台缓存文件无 .exe 后缀。."""
    source = "int main(){return 0;}"
    cache = tmp_path / "cache"
    key = _loader_cache_key(source, AppType.CLI, Platform.LINUX)
    cached = cache / key
    cache.mkdir(parents=True)
    cached.write_bytes(b"linux-exe")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise AssertionError("缓存命中不应调用编译器")

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    out = tmp_path / "app"
    compile_loader(source, out, AppType.CLI, tmp_path / "w", Platform.LINUX, cache_dir=cache)
    assert out.read_bytes() == b"linux-exe"


def test_loader_cache_dir_default() -> None:
    """loader_cache_dir 返回 ~/.fspack/cache/loaders/。."""
    assert loader_cache_dir() == Path.home() / ".fspack" / "cache" / "loaders"


def test_compile_loader_compile_path_sets_stage_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """编译路径（非缓存命中）设置 stage.detail 为编译器名。."""

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return _Completed()

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    stage = StageRecorder("生成 C loader")
    compile_loader(
        "x",
        tmp_path / "app.exe",
        AppType.CLI,
        tmp_path / "w",
        Platform.WINDOWS,
        cache_dir=tmp_path / "cache",
        stage=stage,
    )
    record = stage._finalize()
    assert record.detail == MINGW_GCC
    assert record.cache_hit == 0


def test_compile_loader_cache_writeback_failure_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存回写失败时不影响构建，仅记录警告。."""

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return _Completed()

    def fake_copy2(src: Path, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.loader.shutil.copy2", fake_copy2)
    compile_loader(
        "x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", Platform.WINDOWS, cache_dir=tmp_path / "cache"
    )
    assert (tmp_path / "app.exe").is_file()
