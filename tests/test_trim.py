"""``runtime/trim.py`` 裁剪测试：_trim_stdlib/embed zip 重写、standalone 裁剪与 ELF/Tcl-Tk 剥离."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.builder import (
    _strip_elf_symbols,
    _strip_tcl_tk_counted,
    _trim_standalone_runtime,
    _trim_stdlib,
)
from fspack.platform import Platform
from fspack.progress import StageRecorder
from tests._stubs import CompletedStub, make_standalone_runtime, symlink_or_skip

# ---- _trim_stdlib 测试 ----


def test_trim_stdlib_linux_strips_unwanted_dirs(tmp_path: Path) -> None:
    """Linux 模式剥离 test/ensurepip/idlelib/pydoc_data/turtledemo 等无用目录，保留有用模块."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    for d in ("test", "ensurepip", "idlelib", "pydoc_data", "turtledemo", "json"):
        (stdlib / d).mkdir(parents=True)
    (stdlib / "json" / "__init__.py").write_text("")  # 有用模块应保留

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    assert not (stdlib / "test").exists()
    assert not (stdlib / "ensurepip").exists()
    assert not (stdlib / "idlelib").exists()
    assert not (stdlib / "pydoc_data").exists()
    assert not (stdlib / "turtledemo").exists()
    assert (stdlib / "json").exists()  # 保留有用模块


def test_trim_stdlib_linux_records_saved_bytes(tmp_path: Path) -> None:
    """Linux 模式剥离目录时累加节省字节数到 stage.add_saved_bytes."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)
    (stdlib / "test" / "data.bin").write_bytes(b"x" * 1024)  # 1KB
    (stdlib / "test" / "sub").mkdir()
    (stdlib / "test" / "sub" / "more.bin").write_bytes(b"y" * 512)  # 0.5KB
    (stdlib / "ensurepip").mkdir(parents=True)
    (stdlib / "ensurepip" / "pkg.py").write_bytes(b"z" * 256)  # 0.25KB

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    record = st._finalize()
    # 1KB + 0.5KB + 0.25KB = 1792 字节
    assert record.bytes_saved == 1792
    assert record.skipped == 2  # test + ensurepip


def test_trim_stdlib_windows_standard_skips(tmp_path: Path) -> None:
    """Windows 标准版无 embed stdlib zip（缓存未就绪等场景）时跳过不剥离."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)  # 构造验证跳过

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.WINDOWS, st)

    # 无 python311.zip 时跳过，散装目录不动
    assert (stdlib / "test").exists()
    record = st._finalize()
    assert record.bytes_saved == 0


# ---- _rewrite_embed_stdlib_zip 测试 ----


def _make_embed_zip(runtime: Path, entries: dict[str, bytes]) -> Path:
    """构造伪 embed stdlib zip（python3XX.zip）供重写测试."""
    runtime.mkdir(parents=True, exist_ok=True)
    zip_path = runtime / "python311.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return zip_path


def test_rewrite_embed_stdlib_zip_conservative(tmp_path: Path) -> None:
    """保守档重写：删 pydoc_data/__phello__ 等纯文档条目，保留 xml/json/logging.

    embed zip 内条目为 .pyc 形态（官方全量冻结），测试条目同真实形态。
    """
    from fspack.packaging.runtime.trim import _rewrite_embed_stdlib_zip

    runtime = tmp_path / "runtime"
    zip_path = _make_embed_zip(
        runtime,
        {
            "pydoc_data/topics.pyc": b"x" * 100,  # 纯文档数据，删
            "pydoc_data/__init__.pyc": b"",
            "__phello__/__init__.pyc": b"",  # 嵌入示例，删
            "xml/__init__.pyc": b"y" * 100,  # 保守档保留
            "json/__init__.pyc": b"z" * 100,  # 保留
            "logging/__init__.pyc": b"w" * 100,  # 保留
            "os.pyc": b"os",  # 保留
        },
    )
    size_before = zip_path.stat().st_size

    st = StageRecorder("精简标准库")
    _rewrite_embed_stdlib_zip(runtime, "3.11.9", st)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert not any(n.startswith("pydoc_data/") for n in names)
    assert not any(n.startswith("__phello__") for n in names)
    assert any(n.startswith("xml/") for n in names)
    assert any(n.startswith("json/") for n in names)
    record = st._finalize()
    assert record.bytes_saved > 0
    assert record.bytes_saved <= size_before  # 净节省不超过原大小
    assert record.skipped == 3  # pydoc_data 2 条 + __phello__ 1 条


def test_rewrite_embed_stdlib_zip_aggressive(tmp_path: Path) -> None:
    """激进档重写：再删 xml/unittest/asyncio 与开发工具单文件（.pyc 按 stem 匹配）."""
    from fspack.packaging.runtime.trim import _rewrite_embed_stdlib_zip

    runtime = tmp_path / "runtime"
    zip_path = _make_embed_zip(
        runtime,
        {
            "pydoc_data/topics.pyc": b"x" * 100,
            "xml/__init__.pyc": b"y" * 100,  # 激进档删
            "unittest/__init__.pyc": b"u" * 100,  # 激进档删
            "asyncio/__init__.pyc": b"a" * 100,  # 激进档删
            "pdb.pyc": b"p" * 100,  # 激进档删（开发工具单文件，stem 匹配）
            "json/__init__.pyc": b"z" * 100,  # 保留
            "logging/__init__.pyc": b"w" * 100,  # 保留（常用）
            "concurrent/futures/__init__.pyc": b"c" * 100,  # 保留（通用基础设施）
        },
    )

    st = StageRecorder("精简标准库")
    _rewrite_embed_stdlib_zip(runtime, "3.11.9", st, aggressive=True)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert not any(n.startswith(("pydoc_data/", "xml/", "unittest/", "asyncio/")) for n in names)
    assert "pdb.pyc" not in names
    assert any(n.startswith("json/") for n in names)
    assert any(n.startswith("logging/") for n in names)
    assert any(n.startswith("concurrent/") for n in names)
    record = st._finalize()
    # 激进档节省多于仅保守档（xml/unittest/asyncio/pdb 共 400+ 字节原始数据）
    assert record.bytes_saved > 400
    assert record.skipped == 5


def test_rewrite_embed_stdlib_zip_idempotent(tmp_path: Path) -> None:
    """幂等：重写后黑名单条目不在 zip 内，二次调用剥离数为 0 跳过."""
    from fspack.packaging.runtime.trim import _rewrite_embed_stdlib_zip

    runtime = tmp_path / "runtime"
    zip_path = _make_embed_zip(runtime, {"pydoc_data/topics.pyc": b"x" * 100, "os.pyc": b"os"})

    st1 = StageRecorder("精简标准库")
    _rewrite_embed_stdlib_zip(runtime, "3.11.9", st1)
    saved1 = st1._finalize().bytes_saved
    assert saved1 > 0

    st2 = StageRecorder("精简标准库")
    _rewrite_embed_stdlib_zip(runtime, "3.11.9", st2)
    record2 = st2._finalize()
    assert record2.bytes_saved == 0
    assert record2.skipped == 0
    # zip 完整保留非黑名单条目
    with zipfile.ZipFile(zip_path) as zf:
        assert "os.pyc" in zf.namelist()


def test_rewrite_embed_stdlib_zip_bad_zip_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """畸形 zip 警告跳过不抛异常，原文件保留."""
    from fspack.packaging.runtime.trim import _rewrite_embed_stdlib_zip

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    zip_path = runtime / "python311.zip"
    zip_path.write_bytes(b"{not a zip")

    st = StageRecorder("精简标准库")
    with caplog.at_level("WARNING"):
        _rewrite_embed_stdlib_zip(runtime, "3.11.9", st)

    assert "读取 embed stdlib zip 失败" in caplog.text
    assert zip_path.stat().st_size == len(b"{not a zip")  # 原文件未动
    assert st._finalize().bytes_saved == 0


def test_trim_stdlib_windows_standard_rewrites_zip(tmp_path: Path) -> None:
    """_trim_stdlib Windows 标准版分支走 embed zip 重写（集成）."""
    runtime = tmp_path / "runtime"
    _make_embed_zip(
        runtime,
        {"pydoc_data/topics.pyc": b"x" * 100, "xml/__init__.pyc": b"y" * 100, "os.pyc": b"os"},
    )

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.WINDOWS, st)

    with zipfile.ZipFile(runtime / "python311.zip") as zf:
        names = zf.namelist()
    assert not any(n.startswith("pydoc_data/") for n in names)
    assert any(n.startswith("xml/") for n in names)  # 保守档保留
    assert "os.pyc" in names
    assert st._finalize().bytes_saved > 0


def test_trim_stdlib_windows_standard_aggressive_via_flag(tmp_path: Path) -> None:
    """_trim_stdlib aggressive=True 透传激进档剥离清单."""
    runtime = tmp_path / "runtime"
    _make_embed_zip(runtime, {"xml/__init__.pyc": b"y" * 100, "json/__init__.pyc": b"z" * 100})

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.WINDOWS, st, aggressive=True)

    with zipfile.ZipFile(runtime / "python311.zip") as zf:
        names = zf.namelist()
    assert not any(n.startswith("xml/") for n in names)  # 激进档删除
    assert any(n.startswith("json/") for n in names)


def test_trim_stdlib_windows_t_strips_lib_at_root(tmp_path: Path) -> None:
    """Windows 自由线程版（t 后缀）走 standalone 路径，剥离 runtime/Lib/ 无用目录."""
    runtime = tmp_path / "runtime"
    # python-build-standalone Windows freethreaded tarball 解压扁平化后标准库在 runtime/Lib/
    stdlib = runtime / "Lib"
    for d in ("test", "ensurepip", "idlelib", "pydoc_data", "turtledemo", "json"):
        (stdlib / d).mkdir(parents=True)
    (stdlib / "json" / "__init__.py").write_text("")  # 有用模块应保留

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.13.14t", Platform.WINDOWS, st)

    assert not (stdlib / "test").exists()
    assert not (stdlib / "ensurepip").exists()
    assert not (stdlib / "idlelib").exists()
    assert not (stdlib / "pydoc_data").exists()
    assert not (stdlib / "turtledemo").exists()
    assert (stdlib / "json").exists()  # 保留有用模块


def test_trim_stdlib_windows_t_missing_lib_skips(tmp_path: Path) -> None:
    """Windows 自由线程版 runtime/Lib/ 不存在时不报错."""
    runtime = tmp_path / "runtime"
    # 不创建 Lib 目录（缓存命中场景或扁平化前已删除）

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.14.6t", Platform.WINDOWS, st)
    record = st._finalize()
    assert record.bytes_saved == 0


def test_trim_stdlib_missing_stdlib_skips(tmp_path: Path) -> None:
    """标准库目录不存在时不报错."""
    runtime = tmp_path / "runtime"
    # 不创建 stdlib 目录

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)
    # 不报错即通过
    record = st._finalize()
    assert record.bytes_saved == 0


def test_trim_stdlib_idempotent(tmp_path: Path) -> None:
    """重复调用幂等：已剥离的目录不存在时跳过."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)
    (stdlib / "test" / "data.bin").write_bytes(b"x" * 100)

    st1 = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st1)
    st2 = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st2)  # 二次调用不报错
    assert not (stdlib / "test").exists()
    # 二次调用目录已不存在，bytes_saved 为 0
    assert st2._finalize().bytes_saved == 0
    # 首次调用记录了 100 字节
    assert st1._finalize().bytes_saved == 100


# --- _trim_standalone_runtime 测试 ---


def test_trim_standalone_runtime_windows_skips(tmp_path: Path) -> None:
    """Windows 目标跳过精简（embed python 无调试符号）."""
    runtime = make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.WINDOWS, st, has_tkinter=False)

    assert (runtime / "python" / "bin" / "python3.11").is_file()
    assert (runtime / "python" / "lib" / "libpython3.11.so.1.0").is_file()
    record = st._finalize()
    assert record.bytes_saved == 0


def _make_windows_t_runtime(tmp_path: Path) -> Path:
    """构造扁平化布局的 Windows 自由线程 runtime 目录树.

    模拟 python-build-standalone freethreaded tarball 解压扁平化后的结构：
    runtime 根含 python.exe/python3.14t.exe/pdb、Lib/DLLs/include/libs/Scripts/tcl。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "Lib").mkdir()
    (runtime / "Lib" / "encodings").mkdir()
    (runtime / "DLLs").mkdir()
    (runtime / "include").mkdir()
    (runtime / "libs").mkdir()
    (runtime / "Scripts").mkdir()
    (runtime / "tcl").mkdir()
    (runtime / "python.exe").write_bytes(b"exe")
    (runtime / "pythonw.exe").write_bytes(b"exe")
    (runtime / "python3.14t.exe").write_bytes(b"exe")
    (runtime / "pythonw3.14t.exe").write_bytes(b"exe")
    (runtime / "python314t.pdb").write_bytes(b"pdb" * 100)
    (runtime / "python3t.pdb").write_bytes(b"pdb" * 50)
    (runtime / "DLLs" / "_ssl.cp314t-win_amd64.pdb").write_bytes(b"pdb" * 10)
    (runtime / "DLLs" / "_ssl.cp314t-win_amd64.pyd").write_bytes(b"pyd")
    (runtime / "DLLs" / "_socket.cp314t-win_amd64.pdb").write_bytes(b"pdb" * 10)
    (runtime / "include" / "Python.h").write_text("#include")
    (runtime / "libs" / "python314t.lib").write_bytes(b"lib")
    (runtime / "tcl" / "init.tcl").write_text("# tcl")
    return runtime


def test_trim_standalone_runtime_windows_t_strips_dev_files(tmp_path: Path) -> None:
    """Windows 自由线程版（standalone 布局）剥离 pdb/include/libs/别名 exe/tcl."""
    runtime = _make_windows_t_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.14.6t", Platform.WINDOWS, st, has_tkinter=False)

    # pdb 全部剥离（runtime 根与 DLLs/）
    assert not list(runtime.glob("*.pdb"))
    assert not list((runtime / "DLLs").glob("*.pdb"))
    # 开发期目录剥离
    assert not (runtime / "include").exists()
    assert not (runtime / "libs").exists()
    assert not (runtime / "Scripts").exists()
    # 非 tkinter 项目剥离 tcl/
    assert not (runtime / "tcl").exists()
    # 版本别名 exe 剥离，python.exe/pythonw.exe 保留（fsp r --debug 用）
    assert (runtime / "python.exe").is_file()
    assert (runtime / "pythonw.exe").is_file()
    assert not (runtime / "python3.14t.exe").exists()
    assert not (runtime / "pythonw3.14t.exe").exists()
    # pyd 与 stdlib 保留
    assert (runtime / "DLLs" / "_ssl.cp314t-win_amd64.pyd").is_file()
    assert (runtime / "Lib" / "encodings").is_dir()
    record = st._finalize()
    assert record.bytes_saved > 0


def test_trim_standalone_runtime_windows_t_keeps_tcl_for_tkinter(tmp_path: Path) -> None:
    """tkinter 项目保留 tcl/（_tkinter.pyd 运行时需要 Tcl/Tk 脚本库）."""
    runtime = _make_windows_t_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.14.6t", Platform.WINDOWS, st, has_tkinter=True)

    assert (runtime / "tcl").is_dir()
    assert not list(runtime.glob("*.pdb"))


def test_trim_standalone_runtime_windows_t_idempotent(tmp_path: Path) -> None:
    """重复精简幂等：二次调用无报错、不再累计节省字节."""
    runtime = _make_windows_t_runtime(tmp_path)
    st1 = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.14.6t", Platform.WINDOWS, st1, has_tkinter=False)
    saved1 = st1._finalize().bytes_saved
    st2 = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.14.6t", Platform.WINDOWS, st2, has_tkinter=False)
    assert st2._finalize().bytes_saved == 0
    assert saved1 > 0


def test_trim_standalone_runtime_missing_python_dir_skips(tmp_path: Path) -> None:
    """standalone runtime 目录不存在时不报错."""
    runtime = tmp_path / "runtime"
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=False)

    record = st._finalize()
    assert record.bytes_saved == 0


def test_trim_standalone_runtime_strips_libpython(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标 strip libpython 调试符号."""
    runtime = make_standalone_runtime(tmp_path)

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        target_path = Path(cmd[-1])
        if target_path.is_file():
            original = target_path.read_bytes()
            target_path.write_bytes(original[:100])
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", fake_run)

    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True)

    record = st._finalize()
    assert record.bytes_saved > 0
    assert (runtime / "python" / "lib" / "libpython3.11.so").is_symlink()


def test_trim_standalone_runtime_deletes_python_binary(tmp_path: Path) -> None:
    """Linux 目标删除 python3.X 二进制与符号链接."""
    runtime = make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True, strip_symbols=False)

    bin_dir = runtime / "python" / "bin"
    assert not (bin_dir / "python3.11").exists()
    assert not (bin_dir / "python3").exists()
    assert not (bin_dir / "python").exists()
    assert not (bin_dir / "python3.11-config").exists()


def test_trim_standalone_runtime_deletes_dev_bin_files(tmp_path: Path) -> None:
    """Linux 目标删除 2to3/idle3/pip3/pydoc3 等开发工具脚本."""
    runtime = make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True, strip_symbols=False)

    bin_dir = runtime / "python" / "bin"
    assert not (bin_dir / "2to3").exists()
    assert not (bin_dir / "idle3").exists()
    assert not (bin_dir / "pydoc3").exists()
    assert not (bin_dir / "pip").exists()
    assert not (bin_dir / "pip3").exists()
    assert not (bin_dir / "pip3.11").exists()


def test_trim_standalone_runtime_deletes_include_share(tmp_path: Path) -> None:
    """Linux 目标删除 include/ 与 share/ 目录."""
    runtime = make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True, strip_symbols=False)

    assert not (runtime / "python" / "include").exists()
    assert not (runtime / "python" / "share").exists()


def test_trim_standalone_runtime_strips_tcl_tk_when_no_tkinter(tmp_path: Path) -> None:
    """非 tkinter 项目剥离 Tcl/Tk 运行时文件."""
    runtime = make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=False, strip_symbols=False)

    lib_dir = runtime / "python" / "lib"
    assert not (lib_dir / "libtcl9.0.so").exists()
    assert not (lib_dir / "libtk9.0.so").exists()
    assert not (lib_dir / "tcl9.0").exists()
    assert not (lib_dir / "tk9.0").exists()
    assert not (lib_dir / "itcl4.3.5").exists()
    assert not (lib_dir / "thread3.0.4").exists()


def test_trim_standalone_runtime_keeps_tcl_tk_when_tkinter(tmp_path: Path) -> None:
    """tkinter 项目保留 Tcl/Tk 运行时."""
    runtime = make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True, strip_symbols=False)

    lib_dir = runtime / "python" / "lib"
    assert (lib_dir / "libtcl9.0.so").is_file()
    assert (lib_dir / "libtk9.0.so").is_file()
    assert (lib_dir / "tcl9.0").is_dir()
    assert (lib_dir / "tk9.0").is_dir()
    assert (lib_dir / "itcl4.3.5").is_dir()
    assert (lib_dir / "thread3.0.4").is_dir()


def test_trim_standalone_runtime_idempotent(tmp_path: Path) -> None:
    """重复调用幂等：二次调用 bytes_saved 为 0."""
    runtime = make_standalone_runtime(tmp_path)
    st1 = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st1, has_tkinter=False, strip_symbols=False)
    saved1 = st1._finalize().bytes_saved
    assert saved1 > 0

    st2 = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st2, has_tkinter=False, strip_symbols=False)
    saved2 = st2._finalize().bytes_saved
    assert saved2 == 0


# --- _strip_elf_symbols 测试 ---


class _StripFailed:
    """模拟 strip 命令失败（非零退出码）."""

    returncode = 1
    stdout = ""
    stderr = "strip: bad file"


def test_strip_elf_symbols_strip_missing_silently_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip 命令缺失（FileNotFoundError）静默跳过返回 (0, 0)."""
    lib = tmp_path / "libpython3.11.so.1.0"
    lib.write_bytes(b"\x7fELF" + b"x" * 100)

    def fake_run(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError("strip not found")

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", fake_run)

    ok, saved = _strip_elf_symbols(lib, "linux")
    assert ok == 0
    assert saved == 0
    assert lib.read_bytes() == b"\x7fELF" + b"x" * 100


def test_strip_elf_symbols_strip_fails_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip 命令返回非零退出码时返回 (0, 0) 不抛异常."""
    lib = tmp_path / "libpython3.11.so.1.0"
    lib.write_bytes(b"\x7fELF" + b"x" * 100)

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", lambda *a, **kw: _StripFailed())

    ok, saved = _strip_elf_symbols(lib, "linux")
    assert ok == 0
    assert saved == 0


def test_strip_elf_symbols_success_returns_saved_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip 成功时返回 (1, saved_bytes)."""
    lib = tmp_path / "libpython3.11.so.1.0"
    lib.write_bytes(b"\x7fELF" + b"x" * 200)

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        target = Path(cmd[-1])
        target.write_bytes(b"\x7fELF" + b"x" * 46)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", fake_run)

    ok, saved = _strip_elf_symbols(lib, "linux")
    assert ok == 1
    assert saved == 204 - 50


def test_strip_elf_symbols_already_stripped_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已 stripped 文件 strip 后体积未变返回 saved=0."""
    lib = tmp_path / "libpython3.11.so.1.0"
    lib.write_bytes(b"\x7fELF" + b"x" * 100)

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", lambda *a, **kw: CompletedStub())

    ok, saved = _strip_elf_symbols(lib, "linux")
    assert ok == 1
    assert saved == 0


# --- _strip_tcl_tk_counted 测试 ---


def test_strip_tcl_tk_counted_no_lib_dir(tmp_path: Path) -> None:
    """lib 目录不存在时返回 (0, 0, 0)."""
    python_dir = tmp_path / "python"
    python_dir.mkdir()
    saved, dirs, files = _strip_tcl_tk_counted(python_dir)
    assert (saved, dirs, files) == (0, 0, 0)


def test_strip_tcl_tk_counted_strips_files_and_dirs(tmp_path: Path) -> None:
    """剥离 Tcl/Tk 共享库、脚本目录、itcl/thread 扩展."""
    python_dir = tmp_path / "python"
    lib_dir = python_dir / "lib"
    lib_dir.mkdir(parents=True)

    (lib_dir / "libtcl9.0.so").write_bytes(b"tcl" * 100)
    (lib_dir / "libtk9.0.so").write_bytes(b"tk" * 50)
    (lib_dir / "tcl9.0").mkdir()
    (lib_dir / "tcl9.0" / "init.tcl").write_bytes(b"x" * 50)
    (lib_dir / "tk9.0").mkdir()
    (lib_dir / "tk9.0" / "tk.tcl").write_bytes(b"y" * 30)
    (lib_dir / "itcl4.3.5").mkdir()
    (lib_dir / "itcl4.3.5" / "itcl.tcl").write_bytes(b"z" * 20)
    (lib_dir / "thread3.0.4").mkdir()
    (lib_dir / "thread3.0.4" / "thread.tcl").write_bytes(b"w" * 10)
    (lib_dir / "libpython3.11.so.1.0").write_bytes(b"py" * 200)
    (lib_dir / "libc.so.6").write_bytes(b"c" * 100)

    saved, dirs, files = _strip_tcl_tk_counted(python_dir)

    assert dirs == 4
    assert files == 2
    assert saved == 510
    assert (lib_dir / "libpython3.11.so.1.0").is_file()
    assert (lib_dir / "libc.so.6").is_file()
    assert not (lib_dir / "libtcl9.0.so").exists()
    assert not (lib_dir / "libtk9.0.so").exists()
    assert not (lib_dir / "tcl9.0").exists()
    assert not (lib_dir / "tk9.0").exists()


def test_strip_tcl_tk_counted_handles_symlinks(tmp_path: Path) -> None:
    """符号链接被 unlink 不计 saved_bytes."""
    python_dir = tmp_path / "python"
    lib_dir = python_dir / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "libtcl9.0.so").write_bytes(b"tcl" * 10)
    symlink_or_skip("libtcl9.0.so", lib_dir / "libtcl9.0.so.1")

    saved, dirs, files = _strip_tcl_tk_counted(python_dir)

    assert saved == 30
    assert files == 2
    assert dirs == 0
