"""commands 子命令测试：build/clean/run 直测与 _build_cmd 分支。."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.commands.build import run as build_run
from fspack.commands.clean import run as clean_run
from fspack.commands.package import run as package_run
from fspack.commands.run import _build_cmd, _find_exe
from fspack.commands.run import run as run_run
from fspack.exceptions import FspackError
from fspack.mirror import get_mirror
from fspack.platform import Platform, detect_platform
from fspack.project import DEFAULT_LINUX_PY_VERSION, DEFAULT_PY_VERSION


def test_build_run_default_mirror_and_py_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build(project: Path, mirror: object, py_version: str, target: object = None) -> None:
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["target"] = target

    monkeypatch.setattr("fspack.commands.build.build", fake_build)
    build_run(tmp_path, mirror=None, py_version=None)
    assert captured["mirror"] == get_mirror("aliyun")
    expected_version = DEFAULT_LINUX_PY_VERSION if detect_platform() is Platform.LINUX else DEFAULT_PY_VERSION
    assert captured["py_version"] == expected_version
    assert captured["target"] is None


def test_build_run_explicit_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build(project: Path, mirror: object, py_version: str, target: object = None) -> None:
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["target"] = target

    monkeypatch.setattr("fspack.commands.build.build", fake_build)
    build_run(tmp_path, mirror="aliyun", py_version="3.10.0", target=Platform.LINUX)
    assert captured["mirror"] == get_mirror("aliyun")
    assert captured["py_version"] == "3.10.0"
    assert captured["target"] is Platform.LINUX


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

    def fake_run(cmd: list[str], check: bool) -> _Completed:
        captured["cmd"] = cmd
        captured["check"] = check
        return _Completed()

    monkeypatch.setattr("fspack.commands.run.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    run_run(tmp_path, rest_args=["--foo", "bar"])
    assert captured["cmd"] == [str(exe), "--foo", "bar"]
    assert captured["check"] is False


def test_run_run_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    exe = tmp_path / "dist" / "app.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    class _Completed:
        returncode = 2

    monkeypatch.setattr("fspack.commands.run.subprocess.run", lambda cmd, check: _Completed())
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Windows")
    with pytest.raises(FspackError, match="程序退出码非零"):
        run_run(tmp_path)


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

    def fake_run(cmd: list[str], check: bool) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.commands.run.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.commands.run.platform.system", lambda: "Linux")
    run_run(tmp_path)
    assert captured["cmd"] == [str(exe)]


def test_package_run_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_installer(project: Path, mirror: object, py_version: str, no_build: bool = False) -> Path:
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["no_build"] = no_build
        captured["branch"] = "windows"
        return tmp_path / "app-setup.exe"

    def fake_build_linux_installer(project: Path, mirror: object, py_version: str, no_build: bool = False) -> Path:
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["no_build"] = no_build
        captured["branch"] = "linux"
        return tmp_path / "app.deb"

    monkeypatch.setattr("fspack.commands.package.build_installer", fake_build_installer)
    monkeypatch.setattr("fspack.commands.package.build_linux_installer", fake_build_linux_installer)
    package_run(tmp_path)
    assert captured["mirror"] == get_mirror("aliyun")
    expected_version = DEFAULT_LINUX_PY_VERSION if detect_platform() is Platform.LINUX else DEFAULT_PY_VERSION
    assert captured["py_version"] == expected_version
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
