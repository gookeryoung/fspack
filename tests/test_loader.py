"""C loader 源码生成与编译测试."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType
from fspack.exceptions import LoaderError
from fspack.packaging.loader import (
    MINGW_GCC,
    MINGW_WINDRES,
    _compile_icon_resource,
    _find_windres,
    _icon_hash,
    _loader_cache_key,
    compile_loader,
    gcc_available,
    generate_loader_source,
    loader_cache_dir,
    mingw_available,
)
from fspack.platform import Platform
from fspack.progress import StageRecorder


def test_generate_loader_source_contains_dll_and_entry_reading() -> None:
    src = generate_loader_source("python311")
    assert r"runtime\\python311.dll" in src
    assert "Py_Main" in src
    assert "wmain" in src
    assert ".entry" in src
    assert "read_entry" in src
    # Win7 兼容：用 LoadLibraryExW + LOAD_WITH_ALTERED_SEARCH_PATH
    # 让 Windows 在 python3X.dll 所在目录搜索依赖 DLL
    assert "LoadLibraryExW" in src
    assert "LOAD_WITH_ALTERED_SEARCH_PATH" in src


def test_generate_loader_source_no_entry_hardcoded() -> None:
    """loader 源码不含硬编码入口路径，可跨项目复用."""
    src1 = generate_loader_source("python311")
    src2 = generate_loader_source("python311")
    assert src1 == src2
    assert "helloworld" not in src1
    assert "app.py" not in src1


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

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
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

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    compile_loader("x", out, AppType.GUI, tmp_path / "w", cache_dir=tmp_path / "cache")
    assert "-mwindows" in captured["cmd"]


def test_compile_loader_mingw_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError("no mingw")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match=r"请安装 mingw-w64"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")


def test_compile_loader_compile_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "gcc", stderr="syntax error")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match="loader 编译失败"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")


def test_mingw_available_returns_bool() -> None:
    assert isinstance(mingw_available(), bool)


def test_gcc_available_returns_bool() -> None:
    assert isinstance(gcc_available(), bool)


def test_generate_loader_source_linux() -> None:
    src = generate_loader_source("python311", Platform.LINUX)
    assert "runtime/python/lib/libpython3.11.so" in src
    assert "dlopen" in src
    assert "dlsym" in src
    assert "Py_BytesMain" in src
    assert "setenv" in src
    assert "PYTHONHOME" in src
    assert ".entry" in src
    assert "read_entry" in src


def test_generate_loader_source_linux_310() -> None:
    src = generate_loader_source("python310", Platform.LINUX)
    assert "libpython3.10.so" in src


def test_loader_cache_key_same_for_different_entries() -> None:
    """不同入口路径产生相同缓存键（源码不含入口路径）."""
    from fspack.config import AppType

    src1 = generate_loader_source("python311")
    src2 = generate_loader_source("python311")
    key1 = _loader_cache_key(src1, AppType.CLI, Platform.WINDOWS)
    key2 = _loader_cache_key(src2, AppType.CLI, Platform.WINDOWS)
    assert key1 == key2


def test_compile_loader_linux_uses_gcc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
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

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match=r"请安装 gcc"):
        compile_loader("x", tmp_path / "app", AppType.CLI, tmp_path / "w", Platform.LINUX, cache_dir=tmp_path / "cache")


def test_compile_loader_cache_hit_copies_without_compiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存命中时直接复制，不调用编译器，也不创建编译工作目录."""
    source = "int wmain(){return 0;}"
    cache = tmp_path / "cache"
    cache.mkdir()
    key = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS)
    cached = cache / f"{key}.exe"
    cached.write_bytes(b"cached-exe")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise AssertionError("缓存命中不应调用编译器")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    work_dir = tmp_path / "build"
    stage = StageRecorder("生成 C loader")
    compile_loader(source, out, AppType.CLI, work_dir, Platform.WINDOWS, cache_dir=cache, stage=stage)
    assert out.read_bytes() == b"cached-exe"
    record = stage._finalize()
    assert record.cache_hit == 1
    assert record.detail == "缓存命中"
    # 缓存命中不应创建编译工作目录，避免 dist/build/ 留下空目录
    assert not work_dir.exists()


def test_compile_loader_cache_miss_writes_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存未命中时编译并回写缓存."""
    source = "int wmain(){return 0;}"
    cache = tmp_path / "cache"

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"compiled-exe")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    compile_loader(source, out, AppType.CLI, tmp_path / "w", Platform.WINDOWS, cache_dir=cache)
    assert out.read_bytes() == b"compiled-exe"
    key = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS)
    cached = cache / f"{key}.exe"
    assert cached.is_file()
    assert cached.read_bytes() == b"compiled-exe"


def test_compile_loader_second_call_hits_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """相同配置第二次调用命中缓存，只编译一次."""
    call_count = 0

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        nonlocal call_count
        call_count += 1
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"compiled")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    cache = tmp_path / "cache"
    source = "int wmain(){return 0;}"
    out1 = tmp_path / "app1.exe"
    compile_loader(source, out1, AppType.CLI, tmp_path / "w1", Platform.WINDOWS, cache_dir=cache)
    out2 = tmp_path / "app2.exe"
    compile_loader(source, out2, AppType.CLI, tmp_path / "w2", Platform.WINDOWS, cache_dir=cache)
    assert call_count == 1
    assert out2.read_bytes() == b"compiled"


def test_compile_loader_cache_key_differs_by_app_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """相同源码不同 app_type 产生不同缓存键，互不命中."""
    source = "int wmain(){return 0;}"
    cache = tmp_path / "cache"
    calls: list[str] = []

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        calls.append(cmd[0])
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    compile_loader(source, tmp_path / "cli.exe", AppType.CLI, tmp_path / "w1", Platform.WINDOWS, cache_dir=cache)
    compile_loader(source, tmp_path / "gui.exe", AppType.GUI, tmp_path / "w2", Platform.WINDOWS, cache_dir=cache)
    assert len(calls) == 2


def test_compile_loader_cache_linux_no_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台缓存文件无 .exe 后缀，缓存命中不创建编译工作目录."""
    source = "int main(){return 0;}"
    cache = tmp_path / "cache"
    key = _loader_cache_key(source, AppType.CLI, Platform.LINUX)
    cached = cache / key
    cache.mkdir(parents=True)
    cached.write_bytes(b"linux-exe")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise AssertionError("缓存命中不应调用编译器")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app"
    work_dir = tmp_path / "build"
    compile_loader(source, out, AppType.CLI, work_dir, Platform.LINUX, cache_dir=cache)
    assert out.read_bytes() == b"linux-exe"
    assert not work_dir.exists()


def test_loader_cache_dir_default() -> None:
    """loader_cache_dir 返回 ~/.fspack/cache/loaders/."""
    assert loader_cache_dir() == Path.home() / ".fspack" / "cache" / "loaders"


def test_compile_loader_compile_path_sets_stage_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """编译路径（非缓存命中）设置 stage.detail 为编译器名."""

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
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
    """缓存回写失败时不影响构建，仅记录警告."""

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return _Completed()

    def fake_copy2(src: Path, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.loader.shutil.copy2", fake_copy2)
    compile_loader(
        "x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", Platform.WINDOWS, cache_dir=tmp_path / "cache"
    )
    assert (tmp_path / "app.exe").is_file()


# --- icon 相关测试 ---


def test_icon_hash_stable_and_differs_by_content(tmp_path: Path) -> None:
    """_icon_hash 对同内容稳定，对不同内容产生不同哈希."""
    ico1 = tmp_path / "a.ico"
    ico1.write_bytes(b"icon-data")
    ico2 = tmp_path / "b.ico"
    ico2.write_bytes(b"icon-data")  # 相同内容
    ico3 = tmp_path / "c.ico"
    ico3.write_bytes(b"different-content")
    assert _icon_hash(ico1) == _icon_hash(ico2)
    assert _icon_hash(ico1) != _icon_hash(ico3)
    assert len(_icon_hash(ico1)) == 16


def test_find_windres_prefers_mingw_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """_find_windres 优先返回 mingw 交叉前缀名."""

    def fake_which(name: str) -> str | None:
        if name == MINGW_WINDRES:
            return "/usr/bin/" + MINGW_WINDRES
        if name == "windres":
            return "/usr/bin/windres"
        return None

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    assert _find_windres() == MINGW_WINDRES


def test_find_windres_fallback_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """mingw 前缀不存在时回退到 windres."""

    def fake_which(name: str) -> str | None:
        if name == "windres":
            return "/usr/bin/windres"
        return None

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    assert _find_windres() == "windres"


def test_find_windres_missing_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """两者都不存在时返回默认 mingw 名，让后续 subprocess 报错."""
    monkeypatch.setattr("fspack.packaging.loader.shutil.which", lambda name: None)
    assert _find_windres() == MINGW_WINDRES


def test_compile_icon_resource_missing_file_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """icon 文件不存在时返回 None 并记录警告."""
    result = _compile_icon_resource(tmp_path / "missing.ico", tmp_path / "w")
    assert result is None
    assert "icon 文件不存在" in caplog.text


def test_compile_icon_resource_no_windres_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """windres 不可用时返回 None 并记录警告."""
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    monkeypatch.setattr("fspack.packaging.loader.shutil.which", lambda name: None)
    result = _compile_icon_resource(icon, tmp_path / "w")
    assert result is None
    assert "未找到 windres" in caplog.text


def test_compile_icon_resource_windres_filenotfound_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """windres FileNotFoundError 时返回 None 并记录警告."""
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    work = tmp_path / "w"
    work.mkdir()

    # _find_windres 找到，但 subprocess.run 抛 FileNotFoundError
    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError("no windres in PATH")

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    result = _compile_icon_resource(icon, work)
    assert result is None
    assert "windres 不可用" in caplog.text


def test_compile_icon_resource_windres_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """windres CalledProcessError 时返回 None 并记录警告."""
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    work = tmp_path / "w"
    work.mkdir()
    err = subprocess.CalledProcessError(1, "windres", stderr="invalid ico")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    result = _compile_icon_resource(icon, work)
    assert result is None
    assert "icon 资源编译失败" in caplog.text


def test_compile_icon_resource_success_returns_obj_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """windres 成功时返回 icon.o 路径并复制 icon 到 work_dir."""
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico-content")
    work = tmp_path / "w"
    work.mkdir()

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        # 模拟 windres 生成 icon.o
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_bytes(b"coff-obj")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    result = _compile_icon_resource(icon, work)
    assert result is not None
    assert result.name == "icon.o"
    assert result.is_file()
    # icon 被复制到 work_dir
    assert (work / "icon.ico").read_bytes() == b"ico-content"
    # icon.rc 内容正确
    rc = (work / "icon.rc").read_text(encoding="utf-8")
    assert 'id ICON "icon.ico"' in rc


def test_compile_loader_with_icon_appends_obj_to_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_loader Windows + icon 时把 icon.o 路径加到 gcc 命令末尾."""
    captured: dict[str, list[str]] = {}

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        # 模拟 windres 与 gcc 都生成输出
        if "--output-format=coff" in cmd:
            output = Path(cmd[cmd.index("--output") + 1])
            output.write_bytes(b"obj")
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(b"exe")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)

    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    out = tmp_path / "app.exe"
    compile_loader(
        "x",
        out,
        AppType.GUI,
        tmp_path / "w",
        Platform.WINDOWS,
        icon=icon,
        cache_dir=tmp_path / "cache",
    )
    cmd = captured["cmd"]
    assert cmd[-1].endswith("icon.o")
    assert "-mwindows" in cmd  # GUI 加 -mwindows


def test_compile_loader_linux_ignores_icon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_loader Linux 平台忽略 icon 参数（ELF 无图标资源概念）。"""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)

    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    out = tmp_path / "app"
    compile_loader(
        "x",
        out,
        AppType.CLI,
        tmp_path / "w",
        Platform.LINUX,
        icon=icon,
        cache_dir=tmp_path / "cache",
    )
    # icon 不应出现在 gcc 命令中
    assert "icon.o" not in captured["cmd"]
    assert "icon.ico" not in captured["cmd"]


def test_compile_loader_cache_key_differs_by_icon(tmp_path: Path) -> None:
    """相同源码不同 icon 产生不同缓存键。"""
    source = "int wmain(){return 0;}"
    key_no_icon = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS, "")
    key_with_icon = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS, "abc123")
    assert key_no_icon != key_with_icon


def test_compile_loader_with_icon_second_call_hits_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """相同 icon 第二次调用命中缓存（icon_hash 相同）。"""
    call_count = 0

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        nonlocal call_count
        call_count += 1
        if "--output-format=coff" in cmd:
            output = Path(cmd[cmd.index("--output") + 1])
            output.write_bytes(b"obj")
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(b"exe")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)

    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico-content")
    cache = tmp_path / "cache"
    compile_loader(
        "x",
        tmp_path / "app1.exe",
        AppType.CLI,
        tmp_path / "w1",
        Platform.WINDOWS,
        icon=icon,
        cache_dir=cache,
    )
    compile_loader(
        "x",
        tmp_path / "app2.exe",
        AppType.CLI,
        tmp_path / "w2",
        Platform.WINDOWS,
        icon=icon,
        cache_dir=cache,
    )
    # windres + gcc 只调一次（第二次缓存命中）
    assert call_count == 2  # windres + gcc
    # 缓存命中不应创建第二个编译工作目录
    assert not (tmp_path / "w2").exists()


def test_compile_loader_different_icon_misses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """不同 icon 内容产生不同缓存键，第二次不命中。"""
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        calls.append(cmd[0])
        if "--output-format=coff" in cmd:
            output = Path(cmd[cmd.index("--output") + 1])
            output.write_bytes(b"obj")
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(b"exe")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)

    icon1 = tmp_path / "icon1.ico"
    icon1.write_bytes(b"ico-1")
    icon2 = tmp_path / "icon2.ico"
    icon2.write_bytes(b"ico-2")
    cache = tmp_path / "cache"
    compile_loader(
        "x",
        tmp_path / "app1.exe",
        AppType.CLI,
        tmp_path / "w1",
        Platform.WINDOWS,
        icon=icon1,
        cache_dir=cache,
    )
    compile_loader(
        "x",
        tmp_path / "app2.exe",
        AppType.CLI,
        tmp_path / "w2",
        Platform.WINDOWS,
        icon=icon2,
        cache_dir=cache,
    )
    # 两次都完整编译（windres + gcc 各两次）
    assert len(calls) == 4
