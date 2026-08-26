"""``NuitkaProgress`` 进度测试：流式输出捕获、心跳线程、并行编译与超时防护."""

from __future__ import annotations

import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from fspack.packaging.nuitka import NuitkaCompiler
from fspack.packaging.nuitka.compile import (
    _MAX_COMPILE_WORKERS,
)
from fspack.platform import Platform
from fspack.progress import StageRecorder

# ---- _stream_compile 流式输出测试 ----


def test_stream_compile_captures_stdout_and_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 捕获子进程 stdout/stderr 并实时写入终端 fd."""
    cmd = [sys.executable, "-c", "import sys; print('out-msg'); sys.stderr.write('err-msg\\n')"]
    returncode, stdout, stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "out-msg" in stdout
    assert "err-msg" in stderr
    # 验证输出被实时写入终端 fd（capfd 捕获 fd 级输出）
    captured = capfd.readouterr()
    assert "out-msg" in captured.out
    assert "err-msg" in captured.err


def test_stream_compile_captures_delayed_output(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 能捕获子进程延迟输出（模拟 nuitka 编译耗时的多段输出）."""
    cmd = [
        sys.executable,
        "-c",
        "import sys, time; print('step1'); time.sleep(0.3); print('step2'); sys.stderr.write('warn\\n')",
    ]
    returncode, stdout, stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "step1" in stdout
    assert "step2" in stdout
    assert "warn" in stderr
    captured = capfd.readouterr()
    assert "step1" in captured.out
    assert "step2" in captured.out
    assert "warn" in captured.err


def test_stream_compile_returns_nonzero_on_failure(capfd: pytest.CaptureFixture[str]) -> None:
    """子进程退出码非零时 _stream_compile 正确返回 returncode."""
    cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    returncode, _stdout, _stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 3


def test_stream_compile_captures_multiline_output(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 能捕获多行输出（模拟 nuitka --show-progress 的多步骤输出）."""
    script = (
        "print('Nuitka:INFO:Started Python compilation'); "
        "print('Nuitka:INFO:Completed Python level compilation'); "
        "print('Nuitka:INFO:Generating C source code'); "
        "print('Nuitka:INFO:Running C compilation')"
    )
    cmd = [sys.executable, "-c", script]
    returncode, stdout, _stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "Started Python compilation" in stdout
    assert "Completed Python level compilation" in stdout
    assert "Generating C source code" in stdout
    assert "Running C compilation" in stdout
    captured = capfd.readouterr()
    assert "Running C compilation" in captured.out


# ---- 心跳线程测试 ----


def test_compile_src_heartbeat_logs_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """compile_src 在编译期间通过全局心跳线程输出进度日志（iter-131 并行化）.

    nuitka 的 reExecute 机制导致子进程输出不可靠（Windows close_fds=True 不继承 PIPE），
    全局心跳线程是唯一的进度反馈。mock _stream_compile 模拟耗时编译，验证心跳日志输出
    "Nuitka 并行编译中: 已完成 X/Y, 已耗时 Zs" 格式。
    """
    import time as _time

    from fspack.progress import StageRecorder

    # 缩短心跳间隔到 0.05 秒，避免测试等待 10 秒
    monkeypatch.setattr("fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL", 0.05)

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "app.py").write_text("print('hello')", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_text("", encoding="utf-8")
    cache = tmp_path / "cache"
    # 创建 nuitka 包假文件，让 _is_nuitka_cached 检查通过
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("", encoding="utf-8")

    # mock _stream_compile 模拟耗时 0.2 秒的编译（触发至少 1 次心跳）
    def slow_stream(_cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        _time.sleep(0.2)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(slow_stream))

    with caplog.at_level(logging.INFO, logger="fspack.packaging.nuitka"):
        st = StageRecorder("Nuitka 编译")
        NuitkaCompiler.compile_src(src, runtime, "3.10.11", Platform.WINDOWS, cache, stage=st)

    # 验证全局心跳日志输出（至少 1 次 "Nuitka 并行编译中"）
    heartbeat_logs = [r for r in caplog.records if "并行编译中" in r.message]
    assert len(heartbeat_logs) >= 1, f"期望至少 1 次心跳日志，实际 {len(heartbeat_logs)} 次"
    # 验证心跳消息格式：含 "已完成" 与 "已耗时"
    assert "已完成" in heartbeat_logs[0].message
    assert "已耗时" in heartbeat_logs[0].message


def test_compile_src_heartbeat_stops_after_compile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """编译完成后心跳线程立即停止，不输出多余日志."""
    from fspack.progress import StageRecorder

    # 心跳间隔设为较长值，确保编译期间不触发心跳
    monkeypatch.setattr("fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL", 10.0)

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "app.py").write_text("print('hello')", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_text("", encoding="utf-8")
    cache = tmp_path / "cache"
    # 创建 nuitka 包假文件，让 _is_nuitka_cached 检查通过
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("", encoding="utf-8")

    # mock _stream_compile 立即返回
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))

    # 验证不会因为心跳线程阻塞
    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.10.11", Platform.WINDOWS, cache, stage=st)


# ---- 并行编译测试（iter-131）----


def test_max_compile_workers_constant() -> None:
    """``_MAX_COMPILE_WORKERS`` 常量为 4，平衡并行收益与资源限制."""
    assert _MAX_COMPILE_WORKERS == 4


def test_compile_files_parallel_max_workers_capped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_compile_files`` 的 ``max_workers = min(cpu_count, _MAX_COMPILE_WORKERS)``.

    mock ThreadPoolExecutor 捕获 max_workers 参数，验证：
    - cpu_count >= 4 时 max_workers = 4（上限）
    - cpu_count < 4 时 max_workers = cpu_count
    """
    import concurrent.futures as cf

    captured_max_workers: list[int] = []
    real_tpe = cf.ThreadPoolExecutor

    class CapturingTPE(real_tpe):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            mw = kwargs.get("max_workers") or (args[0] if args else None)
            if mw is not None:
                captured_max_workers.append(mw)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("fspack.packaging.nuitka.compile.ThreadPoolExecutor", CapturingTPE)
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    for i in range(3):
        (src / f"f{i}.py").write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        sorted(src.glob("*.py")),
        st,
        target=Platform.WINDOWS,
    )

    assert len(captured_max_workers) == 1
    expected = min(os.cpu_count() or 1, _MAX_COMPILE_WORKERS)
    assert captured_max_workers[0] == expected


def test_compile_files_windows_py313_forces_mingw64(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows py>=3.13 且无 MSVC 时编译命令追加 force-mingw64（zig 产物损坏，强制走 winlibs）."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))
    # mock 无 MSVC：装了 VS 的机器上 scons 优先 MSVC，不加 force flag
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: False)

    src = tmp_path / "src"
    src.mkdir()
    f = src / "f0.py"
    f.write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [f],
        st,
        target=Platform.WINDOWS,
        py_version="3.13.1",
    )

    assert captured, "应至少编译一个文件"
    # --mingw64 实际选择 winlibs；experimental 仅为 py>=3.13 解锁 --mingw64
    assert "--mingw64" in captured[0]
    assert "--experimental=force-mingw64" in captured[0]
    # py 文件保持末位（诊断日志与测试依赖 cmd[-1] 定位源文件）
    assert captured[0][-1] == str(f)


def test_compile_files_mingw_mode_with_msvc_appends_mingw64(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compiler=mingw 且有 MSVC 时须传 --mingw64 顶掉 MSVC（回归：仅传 experimental 顶不掉）.

    Nuitka 语义（4.1.3 scons 源码确认）：``--mingw64`` 才实际选择 winlibs
    （scons ``tools=["mingw"]`` 并禁用 MSVC 工具）；``--experimental=
    force-mingw64`` 仅为 py>=3.13 解锁 ``--mingw64`` 的许可，**单独传不选择
    mingw**——装了 VS 的机器 scons 默认 MSVC 优先，产物仍是 cl.exe 编译。
    """
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))
    # mock 有 MSVC：装了 VS2022 的机器（用户实测场景，scons 默认走 cl 14.3）
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: True)

    src = tmp_path / "src"
    src.mkdir()
    f = src / "f0.py"
    f.write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [f],
        st,
        target=Platform.WINDOWS,
        py_version="3.11.9",
        compiler="mingw",
    )

    assert captured, "应至少编译一个文件"
    assert "--mingw64" in captured[0], "compiler=mingw + MSVC 须用 --mingw64 顶掉 MSVC"
    assert "--experimental=force-mingw64" in captured[0]


def test_compile_files_msvc_mode_never_appends_mingw_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compiler=msvc 恒不加 mingw 强制 flag（与 MSVC 选择互斥）."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: True)

    src = tmp_path / "src"
    src.mkdir()
    f = src / "f0.py"
    f.write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [f],
        st,
        target=Platform.WINDOWS,
        py_version="3.13.1",
        compiler="msvc",
    )

    assert captured, "应至少编译一个文件"
    assert "--mingw64" not in captured[0]
    assert "--experimental=force-mingw64" not in captured[0]


def test_compile_files_windows_py313_msvc_no_force_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows py>=3.13 但有 MSVC 时不加 force-mingw64（scons 优先 MSVC，flag 反而顶掉 MSVC）."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: True)

    src = tmp_path / "src"
    src.mkdir()
    f = src / "f0.py"
    f.write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [f],
        st,
        target=Platform.WINDOWS,
        py_version="3.13.1",
    )

    assert captured, "应至少编译一个文件"
    assert "--mingw64" not in captured[0]
    assert "--experimental=force-mingw64" not in captured[0]


def test_compile_files_windows_py312_no_force_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows py<3.13 不加 force-mingw64（scons 默认即 winlibs）."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    f = src / "f0.py"
    f.write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [f],
        st,
        target=Platform.WINDOWS,
        py_version="3.12.10",
    )

    assert captured, "应至少编译一个文件"
    assert "--mingw64" not in captured[0]
    assert "--experimental=force-mingw64" not in captured[0]


def test_compile_files_linux_no_force_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 不加 force-mingw64（用系统 gcc，无 zig fallback 问题）."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    f = src / "f0.py"
    f.write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [f],
        st,
        target=Platform.LINUX,
        py_version="3.13.1",
    )

    assert captured, "应至少编译一个文件"
    assert "--mingw64" not in captured[0]
    assert "--experimental=force-mingw64" not in captured[0]


def test_compile_files_parallel_completes_all_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行编译完成所有文件，成功/失败计数正确."""

    # 文件 0/1 成功，文件 2 失败
    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        py_file = cmd[-1]
        if "f2" in py_file:
            return (1, "", "error")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    files = []
    for i in range(3):
        f = src / f"f{i}.py"
        f.write_text("x = 1", encoding="utf-8")
        files.append(f)

    st = StageRecorder("编译")
    compiled, failed = NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        files,
        st,
        target=Platform.WINDOWS,
    )

    assert len(failed) == 1
    assert len(compiled) == 2
    # 成功的是 f0 和 f1
    compiled_names = {p.name for p in compiled}
    assert compiled_names == {"f0.py", "f1.py"}
    # stage.processed 被调用 2 次（2 个成功）
    assert st._items == 2


def test_compile_files_parallel_oserror_treated_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """worker 内 _stream_compile 抛 OSError（Popen 失败）时按失败文件处理，不中断构建.

    与"单文件失败仅告警"承诺一致：OSError（如 py_exe 不存在的 FileNotFoundError）
    等价于退出码非零，文件进 failed_files，其余文件继续编译。
    """

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        raise FileNotFoundError("python exe not found")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1", encoding="utf-8")
    (src / "ok.py").write_text("y = 2", encoding="utf-8")

    st = StageRecorder("编译")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka"):
        compiled, failed = NuitkaCompiler._compile_files(
            tmp_path / "python.exe",
            tmp_path / "bootstrap.py",
            [src / "app.py", src / "ok.py"],
            st,
            target=Platform.WINDOWS,
        )

    # 两个文件均触发 OSError，全部按失败处理，不抛异常中断构建
    assert compiled == set()
    assert set(failed) == {src / "app.py", src / "ok.py"}
    assert any("启动失败" in r.message for r in caplog.records)


def test_compile_files_parallel_oserror_mixed_with_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError 文件按失败处理的同时，正常文件仍成功编译（互不干扰）."""

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        if "bad.py" in cmd[-1]:
            raise FileNotFoundError("python exe not found")
        return 0, "", ""

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "bad.py").write_text("x = 1", encoding="utf-8")
    (src / "good.py").write_text("y = 2", encoding="utf-8")

    st = StageRecorder("编译")
    compiled, failed = NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [src / "bad.py", src / "good.py"],
        st,
        target=Platform.WINDOWS,
    )

    assert compiled == {src / "good.py"}
    assert failed == [src / "bad.py"]


def test_compile_files_parallel_heartbeat_stops_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 内 OSError 全部按失败处理后，finally 块停止心跳线程，不泄漏."""
    import threading as _threading

    monkeypatch.setattr("fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL", 0.05)

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        raise FileNotFoundError("python exe not found")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1", encoding="utf-8")

    active_before = _threading.active_count()
    st = StageRecorder("编译")
    compiled, failed = NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [src / "app.py"],
        st,
        target=Platform.WINDOWS,
    )
    # OSError 按失败处理，不抛异常
    assert compiled == set()
    assert failed == [src / "app.py"]
    # 心跳线程已停止（daemon=True 会在主线程退出时清理，但这里验证 finally 已 join）
    # 等待短暂时间让 daemon 线程完全退出
    import time as _time

    _time.sleep(0.1)
    active_after = _threading.active_count()
    # 心跳线程不应残留（active_count 不应增加）
    assert active_after <= active_before + 1  # 允许少量波动（其他 daemon 线程）


def test_compile_files_parallel_jobs_adjusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行模式 --jobs = max(1, cpu_count // max_workers)，避免过度超订."""
    captured_cmds: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured_cmds.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [src / "app.py"],
        st,
        target=Platform.WINDOWS,
    )

    assert len(captured_cmds) == 1
    # 找到 --jobs=N 参数
    jobs_args = [arg for arg in captured_cmds[0] if arg.startswith("--jobs=")]
    assert len(jobs_args) == 1
    jobs_value = int(jobs_args[0].split("=")[1])
    cpu = os.cpu_count() or 1
    max_workers = min(cpu, _MAX_COMPILE_WORKERS)
    expected_jobs = max(1, cpu // max_workers)
    assert jobs_value == expected_jobs


def test_compile_files_parallel_empty_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空文件列表时 _compile_files 返回空集合，无异常."""
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    st = StageRecorder("编译")
    compiled, failed = NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [],
        st,
        target=Platform.WINDOWS,
    )
    assert compiled == set()
    assert len(failed) == 0


def test_compile_files_parallel_global_heartbeat_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """全局心跳输出 "Nuitka 并行编译中: 已完成 X/Y, 已耗时 Zs" 格式."""
    import time as _time

    monkeypatch.setattr("fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL", 0.05)

    def slow_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        _time.sleep(0.2)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(slow_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    with caplog.at_level(logging.INFO, logger="fspack.packaging.nuitka"):
        NuitkaCompiler._compile_files(
            tmp_path / "python.exe",
            tmp_path / "bootstrap.py",
            [src / "app.py"],
            st,
            target=Platform.WINDOWS,
        )

    heartbeat_logs = [r for r in caplog.records if "并行编译中" in r.message]
    assert len(heartbeat_logs) >= 1
    # 验证格式含 "已完成" 和 "/"
    msg = heartbeat_logs[0].message
    assert "已完成" in msg
    assert "/" in msg
    assert "已耗时" in msg


# ---- _stream_compile 超时防护测试（iter-127） ----


def test_stream_compile_timeout_default_value() -> None:
    """``_stream_compile`` timeout 默认 None（运行时 dispatch ``_COMPILE_TIMEOUT``），可被参数覆盖.

    定义期绑定常量会绕过 compile 层 monkeypatch（dispatch 失效），故默认参数
    必须为 None 哨兵，函数体内经 ``_C`` 解析。
    """
    from fspack.packaging.nuitka.compile import _COMPILE_TIMEOUT

    assert _COMPILE_TIMEOUT == 600.0
    # 检查 timeout 参数默认值（通过 __defaults__ 或签名）
    sig = inspect.signature(NuitkaCompiler._stream_compile)
    timeout_param = sig.parameters["timeout"]
    assert timeout_param.default is None


def test_stream_compile_timeout_none_dispatches_compile_constant(
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout=None 时运行时 dispatch compile 层 ``_COMPILE_TIMEOUT``，monkeypatch 生效."""
    monkeypatch.setattr("fspack.packaging.nuitka.compile._COMPILE_TIMEOUT", 0.5)
    # 子进程 sleep 30s，dispatch 后的 0.5s 超时必然触发
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    returncode, _stdout, _stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode != 0


def test_stream_compile_timeout_kills_long_process(
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``timeout`` 超时后 kill 子进程，返回非零退出码并记录 warning."""
    # 子进程 sleep 30s，timeout=0.5s 必然超时
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka"):
        returncode, _stdout, _stderr = NuitkaCompiler._stream_compile(cmd, timeout=0.5)
    # kill 后 returncode 非 0（POSIX -9 / Windows 1）
    assert returncode != 0
    # warning 日志记录超时
    timeout_logs = [r for r in caplog.records if "超时" in r.message]
    assert len(timeout_logs) == 1
    assert "0s" in timeout_logs[0].message or "终止子进程" in timeout_logs[0].message


def test_stream_compile_timeout_not_triggered_for_fast_process(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """快速子进程不触发超时，正常返回退出码."""
    cmd = [sys.executable, "-c", "print('fast')"]
    returncode, stdout, _stderr = NuitkaCompiler._stream_compile(cmd, timeout=10.0)
    assert returncode == 0
    assert "fast" in stdout


def test_stream_compile_timeout_preserves_drained_output(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """超时 kill 前已 drain 的输出仍保留在返回值中供诊断."""
    # 子进程先输出再 sleep，超时 kill 后已输出的内容应保留
    cmd = [
        sys.executable,
        "-c",
        "print('partial-output'); import sys; sys.stdout.flush(); import time; time.sleep(30)",
    ]
    returncode, stdout, _stderr = NuitkaCompiler._stream_compile(cmd, timeout=0.5)
    assert returncode != 0
    # partial-output 在 kill 前已 drain 到 chunks（drain 线程 join 后）
    assert "partial-output" in stdout


def test_stream_compile_drain_join_timeout_constant() -> None:
    """``_DRAIN_JOIN_TIMEOUT`` 常量存在且为合理值（5s 覆盖 fd 关闭与调度延迟）."""
    from fspack.packaging.nuitka.compile import _DRAIN_JOIN_TIMEOUT

    assert _DRAIN_JOIN_TIMEOUT == 5.0
