"""NuitkaCompiler 单元测试：用户源码编译为本机 .pyd/.so."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack.config import (
    DEFAULT_NUITKA_VERSION,
    NUITKA_VERSIONS,
    get_mirror,
    nuitka_version_for,
)
from fspack.exceptions import NuitkaError
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


# ---- nuitka_version_for 字典查询测试 ----


def test_nuitka_version_for_311_returns_413() -> None:
    """Python 3.11.x 锁定 nuitka 4.1.3."""
    assert nuitka_version_for("3.11.9") == "4.1.3"
    assert nuitka_version_for("3.11.15") == "4.1.3"


def test_nuitka_version_for_38_returns_251() -> None:
    """Python 3.8.x 锁定 nuitka 2.5.1（4.x 不再维护 EOL 3.8）."""
    assert nuitka_version_for("3.8.10") == "2.5.1"
    assert nuitka_version_for("3.9.18") == "2.5.1"


def test_nuitka_version_for_unknown_returns_default() -> None:
    """未知 Python 版本（如 3.15）回退 DEFAULT_NUITKA_VERSION."""
    assert nuitka_version_for("3.15.0") == DEFAULT_NUITKA_VERSION


def test_nuitka_version_for_uses_major_minor_only() -> None:
    """版本查询按 major.minor 匹配，补丁版本不影响结果."""
    # 所有 3.10.x 都映射到同一个 nuitka 版本
    ver_a = nuitka_version_for("3.10.0")
    ver_b = nuitka_version_for("3.10.14")
    assert ver_a == ver_b == NUITKA_VERSIONS["3.10"]


# ---- _check_c_compiler C 编译器检查测试 ----


def test_check_c_compiler_windows_no_mingw_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标缺 mingw 交叉编译器时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: False)
    with pytest.raises(NuitkaError, match="mingw-w64"):
        NuitkaCompiler._check_c_compiler(Platform.WINDOWS)


def test_check_c_compiler_windows_with_mingw_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标有 mingw 时不 raise."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    # 不抛异常即通过
    NuitkaCompiler._check_c_compiler(Platform.WINDOWS)


def test_check_c_compiler_linux_no_gcc_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标缺 gcc 时 raise NuitkaError（用户要求显式报错）."""
    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: False)
    with pytest.raises(NuitkaError, match="gcc"):
        NuitkaCompiler._check_c_compiler(Platform.LINUX)


def test_check_c_compiler_linux_with_gcc_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标有 gcc 时不 raise."""
    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    NuitkaCompiler._check_c_compiler(Platform.LINUX)


# ---- ensure_env 环境就绪测试 ----


def test_ensure_env_runtime_py_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime python 不存在时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match="runtime python 未就绪"):
        NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_already_installed_skips_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime python 已安装 nuitka 时跳过 pip install，stage 标注缓存命中."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    # is_available 返回 True（import nuitka 成功），ensure_env 不会走到 pip install
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    assert st._hits == 1
    assert "4.1.3" in st._detail


def test_ensure_env_pip_install_target_invoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime python 未装 nuitka 时用构建机 pip install --target 装 nuitka."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)

    # 模拟构建机 python 路径
    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # is_available 第一次返回失败（未装），第二次成功（装后验证）
    # _ensure_pip_available 用 subprocess.run 检查 import pip，也要返回成功
    call_count = {"n": 0}

    def fake_run(cmd: list[str], **kw: Any) -> object:
        call_count["n"] += 1
        # 第 1 次：is_available(py_exe) → 失败（未装）
        # 第 2 次：_ensure_pip_available → 成功（有 pip）
        # 第 3 次：pip install --target → 成功
        # 第 4 次：is_available(py_exe) 验证 → 成功
        if call_count["n"] == 1:
            return _CompileFail()
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    # 捕获 pip install 命令
    captured_cmd: list[list[str]] = []

    def capture_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        return _CompileOK()

    # 重新设置：第 1 次 is_available 失败，后续成功
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        captured_cmd.append(cmd)
        if state["n"] == 1:
            return _CompileFail()  # is_available 首次检查
        return _CompileOK()  # _ensure_pip + pip install + 验证

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    # 找到 pip install 命令
    pip_cmds = [c for c in captured_cmd if "install" in c and "--target" in c]
    assert len(pip_cmds) == 1, f"应仅一次 pip install，实际 {len(pip_cmds)}"
    cmd = pip_cmds[0]
    # 用构建机 python
    assert cmd[0] == fake_build_python
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert "--target" in cmd
    # --target 指向 runtime/Lib/site-packages
    target_idx = cmd.index("--target")
    assert cmd[target_idx + 1] == str(runtime / "Lib" / "site-packages")
    # --no-compile 与 --no-cache-dir
    assert "--no-compile" in cmd
    assert "--no-cache-dir" in cmd
    # -i 镜像源
    assert "-i" in cmd
    assert "nuitka==4.1.3" in cmd
    assert "安装完成" in st._detail


def test_ensure_env_no_pip_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建机缺 pip 且 ensurepip 与 uv 两轮自救均失败时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 调用顺序：
    # 1. is_available (import nuitka) → 失败
    # 2. _has_pip (import pip) → 失败（缺 pip）
    # 3. _try_ensurepip (python -m ensurepip) → 失败
    # 4. _has_pip (再次检查) → 失败
    # 5. _try_uv_install_pip (uv pip install pip) → 失败
    # 6. _has_pip (再次检查) → 失败
    # → raise NuitkaError
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        if state["n"] == 1:
            return _CompileFail()  # is_available
        return _ImportAbsent()  # 所有后续调用均失败

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match="缺 pip 模块且两轮自助安装失败"):
        NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_ensurepip_self_heal_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 pip 时 ensurepip 自救成功，继续 pip install nuitka."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 调用顺序：
    # 1. is_available (import nuitka) → 失败
    # 2. _has_pip (import pip) → 失败（缺 pip）
    # 3. _try_ensurepip (python -m ensurepip) → 成功
    # 4. _has_pip (再次检查) → 成功（ensurepip 装好了）
    # 5. pip install --target nuitka → 成功
    # 6. is_available 验证 → 成功
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        if state["n"] == 1:
            return _CompileFail()  # is_available 首次失败
        if state["n"] == 2:
            return _ImportAbsent()  # _has_pip 失败（缺 pip）
        if state["n"] == 3:
            return _CompileOK()  # _try_ensurepip 成功
        if state["n"] == 4:
            return _CompileOK()  # _has_pip 再次检查成功
        return _CompileOK()  # pip install 与验证

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_ensure_env_uv_self_heal_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensurepip 失败但 uv pip install pip 自救成功."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 调用顺序（注意短路求值：_try_ensurepip 返回 False 时不调用 _has_pip）：
    # 1. is_available (import nuitka) → 失败
    # 2. _has_pip (import pip) → 失败（缺 pip）
    # 3. _try_ensurepip → 失败（uv venv 无 ensurepip 模块，短路不调用 _has_pip）
    # 4. _try_uv_install_pip → 成功
    # 5. _has_pip (再次检查) → 成功
    # 6. pip install --target nuitka → 成功
    # 7. is_available 验证 → 成功
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        if state["n"] == 1:
            return _CompileFail()  # is_available
        if state["n"] == 2:
            return _ImportAbsent()  # _has_pip 失败
        if state["n"] == 3:
            return _CompileFail()  # _try_ensurepip 失败
        return _CompileOK()  # uv pip install pip 与后续

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_has_pip_returns_bool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_pip 按 import pip 返回值返回 bool."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")

    # 成功
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())
    assert NuitkaCompiler._has_pip(str(py)) is True

    # 失败
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _ImportAbsent())
    assert NuitkaCompiler._has_pip(str(py)) is False


def test_try_ensurepip_invokes_python_m_ensurepip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_ensurepip 调用 `python -m ensurepip --default-pip`."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> object:
        captured.append(cmd)
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    assert NuitkaCompiler._try_ensurepip(str(py)) is True
    assert captured[0] == [str(py), "-m", "ensurepip", "--default-pip"]


def test_try_uv_install_pip_invokes_uv_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_uv_install_pip 调用 `uv pip install pip`."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> object:
        captured.append(cmd)
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    assert NuitkaCompiler._try_uv_install_pip() is True
    assert captured[0] == ["uv", "pip", "install", "pip"]


def test_ensure_env_pip_install_fails_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install 返回非零退出码时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        # 第 1 次：is_available → 失败
        # 第 2 次：_ensure_pip_available → 成功
        # 第 3 次：pip install → 失败
        if state["n"] == 3:
            return _CompileFail()
        if state["n"] == 1:
            return _CompileFail()
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match=r"pip install nuitka==4\.1\.3 失败"):
        NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_install_fails_import_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install 成功但 import nuitka 仍失败时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        # 第 1 次：is_available → 失败（未装）
        # 第 2 次：_ensure_pip_available → 成功
        # 第 3 次：pip install → 成功
        # 第 4 次：is_available 验证 → 失败（import 仍失败）
        if state["n"] in (1, 4):
            return _CompileFail()
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match="安装后 import nuitka 仍失败"):
        NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_linux_uses_python3_bin_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台 ensure_env 用 runtime/python/bin/python{ver} 与对应 site-packages."""
    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    runtime = tmp_path / "runtime"
    pybin = runtime / "python" / "bin"
    pybin.mkdir(parents=True)
    (pybin / "python3.11").write_bytes(b"")
    (runtime / "python" / "lib" / "python3.11" / "site-packages").mkdir(parents=True)

    # is_available 首次即成功（已装）
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(runtime, "3.11.9", Platform.LINUX, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    assert st._hits == 1


# ---- compile_with_stamp stamp 缓存测试 ----


def test_compile_with_stamp_cache_hit_skips_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 命中时跳过 ensure_env 与 compile_src."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    # 预写匹配的 stamp
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    NuitkaCompiler._stamp_path(dist).write_text(expected_key, encoding="utf-8")

    ensure_called = {"n": 0}
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: ensure_called.__setitem__("n", ensure_called["n"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert ensure_called["n"] == 0
    assert compile_called["n"] == 0
    assert st._hits == 1
    assert "stamp 命中" in st._detail


def test_compile_with_stamp_writes_stamp_after_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 未命中时调用 ensure_env + compile_src 并写入 stamp."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: None))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    # stamp 文件已写入，内容匹配 _stamp_key
    stamp = NuitkaCompiler._stamp_path(dist)
    assert stamp.is_file()
    nuitka_ver = nuitka_version_for("3.11.9")
    expected = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    assert stamp.read_text(encoding="utf-8") == expected


def test_compile_with_stamp_invalidates_on_src_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """源码变化使 stamp 失效，重新调用 ensure_env + compile_src."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    # 预写基于旧源码内容的 stamp
    nuitka_ver = nuitka_version_for("3.11.9")
    old_key = f"{nuitka_ver}|3.11.9|old_fingerprint"
    NuitkaCompiler._stamp_path(dist).write_text(old_key, encoding="utf-8")

    calls = {"ensure": 0, "compile": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("ensure", calls["ensure"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("compile", calls["compile"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    # stamp 不匹配，调用 ensure_env 与 compile_src
    assert calls["ensure"] == 1
    assert calls["compile"] == 1


def test_stamp_key_includes_nuitka_version_py_version_src_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 键含 nuitka_version + py_version + src_fingerprint."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    key = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    assert "4.1.3" in key
    assert "3.11.9" in key
    # 三段式：version|py_version|src_fp
    assert key.count("|") == 2


def test_stamp_path_under_dist(tmp_path: Path) -> None:
    """stamp 文件位于 dist/.nuitka_compile_stamp."""
    dist = tmp_path / "dist"
    assert NuitkaCompiler._stamp_path(dist) == dist / ".nuitka_compile_stamp"
