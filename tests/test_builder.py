"""builder 流水线编排测试。."""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.builder import (
    _PIP_PYTHON_NAMES,
    _find_pip_python,
    _parse_pip_download_wheels,
    _site_packages_has_deps,
    build,
    copy_source,
    download_wheels,
    unpack_wheels,
)
from fspack.console import console
from fspack.exceptions import DependencyError
from fspack.mirror import get_mirror
from fspack.platform import Platform
from fspack.progress import StageRecorder

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
    cache = tmp_path / "cache"
    download_wheels(("numpy", "requests"), "3.11.9", "https://idx/simple", cache)
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
    assert "-d" in cmd


def test_download_wheels_multi_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多个 platform_tags 展开为多个 --platform 参数。."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    download_wheels(
        ("PySide6",),
        "3.11.10",
        "https://idx/simple",
        tmp_path / "cache",
        platform_tags=("manylinux2014_x86_64", "manylinux_2_28_x86_64"),
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
    with pytest.raises(DependencyError, match="未找到可用的 pip"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "cache")


def test_download_wheels_pip_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "pip", stderr="no wheel")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    with pytest.raises(DependencyError, match="依赖下载失败"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "cache")


def test_download_wheels_python_disappeared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_find_pip_python 验证通过后 download 时 python 消失（FileNotFoundError）。."""
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(DependencyError, match="未找到 pip"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "cache")


def test_download_wheels_records_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 回写新增 wheel 字节数到 stage。."""
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    whl_content = b"x" * 100

    class _Result:
        returncode = 0
        stdout = f"Saved {whl_name}\n"
        stderr = ""

    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        (tmp_path / "cache" / whl_name).write_bytes(whl_content)
        return _Result()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

    stage = StageRecorder("下载依赖")
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache", stage=stage)
    record = stage._finalize()
    assert record.bytes_downloaded == 100
    assert record.items == 1


def test_download_wheels_cache_hit_no_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache_dir 已存在的 wheel 不计入新增字节数，但计入缓存命中。."""
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / whl_name).write_bytes(b"old" * 10)

    class _Result:
        returncode = 0
        stdout = f"File was already downloaded {whl_name}\n"
        stderr = ""

    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _Result())
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

    stage = StageRecorder("下载依赖")
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache, stage=stage)
    record = stage._finalize()
    assert record.bytes_downloaded == 0
    assert record.cache_hit == 1


def test_download_wheels_parses_stdout_for_wheels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 从 pip stdout 解析 wheel 列表（含传递依赖）。."""
    whl1 = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    whl2 = "requests-2.31.0-py3-none-any.whl"

    class _Result:
        returncode = 0
        stdout = f"Collecting numpy\n  Saved {whl1}\nCollecting requests\n  File was already downloaded {whl2}\n"
        stderr = ""

    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        (tmp_path / "cache" / whl1).write_bytes(b"numpy")
        (tmp_path / "cache" / whl2).write_bytes(b"requests")
        return _Result()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

    result = download_wheels(("numpy", "requests"), "3.11.9", "https://idx/simple", tmp_path / "cache")
    names = {p.name for p in result}
    assert whl1 in names
    assert whl2 in names


def test_download_wheels_fallback_to_dir_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout 无匹配行时回退到目录扫描。."""
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"

    class _Result:
        returncode = 0
        stdout = "no wheel info here\n"
        stderr = ""

    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        (tmp_path / "cache" / whl_name).write_bytes(b"numpy")
        return _Result()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

    result = download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache")
    assert len(result) == 1
    assert result[0].name == whl_name


def test_parse_pip_download_wheels_saved_and_cached() -> None:
    """解析 Saved 和 File was already downloaded 两种行。."""
    stdout = (
        "Collecting numpy\n  Saved /path/to/numpy-1.0-cp311-win_amd64.whl\n"
        "Collecting requests\n  File was already downloaded /other/requests-2.0-py3-none-any.whl\n"
    )
    names = _parse_pip_download_wheels(stdout)
    assert names == ["numpy-1.0-cp311-win_amd64.whl", "requests-2.0-py3-none-any.whl"]


def test_parse_pip_download_wheels_dedup() -> None:
    """重复 wheel 文件名去重。."""
    stdout = "Saved a-1.0.whl\nSaved a-1.0.whl\nSaved b-2.0.whl\n"
    names = _parse_pip_download_wheels(stdout)
    assert names == ["a-1.0.whl", "b-2.0.whl"]


def test_parse_pip_download_wheels_empty() -> None:
    """无匹配行返回空列表。."""
    assert _parse_pip_download_wheels("nothing here\n") == []
    assert _parse_pip_download_wheels("") == []


def test_site_packages_has_deps_true(tmp_path: Path) -> None:
    """site-packages 含 dist-info 目录时返回 True。."""
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "numpy-1.0.dist-info").mkdir()
    assert _site_packages_has_deps(sp) is True


def test_site_packages_has_deps_false_empty(tmp_path: Path) -> None:
    """site-packages 为空目录时返回 False。."""
    sp = tmp_path / "sp"
    sp.mkdir()
    assert _site_packages_has_deps(sp) is False


def test_site_packages_has_deps_false_no_dir(tmp_path: Path) -> None:
    """site-packages 不存在时返回 False。."""
    assert _site_packages_has_deps(tmp_path / "nonexistent") is False


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
    count = unpack_wheels([pkg_whl], sp)
    assert count == 1
    assert (sp / "numpy" / "__init__.py").is_file()


def test_unpack_wheels_bad_zip(tmp_path: Path) -> None:
    wh = tmp_path / "wh"
    wh.mkdir()
    bad_whl = wh / "bad.whl"
    bad_whl.write_bytes(b"nope")
    with pytest.raises(DependencyError, match="wheel 损坏"):
        unpack_wheels([bad_whl], tmp_path / "sp")


def test_unpack_wheels_with_submodule_usage(tmp_path: Path) -> None:
    """提供 submodule_usage 时按需解压，跳过未用子模块。."""
    wh = tmp_path / "wh"
    wh.mkdir()
    whl = wh / "PySide2-5.15.2.1-cp39-none-win_amd64.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("PySide2/__init__.py", "")
        zf.writestr("PySide2/QtCore.pyd", b"core")
        zf.writestr("PySide2/QtGui.pyd", b"gui")
        zf.writestr("PySide2/QtWidgets.pyd", b"widgets")
        zf.writestr("PySide2/Qt5Core.dll", b"c")
        zf.writestr("PySide2/Qt5Gui.dll", b"g")
        zf.writestr("PySide2/Qt5Widgets.dll", b"w")
        zf.writestr("PySide2-5.15.2.1.dist-info/METADATA", b"m")
    sp = tmp_path / "sp"
    count = unpack_wheels([whl], sp, {"PySide2": frozenset({"QtCore", "QtWidgets"})})
    assert count == 1
    assert (sp / "PySide2" / "QtCore.pyd").is_file()
    assert (sp / "PySide2" / "QtWidgets.pyd").is_file()
    assert (sp / "PySide2" / "Qt5Core.dll").is_file()
    assert (sp / "PySide2" / "Qt5Widgets.dll").is_file()
    assert not (sp / "PySide2" / "QtGui.pyd").exists()
    assert (sp / "PySide2" / "Qt5Gui.dll").is_file()


def test_build_forwards_keep_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build() 将 keep_modules 和 ast_submodules 透传给 unpack_wheels。."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\nfrom requests import get\ndef main():\n    pass\n")

    monkeypatch.setattr(
        "fspack.builder.ensure_embed",
        lambda v, m, c, r, **kw: (
            r.mkdir(parents=True, exist_ok=True),
            (r / "python311.dll").write_bytes(b""),
            (r / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    monkeypatch.setattr(
        "fspack.builder.download_wheels",
        lambda packages, py_version, index, cache_dir, platform_tags=("win_amd64",), **kw: [],
    )

    captured: dict[str, Any] = {}

    def fake_unpack(wheels: object, sp: object, submodule_usage: object, keep_modules: object, **kw: Any) -> int:
        captured["submodule_usage"] = submodule_usage
        captured["keep_modules"] = keep_modules
        return 0

    monkeypatch.setattr("fspack.builder.unpack_wheels", fake_unpack)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, keep_modules={"requests.adapters"})
    assert captured["keep_modules"] == {"requests.adapters"}
    assert isinstance(captured["submodule_usage"], dict)
    assert "requests" in captured["submodule_usage"]


def test_build_orchestration_helloworld(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "helloworld"
    shutil.copytree(_EXAMPLES / "helloworld", proj)

    calls: dict[str, Any] = {}

    def fake_ensure_embed(version: str, mirror: object, cache: Path, runtime_dir: Path, **kw: Any) -> Path:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        major, minor = version.split(".")[:2]
        (runtime_dir / f"python{major}{minor}.dll").write_bytes(b"")
        (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
        calls["ensure"] = version
        return runtime_dir

    monkeypatch.setattr("fspack.builder.ensure_embed", fake_ensure_embed)

    def fake_download(packages: object, py_version: str, index: str, cache_dir: Path, **kw: Any) -> list[Path]:
        calls["download"] = True
        return []

    monkeypatch.setattr("fspack.builder.download_wheels", fake_download)
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        calls["compile_source"] = source
        return out_exe

    monkeypatch.setattr("fspack.builder.compile_loader", fake_compile)

    with console.capture() as capture:
        info = build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert info.name == "helloworld"
    assert (proj / "dist" / "helloworld.exe").is_file()
    assert (proj / "dist" / "runtime" / "python311._pth").is_file()
    assert (proj / "dist" / "src" / "helloworld.py").is_file()
    assert (proj / "dist" / "runtime" / "python311.dll").is_file()
    assert (proj / "dist" / ".entry").is_file()
    assert (proj / "dist" / ".entry").read_text(encoding="utf-8") == "src/helloworld.py"
    pth = (proj / "dist" / "runtime" / "python311._pth").read_text()
    assert "python311.zip" in pth
    assert "..\\src" in pth
    assert ".entry" in calls["compile_source"]
    assert "read_entry" in calls["compile_source"]
    assert "download" not in calls
    out = capture.get()
    assert "构建阶段汇总" in out
    assert "解析项目" in out
    assert "准备运行时" in out
    assert "生成 C loader" in out
    assert "总计" in out


def test_build_orchestration_with_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\ndef main():\n    pass\n")

    monkeypatch.setattr(
        "fspack.builder.ensure_embed",
        lambda v, m, c, r, **kw: (
            r.mkdir(parents=True, exist_ok=True),
            (r / "python311.dll").write_bytes(b""),
            (r / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    downloaded: dict[str, bool] = {}
    monkeypatch.setattr(
        "fspack.builder.download_wheels",
        lambda packages, py_version, index, cache_dir, platform_tags=("win_amd64",), **kw: (
            downloaded.__setitem__("called", True) or []
        ),
    )
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert downloaded.get("called") is True


def test_build_skips_download_when_site_packages_has_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """site-packages 已有 dist-info 时跳过下载解压，记录跳过数。."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\ndef main():\n    pass\n")

    def fake_ensure_embed(version: str, mirror: object, cache: Path, runtime_dir: Path, **kw: Any) -> Path:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "python311.dll").write_bytes(b"")
        sp = runtime_dir / "Lib" / "site-packages"
        sp.mkdir(parents=True)
        (sp / "requests-2.31.0.dist-info").mkdir()
        return runtime_dir

    monkeypatch.setattr("fspack.builder.ensure_embed", fake_ensure_embed)

    download_called = False

    def fake_download(*a: Any, **kw: Any) -> list[Path]:
        nonlocal download_called
        download_called = True
        return []

    monkeypatch.setattr("fspack.builder.download_wheels", fake_download)
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    with console.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert not download_called
    out = capture.get()
    assert "已存在跳过" in out


def test_build_orchestration_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "helloworld"
    shutil.copytree(_EXAMPLES / "helloworld", proj)
    calls: dict[str, Any] = {}

    def fake_ensure_standalone(version: str, release: str, cache: Path, runtime_dir: Path, **kw: Any) -> Path:
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

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
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
    assert (proj / "dist" / ".entry").is_file()
    assert "standalone" in calls
    assert "dlopen" in calls["compile_source"]
    assert "libpython3.11.so" in calls["compile_source"]
    assert ".entry" in calls["compile_source"]
