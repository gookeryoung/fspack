"""C loader 源码生成与编译测试。."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType
from fspack.exceptions import LoaderError
from fspack.loader import compile_loader, gcc_available, generate_loader_source, mingw_available
from fspack.platform import Platform


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
    compile_loader("int wmain(){return 0;}", out, AppType.CLI, tmp_path / "w")
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
    compile_loader("x", out, AppType.GUI, tmp_path / "w")
    assert "-mwindows" in captured["cmd"]


def test_compile_loader_mingw_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError("no mingw")

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match=r"请安装 mingw-w64"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w")


def test_compile_loader_compile_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "gcc", stderr="syntax error")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match="loader 编译失败"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w")


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
    compile_loader("int main(){return 0;}", out, AppType.CLI, tmp_path / "w", Platform.LINUX)
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
        compile_loader("x", tmp_path / "app", AppType.CLI, tmp_path / "w", Platform.LINUX)
