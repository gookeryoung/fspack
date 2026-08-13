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
    LoaderVersionInfo,
    _compile_resource_obj,
    _find_mingw_gcc,
    _find_windres,
    _icon_hash,
    _loader_cache_key,
    _version_info_hash,
    clang_available,
    compile_loader,
    gcc_available,
    generate_loader_source,
    loader_cache_dir,
    mingw_available,
)
from fspack.packaging.loader.resource import generate_app_manifest, generate_resource_rc
from fspack.platform import Platform
from fspack.progress import StageRecorder
from tests._stubs import CompletedStub


def test_generate_loader_source_contains_dll_and_entry_reading() -> None:
    src = generate_loader_source("python311")
    assert r"runtime\\python311.dll" in src
    assert "Py_Main" in src
    assert "wmain" in src
    assert ".entry" in src
    assert "read_entry" in src
    # Win7 兼容：用 SetDllDirectoryW 把 runtime\ 加入 DLL 搜索路径，
    # 让 Windows 加载 python3X.dll 及其传递依赖时能在 runtime\ 中查找
    assert "SetDllDirectoryW" in src
    assert "LoadLibraryW" in src


def test_generate_loader_source_no_entry_hardcoded() -> None:
    """loader 源码不含硬编码入口路径，可跨项目复用."""
    src1 = generate_loader_source("python311")
    src2 = generate_loader_source("python311")
    assert src1 == src2
    assert "helloworld" not in src1
    assert "app.py" not in src1


def _touch_out(cmd: list[str]) -> None:
    Path(cmd[cmd.index("-o") + 1]).touch()


def test_compile_loader_cli_invokes_mingw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    compile_loader("int wmain(){return 0;}", out, AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")
    assert out.is_file()
    assert "-municode" in captured["cmd"]
    assert "-mwindows" not in captured["cmd"]
    assert captured["cmd"][0] == _find_mingw_gcc()


def test_compile_loader_gui_adds_mwindows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

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


# ---- macOS loader 测试 ----


def test_generate_loader_source_macos() -> None:
    """macOS loader 源码含 libpython3.X.dylib、dlopen、_NSGetExecutablePath."""
    src = generate_loader_source("python311", Platform.MACOS)
    assert "runtime/python/lib/libpython3.11.dylib" in src
    assert "dlopen" in src
    assert "dlsym" in src
    assert "Py_BytesMain" in src
    assert "setenv" in src
    assert "PYTHONHOME" in src
    assert ".entry" in src
    assert "read_entry" in src
    # macOS 特有：用 _NSGetExecutablePath 取可执行路径（无 /proc/self/exe）
    assert "_NSGetExecutablePath" in src
    assert "mach-o/dyld.h" in src
    # 不应含 Linux 的 /proc/self/exe
    assert "/proc/self/exe" not in src


def test_generate_loader_source_macos_310() -> None:
    """macOS loader 源码按 py_xy 填充正确的 dylib 版本号."""
    src = generate_loader_source("python310", Platform.MACOS)
    assert "libpython3.10.dylib" in src


def test_generate_loader_source_macos_no_hardcoded_entry() -> None:
    """macOS loader 源码不含硬编码入口路径，可跨项目复用."""
    src1 = generate_loader_source("python311", Platform.MACOS)
    src2 = generate_loader_source("python311", Platform.MACOS)
    assert src1 == src2
    assert "helloworld" not in src1
    assert "app.py" not in src1


def test_compile_loader_macos_uses_clang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS 平台用 clang 编译，命令含 -O2，不含 -ldl（dlopen 在 libSystem）."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app"
    compile_loader(
        "int main(){return 0;}", out, AppType.CLI, tmp_path / "w", Platform.MACOS, cache_dir=tmp_path / "cache"
    )
    assert out.is_file()
    assert captured["cmd"][0] == "clang"
    assert "-O2" in captured["cmd"]
    assert "-ldl" not in captured["cmd"]
    assert "-municode" not in captured["cmd"]
    assert "-mwindows" not in captured["cmd"]


def test_compile_loader_macos_clang_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS clang 缺失时抛 LoaderError，提示安装 clang."""

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    with pytest.raises(LoaderError, match=r"请安装 clang"):
        compile_loader("x", tmp_path / "app", AppType.CLI, tmp_path / "w", Platform.MACOS, cache_dir=tmp_path / "cache")


def test_compile_loader_macos_ignores_icon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS 平台忽略 icon 参数（Mach-O 无图标资源概念）。"""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)

    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico")
    out = tmp_path / "app"
    compile_loader(
        "x",
        out,
        AppType.CLI,
        tmp_path / "w",
        Platform.MACOS,
        icon=icon,
        cache_dir=tmp_path / "cache",
    )
    assert "icon.o" not in captured["cmd"]
    assert "icon.ico" not in captured["cmd"]


def test_compile_loader_macos_cache_no_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS 平台缓存文件无后缀（与 Linux 一致），缓存命中不创建编译工作目录."""
    source = "int main(){return 0;}"
    cache = tmp_path / "cache"
    key = _loader_cache_key(source, AppType.CLI, Platform.MACOS)
    cached = cache / key
    cache.mkdir(parents=True)
    cached.write_bytes(b"macos-exe")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise AssertionError("缓存命中不应调用编译器")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app"
    work_dir = tmp_path / "build"
    compile_loader(source, out, AppType.CLI, work_dir, Platform.MACOS, cache_dir=cache)
    assert out.read_bytes() == b"macos-exe"
    assert not work_dir.exists()


def test_compile_loader_macos_cache_key_differs_from_linux() -> None:
    """相同源码不同平台产生不同缓存键，macOS 与 Linux 互不命中."""
    source = "int main(){return 0;}"
    key_linux = _loader_cache_key(source, AppType.CLI, Platform.LINUX)
    key_macos = _loader_cache_key(source, AppType.CLI, Platform.MACOS)
    key_windows = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS)
    assert key_linux != key_macos
    assert key_macos != key_windows
    assert key_linux != key_windows


def test_clang_available_returns_bool() -> None:
    """clang_available 返回 bool（macOS 编译器检测）."""
    assert isinstance(clang_available(), bool)


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

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

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

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"compiled-exe")
        return CompletedStub()

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

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        nonlocal call_count
        call_count += 1
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"compiled")
        return CompletedStub()

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

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd[0])
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return CompletedStub()

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

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return CompletedStub()

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

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return CompletedStub()

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


def test_compile_resource_obj_no_windres_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """windres 不可用时返回 None 并记录警告（不检查 icon/version_info）."""
    monkeypatch.setattr("fspack.packaging.loader.shutil.which", lambda name: None)
    result = _compile_resource_obj(None, tmp_path / "w")
    assert result is None
    assert "未找到 windres" in caplog.text


def test_compile_resource_obj_windres_filenotfound_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """windres FileNotFoundError 时返回 None 并记录警告."""
    work = tmp_path / "w"

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError("no windres in PATH")

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    result = _compile_resource_obj(None, work)
    assert result is None
    assert "windres 不可用" in caplog.text


def test_compile_resource_obj_windres_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """windres CalledProcessError 时返回 None 并记录警告."""
    work = tmp_path / "w"
    err = subprocess.CalledProcessError(1, "windres", stderr="invalid rc")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    result = _compile_resource_obj(None, work)
    assert result is None
    assert "资源编译失败" in caplog.text


def test_compile_resource_obj_success_with_icon_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """windres 成功时返回 resource.o，rc 含 icon 引用与 VERSIONINFO，manifest 已写入."""
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"ico-content")
    work = tmp_path / "w"
    info = LoaderVersionInfo(
        name="myapp", version="1.2.3", description="测试应用", author="作者", exe_filename="myapp.exe"
    )

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_bytes(b"coff-obj")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    result = _compile_resource_obj(icon, work, version_info=info)
    assert result is not None
    assert result.name == "resource.o"
    assert result.is_file()
    # icon 被复制到 work_dir
    assert (work / "icon.ico").read_bytes() == b"ico-content"
    # resource.rc 含 icon 引用 + VERSIONINFO + manifest 引用
    rc = (work / "resource.rc").read_text(encoding="utf-8")
    assert "#pragma code_page(65001)" in rc
    assert '1 ICON "icon.ico"' in rc
    assert "1 VERSIONINFO" in rc
    assert "FILEVERSION 1,2,3,0" in rc
    assert "PRODUCTVERSION 1,2,3,0" in rc
    assert 'VALUE "CompanyName", "作者"' in rc
    assert 'VALUE "FileDescription", "测试应用"' in rc
    assert 'VALUE "ProductName", "myapp"' in rc
    assert 'VALUE "OriginalFilename", "myapp.exe"' in rc
    assert '1 24 "app.manifest"' in rc
    # app.manifest 已生成
    manifest = (work / "app.manifest").read_text(encoding="utf-8")
    assert "asInvoker" in manifest
    assert "PerMonitorV2" in manifest


def test_compile_resource_obj_missing_icon_logs_warning_but_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """icon 不存在但 version_info 有时仍编译资源（manifest+version），记录 icon 警告."""
    work = tmp_path / "w"
    info = LoaderVersionInfo(name="myapp", version="1.0.0", description="", author="", exe_filename="myapp.exe")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_bytes(b"coff")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    result = _compile_resource_obj(tmp_path / "missing.ico", work, version_info=info)
    assert result is not None
    assert "icon 文件不存在" in caplog.text
    rc = (work / "resource.rc").read_text(encoding="utf-8")
    assert "1 ICON" not in rc  # icon 跳过
    assert "VERSIONINFO" in rc  # version 仍嵌入


def test_compile_resource_obj_no_icon_no_version_still_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """icon=None + version_info=None 时仍生成 manifest，返回 resource.o."""
    work = tmp_path / "w"

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_bytes(b"coff")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    result = _compile_resource_obj(None, work)
    assert result is not None
    rc = (work / "resource.rc").read_text(encoding="utf-8")
    assert "1 ICON" not in rc
    assert "VERSIONINFO" not in rc
    assert '1 24 "app.manifest"' in rc


def test_compile_loader_with_icon_appends_obj_to_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_loader Windows + icon 时把资源 .o 路径加到 gcc 命令末尾."""
    captured: dict[str, list[str]] = {}

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        # 模拟 windres 与 gcc 都生成输出
        if "--output-format=coff" in cmd:
            output = Path(cmd[cmd.index("--output") + 1])
            output.write_bytes(b"obj")
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(b"exe")
        return CompletedStub()

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
    assert cmd[-1].endswith("resource.o")
    assert "-mwindows" in cmd  # GUI 加 -mwindows


def test_compile_loader_linux_ignores_icon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_loader Linux 平台忽略 icon 参数（ELF 无图标资源概念）。"""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

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

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        nonlocal call_count
        call_count += 1
        if "--output-format=coff" in cmd:
            output = Path(cmd[cmd.index("--output") + 1])
            output.write_bytes(b"obj")
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(b"exe")
        return CompletedStub()

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

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd[0])
        if "--output-format=coff" in cmd:
            output = Path(cmd[cmd.index("--output") + 1])
            output.write_bytes(b"obj")
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(b"exe")
        return CompletedStub()

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


# --- iter-148 前后端分离 Web 打包：WEB 类型 loader 加 -mwindows ---


def test_compile_loader_web_adds_mwindows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WEB 类型与 GUI 一样加 -mwindows 关闭控制台窗口."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    compile_loader("x", out, AppType.WEB, tmp_path / "w", cache_dir=tmp_path / "cache")
    assert "-mwindows" in captured["cmd"]


# --- resource.py 资源段生成测试 ---


def test_generate_app_manifest_contains_as_invoker_and_dpi() -> None:
    """manifest 含 asInvoker、PerMonitorV2、supportedOS 与 assemblyIdentity."""
    xml = generate_app_manifest("myapp", "1.2.3")
    assert "asInvoker" in xml
    assert "PerMonitorV2" in xml
    assert "requestedExecutionLevel" in xml
    assert "supportedOS" in xml
    assert "fspack.myapp" in xml
    assert 'version="1,2,3,0"' in xml
    # Win10/11 supportedOS GUID
    assert "8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a" in xml


def test_generate_app_manifest_escapes_special_chars() -> None:
    """name 中的 XML 特殊字符（& <）被转义，避免破坏 manifest 文档结构."""
    xml = generate_app_manifest("a&b<c", "1.0.0")
    assert "fspack.a&amp;b&lt;c" in xml


def test_generate_resource_rc_with_icon_and_version() -> None:
    """rc 含 code_page 声明、icon 引用、VERSIONINFO 全字段与 manifest 引用."""
    info = LoaderVersionInfo("myapp", "0.4.9", "描述", "公司", "myapp.exe")
    rc = generate_resource_rc(info, has_icon=True)
    assert "#pragma code_page(65001)" in rc
    assert '1 ICON "icon.ico"' in rc
    assert "1 VERSIONINFO" in rc
    assert "FILEVERSION 0,4,9,0" in rc
    assert "PRODUCTVERSION 0,4,9,0" in rc
    assert 'VALUE "CompanyName", "公司"' in rc
    assert 'VALUE "FileDescription", "描述"' in rc
    assert 'VALUE "ProductName", "myapp"' in rc
    assert 'VALUE "ProductVersion", "0.4.9"' in rc
    assert 'VALUE "InternalName", "myapp"' in rc
    assert 'VALUE "OriginalFilename", "myapp.exe"' in rc
    assert '1 24 "app.manifest"' in rc


def test_generate_resource_rc_without_icon_falls_back_name() -> None:
    """无 icon 时省略 ICON 行；description/author 空时回退到 name."""
    info = LoaderVersionInfo("myapp", "1.0.0", "", "", "myapp.exe")
    rc = generate_resource_rc(info, has_icon=False)
    assert "1 ICON" not in rc
    assert "VERSIONINFO" in rc
    assert 'VALUE "CompanyName", "myapp"' in rc
    assert 'VALUE "FileDescription", "myapp"' in rc


def test_generate_resource_rc_without_version_info() -> None:
    """version_info=None 时省略 VERSIONINFO，仅保留 manifest 引用."""
    rc = generate_resource_rc(None, has_icon=False)
    assert "VERSIONINFO" not in rc
    assert "1 ICON" not in rc
    assert '1 24 "app.manifest"' in rc


def test_generate_resource_rc_escapes_quotes() -> None:
    """rc 字符串值中的双引号转义为 ""."""
    info = LoaderVersionInfo('name"quote', "1.0.0", "", "", "app.exe")
    rc = generate_resource_rc(info, has_icon=False)
    assert 'name""quote' in rc


def test_version_to_quad_pads_and_truncates() -> None:
    """_version_to_quad 不足 4 段补 0，超出取前 4，非数字段取前导数字."""
    from fspack.packaging.loader.resource import _version_to_quad

    assert _version_to_quad("0.4.9") == "0,4,9,0"
    assert _version_to_quad("1.2") == "1,2,0,0"
    assert _version_to_quad("1.2.3.4") == "1,2,3,4"
    assert _version_to_quad("1.2.3.4.5") == "1,2,3,4"
    assert _version_to_quad("1.0.0rc1") == "1,0,0,0"
    assert _version_to_quad("") == "0,0,0,0"


# --- version_info 缓存键测试 ---


def test_version_info_hash_stable_and_differs() -> None:
    """相同 version_info 哈希稳定且 16 字符；不同版本哈希不同."""
    info1 = LoaderVersionInfo("app", "1.0.0", "", "", "app.exe")
    info2 = LoaderVersionInfo("app", "2.0.0", "", "", "app.exe")
    assert _version_info_hash(info1) == _version_info_hash(info1)
    assert len(_version_info_hash(info1)) == 16
    assert _version_info_hash(info1) != _version_info_hash(info2)


def test_loader_cache_key_differs_by_version_info() -> None:
    """相同源码不同 version_info 产生不同缓存键，互不命中."""
    source = "int wmain(){return 0;}"
    info1 = LoaderVersionInfo("app", "1.0.0", "", "", "app.exe")
    info2 = LoaderVersionInfo("app", "2.0.0", "", "", "app.exe")
    key1 = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS, "", _version_info_hash(info1))
    key2 = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS, "", _version_info_hash(info2))
    assert key1 != key2


def test_loader_cache_key_default_version_info_hash_backward_compat() -> None:
    """version_info_hash 默认空串，与不传时缓存键一致（向后兼容旧调用）."""
    source = "int wmain(){return 0;}"
    key_implicit = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS, "")
    key_explicit = _loader_cache_key(source, AppType.CLI, Platform.WINDOWS, "", "")
    assert key_implicit == key_explicit


def test_compile_loader_version_info_triggers_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_loader 传 version_info 时调用 windres 编译资源（cmd 含 resource.o）."""
    captured: dict[str, list[str]] = {}

    def fake_which(name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        if "--output-format=coff" in cmd:
            Path(cmd[cmd.index("--output") + 1]).write_bytes(b"obj")
        else:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"exe")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    info = LoaderVersionInfo("app", "1.0.0", "desc", "auth", "app.exe")
    compile_loader(
        "x",
        tmp_path / "app.exe",
        AppType.CLI,
        tmp_path / "w",
        Platform.WINDOWS,
        version_info=info,
        cache_dir=tmp_path / "cache",
    )
    # gcc 命令末尾链接 resource.o
    assert captured["cmd"][-1].endswith("resource.o")
