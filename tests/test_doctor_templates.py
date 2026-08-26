"""doctor/templates.py 测试：平台过滤、产物定位、调试命令构造、_run_template 运行与残留过滤."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from fspack.console import console
from fspack.doctor import (
    _build_debug_cmd,
    _build_run_cmd,
    _filter_platform_supported,
    _find_debug_python,
    _find_dist_exe,
    _find_wrapper,
    _platform_skip_reason,
    _run_template,
)
from fspack.platform import Platform
from fspack.templates.registry import Template


@pytest.fixture(autouse=True)
def _fixed_rich_width(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """固定 Rich Console 宽度，避免窄终端环境下 word wrap 导致断言失败.

    多个测试渲染含 8 列的 Rich Table（bench 模式汇总表），窄终端（width<80）下
    Rich 会截断长文本（如 ``ModuleNotFoundError`` → ``ModuleNot…``）或丢弃列，
    导致断言偶发失败。固定 width=200 确保所有环境渲染一致。

    必须直接 patch ``_width`` 而非走 ``width`` 属性往返：rich 的 ``width`` getter
    在 ``_width``/``_height`` 均已设置时（shell 导出 ``COLUMNS``/``LINES`` 环境变量
    即如此）返回 ``_width - legacy_windows``，而 setter 存原始值——往返一次宽度净
    减 1（legacy Windows 控制台）。本文件数百个测试逐个缩水，跑完后宽度变负数，
    rich 会把后续所有文本裁剪为空，殃及后续文件 27 个 capsys 断言。
    ``monkeypatch`` 记录的是原始 ``_width`` 值，恢复无损。
    """
    monkeypatch.setattr(console.rich, "_width", 200)
    yield


# ---- 平台兼容性过滤（doctor --test 跳过无兼容 Python 的模板）----


def _tpl(tpl_id: str, requires_python: str) -> Template:
    """构造最小 Template（仅 id 与 requires_python 参与平台过滤）."""
    return Template(id=tpl_id, name=tpl_id, description="", category="cli", files=(), requires_python=requires_python)


def test_platform_skip_reason_incompatible_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 无 3.8/3.9 standalone，``>=3.8,<3.10`` 约束应给出跳过原因."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    reason = _platform_skip_reason(_tpl("pyside2", ">=3.8,<3.10"))
    assert reason is not None
    assert "requires-python" in reason
    assert ">=3.8,<3.10" in reason


def test_platform_skip_reason_compatible_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """宽松约束（``>=3.8``）在 Linux 有 3.10+ 可用，可构建返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    assert _platform_skip_reason(_tpl("helloworld", ">=3.8")) is None


def test_platform_skip_reason_pyside2_ok_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 ``>=3.8,<3.10`` 约束在 Windows 有 3.8/3.9 embed 可用，不跳过."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    assert _platform_skip_reason(_tpl("pyside2", ">=3.8,<3.10")) is None


def test_platform_skip_reason_unsatisfiable_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何平台版本表都无法满足的约束（``>=3.15``）给出跳过原因."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    reason = _platform_skip_reason(_tpl("future", ">=3.15"))
    assert reason is not None
    assert "requires-python" in reason


def test_filter_platform_supported_splits(monkeypatch: pytest.MonkeyPatch) -> None:
    """混合列表按平台兼容性拆分为 (可构建, 跳过及原因)."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    ok1, ok2 = _tpl("a", ">=3.8"), _tpl("b", ">=3.10,<3.14")
    skip1 = _tpl("pyside2", ">=3.8,<3.10")
    buildable, skipped = _filter_platform_supported([ok1, skip1, ok2])
    assert buildable == [ok1, ok2]
    assert [tpl for tpl, _ in skipped] == [skip1]
    assert all("requires-python" in reason for _, reason in skipped)


# ---- _find_dist_exe ----


def test_find_dist_exe_linux_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 优先返回原生无后缀可执行文件."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    (dist / "app.exe").write_bytes(b"")
    found = _find_dist_exe(tmp_path, "app")
    assert found == dist / "app"


def test_find_dist_exe_linux_fallback_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 无原生可执行文件时回退 .exe（用 wine 运行）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    found = _find_dist_exe(tmp_path, "app")
    assert found == dist / "app.exe"


def test_find_dist_exe_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 仅查 .exe."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    found = _find_dist_exe(tmp_path, "app")
    assert found == dist / "app.exe"


def test_find_dist_exe_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dist 下无可执行文件时返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    (tmp_path / "dist").mkdir()
    assert _find_dist_exe(tmp_path, "missing") is None


def test_find_dist_exe_no_dist_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dist 目录不存在时返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    assert _find_dist_exe(tmp_path, "app") is None


# ---- _build_run_cmd ----


def test_build_run_cmd_native_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 原生可执行文件（无 .exe 后缀）直跑."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    exe = tmp_path / "app"
    assert _build_run_cmd(exe) == [str(exe)]


def test_build_run_cmd_exe_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 下 .exe 直跑，不调 wine."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    exe = tmp_path / "app.exe"
    assert _build_run_cmd(exe) == [str(exe)]


def test_build_run_cmd_exe_on_linux_with_wine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 下 .exe 用 wine 运行（wine 在 PATH）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("fspack.doctor.shutil.which", lambda name: "/usr/bin/wine")
    exe = tmp_path / "app.exe"
    assert _build_run_cmd(exe) == ["/usr/bin/wine", str(exe)]


def test_build_run_cmd_exe_on_linux_no_wine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 下 .exe 但 wine 未安装时回退字符串 'wine'（_run_template 捕获 OSError）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("fspack.doctor.shutil.which", lambda name: None)
    exe = tmp_path / "app.exe"
    assert _build_run_cmd(exe) == ["wine", str(exe)]


# ---- _find_debug_python / _find_wrapper / _build_debug_cmd ----


def test_find_debug_python_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux debug 模式查 dist/runtime/python/bin/python3.X."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    bin_dir = tmp_path / "dist" / "runtime" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.11").write_bytes(b"")
    (bin_dir / "python3.12").write_bytes(b"")
    py = _find_debug_python(tmp_path)
    assert py == bin_dir / "python3.11"  # sorted 取首个


def test_find_debug_python_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows debug 模式查 dist/runtime/python.exe."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    runtime = tmp_path / "dist" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"")
    py = _find_debug_python(tmp_path)
    assert py == runtime / "python.exe"


def test_find_debug_python_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime 目录不存在时返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    assert _find_debug_python(tmp_path) is None


def test_find_wrapper_present(tmp_path: Path) -> None:
    """存在 dist/_entry_<name>.py 时返回路径."""
    dist = tmp_path / "dist"
    dist.mkdir()
    wrapper = dist / "_entry_app.py"
    wrapper.write_text("")
    assert _find_wrapper(tmp_path, "app") == wrapper


def test_find_wrapper_absent(tmp_path: Path) -> None:
    """wrapper 不存在时返回 None."""
    assert _find_wrapper(tmp_path, "app") is None


def test_build_debug_cmd_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux debug 命令 = [python, wrapper]，env 含 PYTHONHOME + PYTHONUNBUFFERED."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    bin_dir = tmp_path / "dist" / "runtime" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    py_file = bin_dir / "python3.11"
    py_file.write_bytes(b"")
    wrapper = tmp_path / "dist" / "_entry_app.py"
    wrapper.write_text("")
    result = _build_debug_cmd(tmp_path, "app")
    assert result is not None
    cmd, env = result
    assert cmd == [str(py_file), str(wrapper)]
    assert env["PYTHONHOME"] == str(tmp_path / "dist" / "runtime" / "python")
    assert env["PYTHONUNBUFFERED"] == "1"


def test_build_debug_cmd_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows debug 命令不含 PYTHONHOME（embed python 用 _pth 定位标准库）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    runtime = tmp_path / "dist" / "runtime"
    runtime.mkdir(parents=True)
    py_file = runtime / "python.exe"
    py_file.write_bytes(b"")
    wrapper = tmp_path / "dist" / "_entry_app.py"
    wrapper.write_text("")
    result = _build_debug_cmd(tmp_path, "app")
    assert result is not None
    cmd, env = result
    assert cmd == [str(py_file), str(wrapper)]
    assert "PYTHONHOME" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


def test_build_debug_cmd_wrapper_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wrapper 缺失时返回 None（调用方回退直跑 exe）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    bin_dir = tmp_path / "dist" / "runtime" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.11").write_bytes(b"")
    # 不创建 _entry_app.py
    assert _build_debug_cmd(tmp_path, "app") is None


def test_build_debug_cmd_python_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """embed python 缺失时返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "_entry_app.py").write_text("")
    # runtime 目录不存在
    assert _build_debug_cmd(tmp_path, "app") is None


# ---- _run_template ----


class _FakeProc:
    """模拟 subprocess.Popen 返回的进程对象."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.terminated = False
        self.killed = False
        self.communicate_calls = 0
        self._raise_timeout = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_calls += 1
        if timeout is not None and self._raise_timeout:
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)
        return (self._stdout, self._stderr)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_run_template_success_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程退出码 0 → 运行成功（CLI 正常执行完成）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(returncode=0, stdout="hello, world\n", stderr="")
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is True
    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.error == ""


def test_run_template_failure_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程退出码非 0 → 运行失败，捕获 stderr 首行."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(
        returncode=1,
        stdout="",
        stderr="ModuleNotFoundError: No module named 'modules'\nTraceback follows",
    )
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is False
    assert result.timed_out is False
    assert result.exit_code == 1
    assert "ModuleNotFoundError" in result.error


def test_run_template_failure_empty_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程退出码非 0 且 stderr 为空时 error 显示退出码."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(returncode=2, stdout="", stderr="")
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is False
    assert result.exit_code == 2
    assert "退出码 2" in result.error


def test_run_template_timeout_treated_as_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时未退出视为 GUI/Web 事件循环正常，主动 terminate 后返回成功."""

    class _TimeoutProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self._raise_timeout = True

    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _TimeoutProc()
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=0.5)
    assert result.success is True
    assert result.timed_out is True
    assert result.exit_code is None
    assert proc.terminated is True


def test_run_template_timeout_terminate_then_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时后 terminate 仍超时则升级为 kill."""

    class _StubbornProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self._raise_timeout = True

    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _StubbornProc()
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=0.3)
    assert result.success is True
    assert result.timed_out is True
    # terminate 后再次 communicate 超时 → 调 kill
    assert proc.terminated is True
    assert proc.killed is True


def test_run_template_oserror_startup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Popen 抛 OSError（如 wine 未安装）→ 运行失败，error 含启动失败."""

    def _raise_popen(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'wine'")

    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", _raise_popen)
    result = _run_template(["wine", str(tmp_path / "app.exe")], timeout=1.0)
    assert result.success is False
    assert result.timed_out is False
    assert result.exit_code is None
    assert "启动失败" in result.error
    assert "wine" in result.error


def test_run_template_passes_env_to_popen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式传入 env 时透传给 Popen，并追加 FSPACK_TIMING=1 激活自终止钩子."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    captured: dict[str, object] = {}
    proc = _FakeProc(returncode=0)

    def _capture_popen(*args: object, **kwargs: object) -> _FakeProc:
        captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr("fspack.doctor.subprocess.Popen", _capture_popen)
    env = {"PYTHONHOME": "/tmp/python", "PYTHONUNBUFFERED": "1"}
    _run_template([str(tmp_path / "app")], env, timeout=1.0)  # type: ignore[arg-type]
    assert captured["env"] == {**env, "FSPACK_TIMING": "1"}


def test_run_template_env_none_injects_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """env=None 继承当前环境，同样追加 FSPACK_TIMING=1（回退直跑 exe 场景）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    captured: dict[str, object] = {}
    proc = _FakeProc(returncode=0)

    def _capture_popen(*args: object, **kwargs: object) -> _FakeProc:
        captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr("fspack.doctor.subprocess.Popen", _capture_popen)
    _run_template([str(tmp_path / "app")], timeout=1.0)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FSPACK_TIMING"] == "1"
    # 基于当前环境复制（PATH 等原键保留），未误用 env=None 继承语义
    assert env.get("PATH") == os.environ.get("PATH")


def test_run_template_failure_skips_timing_marker_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非零退出时 stderr 的 ``[fspack`` 打点行被跳过，error 取首个真实错误行."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(
        returncode=1,
        stdout="",
        stderr=(
            "[fspack timing] env_ready @1.0ms\n[fspack timing] gui_ready @50.0ms\nValueError: boom\nTraceback follows\n"
        ),
    )
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is False
    assert "ValueError: boom" in result.error
    assert "fspack timing" not in result.error


# ---- _copy_ignore（模板复制过滤）----


def test_copy_ignore_filters_dev_and_build_residue() -> None:
    """回调过滤本地开发/构建残留：node_modules/dist/deploy/缓存目录与编译产物.

    模板源目录中的这些条目均为本地测试残留（git 已忽略），带入 doctor 临时
    环境会破坏构建语义（node_modules 触发 pnpm 交互式询问挂起、dist/deploy
    非空让前端阶段误判产物就绪跳过构建）。
    """
    from fspack.doctor.templates import _copy_ignore

    names = ["node_modules", "dist", "deploy", "__pycache__", ".venv", ".git", "a.pyc", "b.pyo", "c.pyd"]
    assert _copy_ignore("src", names) == set(names)

    kept = ["main.py", "package.json", "src", "public", "pyproject.toml"]
    assert _copy_ignore("src", kept) == set()


def test_copytree_with_copy_ignore_skips_residue(tmp_path: Path) -> None:
    """copytree 挂载 _copy_ignore 后残留目录不进入目标，正常源文件完整复制."""
    import shutil as _shutil

    from fspack.doctor.templates import _copy_ignore

    src = tmp_path / "tpl"
    (src / "src" / "app").mkdir(parents=True)
    (src / "src" / "app" / "main.py").write_text('print("hi")', encoding="utf-8")
    (src / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (src / "node_modules" / "vue").mkdir(parents=True)
    (src / "node_modules" / "vue" / "package.json").write_text("{}", encoding="utf-8")
    (src / "dist").mkdir()
    (src / "dist" / "app.exe").write_bytes(b"bin")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "m.pyc").write_bytes(b"pyc")

    dst = tmp_path / "dst"
    _shutil.copytree(src, dst, ignore=_copy_ignore)

    assert (dst / "src" / "app" / "main.py").read_text(encoding="utf-8") == 'print("hi")'
    assert (dst / "pyproject.toml").is_file()
    assert not (dst / "node_modules").exists()
    assert not (dst / "dist").exists()
    assert not (dst / "__pycache__").exists()
