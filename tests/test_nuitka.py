"""NuitkaCompiler 单元测试：用户源码编译为本机 .pyd/.so."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.nuitka import NuitkaCompiler
from fspack.platform import Platform
from fspack.progress import StageRecorder


class _CompileOK:
    """subprocess.run 成功返回值桩."""

    returncode = 0
    stdout = ""
    stderr = ""


class _CompileFail:
    """subprocess.run 失败返回值桩."""

    returncode = 1
    stdout = ""
    stderr = "syntax error in foo.py"


class _ImportAbsent:
    """subprocess.run 失败返回值桩（模拟 nuitka 未安装）."""

    returncode = 1
    stdout = ""
    stderr = "ModuleNotFoundError: No module named 'nuitka'"


def test_is_available_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime python 已安装 nuitka 时 is_available 返回 True."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())
    assert NuitkaCompiler.is_available(py) is True


def test_is_available_false_on_import_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime python 未安装 nuitka 时 is_available 返回 False."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _ImportAbsent())
    assert NuitkaCompiler.is_available(py) is False


def test_is_available_false_when_py_missing(tmp_path: Path) -> None:
    """runtime python 文件不存在时直接返回 False（不调用 subprocess）."""
    assert NuitkaCompiler.is_available(tmp_path / "nonexistent.exe") is False


def test_runtime_python_windows(tmp_path: Path) -> None:
    """Windows 平台 runtime python 路径为 runtime/python.exe."""
    runtime = tmp_path / "runtime"
    py = NuitkaCompiler._runtime_python(runtime, "3.11.9", Platform.WINDOWS)
    assert py == runtime / "python.exe"


def test_runtime_python_linux(tmp_path: Path) -> None:
    """Linux 平台 runtime python 路径为 runtime/python/bin/python{major}.{minor}."""
    runtime = tmp_path / "runtime"
    py = NuitkaCompiler._runtime_python(runtime, "3.11.9", Platform.LINUX)
    assert py == runtime / "python" / "bin" / "python3.11"


def test_compile_src_skips_when_python_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """runtime python 未就绪时告警并跳过编译."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, stage=st)
    assert any("runtime python 未就绪" in r.message for r in caplog.records)
    assert "未就绪" in st._detail


def test_compile_src_skips_when_nuitka_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """runtime python 未安装 nuitka 时告警并跳过（回退到 .pyc 模式）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")

    # is_available 调用返回失败，模拟 nuitka 未安装
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _ImportAbsent())

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, stage=st)
    # 日志多行换行，用 "未安装" 子串匹配
    assert any("未安装" in r.message for r in caplog.records)
    assert "未安装" in st._detail


def test_compile_src_no_py_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """src 目录无 .py 文件时直接返回，detail 标注无文件."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, stage=st)
    assert "无 .py 文件" in st._detail


def test_compile_src_invokes_nuitka_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_src 对每个 .py 调用 `python -m nuitka --module`."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "util.py").write_text("x = 1")

    captured: list[list[str]] = []
    # is_available 与 compile 各调一次 subprocess.run
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kw: captured.append(cmd) or _CompileOK(),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, stage=st)

    # 第一次是 is_available 的 `import nuitka` 检查
    assert "import nuitka" in captured[0]
    # 后续是 compile 调用，每个 .py 一次
    compile_calls = captured[1:]
    assert len(compile_calls) == 2
    for cmd in compile_calls:
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "nuitka"
        assert "--module" in cmd
        assert "--no-pyi-file" in cmd
        assert "--remove-output" in cmd
        assert "--quiet" in cmd
        assert str(runtime / "python.exe") in cmd[0]


def test_compile_src_deletes_non_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """编译后删除非 __init__.py 的 .py，保留 __init__.py 维持包标识."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('hi')")
    (src / "sub").mkdir()
    (src / "sub" / "__init__.py").write_text("")
    (src / "sub" / "mod.py").write_text("x = 1")

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, stage=st)

    # __init__.py 保留
    assert (src / "__init__.py").is_file()
    assert (src / "sub" / "__init__.py").is_file()
    # 非 __init__.py 被删
    assert not (src / "app.py").exists()
    assert not (src / "sub" / "mod.py").exists()


def test_compile_src_failure_warns_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """单文件编译失败仅告警不中断，后续文件继续编译."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.py").write_text("x = 1")
    (src / "bad.py").write_text("invalid syntax !!!")

    # is_available 成功，编译时第一文件失败第二文件成功
    call_count = {"n": 0}

    def fake_run(cmd: list[str], **kw: Any) -> object:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _CompileOK()  # is_available
        if "bad.py" in cmd[-1]:
            return _CompileFail()
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, stage=st)

    # bad.py 编译失败告警
    assert any("Nuitka 编译失败" in r.message and "bad.py" in r.message for r in caplog.records)
    # detail 含失败计数
    assert "失败 1" in st._detail
    assert "编译 1" in st._detail


def test_compile_src_linux_uses_python3_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台用 runtime/python/bin/python{ver} 调 nuitka."""
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kw: captured.append(cmd) or _CompileOK(),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.LINUX, stage=st)

    # 第一个 subprocess 调用是 is_available 检查
    assert "python3.11" in captured[0][0]


def test_compile_src_records_stage_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_src 调用 stage.processed 与 stage.skip 记录编译与剥离计数."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('hi')")
    (src / "util.py").write_text("x = 1")

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, stage=st)

    # 2 个非 __init__.py 被剥离（__init__.py 保留维持包标识）
    assert st._skipped == 2
    # 3 个 .py 编译成功（__init__.py + app.py + util.py，不算 is_available 调用）
    assert st._items == 3


def test_compile_src_unlink_failure_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """删除 .py 失败时仅告警不中断（OSError 容错）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    # 让 Path.unlink 抛 OSError
    def fake_unlink(self: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, stage=st)

    # unlink 失败告警
    assert any("删除 .py 失败" in r.message for r in caplog.records)
    # stripped 仍为 0（unlink 失败不计入）
    assert st._skipped == 0
    # 编译仍计入
    assert st._items == 1
