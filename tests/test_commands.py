"""commands 子命令测试：build/clean/run 直测与 _build_cmd 分支。."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.commands.build import run as build_run
from fspack.commands.clean import run as clean_run
from fspack.commands.package import run as package_run
from fspack.commands.run import _build_cmd, _find_exe, _select_entry
from fspack.commands.run import run as run_run
from fspack.config import AppType, EntryPoint, ProjectInfo
from fspack.exceptions import FspackError
from fspack.mirror import get_mirror
from fspack.platform import Platform, detect_platform


def test_build_run_default_mirror_and_py_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build(
        project: Path,
        mirror: object,
        py_version: str | None,
        target: object = None,
        keep_modules: set[str] | None = None,
    ) -> None:
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["target"] = target

    monkeypatch.setattr("fspack.commands.build.build", fake_build)
    build_run(tmp_path, mirror=None, py_version=None)
    assert captured["mirror"] == get_mirror("aliyun")
    assert captured["py_version"] is None
    assert captured["target"] is detect_platform()


def test_build_run_explicit_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build(
        project: Path,
        mirror: object,
        py_version: str,
        target: object = None,
        keep_modules: set[str] | None = None,
    ) -> None:
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["target"] = target

    monkeypatch.setattr("fspack.commands.build.build", fake_build)
    build_run(tmp_path, mirror="aliyun", py_version="3.10.0", target=Platform.LINUX)
    assert captured["mirror"] == get_mirror("aliyun")
    assert captured["py_version"] == "3.10.0"
    assert captured["target"] is Platform.LINUX


def test_build_run_keep_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build(
        project: Path,
        mirror: object,
        py_version: str | None,
        target: object = None,
        keep_modules: set[str] | None = None,
    ) -> None:
        captured["keep_modules"] = keep_modules

    monkeypatch.setattr("fspack.commands.build.build", fake_build)
    build_run(tmp_path, keep_modules={"PySide2.QtGui"})
    assert captured["keep_modules"] == {"PySide2.QtGui"}


def test_clean_run_removes_dist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "x.txt").write_text("x")
    clean_run(tmp_path)
    assert not dist.exists()


def test_clean_run_no_dist(tmp_path: Path) -> None:
    clean_run(tmp_path)


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

    monkeypatch.setattr("fspack.commands.run.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
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

    monkeypatch.setattr("fspack.commands.run.subprocess.run", lambda cmd, **kw: _Completed())
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    with pytest.raises(FspackError, match="程序退出码非零"):
        run_run(tmp_path)


def test_run_run_debug_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式 Windows 用 embed python.exe 直跑入口包装器。."""
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

    monkeypatch.setattr("fspack.commands.run.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    run_run(tmp_path, debug=True, rest_args=["--foo"])
    py = dist / "runtime" / "python.exe"
    wrapper = dist / "_entry_app.py"
    assert captured["cmd"] == [str(py), str(wrapper), "--foo"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONUNBUFFERED"] == "1"


def test_run_run_debug_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式 Linux 用 standalone python + PYTHONHOME。."""
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

    monkeypatch.setattr("fspack.commands.run.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    run_run(tmp_path, debug=True)
    py = bin_dir / "python3.11"
    wrapper = dist / "_entry_app.py"
    assert captured["cmd"] == [str(py), str(wrapper)]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONHOME"] == str(dist / "runtime" / "python")
    assert env["PYTHONUNBUFFERED"] == "1"


def test_run_run_debug_missing_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式 embed python 不存在时报错。."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")
    # wrapper 文件已存在，使流程进入 python 检查
    (dist / "_entry_app.py").write_text('"""fspack 生成的入口包装器（app）。"""\n')
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    with pytest.raises(FspackError, match="未找到 embed python"):
        run_run(tmp_path, debug=True)


def test_run_run_debug_missing_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式入口包装器不存在时报错。."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    (dist / "runtime" / "python.exe").write_bytes(b"")
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    with pytest.raises(FspackError, match="未找到入口包装器"):
        run_run(tmp_path, debug=True)


def test_run_run_gui_nonzero_hints_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """GUI 应用非零退出码时提示用 --debug。."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\ndependencies = ["PySide6"]\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    exe = tmp_path / "dist" / "app.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    class _Completed:
        returncode = 1

    monkeypatch.setattr("fspack.commands.run.subprocess.run", lambda cmd, **kw: _Completed())
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    with caplog.at_level("WARNING", logger="fspack.commands.run"), pytest.raises(FspackError, match="程序退出码非零"):
        run_run(tmp_path)
    assert "fspack r --debug" in caplog.text


def test_build_cmd_linux_with_wine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    monkeypatch.setattr("fspack.commands.run.shutil.which", lambda x: "/usr/bin/wine")
    exe = Path("/tmp/app.exe")
    cmd = _build_cmd(exe)
    assert cmd == ["/usr/bin/wine", str(exe)]


def test_build_cmd_linux_wine_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    monkeypatch.setattr("fspack.commands.run.shutil.which", lambda x: None)
    exe = Path("/tmp/app.exe")
    cmd = _build_cmd(exe)
    assert cmd == ["wine", str(exe)]


def test_build_cmd_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    exe = Path("/tmp/app.exe")
    cmd = _build_cmd(exe)
    assert cmd == [str(exe)]


def test_build_cmd_linux_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 原生可执行文件（无后缀）直接运行，不用 wine。."""
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    exe = Path("/tmp/app")
    cmd = _build_cmd(exe)
    assert cmd == [str(exe)]


def test_find_exe_linux_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 优先找原生无后缀可执行文件。."""
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    (dist / "app.exe").write_bytes(b"")
    assert _find_exe(tmp_path, "app") == dist / "app"


def test_find_exe_linux_fallback_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 无原生可执行文件时回退 .exe（wine 运行）。."""
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    assert _find_exe(tmp_path, "app") == dist / "app.exe"


def test_find_exe_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 只找 .exe。."""
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    assert _find_exe(tmp_path, "app") == dist / "app.exe"


def test_find_exe_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无任何可执行文件返回 None。."""
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    (tmp_path / "dist").mkdir()
    assert _find_exe(tmp_path, "app") is None


def test_run_run_linux_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 原生可执行文件直接运行（不调 wine）。."""
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

    monkeypatch.setattr("fspack.commands.run.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    run_run(tmp_path)
    assert captured["cmd"] == [str(exe)]


def test_package_run_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_installer(project: Path, mirror: object, py_version: str | None, no_build: bool = False) -> Path:
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["no_build"] = no_build
        captured["branch"] = "windows"
        return tmp_path / "app-setup.exe"

    def fake_build_linux_installer(
        project: Path, mirror: object, py_version: str | None, no_build: bool = False
    ) -> Path:
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["no_build"] = no_build
        captured["branch"] = "linux"
        return tmp_path / "app.deb"

    monkeypatch.setattr("fspack.commands.package.build_installer", fake_build_installer)
    monkeypatch.setattr("fspack.commands.package.build_linux_installer", fake_build_linux_installer)
    package_run(tmp_path)
    assert captured["mirror"] == get_mirror("aliyun")
    assert captured["py_version"] is None
    assert captured["no_build"] is False
    expected_branch = "linux" if detect_platform() is Platform.LINUX else "windows"
    assert captured["branch"] == expected_branch


def test_package_run_explicit_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_installer(project: Path, mirror: object, py_version: str, no_build: bool = False) -> Path:
        captured["py_version"] = py_version
        captured["no_build"] = no_build
        captured["branch"] = "windows"
        return Path("out.exe")

    def fake_build_linux_installer(project: Path, mirror: object, py_version: str, no_build: bool = False) -> Path:
        captured["branch"] = "linux"
        return Path("out.deb")

    monkeypatch.setattr("fspack.commands.package.build_installer", fake_build_installer)
    monkeypatch.setattr("fspack.commands.package.build_linux_installer", fake_build_linux_installer)
    package_run(tmp_path, mirror="aliyun", py_version="3.10.0", no_build=True, target=Platform.WINDOWS)
    assert captured["py_version"] == "3.10.0"
    assert captured["no_build"] is True
    assert captured["branch"] == "windows"


def test_package_run_explicit_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_installer(project: Path, mirror: object, py_version: str, no_build: bool = False) -> Path:
        captured["branch"] = "windows"
        return Path("out.exe")

    def fake_build_linux_installer(project: Path, mirror: object, py_version: str, no_build: bool = False) -> Path:
        captured["py_version"] = py_version
        captured["no_build"] = no_build
        captured["branch"] = "linux"
        return Path("out.deb")

    monkeypatch.setattr("fspack.commands.package.build_installer", fake_build_installer)
    monkeypatch.setattr("fspack.commands.package.build_linux_installer", fake_build_linux_installer)
    package_run(tmp_path, mirror="aliyun", py_version="3.11.10", no_build=True, target=Platform.LINUX)
    assert captured["py_version"] == "3.11.10"
    assert captured["no_build"] is True
    assert captured["branch"] == "linux"


# --- 多入口 _select_entry 测试 ---


def _make_multi_entry_info() -> ProjectInfo:
    """构造多入口 ProjectInfo 用于 _select_entry 测试。."""
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


def test_select_entry_default_returns_first(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """多入口未指定 --entry 时返回首个入口并日志提示。."""
    info = _make_multi_entry_info()
    with caplog.at_level("INFO", logger="fspack.commands.run"):
        ep = _select_entry(info, None)
    assert ep.name == "cli"
    assert "未指定 --entry" in caplog.text


def test_select_entry_by_name() -> None:
    """--entry 按名匹配返回对应入口。."""
    info = _make_multi_entry_info()
    assert _select_entry(info, "gui").name == "gui"
    assert _select_entry(info, "web").name == "web"


def test_select_entry_not_found() -> None:
    """--entry 未匹配时报错列出可用入口。."""
    info = _make_multi_entry_info()
    with pytest.raises(FspackError, match="未找到入口: missing"):
        _select_entry(info, "missing")


def test_select_entry_single_project_no_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """单入口项目未指定 --entry 时不输出多入口提示日志。."""
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
    with caplog.at_level("INFO", logger="fspack.commands.run"):
        ep = _select_entry(info, None)
    assert ep.name == "app"
    assert "未指定 --entry" not in caplog.text


def test_run_run_multi_entry_select(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fspack r --entry gui 运行对应入口的 exe。."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "multi"\nversion = "0.1"\n\n[tool.fspack.entries]\ncli = "cli.py"\ngui = "gui.py"\n'
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

    monkeypatch.setattr("fspack.commands.run.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    run_run(tmp_path, entry="gui")
    assert captured["cmd"] == [str(gui_exe)]
