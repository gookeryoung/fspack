"""C loader 源码生成与编译测试."""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
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


@pytest.fixture(autouse=True)
def _stable_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """编译测试不运行真实工具探测；fingerprint 自测直接调用 toolchain 模块."""
    monkeypatch.setattr(
        "fspack.packaging.loader.compile.toolchain_fingerprint",
        lambda compiler, resource=False, store=None: f"fake:{compiler}:{resource}",
    )
    monkeypatch.setattr("fspack.packaging.loader.shutil.which", lambda name: f"/fake/bin/{name}")


def _command_kind(cmd: list[str]) -> str:
    if "--output" in cmd:
        return "resource"
    return "c" if "-c" in cmd else "link"


def _assert_build_calls(calls: list[list[str]], *, c: int, resource: int, link: int) -> None:
    assert {kind: sum(_command_kind(cmd) == kind for cmd in calls) for kind in ("c", "resource", "link")} == {
        "c": c,
        "resource": resource,
        "link": link,
    }


def _fake_build(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """模拟三类工具输出，保留资源文本与链接子系统便于检查复用边界."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(list(cmd))
        kind = _command_kind(cmd)
        flag = "--output" if kind == "resource" else "-o"
        out = Path(cmd[cmd.index(flag) + 1])
        if kind == "resource":
            rc = Path(cmd[cmd.index("--input") + 1])
            payload = rc.read_bytes() + (rc.parent / "app.manifest").read_bytes()
            if (rc.parent / "icon.ico").is_file():
                payload += (rc.parent / "icon.ico").read_bytes()
        elif kind == "c":
            payload = b"object:" + next(Path(arg).read_bytes() for arg in cmd if arg.endswith(".c"))
        else:
            payload = b"gui:" if "-mwindows" in cmd else b"cli:"
            payload += b"".join(Path(arg).read_bytes() for arg in cmd if arg.endswith(".o"))
        out.write_bytes(payload)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    return calls


def _assert_cache_sidecars(cache: Path) -> None:
    binaries = [p for p in cache.rglob("*") if p.is_file() and p.suffix not in (".sha256", ".lock")]
    assert binaries
    for binary in binaries:
        sidecar = binary.with_name(binary.name + ".sha256")
        assert sidecar.is_file(), binary
        assert sidecar.read_text(encoding="ascii").strip() == hashlib.sha256(binary.read_bytes()).hexdigest()


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


def test_generate_loader_source_error_reporting() -> None:
    """Windows loader 错误三通道报告：stderr + GUI 弹窗 + 日志文件."""
    src = generate_loader_source("python311")
    # GUI 子系统无控制台时 MessageBox 弹窗（GetConsoleWindow 判定）
    assert "MessageBoxW" in src
    assert "GetConsoleWindow" in src
    # 日志文件持久兜底：<exe_dir>\logs\loader.log
    assert "write_log" in src
    assert "loader.log" in src
    # 错误码翻译：GetLastError 高频错误码给人话与修复建议
    assert "describe_load_error" in src
    # 退出码规范：100=entry 101=dll 102=sym 103=mem
    assert "#define ERR_ENTRY 100" in src
    assert "#define ERR_DLL 101" in src
    assert "#define ERR_SYM 102" in src
    assert "#define ERR_MEM 103" in src
    # 文案统一前缀与建议格式
    assert "[fspack loader]" in src
    assert "建议:" in src


def test_generate_loader_source_startup_perf() -> None:
    """Windows loader 启动提速措施：耗时打点 + 禁用 pyc 回写."""
    src = generate_loader_source("python311")
    # FSPACK_LOADER_VERBOSE=1 输出各阶段耗时（默认关闭零开销）
    assert "FSPACK_LOADER_VERBOSE" in src
    assert "verbose_enabled" in src
    # QPC 高精度计时：GetTickCount64 精度 15.6ms 会把快速阶段量化成 0ms
    # （仅注释提及 GetTickCount64，计时调用不再使用）
    assert "QueryPerformanceCounter" in src
    assert "now_ms" in src
    assert "GetTickCount64()" not in src
    # Py_Main 调用前写 QPC 锚点（wrapper 的 perf_counter 同源），供
    # fsp r --profile 实测 C 层初始化缝隙
    assert "FSPACK_LOADER_QPC_MS" in src
    assert "timing_enabled" in src
    # 禁用运行时 pyc 回写（发行目录只读/杀软监控场景），用户已设置时不覆盖
    assert "PYTHONDONTWRITEBYTECODE" in src
    assert "set_dont_write_bytecode" in src
    # stderr 重定向时中文转 UTF-8 字节输出（宽字符流默认 locale 下中文丢失）
    assert "_isatty" in src
    assert "stderr_write" in src


def test_generate_loader_source_linux_error_reporting() -> None:
    """Linux loader 错误双通道报告：stderr + 日志文件."""
    src = generate_loader_source("python311", Platform.LINUX)
    assert "report_error" in src
    assert "write_log" in src
    assert "loader.log" in src
    # 退出码规范与 Windows 版一致
    assert "#define ERR_ENTRY 100" in src
    assert "#define ERR_DLL 101" in src
    assert "#define ERR_SYM 102" in src
    assert "#define ERR_MEM 103" in src
    # 日志写入用时间戳（time.h + strftime）
    assert "strftime" in src
    assert "[fspack loader]" in src


def test_generate_loader_source_linux_startup_perf() -> None:
    """Linux loader 启动提速：RTLD_LAZY 惰性绑定 + 打点 + 禁用 pyc 回写."""
    src = generate_loader_source("python311", Platform.LINUX)
    # dlopen 惰性绑定：跳过 libpython 全量符号重定位（CPython 官方同款）
    assert "RTLD_LAZY | RTLD_GLOBAL" in src
    assert "RTLD_NOW" not in src
    assert "FSPACK_LOADER_VERBOSE" in src
    assert "CLOCK_MONOTONIC" in src
    assert "PYTHONDONTWRITEBYTECODE" in src


def test_generate_loader_source_macos_error_reporting() -> None:
    """macOS loader 错误双通道报告：stderr + 日志文件."""
    src = generate_loader_source("python311", Platform.MACOS)
    assert "report_error" in src
    assert "write_log" in src
    assert "loader.log" in src
    assert "#define ERR_ENTRY 100" in src
    assert "#define ERR_DLL 101" in src
    assert "#define ERR_SYM 102" in src
    assert "#define ERR_MEM 103" in src
    assert "[fspack loader]" in src


def test_generate_loader_source_macos_startup_perf() -> None:
    """macOS loader 启动提速：RTLD_LAZY 惰性绑定 + 打点 + 禁用 pyc 回写."""
    src = generate_loader_source("python311", Platform.MACOS)
    assert "RTLD_LAZY | RTLD_GLOBAL" in src
    assert "RTLD_NOW" not in src
    assert "FSPACK_LOADER_VERBOSE" in src
    assert "CLOCK_MONOTONIC" in src
    assert "PYTHONDONTWRITEBYTECODE" in src


def test_generate_loader_source_splash_inject() -> None:
    """--splash 注入启动画面：独立线程 + 首窗口轮询 + 事件关闭 + 超时兜底."""
    src = generate_loader_source("python311", splash=True, splash_title="我的应用")
    # 标记全部被替换（无残留注释标记）
    assert "FSPACK:SPLASH" not in src
    # 独立线程 + 消息循环（主线程不被阻塞）
    assert "splash_thread" in src
    assert "CreateThread" in src
    # 三关闭条件：首窗口轮询（GUI 通用）/ 命名事件（WEB server）/ 超时兜底
    assert "EnumWindows" in src
    assert "splash_enum_cb" in src
    assert 'L"Local\\\\fspack_splash_%lu"' in src
    assert "MsgWaitForMultipleObjects" in src
    assert "SPLASH_TIMEOUT_MS" in src
    # 调用点：wmain 显示、report_error 先关画面再弹错误框
    assert "    splash_show();" in src
    assert "    splash_close();" in src
    # 画面样式：无边框居中窗口 + 标题绘制
    assert "WS_POPUP | WS_VISIBLE" in src
    assert "DrawTextW" in src
    assert 'L"我的应用"' in src


def test_generate_loader_source_splash_title_escaped() -> None:
    """splash 标题含双引号/反斜杠时转义为合法 C 字符串字面量."""
    src = generate_loader_source("python311", splash=True, splash_title='My "App"\\1')
    assert 'L"My \\"App\\"\\\\1"' in src


def test_generate_loader_source_splash_disabled_default() -> None:
    """默认构建不含 splash：标记剥离、无 splash 代码，源码与旧行为一致."""
    src = generate_loader_source("python311")
    assert "FSPACK:SPLASH" not in src
    assert "splash_show" not in src
    assert "splash_close" not in src
    assert "Splash" not in src
    # Linux/macOS 传 splash=True 也忽略（无 Windows 窗口系统概念）
    linux = generate_loader_source("python311", Platform.LINUX, splash=True, splash_title="x")
    assert "splash_show" not in linux


def test_generate_loader_source_splash_changes_cache_key() -> None:
    """splash 开关改变源码 → loader 缓存键自然分离（同配置命中、改配置失效）."""
    plain = generate_loader_source("python311")
    splashed = generate_loader_source("python311", splash=True, splash_title="app")
    other_title = generate_loader_source("python311", splash=True, splash_title="other")
    keys = {
        _loader_cache_key(plain, AppType.CLI, Platform.WINDOWS),
        _loader_cache_key(splashed, AppType.CLI, Platform.WINDOWS),
        _loader_cache_key(other_title, AppType.CLI, Platform.WINDOWS),
    }
    assert len(keys) == 3


def test_loader_c_sources_come_from_assets() -> None:
    """C 源码来自 assets/loader/*.c（不再内嵌于 Python 模块）."""
    from fspack.packaging.loader import source as loader_source

    for name, loader in (
        ("loader_windows.c", loader_source.loader_windows_source),
        ("loader_linux.c", loader_source.loader_linux_source),
        ("loader_macos.c", loader_source.loader_macos_source),
        ("splash_windows.c", loader_source.splash_windows_source),
    ):
        assert loader() == (loader_source._LOADER_ASSET_DIR / name).read_text(encoding="utf-8")


def test_loader_asset_missing_raises_loader_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """assets/loader 缺失时抛出可诊断的 LoaderError（打包清单遗漏信号）."""
    from fspack.packaging.loader import source as loader_source

    monkeypatch.setattr(loader_source, "_LOADER_ASSET_DIR", tmp_path)
    loader_source._loader_asset.cache_clear()
    try:
        with pytest.raises(LoaderError, match="loader C 源码资源缺失"):
            loader_source.loader_windows_source()
    finally:
        loader_source._loader_asset.cache_clear()


def _touch_out(cmd: list[str]) -> None:
    """从命令提取输出路径并 touch（gcc 用 -o，windres 用 --output）。"""
    for flag in ("-o", "--output"):
        if flag in cmd:
            Path(cmd[cmd.index(flag) + 1]).touch()
            return


def _disable_windres(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁用 windres：保留 C 编译与链接，但资源缺失的 Windows exe 不应缓存."""

    def fake_which(name: str) -> str | None:
        return None if "windres" in name else f"/usr/bin/{name}"

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", fake_which)


def test_compile_loader_cli_invokes_mingw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_build(monkeypatch)
    _disable_windres(monkeypatch)
    out = tmp_path / "app.exe"
    compile_loader("int wmain(){return 0;}", out, AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")
    assert out.is_file()
    _assert_build_calls(calls, c=1, resource=0, link=1)
    c_cmd, link_cmd = calls
    assert "-municode" in c_cmd
    assert "-O2" in c_cmd
    assert "-mwindows" not in c_cmd
    assert "-mwindows" not in link_cmd
    assert any(arg.endswith("loader.o") for arg in link_cmd)
    assert not any(arg.endswith(".c") for arg in link_cmd)
    # 错误报告用 MessageBoxW/GetConsoleWindow，需显式链接 user32；
    # splash 启动画面绘制用 GDI 函数，需显式链接 gdi32（--splash 注入，
    # 未注入时多余链接无副作用，保持命令稳定）
    assert "-luser32" in link_cmd
    assert "-lgdi32" in link_cmd
    assert link_cmd[0] == _find_mingw_gcc()


def test_compile_loader_gui_adds_mwindows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    _disable_windres(monkeypatch)
    out = tmp_path / "app.exe"
    compile_loader("x", out, AppType.GUI, tmp_path / "w", cache_dir=tmp_path / "cache")
    assert "-mwindows" in captured["cmd"]


def test_compile_loader_mingw_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError("no mingw")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    _disable_windres(monkeypatch)
    with pytest.raises(LoaderError, match=r"请安装 mingw-w64"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")


def test_compile_loader_compile_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "gcc", stderr="syntax error")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    _disable_windres(monkeypatch)
    with pytest.raises(LoaderError, match="loader 编译失败"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")


def test_compile_loader_timeout_raises_loader_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """编译超时抛 LoaderError（含超时秒数），不再无限阻塞构建."""

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 300))

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    _disable_windres(monkeypatch)
    with pytest.raises(LoaderError, match="loader 编译超时"):
        compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")


def test_compile_loader_timeout_passed_to_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gcc 编译命令带 timeout 参数（默认 300s），防编译器卡死无限阻塞."""
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        seen["timeout"] = kw.get("timeout")
        _touch_out(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    _disable_windres(monkeypatch)
    compile_loader("x", tmp_path / "app.exe", AppType.CLI, tmp_path / "w", cache_dir=tmp_path / "cache")
    assert seen["timeout"] == 300.0


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
    calls = _fake_build(monkeypatch)
    seed = tmp_path / "seed"
    compile_loader(source, seed, AppType.CLI, tmp_path / "seed-build", Platform.MACOS, cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=0, link=1)
    assert len(list(cache.glob("*.sha256"))) == 1
    assert not list(cache.glob("*.exe"))
    _assert_cache_sidecars(cache)

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise AssertionError("缓存命中不应调用编译器")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app"
    work_dir = tmp_path / "build"
    compile_loader(source, out, AppType.CLI, work_dir, Platform.MACOS, cache_dir=cache)
    assert out.read_bytes() == seed.read_bytes()
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
    calls = _fake_build(monkeypatch)
    seed = tmp_path / "seed.exe"
    compile_loader(source, seed, AppType.CLI, tmp_path / "seed-build", Platform.WINDOWS, cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=1, link=1)
    assert len(list(cache.glob("*.exe"))) == 1
    _assert_cache_sidecars(cache)

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise AssertionError("缓存命中不应调用编译器")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app.exe"
    work_dir = tmp_path / "build"
    stage = StageRecorder("生成 C loader")
    compile_loader(source, out, AppType.CLI, work_dir, Platform.WINDOWS, cache_dir=cache, stage=stage)
    assert out.read_bytes() == seed.read_bytes()
    record = stage._finalize()
    assert record.cache_hit == 1
    assert record.detail == "缓存命中"
    # 缓存命中不应创建编译工作目录，避免 dist/build/ 留下空目录
    assert not work_dir.exists()


def test_compile_loader_cache_miss_writes_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存未命中时编译并回写缓存."""
    source = "int wmain(){return 0;}"
    cache = tmp_path / "cache"
    calls = _fake_build(monkeypatch)
    out = tmp_path / "app.exe"
    compile_loader(source, out, AppType.CLI, tmp_path / "w", Platform.WINDOWS, cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=1, link=1)
    (cached,) = cache.glob("*.exe")
    assert cached.read_bytes() == out.read_bytes()
    assert len(list((cache / "objects").glob("*.o"))) == 1
    assert len(list((cache / "resources").glob("*.o"))) == 1
    _assert_cache_sidecars(cache)


def test_compile_loader_second_call_hits_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """相同完整资源配置第二次调用命中缓存，不运行任一工具."""
    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    source = "int wmain(){return 0;}"
    out1 = tmp_path / "app1.exe"
    compile_loader(source, out1, AppType.CLI, tmp_path / "w1", Platform.WINDOWS, cache_dir=cache)
    out2 = tmp_path / "app2.exe"
    compile_loader(source, out2, AppType.CLI, tmp_path / "w2", Platform.WINDOWS, cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=1, link=1)
    assert out2.read_bytes() == out1.read_bytes()
    assert not (tmp_path / "w2").exists()


def test_compile_loader_cache_key_differs_by_app_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows CLI/GUI 共享对象与资源，但按不同子系统各链接一次."""
    source = "int wmain(){return 0;}"
    cache = tmp_path / "cache"
    calls = _fake_build(monkeypatch)
    compile_loader(source, tmp_path / "cli.exe", AppType.CLI, tmp_path / "w1", Platform.WINDOWS, cache_dir=cache)
    compile_loader(source, tmp_path / "gui.exe", AppType.GUI, tmp_path / "w2", Platform.WINDOWS, cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=1, link=2)
    links = [cmd for cmd in calls if _command_kind(cmd) == "link"]
    assert "-mwindows" not in links[0]
    assert "-mwindows" in links[1]
    assert (tmp_path / "cli.exe").read_bytes() != (tmp_path / "gui.exe").read_bytes()
    assert len(list(cache.glob("*.exe"))) == 2


def test_compile_loader_cache_linux_no_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台缓存文件无 .exe 后缀，缓存命中不创建编译工作目录."""
    source = "int main(){return 0;}"
    cache = tmp_path / "cache"
    calls = _fake_build(monkeypatch)
    seed = tmp_path / "seed"
    compile_loader(source, seed, AppType.CLI, tmp_path / "seed-build", Platform.LINUX, cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=0, link=1)
    assert len(list(cache.glob("*.sha256"))) == 1
    assert not list(cache.glob("*.exe"))
    _assert_cache_sidecars(cache)

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise AssertionError("缓存命中不应调用编译器")

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    out = tmp_path / "app"
    work_dir = tmp_path / "build"
    compile_loader(source, out, AppType.CLI, work_dir, Platform.LINUX, cache_dir=cache)
    assert out.read_bytes() == seed.read_bytes()
    assert not work_dir.exists()


def test_loader_cache_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """loader_cache_dir 返回 ~/.fspack/cache/loaders/."""
    # 本机设置 FSPACK_CACHE_DIR 时会覆盖 home 推导，须隔离保证断言语义
    monkeypatch.delenv("FSPACK_CACHE_DIR", raising=False)
    assert loader_cache_dir() == Path.home() / ".fspack" / "cache" / "loaders"


def test_compile_loader_compile_path_sets_stage_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """编译路径（非缓存命中）设置 stage.detail 为编译器名."""

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"exe")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    _disable_windres(monkeypatch)
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


def test_compile_loader_cache_writeback_failure_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """仅缓存写入失败时构建仍成功；正常的工作目录/热缓存复制不可被 mock 吞掉."""
    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    original_copy2 = shutil.copy2

    def fake_copy2(src: Path, dst: Path, **kw: Any) -> str | Path:
        if cache in Path(dst).parents:
            raise OSError("disk full")
        return original_copy2(src, dst, **kw)

    monkeypatch.setattr("fspack.packaging.loader.shutil.copy2", fake_copy2)
    compile_loader("x", tmp_path / "app", AppType.CLI, tmp_path / "w", Platform.LINUX, cache_dir=cache)
    assert (tmp_path / "app").is_file()
    _assert_build_calls(calls, c=1, resource=0, link=1)
    assert "缓存回写失败" in caplog.text
    assert not list(cache.glob("*.sha256"))

    monkeypatch.setattr("fspack.packaging.loader.shutil.copy2", original_copy2)
    compile_loader("x", tmp_path / "seed", AppType.CLI, tmp_path / "seed-w", Platform.LINUX, cache_dir=cache)
    monkeypatch.setattr("fspack.packaging.loader.shutil.copy2", fake_copy2)
    compile_loader("x", tmp_path / "warm", AppType.CLI, tmp_path / "warm-w", Platform.LINUX, cache_dir=cache)
    _assert_build_calls(calls, c=2, resource=0, link=2)
    assert (tmp_path / "warm").read_bytes() == (tmp_path / "seed").read_bytes()
    assert not (tmp_path / "warm-w").exists()


@pytest.mark.parametrize("with_version", [False, True], ids=["manifest-only", "version-info"])
def test_compile_loader_resource_failure_skips_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, with_version: bool
) -> None:
    """资源缺失不缓存 exe，但保留 C 对象；重复构建重链，恢复 windres 后补齐资源."""
    calls = _fake_build(monkeypatch)
    _disable_windres(monkeypatch)
    info = LoaderVersionInfo("app", "1.0.0", "desc", "author", "app.exe") if with_version else None
    cache = tmp_path / "cache"
    for i in range(2):
        compile_loader(
            "x",
            tmp_path / f"app{i}.exe",
            AppType.CLI,
            tmp_path / f"w{i}",
            Platform.WINDOWS,
            cache_dir=cache,
            version_info=info,
        )
        assert (tmp_path / f"app{i}.exe").is_file()
        assert not list(cache.glob("*.exe"))
    _assert_build_calls(calls, c=1, resource=0, link=2)
    assert len(list((cache / "objects").glob("*.o"))) == 1
    assert not list((cache / "resources").glob("*.o"))
    assert "跳过 loader 缓存" in caplog.text

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", lambda name: f"/fake/bin/{name}")
    for i in range(2, 4):
        compile_loader(
            "x",
            tmp_path / f"app{i}.exe",
            AppType.CLI,
            tmp_path / f"w{i}",
            Platform.WINDOWS,
            cache_dir=cache,
            version_info=info,
        )
    _assert_build_calls(calls, c=1, resource=1, link=3)
    assert (tmp_path / "app2.exe").read_bytes() != (tmp_path / "app0.exe").read_bytes()
    assert (tmp_path / "app3.exe").read_bytes() == (tmp_path / "app2.exe").read_bytes()
    assert not (tmp_path / "w3").exists()
    _assert_cache_sidecars(cache)


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


def test_compile_resource_obj_success_with_icon_and_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    calls = _fake_build(monkeypatch)

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
    _assert_build_calls(calls, c=1, resource=1, link=1)
    assert (tmp_path / "app2.exe").read_bytes() == (tmp_path / "app1.exe").read_bytes()
    # 缓存命中不应创建第二个编译工作目录
    assert not (tmp_path / "w2").exists()


def test_compile_loader_different_icon_misses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """不同 icon 内容产生不同缓存键，第二次不命中。"""
    calls = _fake_build(monkeypatch)

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
    _assert_build_calls(calls, c=1, resource=2, link=2)
    assert (tmp_path / "app1.exe").read_bytes() != (tmp_path / "app2.exe").read_bytes()


# --- iter-148 前后端分离 Web 打包：WEB 类型 loader 加 -mwindows ---


def test_compile_loader_web_adds_mwindows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WEB 类型与 GUI 一样加 -mwindows 关闭控制台窗口."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        _touch_out(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fake_run)
    _disable_windres(monkeypatch)
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
    assert 'version="1.2.3.0"' in xml  # manifest version 用点分隔（非 .rc 的逗号）
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
    # BEGIN/END 必须配对，否则 windres 报 syntax error（回归：StringFileInfo 的 END 曾缺失）
    assert rc.count("BEGIN") == rc.count("END"), "rc 文件 BEGIN/END 未配对"


def test_generate_resource_rc_without_icon_falls_back_name() -> None:
    """无 icon 时省略 ICON 行；description/author 空时回退到 name."""
    info = LoaderVersionInfo("myapp", "1.0.0", "", "", "myapp.exe")
    rc = generate_resource_rc(info, has_icon=False)
    assert "1 ICON" not in rc
    assert "VERSIONINFO" in rc
    assert 'VALUE "CompanyName", "myapp"' in rc
    assert 'VALUE "FileDescription", "myapp"' in rc
    assert rc.count("BEGIN") == rc.count("END"), "rc 文件 BEGIN/END 未配对"


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


# ---- 编译器原始输出转发（[loader]） ----


def test_forward_compile_output_logs_loader_debug(caplog: pytest.LogCaptureFixture) -> None:
    """gcc 成功路径为静默；一旦有输出（告警）以 [loader] 前缀进入 DEBUG."""
    from fspack.packaging.loader.compile import _forward_tool_output

    completed = subprocess.CompletedProcess(["gcc"], 0, stdout="", stderr="warning: unused variable\n")
    with caplog.at_level(logging.DEBUG, logger="fspack.console"):
        _forward_tool_output(completed)
    assert "[loader] warning: unused variable" in caplog.text


def test_forward_compile_output_silent_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """默认（INFO）输出不显示编译器原始输出."""
    from fspack.packaging.loader.compile import _forward_tool_output

    completed = subprocess.CompletedProcess(["gcc"], 0, stdout="collect2 notes\n", stderr="")
    with caplog.at_level(logging.INFO, logger="fspack.console"):
        _forward_tool_output(completed)
    assert "collect2 notes" not in caplog.text


def test_forward_compile_output_empty_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    """无输出时不产生日志记录."""
    from fspack.packaging.loader.compile import _forward_tool_output

    with caplog.at_level(logging.DEBUG, logger="fspack.console"):
        _forward_tool_output(subprocess.CompletedProcess(["gcc"], 0, stdout="", stderr=""))
    assert caplog.records == []


@pytest.mark.parametrize("platform", [Platform.WINDOWS, Platform.LINUX, Platform.MACOS])
@pytest.mark.parametrize("app_type", [AppType.CLI, AppType.GUI, AppType.WEB])
def test_effective_app_type(platform: Platform, app_type: AppType) -> None:
    from fspack.packaging.loader.cache_keys import _effective_app_type

    expected = AppType.GUI if platform is Platform.WINDOWS and app_type is not AppType.CLI else AppType.CLI
    assert _effective_app_type(app_type, platform) is expected


def test_cache_keys_have_unambiguous_structured_fields() -> None:
    from fspack.packaging.loader.cache_keys import _object_cache_key, _resource_cache_key

    assert _version_info_hash(LoaderVersionInfo("a", "bc", "", "", "app.exe")) != _version_info_hash(
        LoaderVersionInfo("ab", "c", "", "", "app.exe")
    )
    assert _loader_cache_key("x", AppType.CLI, Platform.WINDOWS, "a", "bc") != _loader_cache_key(
        "x", AppType.CLI, Platform.WINDOWS, "ab", "c"
    )
    assert _object_cache_key("a", Platform.LINUX, "bc", ()) != _object_cache_key("ab", Platform.LINUX, "c", ())
    assert _resource_cache_key("a", None, "bc") != _resource_cache_key("ab", None, "c")
    assert _loader_cache_key("x", AppType.CLI, Platform.LINUX, compile_flags=("ab", "c")) != _loader_cache_key(
        "x", AppType.CLI, Platform.LINUX, compile_flags=("a", "bc")
    )


@pytest.mark.parametrize("field", ["toolchain_key", "compile_flags", "link_flags", "resource_key"])
def test_loader_cache_key_tracks_build_inputs(field: str) -> None:
    kwargs: dict[str, Any] = {field: ("changed",) if field.endswith("flags") else "changed"}
    assert _loader_cache_key("x", AppType.CLI, Platform.WINDOWS) != _loader_cache_key(
        "x", AppType.CLI, Platform.WINDOWS, **kwargs
    )


@pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
def test_compile_loader_posix_all_app_types_reuse_complete_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: Platform
) -> None:
    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    outputs = []
    for app_type in (AppType.CLI, AppType.GUI, AppType.WEB):
        out = tmp_path / app_type.value
        outputs.append(out)
        compile_loader("x", out, app_type, tmp_path / f"w-{app_type.value}", platform, cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=0, link=1)
    assert len({out.read_bytes() for out in outputs}) == 1
    assert not (tmp_path / "w-gui").exists()
    assert not (tmp_path / "w-web").exists()


def test_compile_loader_gui_web_reuse_complete_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    info = LoaderVersionInfo("app", "1.0", "desc", "author", "shared.exe")
    for app_type in (AppType.GUI, AppType.WEB):
        compile_loader(
            "x",
            tmp_path / f"{app_type.value}.exe",
            app_type,
            tmp_path / f"w-{app_type.value}",
            Platform.WINDOWS,
            version_info=info,
            cache_dir=cache,
        )
    _assert_build_calls(calls, c=1, resource=1, link=1)
    assert (tmp_path / "gui.exe").read_bytes() == (tmp_path / "web.exe").read_bytes()
    assert not (tmp_path / "w-web").exists()
    assert len(list(cache.glob("*.exe"))) == 1


def test_compile_loader_multi_entry_shares_c_preserves_filename_and_subsystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    for app_type in (AppType.CLI, AppType.GUI, AppType.WEB):
        name = app_type.value
        info = LoaderVersionInfo("app", "1.0", "desc", "author", f"{name}.exe")
        out = tmp_path / f"{name}.exe"
        compile_loader("x", out, app_type, tmp_path / f"w-{name}", version_info=info, cache_dir=cache)
        content = out.read_bytes().decode("utf-8")
        assert f'VALUE "OriginalFilename", "{name}.exe"' in content
        assert content.startswith("cli:" if app_type is AppType.CLI else "gui:")
        for other in (AppType.CLI, AppType.GUI, AppType.WEB):
            if other is not app_type:
                assert f'VALUE "OriginalFilename", "{other.value}.exe"' not in content
    _assert_build_calls(calls, c=1, resource=3, link=3)
    assert len(list((cache / "objects").glob("*.o"))) == 1
    assert len(list((cache / "resources").glob("*.o"))) == 3
    assert len(list(cache.glob("*.exe"))) == 3
    _assert_cache_sidecars(cache)


@pytest.mark.parametrize("resource_tool", [False, True], ids=["compiler", "windres"])
def test_compile_loader_tool_identity_change_invalidates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_tool: bool
) -> None:
    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    compile_loader("x", tmp_path / "before.exe", AppType.CLI, tmp_path / "w1", cache_dir=cache)

    def changed_identity(compiler: str, resource: bool = False, store: Path | None = None) -> str:
        return f"{'changed' if resource == resource_tool else 'fake'}:{compiler}:{resource}"

    monkeypatch.setattr("fspack.packaging.loader.compile.toolchain_fingerprint", changed_identity)
    compile_loader("x", tmp_path / "after.exe", AppType.CLI, tmp_path / "w2", cache_dir=cache)
    _assert_build_calls(calls, c=1 if resource_tool else 2, resource=2, link=2)
    assert len(list(cache.glob("*.exe"))) == 2


@pytest.mark.parametrize("generator", ["generate_resource_rc", "generate_app_manifest"])
def test_resource_content_change_invalidates_resource_and_exe_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generator: str
) -> None:
    from fspack.packaging.loader import cache_keys, resource, toolchain

    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    info = LoaderVersionInfo("app", "1.0", "desc", "author", "app.exe")
    compile_loader("x", tmp_path / "before.exe", AppType.CLI, tmp_path / "w1", version_info=info, cache_dir=cache)
    original = getattr(resource, generator)

    def changed(*args: Any, **kwargs: Any) -> str:
        suffix = "\n// changed resource\n" if generator == "generate_resource_rc" else "\n<!-- changed manifest -->\n"
        return original(*args, **kwargs) + suffix

    for module in (resource, cache_keys, toolchain):
        if hasattr(module, generator):
            monkeypatch.setattr(module, generator, changed)
    compile_loader("x", tmp_path / "after.exe", AppType.CLI, tmp_path / "w2", version_info=info, cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=2, link=2)
    assert (tmp_path / "before.exe").read_bytes() != (tmp_path / "after.exe").read_bytes()
    assert len(list((cache / "resources").glob("*.o"))) == 2
    assert len(list(cache.glob("*.exe"))) == 2


@pytest.mark.parametrize("layer", ["exe", "objects", "resources"])
@pytest.mark.parametrize("damage", ["binary", "missing-sidecar", "bad-sidecar"])
def test_compile_loader_recovers_corrupted_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layer: str, damage: str
) -> None:
    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    compile_loader("x", tmp_path / "seed.exe", AppType.CLI, tmp_path / "w1", cache_dir=cache)
    (cached_exe,) = cache.glob("*.exe")
    if layer == "exe":
        binary = cached_exe
    else:
        (binary,) = (cache / layer).glob("*.o")
        cached_exe.unlink()
        cached_exe.with_name(cached_exe.name + ".sha256").unlink()
    sidecar = binary.with_name(binary.name + ".sha256")
    if damage == "binary":
        binary.write_bytes(b"corrupt")
    elif damage == "missing-sidecar":
        sidecar.unlink()
    else:
        sidecar.write_text("invalid hash", encoding="ascii")
    calls.clear()
    compile_loader("x", tmp_path / "fixed.exe", AppType.CLI, tmp_path / "w2", cache_dir=cache)
    _assert_build_calls(calls, c=int(layer == "objects"), resource=int(layer == "resources"), link=1)
    assert (tmp_path / "fixed.exe").read_bytes() == (tmp_path / "seed.exe").read_bytes()
    _assert_cache_sidecars(cache)
    calls.clear()
    compile_loader("x", tmp_path / "warm.exe", AppType.CLI, tmp_path / "w3", cache_dir=cache)
    _assert_build_calls(calls, c=0, resource=0, link=0)
    assert (tmp_path / "warm.exe").read_bytes() == (tmp_path / "seed.exe").read_bytes()


@pytest.mark.parametrize("same_resources", [False, True], ids=["different-entry", "same-key"])
def test_compile_loader_concurrent_misses_share_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_resources: bool
) -> None:
    calls = _fake_build(monkeypatch)
    barrier = Barrier(4)
    cache = tmp_path / "cache"

    def build(i: int) -> Path:
        info = LoaderVersionInfo("app", "1.0", "desc", "author", "same.exe" if same_resources else f"app{i}.exe")
        barrier.wait(timeout=5)
        return compile_loader(
            "x",
            tmp_path / f"app{i}.exe",
            AppType.GUI,
            tmp_path / f"w{i}",
            version_info=info,
            cache_dir=cache,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(build, range(4)))
    count = 1 if same_resources else 4
    _assert_build_calls(calls, c=1, resource=count, link=count)
    assert len({out.read_bytes() for out in outputs}) == count
    assert len(list(cache.glob("*.exe"))) == count
    _assert_cache_sidecars(cache)


@pytest.mark.parametrize("failure_phase", ["c", "link"])
def test_compile_loader_compile_failure_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    calls = _fake_build(monkeypatch)
    successful_run = subprocess.run
    failed = False

    def fail_once(cmd: list[str], **kw: Any) -> CompletedStub | subprocess.CompletedProcess[str]:
        nonlocal failed
        if _command_kind(cmd) == failure_phase and not failed:
            failed = True
            calls.append(list(cmd))
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"partial")
            raise subprocess.CalledProcessError(1, cmd, stderr="compile broken")
        return successful_run(cmd, **kw)

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", fail_once)
    cache = tmp_path / "cache"
    with pytest.raises(LoaderError, match="compile broken"):
        compile_loader("x", tmp_path / "app", AppType.CLI, tmp_path / "w1", Platform.LINUX, cache_dir=cache)
    assert not list(cache.glob("*.sha256"))
    assert len(list((cache / "objects").glob("*.o"))) == int(failure_phase == "link")
    compile_loader("x", tmp_path / "app", AppType.CLI, tmp_path / "w1", Platform.LINUX, cache_dir=cache)
    compile_loader("x", tmp_path / "warm", AppType.CLI, tmp_path / "w2", Platform.LINUX, cache_dir=cache)
    _assert_build_calls(calls, c=2 if failure_phase == "c" else 1, resource=0, link=1 if failure_phase == "c" else 2)
    assert b"partial" not in (tmp_path / "app").read_bytes()
    assert (tmp_path / "warm").read_bytes() == (tmp_path / "app").read_bytes()
    _assert_cache_sidecars(cache)


@pytest.mark.parametrize("error", ["missing", "invalid", "timeout"])
def test_compile_loader_resource_error_recovers_without_recompiling_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    calls = _fake_build(monkeypatch)
    successful_run = subprocess.run
    resource_broken = True

    def maybe_fail(cmd: list[str], **kw: Any) -> CompletedStub | subprocess.CompletedProcess[str]:
        if _command_kind(cmd) == "resource" and resource_broken:
            calls.append(list(cmd))
            if error == "missing":
                raise FileNotFoundError("windres missing")
            if error == "timeout":
                raise subprocess.TimeoutExpired(cmd, 120)
            Path(cmd[cmd.index("--output") + 1]).write_bytes(b"partial resource")
            raise subprocess.CalledProcessError(1, cmd, stderr="invalid rc")
        return successful_run(cmd, **kw)

    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", maybe_fail)
    cache = tmp_path / "cache"
    compile_loader("x", tmp_path / "broken.exe", AppType.CLI, tmp_path / "w", cache_dir=cache)
    assert not list(cache.glob("*.exe"))
    assert not list((cache / "resources").glob("*.o"))
    assert len(list((cache / "objects").glob("*.o"))) == 1
    resource_broken = False
    compile_loader("x", tmp_path / "fixed.exe", AppType.CLI, tmp_path / "w", cache_dir=cache)
    compile_loader("x", tmp_path / "warm.exe", AppType.CLI, tmp_path / "warm-w", cache_dir=cache)
    _assert_build_calls(calls, c=1, resource=2, link=2)
    assert (tmp_path / "fixed.exe").read_bytes() != (tmp_path / "broken.exe").read_bytes()
    assert b"partial resource" not in (tmp_path / "fixed.exe").read_bytes()
    assert (tmp_path / "warm.exe").read_bytes() == (tmp_path / "fixed.exe").read_bytes()
    _assert_cache_sidecars(cache)


@pytest.mark.parametrize("platform", [Platform.WINDOWS, Platform.LINUX, Platform.MACOS])
def test_compile_loader_cache_keys_use_canonical_build_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: Platform
) -> None:
    from fspack.packaging.loader.cache_keys import _object_cache_key, _resource_cache_key
    from fspack.packaging.loader.compile import LinuxLoader, MacLoader, WindowsLoader

    cls = {Platform.WINDOWS: WindowsLoader, Platform.LINUX: LinuxLoader, Platform.MACOS: MacLoader}[platform]
    flags = ("-O2", "-municode") if platform is Platform.WINDOWS else ("-O2",)
    assert cls._compile_flags() == flags
    cmd = cls._build_command(
        Path("loader.o"), Path("loader"), AppType.GUI, Path("resource.o") if platform is Platform.WINDOWS else None
    )
    assert "loader.o" in cmd and "loader.c" not in cmd
    compiler_key = f"fake:{cmd[0]}:False"
    resource_key = (
        _resource_cache_key("", None, f"fake:{_find_windres()}:True" + compiler_key)
        if platform is Platform.WINDOWS
        else ""
    )
    key = _loader_cache_key(
        "x",
        AppType.GUI,
        platform,
        toolchain_key=compiler_key,
        compile_flags=flags,
        link_flags=tuple(cmd),
        resource_key=resource_key,
    )
    calls = _fake_build(monkeypatch)
    cache = tmp_path / "cache"
    out = tmp_path / ("app.exe" if platform is Platform.WINDOWS else "app")
    compile_loader("x", out, AppType.GUI, tmp_path / "w", platform, cache_dir=cache)
    cached = cache / (f"{key}.exe" if platform is Platform.WINDOWS else key)
    assert cached.read_bytes() == out.read_bytes()
    object_key = _object_cache_key("x", platform, compiler_key, flags)
    assert (cache / "objects" / f"{object_key}.o").is_file()
    c_cmd = next(cmd for cmd in calls if _command_kind(cmd) == "c")
    assert tuple(c_cmd[1 : c_cmd.index("-c")]) == flags
    if platform is Platform.WINDOWS:
        assert (cache / "resources" / f"{resource_key}.o").is_file()
    _assert_cache_sidecars(cache)


@pytest.mark.parametrize("resource", [False, True], ids=["compiler", "windres"])
def test_toolchain_fingerprint_memoizes_probes_and_tracks_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: bool
) -> None:
    from fspack.packaging.loader.toolchain import toolchain_fingerprint

    tools = [tmp_path / name for name in ("gcc.exe", "windres.exe", "other-gcc.exe", "other-windres.exe")]
    for tool in tools:
        tool.write_bytes(b"fake tool")
    current = tools[1 if resource else 0]
    version = "version 1"
    calls: list[list[str]] = []

    def which(name: str) -> str:
        return str(current if ("windres" in name) == resource else tools[0])

    def probe(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert len(cmd) == 2 and "-o" not in cmd and "--output" not in cmd
        calls.append(list(cmd))
        output = version if cmd[1] in ("-v", "--version") else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    monkeypatch.setattr("fspack.packaging.loader.shutil.which", which)
    monkeypatch.setattr("fspack.packaging.loader.subprocess.run", probe)
    name = "windres" if resource else "gcc"
    first = toolchain_fingerprint(name, resource=resource)
    assert calls
    probe_count = len(calls)
    assert toolchain_fingerprint(name, resource=resource) == first
    assert len(calls) == probe_count

    current = tools[3 if resource else 2]
    monkeypatch.setenv("PATH", str(tmp_path / "moved"))
    moved = toolchain_fingerprint(name, resource=resource)
    assert moved != first
    assert len(calls) > probe_count
    version = "version 2"
    current.write_bytes(b"updated fake tool")
    updated = toolchain_fingerprint(name, resource=resource)
    assert updated != moved

    monkeypatch.setenv("CPATH", str(tmp_path / "new-include"))
    environment_changed = toolchain_fingerprint(name, resource=resource)
    assert environment_changed != updated
    probe_count = len(calls)
    assert toolchain_fingerprint(name, resource=resource) == environment_changed
    assert len(calls) == probe_count
