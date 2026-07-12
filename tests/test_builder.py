"""builder 流水线编排测试。."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.builder import build, copy_source, download_wheels, unpack_wheels
from fspack.exceptions import DependencyError
from fspack.mirror import get_mirror
from fspack.platform import Platform

_EXAMPLES = Path(__file__).parent / "examples"


def test_copy_source_excludes_dist(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    pass\n")
    (src / "dist").mkdir()
    (src / "dist" / "junk.txt").write_text("x")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "c.pyc").write_text("x")
    dst = tmp_path / "out" / "src"
    copy_source(src, dst)
    assert (dst / "app.py").is_file()
    assert not (dst / "dist").exists()
    assert not (dst / "__pycache__").exists()


def test_copy_source_overwrites_existing(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("v2")
    dst = tmp_path / "out" / "src"
    dst.mkdir(parents=True)
    (dst / "old.py").write_text("old")
    copy_source(src, dst)
    assert (dst / "app.py").read_text() == "v2"
    assert not (dst / "old.py").exists()


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def test_download_wheels_cmd_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder.sys.executable", "/py/python")
    wh = tmp_path / "wh"
    download_wheels(("numpy", "requests"), "3.11.9", "https://idx/simple", wh)
    cmd = captured["cmd"]
    assert cmd[0] == "/py/python"
    assert "download" in cmd
    assert "win_amd64" in cmd
    assert "3.11" in cmd
    assert "cp311" in cmd
    assert "https://idx/simple" in cmd
    assert "numpy" in cmd and "requests" in cmd


def test_download_wheels_pip_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    with pytest.raises(DependencyError, match="未找到 pip"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "wh")


def test_download_wheels_pip_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "pip", stderr="no wheel")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    with pytest.raises(DependencyError, match="依赖下载失败"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "wh")


def test_unpack_wheels(tmp_path: Path) -> None:
    wh = tmp_path / "wh"
    wh.mkdir()
    pkg_whl = wh / "numpy-1.0-cp311-win_amd64.whl"
    with zipfile.ZipFile(pkg_whl, "w") as zf:
        zf.writestr("numpy/__init__.py", "")
        zf.writestr("numpy-1.0.dist-info/METADATA", "")
    sp = tmp_path / "sp"
    count = unpack_wheels(wh, sp)
    assert count == 1
    assert (sp / "numpy" / "__init__.py").is_file()


def test_unpack_wheels_bad_zip(tmp_path: Path) -> None:
    wh = tmp_path / "wh"
    wh.mkdir()
    (wh / "bad.whl").write_bytes(b"nope")
    with pytest.raises(DependencyError, match="wheel 损坏"):
        unpack_wheels(wh, tmp_path / "sp")


def test_build_orchestration_helloworld(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "helloworld"
    shutil.copytree(_EXAMPLES / "helloworld", proj)

    calls: dict[str, Any] = {}

    def fake_ensure_embed(version: str, mirror: object, cache: Path, runtime_dir: Path) -> Path:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        major, minor = version.split(".")[:2]
        (runtime_dir / f"python{major}{minor}.dll").write_bytes(b"")
        (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
        calls["ensure"] = version
        return runtime_dir

    monkeypatch.setattr("fspack.builder.ensure_embed", fake_ensure_embed)

    def fake_download(packages: object, py_version: str, index: str, wheelhouse: Path) -> list[Path]:
        calls["download"] = True
        return []

    monkeypatch.setattr("fspack.builder.download_wheels", fake_download)
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object) -> Path:
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        calls["compile_source"] = source
        return out_exe

    monkeypatch.setattr("fspack.builder.compile_loader", fake_compile)

    info = build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert info.name == "helloworld"
    assert (proj / "dist" / "helloworld.exe").is_file()
    assert (proj / "dist" / "runtime" / "python311._pth").is_file()
    assert (proj / "dist" / "src" / "helloworld.py").is_file()
    assert (proj / "dist" / "runtime" / "python311.dll").is_file()
    pth = (proj / "dist" / "runtime" / "python311._pth").read_text()
    assert "python311.zip" in pth
    assert "..\\src" in pth
    assert r"src\\helloworld.py" in calls["compile_source"]
    assert "download" not in calls


def test_build_orchestration_with_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\ndef main():\n    pass\n")

    monkeypatch.setattr(
        "fspack.builder.ensure_embed",
        lambda v, m, c, r: (
            r.mkdir(parents=True, exist_ok=True),
            (r / "python311.dll").write_bytes(b""),
            (r / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    downloaded: dict[str, bool] = {}
    monkeypatch.setattr(
        "fspack.builder.download_wheels",
        lambda packages, py_version, index, wheelhouse, platform_tag="win_amd64": (
            downloaded.__setitem__("called", True) or []
        ),
    )
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert downloaded.get("called") is True


def test_build_orchestration_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "helloworld"
    shutil.copytree(_EXAMPLES / "helloworld", proj)
    calls: dict[str, Any] = {}

    def fake_ensure_standalone(version: str, release: str, cache: Path, runtime_dir: Path) -> Path:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        major, minor = version.split(".")[:2]
        pydir = runtime_dir / "python"
        (pydir / "bin").mkdir(parents=True)
        (pydir / "bin" / f"python{major}.{minor}").write_text("")
        (pydir / "lib" / f"python{major}.{minor}" / "site-packages").mkdir(parents=True)
        calls["standalone"] = version
        return runtime_dir

    monkeypatch.setattr("fspack.builder.ensure_standalone", fake_ensure_standalone)
    monkeypatch.setattr("fspack.builder.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object) -> Path:
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        calls["compile_platform"] = platform
        calls["compile_source"] = source
        return out_exe

    monkeypatch.setattr("fspack.builder.compile_loader", fake_compile)

    info = build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX)
    assert info.name == "helloworld"
    assert (proj / "dist" / "helloworld").is_file()
    assert not (proj / "dist" / "helloworld.exe").exists()
    assert not (proj / "dist" / "runtime" / "python311._pth").exists()
    assert (proj / "dist" / "src" / "helloworld.py").is_file()
    assert "standalone" in calls
    assert "dlopen" in calls["compile_source"]
    assert "libpython3.11.so" in calls["compile_source"]
