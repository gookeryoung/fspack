"""builder 流水线编排测试。."""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.builder import _PIP_PYTHON_NAMES, _find_pip_python, build, copy_source, download_wheels, unpack_wheels
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
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.wheel_cache.harvest_external_caches", lambda *a, **kw: 0)
    wh = tmp_path / "wh"
    cache = tmp_path / "cache"
    download_wheels(("numpy", "requests"), "3.11.9", "https://idx/simple", wh, wheel_cache_dir=cache)
    cmd = captured["cmd"]
    assert cmd[0] == "/py/python"
    assert "download" in cmd
    assert "win_amd64" in cmd
    assert "3.11" in cmd
    assert "cp311" in cmd
    assert "https://idx/simple" in cmd
    assert "numpy" in cmd and "requests" in cmd
    assert "--find-links" in cmd
    assert str(cache) in cmd


def test_download_wheels_multi_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多个 platform_tags 展开为多个 --platform 参数。."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.wheel_cache.harvest_external_caches", lambda *a, **kw: 0)
    wh = tmp_path / "wh"
    download_wheels(
        ("PySide6",),
        "3.11.10",
        "https://idx/simple",
        wh,
        platform_tags=("manylinux2014_x86_64", "manylinux_2_28_x86_64"),
        wheel_cache_dir=tmp_path / "cache",
    )
    cmd = captured["cmd"]
    platform_count = cmd.count("--platform")
    assert platform_count == 2, f"应有 2 个 --platform，实际 {platform_count}"
    assert "manylinux2014_x86_64" in cmd
    assert "manylinux_2_28_x86_64" in cmd


def test_download_wheels_pip_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_find_pip_python 抛 DependencyError 时 download_wheels 透传。."""
    monkeypatch.setattr(
        "fspack.builder._find_pip_python",
        lambda: (_ for _ in ()).throw(DependencyError("未找到可用的 pip")),
    )
    monkeypatch.setattr("fspack.wheel_cache.harvest_external_caches", lambda *a, **kw: 0)
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "wh", wheel_cache_dir=tmp_path / "cache")


def test_download_wheels_pip_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "pip", stderr="no wheel")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.wheel_cache.harvest_external_caches", lambda *a, **kw: 0)
    with pytest.raises(DependencyError, match="依赖下载失败"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "wh", wheel_cache_dir=tmp_path / "cache")


def test_download_wheels_python_disappeared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_find_pip_python 验证通过后 download 时 python 消失（FileNotFoundError）。."""
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr("fspack.wheel_cache.harvest_external_caches", lambda *a, **kw: 0)
    with pytest.raises(DependencyError, match="未找到 pip"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "wh", wheel_cache_dir=tmp_path / "cache")


def test_download_wheels_harvests_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 调用 harvest_external_caches 且下载后回写缓存。."""
    harvest_calls: list[dict[str, Any]] = []

    def fake_harvest(packages: set[str], py_version: str, platform_tags: Any, dest: Path) -> int:
        harvest_calls.append({"packages": packages, "dest": dest})
        return 1

    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        (tmp_path / "wh" / whl_name).write_bytes(b"numpy-content")
        return _Completed()

    monkeypatch.setattr("fspack.wheel_cache.harvest_external_caches", fake_harvest)
    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

    wh = tmp_path / "wh"
    cache = tmp_path / "cache"
    result = download_wheels(("numpy",), "3.11.9", "https://idx/simple", wh, wheel_cache_dir=cache)

    assert len(harvest_calls) == 1
    assert harvest_calls[0]["dest"] == cache
    assert "numpy" in harvest_calls[0]["packages"]
    assert len(result) == 1
    assert (cache / whl_name).read_bytes() == b"numpy-content"


def test_find_pip_python_uses_sys_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sys.executable 能跑 pip 时优先用它。."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    monkeypatch.setattr("fspack.builder.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.builder.os.environ", {"PATH": ""})

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        if cmd[0] == str(venv_py):
            return _Completed()
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    assert _find_pip_python() == str(venv_py)


def test_find_pip_python_falls_back_to_system(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sys.executable 无 pip 时遍历 PATH 找系统 python。."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    sys_py = sys_bin / _PIP_PYTHON_NAMES[0]
    sys_py.write_text("")
    monkeypatch.setattr("fspack.builder.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.builder.os.environ", {"PATH": str(sys_bin)})

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        if cmd[0] == str(venv_py):
            raise subprocess.CalledProcessError(1, cmd)
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    assert _find_pip_python() == str(sys_py.resolve())


def test_find_pip_python_skips_venv_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PATH 中 venv 所在目录的系统 python 被跳过。."""
    venv_bin = tmp_path / "venv"
    venv_bin.mkdir()
    venv_py = venv_bin / _PIP_PYTHON_NAMES[0]
    venv_py.write_text("")
    monkeypatch.setattr("fspack.builder.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.builder.os.environ", {"PATH": str(venv_bin)})
    monkeypatch.setattr(
        "fspack.builder.subprocess.run",
        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, cmd)),
    )
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        _find_pip_python()


def test_find_pip_python_all_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """所有候选都无 pip 时抛 DependencyError。."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    (sys_bin / _PIP_PYTHON_NAMES[0]).write_text("")
    monkeypatch.setattr("fspack.builder.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.builder.os.environ", {"PATH": str(sys_bin)})

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        _find_pip_python()


def test_find_pip_python_empty_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PATH 为空时只检测 sys.executable。."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    monkeypatch.setattr("fspack.builder.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.builder.os.environ", {"PATH": ""})
    monkeypatch.setattr(
        "fspack.builder.subprocess.run",
        lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        _find_pip_python()


def test_find_pip_python_skips_dir_without_python3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PATH 中无系统 python 的目录被跳过，继续找下一个。."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    empty_bin = tmp_path / "empty"
    empty_bin.mkdir()
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    sys_py = sys_bin / _PIP_PYTHON_NAMES[0]
    sys_py.write_text("")
    monkeypatch.setattr("fspack.builder.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.builder.os.environ", {"PATH": f"{empty_bin}{os.pathsep}{sys_bin}"})

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        if cmd[0] == str(venv_py):
            raise subprocess.CalledProcessError(1, cmd)
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    assert _find_pip_python() == str(sys_py.resolve())


def test_find_pip_python_skips_unresolvable_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Path.resolve 抛 OSError 的目录被跳过。."""
    venv_py = tmp_path / "venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    sys_bin = tmp_path / "sysbin"
    sys_bin.mkdir()
    sys_py = sys_bin / _PIP_PYTHON_NAMES[0]
    sys_py.write_text("")
    monkeypatch.setattr("fspack.builder.sys.executable", str(venv_py))
    monkeypatch.setattr("fspack.builder.os.environ", {"PATH": f"{bad_dir}{os.pathsep}{sys_bin}"})
    original_resolve = Path.resolve

    def fake_resolve(self: Path) -> Path:
        if self == bad_dir:
            raise OSError("mocked")
        return original_resolve(self)

    monkeypatch.setattr("fspack.builder.Path.resolve", fake_resolve)

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        if cmd[0] == str(venv_py):
            raise subprocess.CalledProcessError(1, cmd)
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    assert _find_pip_python() == str(sys_py.resolve())


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
        lambda packages, py_version, index, wheelhouse, platform_tags=("win_amd64",): (
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
