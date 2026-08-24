"""runner 模块测试：``fsp r`` 直测与 ``_build_cmd`` 分支.

``run`` 与 ``_select_entry``/``_find_exe``/``_build_cmd``/``_build_debug_cmd``
原属 ``fspack/commands/run.py``，已整合到 :mod:`fspack.runner`。
``fsp c`` 的 ``clean_dist`` 测试见 :mod:`tests.test_builder`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.config import AppType, EntryPoint, ProjectInfo
from fspack.exceptions import FspackError
from fspack.runner import RunOptions, _build_cmd, _find_exe, _select_entry
from fspack.runner import run as run_run


def test_run_run_missing_exe(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(FspackError, match="未找到已构建"):
        run_run(tmp_path)


def test_run_run_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    exe = tmp_path / "dist" / "app.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0

    def fake_run(cmd: list[str], **kw: object) -> _Completed:
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return _Completed()

    monkeypatch.setattr("fspack.runner.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(tmp_path, rest_args=["--foo", "bar"])
    assert captured["cmd"] == [str(exe), "--foo", "bar"]
    assert captured["env"] is None


def test_run_run_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    exe = tmp_path / "dist" / "app.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    class _Completed:
        returncode = 2

    monkeypatch.setattr("fspack.runner.subprocess.run", lambda cmd, **kw: _Completed())
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    with pytest.raises(FspackError, match="程序退出码非零"):
        run_run(tmp_path)


def test_run_run_debug_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式 Windows 用 embed python.exe 直跑入口包装器."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    (dist / "src").mkdir(parents=True)
    (dist / "runtime" / "python.exe").write_bytes(b"")
    (dist / "src" / "app.py").write_text("")
    # wrapper 文件由 fspack b 生成，debug 模式运行 wrapper 而非直接入口
    (dist / "_entry_app.py").write_text('"""fspack 生成的入口包装器（app）。"""\n')

    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0

    def fake_run(cmd: list[str], **kw: object) -> _Completed:
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return _Completed()

    monkeypatch.setattr("fspack.runner.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(tmp_path, rest_args=["--foo"], options=RunOptions(debug=True))
    py = dist / "runtime" / "python.exe"
    wrapper = dist / "_entry_app.py"
    assert captured["cmd"] == [str(py), str(wrapper), "--foo"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONUNBUFFERED"] == "1"


def test_run_run_debug_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式 Linux 用 standalone python + PYTHONHOME."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    bin_dir = dist / "runtime" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.11").write_bytes(b"")
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")
    # wrapper 文件由 fspack b 生成，debug 模式运行 wrapper 而非直接入口
    (dist / "_entry_app.py").write_text('"""fspack 生成的入口包装器（app）。"""\n')

    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0

    def fake_run(cmd: list[str], **kw: object) -> _Completed:
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return _Completed()

    monkeypatch.setattr("fspack.runner.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Linux")
    run_run(tmp_path, options=RunOptions(debug=True))
    py = bin_dir / "python3.11"
    wrapper = dist / "_entry_app.py"
    assert captured["cmd"] == [str(py), str(wrapper)]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONHOME"] == str(dist / "runtime" / "python")
    assert env["PYTHONUNBUFFERED"] == "1"


def test_run_run_debug_missing_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式 embed python 不存在时报错."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")
    # wrapper 文件已存在，使流程进入 python 检查
    (dist / "_entry_app.py").write_text('"""fspack 生成的入口包装器（app）。"""\n')
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    with pytest.raises(FspackError, match="未找到 embed python"):
        run_run(tmp_path, options=RunOptions(debug=True))


def test_run_run_debug_missing_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式入口包装器不存在时报错."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    (dist / "runtime" / "python.exe").write_bytes(b"")
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    with pytest.raises(FspackError, match="未找到入口包装器"):
        run_run(tmp_path, options=RunOptions(debug=True))


def test_run_run_gui_nonzero_hints_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """GUI 应用非零退出码时提示用 --debug."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["PySide6"]\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    exe = tmp_path / "dist" / "app.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    class _Completed:
        returncode = 1

    monkeypatch.setattr("fspack.runner.subprocess.run", lambda cmd, **kw: _Completed())
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    with caplog.at_level("WARNING", logger="fspack.runner"), pytest.raises(FspackError, match="程序退出码非零"):
        run_run(tmp_path)
    assert "fspack r --debug" in caplog.text


def test_build_cmd_linux_with_wine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Linux")
    monkeypatch.setattr("fspack.runner.shutil.which", lambda x: "/usr/bin/wine")
    exe = Path("/tmp/app.exe")
    cmd = _build_cmd(exe)
    assert cmd == ["/usr/bin/wine", str(exe)]


def test_build_cmd_linux_wine_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Linux")
    monkeypatch.setattr("fspack.runner.shutil.which", lambda x: None)
    exe = Path("/tmp/app.exe")
    cmd = _build_cmd(exe)
    assert cmd == ["wine", str(exe)]


def test_build_cmd_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    exe = Path("/tmp/app.exe")
    cmd = _build_cmd(exe)
    assert cmd == [str(exe)]


def test_build_cmd_linux_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 原生可执行文件（无后缀）直接运行，不用 wine."""
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Linux")
    exe = Path("/tmp/app")
    cmd = _build_cmd(exe)
    assert cmd == [str(exe)]


def test_find_exe_linux_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 优先找原生无后缀可执行文件."""
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Linux")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    (dist / "app.exe").write_bytes(b"")
    assert _find_exe(tmp_path, "app") == dist / "app"


def test_find_exe_linux_fallback_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 无原生可执行文件时回退 .exe（wine 运行）."""
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Linux")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    assert _find_exe(tmp_path, "app") == dist / "app.exe"


def test_find_exe_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 只找 .exe."""
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    assert _find_exe(tmp_path, "app") == dist / "app.exe"


def test_find_exe_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无任何可执行文件返回 None."""
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Linux")
    (tmp_path / "dist").mkdir()
    assert _find_exe(tmp_path, "app") is None


def test_run_run_linux_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 原生可执行文件直接运行（不调 wine）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    exe = tmp_path / "dist" / "app"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0

    def fake_run(cmd: list[str], **kw: object) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.runner.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Linux")
    run_run(tmp_path)
    assert captured["cmd"] == [str(exe)]


# --- 多入口 _select_entry 测试 ---


def _make_multi_entry_info() -> ProjectInfo:
    """构造多入口 ProjectInfo 用于 _select_entry 测试."""
    ep1 = EntryPoint(name="cli", module="cli", file=Path("cli.py"), app_type=AppType.CLI)
    ep2 = EntryPoint(name="gui", module="gui", file=Path("gui.py"), app_type=AppType.GUI)
    ep3 = EntryPoint(name="web", module="web", file=Path("web.py"), app_type=AppType.CLI)
    return ProjectInfo(
        name="multi",
        version="0.1",
        src_dir=Path(),
        entry_module="cli",
        entry_file=Path("cli.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.10.11",
        entries=(ep1, ep2, ep3),
    )


def test_select_entry_default_prefers_gui(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """多入口未指定 --entry 时按 GUI 优先选默认入口并日志提示.

    entries=(cli=CLI, gui=GUI, web=CLI)，默认应选 gui（GUI 优先于 CLI），
    而非首个入口 cli。
    """
    info = _make_multi_entry_info()
    with caplog.at_level("INFO", logger="fspack.runner"):
        ep = _select_entry(info, None)
    assert ep.name == "gui"
    assert "未指定 --entry" in caplog.text
    assert "使用默认入口" in caplog.text


def test_select_entry_by_name() -> None:
    """--entry 按名匹配返回对应入口."""
    info = _make_multi_entry_info()
    assert _select_entry(info, "gui").name == "gui"
    assert _select_entry(info, "web").name == "web"


def test_select_entry_not_found() -> None:
    """--entry 未匹配时报错列出可用入口."""
    info = _make_multi_entry_info()
    with pytest.raises(FspackError, match="未找到入口: missing"):
        _select_entry(info, "missing")


def test_select_entry_single_project_no_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """单入口项目未指定 --entry 时不输出多入口提示日志."""
    info = ProjectInfo(
        name="app",
        version="0.1",
        src_dir=Path(),
        entry_module="app",
        entry_file=Path("app.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
    )
    with caplog.at_level("INFO", logger="fspack.runner"):
        ep = _select_entry(info, None)
    assert ep.name == "app"
    assert "未指定 --entry" not in caplog.text


# --- ProjectInfo.default_entry 测试 ---


def _make_same_type_multi_entry_info() -> ProjectInfo:
    """构造同类型（全 CLI）多入口 ProjectInfo 用于字母排序测试."""
    return ProjectInfo(
        name="same",
        version="0.1",
        src_dir=Path(),
        entry_module="zebra",
        entry_file=Path("zebra.py"),
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.10.11",
        entries=(
            EntryPoint(name="zebra", module="zebra", file=Path("zebra.py"), app_type=AppType.CLI),
            EntryPoint(name="alpha", module="alpha", file=Path("alpha.py"), app_type=AppType.CLI),
            EntryPoint(name="mango", module="mango", file=Path("mango.py"), app_type=AppType.CLI),
        ),
    )


def _make_multi_gui_entry_info() -> ProjectInfo:
    """构造多 GUI 入口 ProjectInfo 用于 GUI 内字母排序测试."""
    return ProjectInfo(
        name="guis",
        version="0.1",
        src_dir=Path(),
        entry_module="zgui",
        entry_file=Path("zgui.py"),
        app_type=AppType.GUI,
        dependencies=(),
        py_version="3.10.11",
        entries=(
            EntryPoint(name="zgui", module="zgui", file=Path("zgui.py"), app_type=AppType.GUI),
            EntryPoint(name="agui", module="agui", file=Path("agui.py"), app_type=AppType.GUI),
            EntryPoint(name="mgui", module="mgui", file=Path("mgui.py"), app_type=AppType.GUI),
        ),
    )


def test_default_entry_prefers_gui_over_cli() -> None:
    """多入口混合 CLI/GUI 时 default_entry 选 GUI（即使 GUI 非首入口）."""
    info = _make_multi_entry_info()
    assert info.default_entry.name == "gui"
    assert info.default_entry.app_type is AppType.GUI


def test_default_entry_alphabetical_within_same_type() -> None:
    """同类型多入口按名字母序选第一个（CLI 内 alpha < mango < zebra）."""
    info = _make_same_type_multi_entry_info()
    assert info.default_entry.name == "alpha"


def test_default_entry_alphabetical_within_gui() -> None:
    """多 GUI 入口按名字母序选第一个（agui < mgui < zgui）."""
    info = _make_multi_gui_entry_info()
    assert info.default_entry.name == "agui"


def test_default_entry_single_project_returns_only_entry() -> None:
    """单入口项目 default_entry 返回唯一入口（自身）."""
    info = ProjectInfo(
        name="solo",
        version="0.1",
        src_dir=Path(),
        entry_module="solo",
        entry_file=Path("solo.py"),
        app_type=AppType.GUI,
        dependencies=(),
        py_version="3.11.9",
    )
    assert info.default_entry.name == "solo"
    assert info.default_entry.app_type is AppType.GUI


def test_run_run_multi_entry_select(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fspack r --entry gui 运行对应入口的 exe."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "multi"\nversion = "0.1"\n\n[project.scripts]\ncli = "cli:main"\ngui = "gui:main"\n'
    )
    (tmp_path / "cli.py").write_text("def main():\n    pass\n")
    (tmp_path / "gui.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "cli.exe").write_bytes(b"")
    gui_exe = dist / "gui.exe"
    gui_exe.write_bytes(b"")

    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0

    def fake_run(cmd: list[str], **kw: object) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.runner.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    run_run(tmp_path, options=RunOptions(entry="gui"))
    assert captured["cmd"] == [str(gui_exe)]


# --- iter-148 前后端分离 Web 打包：WEB 类型非零退出码警告 ---


def test_run_run_web_nonzero_hints_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """WEB 应用非零退出码时提示用 --debug（与 GUI 一样关闭控制台）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["flask"]\n')
    (tmp_path / "app.py").write_text("from flask import Flask\ndef main():\n    pass\n")
    exe = tmp_path / "dist" / "app.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    class _Completed:
        returncode = 1

    monkeypatch.setattr("fspack.runner.subprocess.run", lambda cmd, **kw: _Completed())
    monkeypatch.setattr("fspack.runner.platform.system", lambda: "Windows")
    with caplog.at_level("WARNING", logger="fspack.runner"), pytest.raises(FspackError, match="程序退出码非零"):
        run_run(tmp_path)
    # WEB 类型警告含 "WEB"（app_type.value.upper()）
    assert "WEB" in caplog.text
    assert "fspack r --debug" in caplog.text
