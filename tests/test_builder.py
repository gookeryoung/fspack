"""builder 流水线编排测试."""

from __future__ import annotations

import os
import shutil
import subprocess
import types
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.builder import (
    _PIP_PYTHON_NAMES,
    _build_sdist_wheels,
    _deps_cache_key,
    _download_online,
    _eval_python_version_marker,
    _eval_single_marker,
    _filter_by_python_version,
    _find_pip_python,
    _find_uv,
    _load_deps_cache,
    _parse_missing_packages,
    _parse_pip_download_wheels,
    _resolve_with_uv,
    _run_pip,
    _save_deps_cache,
    _site_packages_has_deps,
    _stream_subprocess,
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

_EXAMPLES = Path(__file__).parent.parent / "examples"


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


def test_copy_source_strips_dev_artifacts(tmp_path: Path) -> None:
    """剥离开发期元数据/工具配置/凭证/文档/测试目录."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')\n")
    # Python 项目元数据
    (src / ".python-version").write_text("3.11\n")
    (src / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (src / "uv.lock").write_text("version = 1\n")
    (src / "uv.toml").write_text("preview = true\n")
    (src / "setup.py").write_text("from setuptools import setup\n")
    (src / "setup.cfg").write_text("[metadata]\n")
    (src / "MANIFEST.in").write_text("include LICENSE\n")
    (src / "requirements.txt").write_text("rich\n")
    (src / "requirements-dev.txt").write_text("pytest\n")
    # 工具链配置
    for cfg in ("ruff.toml", "pyrefly.toml", "pytest.ini", "tox.ini", "uv.toml"):
        if not (src / cfg).exists():
            (src / cfg).write_text("# cfg\n")
    (src / ".ruff.toml").write_text("# ruff\n")
    (src / ".bumpversion.toml").write_text("[bumpversion]\n")
    (src / ".pre-commit-config.yaml").write_text("repos: []\n")
    (src / ".coveragerc").write_text("[run]\n")
    (src / ".readthedocs.yaml").write_text("version: 2\n")
    (src / "Makefile").write_text("all:\n\techo hi\n")
    (src / ".copier-answers.yml").write_text("_commit: x\n")
    # 凭证
    (src / ".env").write_text("SECRET=x\n")
    (src / ".env.local").write_text("SECRET=y\n")
    # 版本控制与 IDE
    (src / ".gitignore").write_text("dist/\n")
    (src / ".gitattributes").write_text("* text=auto\n")
    (src / ".vscode").mkdir()
    (src / ".vscode" / "settings.json").write_text("{}")
    (src / ".idea").mkdir()
    (src / ".github").mkdir()
    (src / ".github" / "ci.yml").write_text("on: push\n")
    # 文档
    (src / "README.md").write_text("# app\n")
    (src / "CHANGELOG.rst").write_text("v0.1\n")
    (src / "docs").mkdir()
    (src / "docs" / "index.md").write_text("# docs\n")
    # 测试目录
    (src / "tests").mkdir()
    (src / "tests" / "test_app.py").write_text("def test(): pass\n")
    # 覆盖率与缓存
    (src / ".coverage").write_text("x")
    (src / "htmlcov").mkdir()
    (src / "htmlcov" / "index.html").write_text("<html/>")
    (src / ".ruff_cache").mkdir()
    (src / ".pyrefly_cache").mkdir()

    dst = tmp_path / "out" / "src"
    copy_source(src, dst)

    # 应用源码保留
    assert (dst / "app.py").is_file()
    # 元数据与配置全部剥离
    for name in (
        ".python-version",
        "pyproject.toml",
        "uv.lock",
        "uv.toml",
        "setup.py",
        "setup.cfg",
        "MANIFEST.in",
        "requirements.txt",
        "requirements-dev.txt",
        "ruff.toml",
        ".ruff.toml",
        "pyrefly.toml",
        "pytest.ini",
        "tox.ini",
        ".bumpversion.toml",
        ".pre-commit-config.yaml",
        ".coveragerc",
        ".readthedocs.yaml",
        "Makefile",
        ".copier-answers.yml",
        ".env",
        ".env.local",
        ".gitignore",
        ".gitattributes",
        "README.md",
        "CHANGELOG.rst",
        ".coverage",
    ):
        assert not (dst / name).exists(), f"应被剥离: {name}"
    # 目录全部剥离
    for d in (".vscode", ".idea", ".github", "docs", "tests", "htmlcov", ".ruff_cache", ".pyrefly_cache"):
        assert not (dst / d).exists(), f"应被剥离目录: {d}"


def test_copy_source_keeps_runtime_resources(tmp_path: Path) -> None:
    """保留运行时所需资源：源码、数据文件、LICENSE、子包."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')\n")
    (src / "LICENSE").write_text("MIT License\n")
    (src / "data.json").write_text("{}\n")
    (src / "assets").mkdir()
    (src / "assets" / "logo.png").write_bytes(b"\x89PNG")
    (src / "pkg").mkdir()
    (src / "pkg" / "__init__.py").write_text("")
    (src / "pkg" / "mod.py").write_text("x = 1\n")
    # 子包内的开发文件也应剥离
    (src / "pkg" / "README.md").write_text("# pkg\n")
    (src / "pkg" / "tests").mkdir()
    (src / "pkg" / "tests" / "test_mod.py").write_text("pass\n")

    dst = tmp_path / "out" / "src"
    copy_source(src, dst)

    assert (dst / "app.py").is_file()
    assert (dst / "LICENSE").is_file(), "LICENSE 应保留以符合开源协议分发要求"
    assert (dst / "data.json").is_file()
    assert (dst / "assets" / "logo.png").is_file()
    assert (dst / "pkg" / "__init__.py").is_file()
    assert (dst / "pkg" / "mod.py").is_file()
    # 子包内的开发文件同样剥离
    assert not (dst / "pkg" / "README.md").exists()
    assert not (dst / "pkg" / "tests").exists()


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def test_download_wheels_cmd_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-index 成功路径：命令含 --no-index，不含 -i index."""
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
    assert "--no-index" in cmd
    assert "https://idx/simple" not in cmd
    assert "numpy" in cmd and "requests" in cmd
    assert "--find-links" in cmd
    assert str(cache) in cmd
    assert "-d" in cmd


def test_download_wheels_fallback_cmd_has_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-index 失败且 uv 不可用时回退到带 -i index 的命令."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        calls.append(cmd)
        # --no-index 路径失败，触发回退
        raise subprocess.CalledProcessError(1, "pip", stderr="not in cache")

    def fake_stream(cmd: list[str]) -> _Completed:
        calls.append(cmd)
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.builder._find_uv", lambda: None)
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache")
    assert len(calls) == 2
    assert "--no-index" in calls[0]
    assert "https://idx/simple" in calls[1]
    assert "--no-index" not in calls[1]


def test_download_wheels_no_index_skips_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-index 成功时只调用 pip 一次，不查询网络 index."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        calls.append(cmd)
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache")
    assert len(calls) == 1
    assert "--no-index" in calls[0]
    assert "https://idx/simple" not in calls[0]


def test_download_wheels_multi_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多个 platform_tags 展开为多个 --platform 参数."""
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
    """_find_pip_python 抛 DependencyError 时 download_wheels 透传."""
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

    def fake_stream(cmd: list[str]) -> _Completed:
        raise err

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.builder._find_uv", lambda: None)
    with pytest.raises(DependencyError, match="依赖下载失败"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "cache")


def test_download_wheels_python_disappeared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_find_pip_python 验证通过后 download 时 python 消失（FileNotFoundError）."""
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(DependencyError, match="未找到 pip"):
        download_wheels(("numpy",), "3.11.9", "https://idx", tmp_path / "cache")


def test_download_wheels_records_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 回写新增 wheel 字节数到 stage."""
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
    """cache_dir 已存在的 wheel 不计入新增字节数，但计入缓存命中."""
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
    """download_wheels 从 pip stdout 解析 wheel 列表（含传递依赖）."""
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
    """stdout 无匹配行时回退到目录扫描."""
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


def test_deps_cache_key_stable_and_distinct() -> None:
    """相同输入产生相同键；不同输入产生不同键."""
    k1 = _deps_cache_key(("numpy", "requests"), "3.11.9", ("win_amd64",))
    k2 = _deps_cache_key(("numpy", "requests"), "3.11.9", ("win_amd64",))
    k3 = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    k4 = _deps_cache_key(("numpy", "requests"), "3.10.11", ("win_amd64",))
    k5 = _deps_cache_key(("numpy", "requests"), "3.11.9", ("manylinux2014_x86_64",))
    assert k1 == k2
    assert k1 != k3
    assert k1 != k4
    assert k1 != k5


def test_deps_cache_key_order_independent() -> None:
    """包顺序不影响键（sorted 后哈希）."""
    k1 = _deps_cache_key(("numpy", "requests"), "3.11.9", ("win_amd64",))
    k2 = _deps_cache_key(("requests", "numpy"), "3.11.9", ("win_amd64",))
    assert k1 == k2


def test_load_deps_cache_miss_when_no_file(tmp_path: Path) -> None:
    """缓存文件不存在时返回 None."""
    assert _load_deps_cache(tmp_path / "cache", "abc123") is None


def test_load_deps_cache_hit_when_wheels_exist(tmp_path: Path) -> None:
    """缓存文件存在且 wheel 文件齐全时返回路径列表."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "numpy-1.0.whl").write_bytes(b"x")
    (cache / "requests-2.0.whl").write_bytes(b"y")
    _save_deps_cache(cache, "abc123", [cache / "numpy-1.0.whl", cache / "requests-2.0.whl"])
    loaded = _load_deps_cache(cache, "abc123")
    assert loaded is not None
    names = {p.name for p in loaded}
    assert names == {"numpy-1.0.whl", "requests-2.0.whl"}


def test_load_deps_cache_miss_when_wheel_deleted(tmp_path: Path) -> None:
    """缓存文件存在但 wheel 文件被删时返回 None（需重新解析）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _save_deps_cache(cache, "abc123", [cache / "numpy-1.0.whl"])
    # 不创建 wheel 文件
    assert _load_deps_cache(cache, "abc123") is None


def test_load_deps_cache_handles_corrupt_json(tmp_path: Path) -> None:
    """缓存文件 JSON 损坏时返回 None 不抛异常."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-corrupt.json").write_text("{bad json", encoding="utf-8")
    assert _load_deps_cache(cache, "corrupt") is None


def test_save_deps_cache_best_effort(tmp_path: Path) -> None:
    """写入失败仅 warning 不抛异常（best-effort）."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _save_deps_cache(cache, "abc123", [cache / "numpy-1.0.whl"])
    cache_file = cache / ".deps-abc123.json"
    assert cache_file.is_file()
    import json as _json

    data = _json.loads(cache_file.read_text(encoding="utf-8"))
    assert data == {"wheels": ["numpy-1.0.whl"]}


def test_download_wheels_deps_cache_hit_skips_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖解析缓存命中时完全跳过 pip 调用."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    (cache / whl_name).write_bytes(b"numpy")

    # 预写依赖解析缓存
    key = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    _save_deps_cache(cache, key, [cache / whl_name])

    pip_called = False

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        nonlocal pip_called
        pip_called = True
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

    stage = StageRecorder("下载依赖")
    result = download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache, stage=stage)
    record = stage._finalize()
    assert not pip_called
    assert len(result) == 1
    assert result[0].name == whl_name
    assert record.cache_hit == 1
    assert record.bytes_downloaded == 0


def test_download_wheels_writes_deps_cache_after_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip 解析成功后写入依赖解析缓存，下次调用命中."""
    cache = tmp_path / "cache"
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"

    class _Result:
        returncode = 0
        stdout = f"Saved {whl_name}\n"
        stderr = ""

    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        (cache / whl_name).write_bytes(b"numpy")
        return _Result()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

    # 第一次调用：cache miss，走 pip，写缓存
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache)
    key = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    cache_file = cache / f".deps-{key}.json"
    assert cache_file.is_file()
    import json as _json

    data = _json.loads(cache_file.read_text(encoding="utf-8"))
    assert whl_name in data["wheels"]


def test_download_wheels_deps_cache_hit_no_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖解析缓存命中且 stage=None 时不报错."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    (cache / whl_name).write_bytes(b"numpy")
    key = _deps_cache_key(("numpy",), "3.11.9", ("win_amd64",))
    _save_deps_cache(cache, key, [cache / whl_name])

    pip_called = False

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        nonlocal pip_called
        pip_called = True
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")

    result = download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache)
    assert not pip_called
    assert len(result) == 1


def test_save_deps_cache_oserror_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """write_text 抛 OSError 时仅 warning 不抛异常."""
    cache = tmp_path / "cache"
    cache.mkdir()

    def fake_write_text(self: Path, *a: object, **kw: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", fake_write_text)
    _save_deps_cache(cache, "abc123", [cache / "numpy-1.0.whl"])


def test_build_skips_runtime_when_already_prepared_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime 已就绪（dll 存在）时跳过下载和解压，两 stage 均 hit_cache."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    # 预创建 runtime 目录与 dll 标记
    runtime_dir = proj / "dist" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "python311.dll").write_bytes(b"")
    (runtime_dir / "Lib" / "site-packages").mkdir(parents=True)

    download_called = False
    extract_called = False

    def fake_download_embed(*a: Any, **kw: Any) -> Path:
        nonlocal download_called
        download_called = True
        return tmp_path / "fake.zip"

    def fake_extract_embed(*a: Any, **kw: Any) -> None:
        nonlocal extract_called
        extract_called = True

    monkeypatch.setattr("fspack.builder.download_embed", fake_download_embed)
    monkeypatch.setattr("fspack.builder.extract_embed", fake_extract_embed)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert not download_called
    assert not extract_called


def test_build_skips_runtime_when_already_prepared_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime 已就绪（python bin 存在）时跳过下载和解压."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    # 预创建 runtime 目录与 python bin 标记
    runtime_dir = proj / "dist" / "runtime"
    pybin = runtime_dir / "python" / "bin"
    pybin.mkdir(parents=True)
    (pybin / "python3.11").write_text("")
    (runtime_dir / "python" / "lib" / "python3.11" / "site-packages").mkdir(parents=True)

    download_called = False
    extract_called = False

    def fake_download_standalone(*a: Any, **kw: Any) -> Path:
        nonlocal download_called
        download_called = True
        return tmp_path / "fake.tar.gz"

    def fake_extract_standalone(*a: Any, **kw: Any) -> None:
        nonlocal extract_called
        extract_called = True

    monkeypatch.setattr("fspack.builder.download_standalone", fake_download_standalone)
    monkeypatch.setattr("fspack.builder.extract_standalone", fake_extract_standalone)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX)
    assert not download_called
    assert not extract_called


def test_parse_pip_download_wheels_saved_and_cached() -> None:
    """解析 Saved 和 File was already downloaded 两种行."""
    stdout = (
        "Collecting numpy\n  Saved /path/to/numpy-1.0-cp311-win_amd64.whl\n"
        "Collecting requests\n  File was already downloaded /other/requests-2.0-py3-none-any.whl\n"
    )
    names = _parse_pip_download_wheels(stdout)
    assert names == ["numpy-1.0-cp311-win_amd64.whl", "requests-2.0-py3-none-any.whl"]


def test_parse_pip_download_wheels_dedup() -> None:
    """重复 wheel 文件名去重."""
    stdout = "Saved a-1.0.whl\nSaved a-1.0.whl\nSaved b-2.0.whl\n"
    names = _parse_pip_download_wheels(stdout)
    assert names == ["a-1.0.whl", "b-2.0.whl"]


def test_parse_pip_download_wheels_empty() -> None:
    """无匹配行返回空列表."""
    assert _parse_pip_download_wheels("nothing here\n") == []
    assert _parse_pip_download_wheels("") == []


def test_site_packages_has_deps_true(tmp_path: Path) -> None:
    """site-packages 含 dist-info 目录时返回 True."""
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "numpy-1.0.dist-info").mkdir()
    assert _site_packages_has_deps(sp) is True


def test_site_packages_has_deps_false_empty(tmp_path: Path) -> None:
    """site-packages 为空目录时返回 False."""
    sp = tmp_path / "sp"
    sp.mkdir()
    assert _site_packages_has_deps(sp) is False


def test_site_packages_has_deps_false_no_dir(tmp_path: Path) -> None:
    """site-packages 不存在时返回 False."""
    assert _site_packages_has_deps(tmp_path / "nonexistent") is False


def test_find_pip_python_uses_sys_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sys.executable 能跑 pip 时优先用它."""
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
    """sys.executable 无 pip 时遍历 PATH 找系统 python."""
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
    """PATH 中 venv 所在目录的系统 python 被跳过."""
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
    """所有候选都无 pip 时抛 DependencyError."""
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
    """PATH 为空时只检测 sys.executable."""
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
    """PATH 中无系统 python 的目录被跳过，继续找下一个."""
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
    """Path.resolve 抛 OSError 的目录被跳过."""
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
    """提供 submodule_usage 时按需解压，Qt 闭包自动加入 C 层依赖子模块."""
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
    # 用户 import QtCore/QtWidgets，闭包自动加入 Gui（C 层依赖）
    count = unpack_wheels([whl], sp, {"PySide2": frozenset({"QtCore", "QtWidgets"})})
    assert count == 1
    # 闭包内 Core/Widgets/Gui → 对应 .pyd 与 Qt5*.dll 保留
    assert (sp / "PySide2" / "QtCore.pyd").is_file()
    assert (sp / "PySide2" / "QtWidgets.pyd").is_file()
    assert (sp / "PySide2" / "QtGui.pyd").is_file()  # 闭包自动加入
    assert (sp / "PySide2" / "Qt5Core.dll").is_file()
    assert (sp / "PySide2" / "Qt5Widgets.dll").is_file()
    assert (sp / "PySide2" / "Qt5Gui.dll").is_file()  # 闭包自动加入


def test_build_forwards_keep_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build() 将 keep_modules 和 ast_submodules 透传给 unpack_wheels."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\nfrom requests import get\ndef main():\n    pass\n")

    monkeypatch.setattr(
        "fspack.builder.download_embed",
        lambda v, m, c, **kw: tmp_path / "fake.zip",
    )
    monkeypatch.setattr(
        "fspack.builder.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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
    proj = tmp_path / "cli_helloworld"
    shutil.copytree(_EXAMPLES / "cli_helloworld", proj, ignore=shutil.ignore_patterns("dist", "__pycache__"))
    calls: dict[str, Any] = {}

    def fake_extract_embed(zip_path: object, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "python311.dll").write_bytes(b"")
        (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
        calls["extract"] = True

    monkeypatch.setattr("fspack.builder.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr("fspack.builder.extract_embed", fake_extract_embed)

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
    assert info.name == "cli_helloworld"
    assert (proj / "dist" / "cli_helloworld.exe").is_file()
    assert (proj / "dist" / "runtime" / "python311._pth").is_file()
    assert (proj / "dist" / "src" / "helloworld.py").is_file()
    assert (proj / "dist" / "runtime" / "python311.dll").is_file()
    assert (proj / "dist" / ".entry").is_file()
    assert (proj / "dist" / ".entry").read_text(encoding="utf-8") == "_entry_cli_helloworld.py"
    wrapper = proj / "dist" / "_entry_cli_helloworld.py"
    assert wrapper.is_file()
    assert "fspack 生成的入口包装器" in wrapper.read_text(encoding="utf-8")
    pth = (proj / "dist" / "runtime" / "python311._pth").read_text()
    assert "python311.zip" in pth
    assert "..\\src" in pth
    assert ".entry" in calls["compile_source"]
    assert "read_entry" in calls["compile_source"]
    assert "download" not in calls
    out = capture.get()
    assert "构建阶段汇总" in out
    assert "解析项目" in out
    assert "下载运行时" in out
    assert "解压运行时" in out
    assert "生成 C loader" in out
    assert "总计" in out


def test_build_orchestration_with_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\ndef main():\n    pass\n")

    monkeypatch.setattr(
        "fspack.builder.download_embed",
        lambda v, m, c, **kw: tmp_path / "fake.zip",
    )
    monkeypatch.setattr(
        "fspack.builder.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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


def test_build_prefers_declared_over_ast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """declared 非空时用 declared 的 PyPI 包名下载，不用 AST 扫描的导入名。

    覆盖导入名 ≠ PyPI 包名场景：代码 ``import orderedset``（导入名），
    pyproject 声明 ``ordered-set``（PyPI 包名），应下载 ``ordered-set`` 而非 ``orderedset``。
    """
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\ndependencies = ["ordered-set", "lxml"]\n'
    )
    (proj / "app.py").write_text("import orderedset\nimport lxml\ndef main():\n    pass\n")

    monkeypatch.setattr("fspack.builder.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr(
        "fspack.builder.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    captured: dict[str, Any] = {}

    def fake_download(
        packages: tuple[str, ...] | list[str], py_version: str, index: str, cache_dir: Path, **kw: Any
    ) -> list[Path]:
        captured["packages"] = tuple(packages)
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

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    # 下载用的是 declared 的 PyPI 包名（ordered-set），而非 AST 扫描的导入名（orderedset）
    assert captured["packages"] == ("ordered-set", "lxml")


def test_build_skips_download_when_site_packages_has_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """site-packages 已有 dist-info 时跳过下载解压，记录跳过数."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\ndef main():\n    pass\n")

    def fake_extract_embed(zip_path: object, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "python311.dll").write_bytes(b"")
        sp = runtime_dir / "Lib" / "site-packages"
        sp.mkdir(parents=True)
        (sp / "requests-2.31.0.dist-info").mkdir()

    monkeypatch.setattr("fspack.builder.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr("fspack.builder.extract_embed", fake_extract_embed)

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
    proj = tmp_path / "cli_helloworld"
    shutil.copytree(_EXAMPLES / "cli_helloworld", proj, ignore=shutil.ignore_patterns("dist", "__pycache__"))
    calls: dict[str, Any] = {}

    def fake_extract_standalone(tar_path: object, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        major, minor = "3", "11"
        pydir = runtime_dir / "python"
        (pydir / "bin").mkdir(parents=True)
        (pydir / "bin" / f"python{major}.{minor}").write_text("")
        (pydir / "lib" / f"python{major}.{minor}" / "site-packages").mkdir(parents=True)
        calls["standalone"] = "3.11.9"

    monkeypatch.setattr("fspack.builder.download_standalone", lambda v, r, c, **kw: tmp_path / "fake.tar.gz")
    monkeypatch.setattr("fspack.builder.extract_standalone", fake_extract_standalone)
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
    assert info.name == "cli_helloworld"
    assert (proj / "dist" / "cli_helloworld").is_file()
    assert not (proj / "dist" / "cli_helloworld.exe").exists()
    assert not (proj / "dist" / "runtime" / "python311._pth").exists()
    assert (proj / "dist" / "src" / "helloworld.py").is_file()
    assert (proj / "dist" / ".entry").is_file()
    assert (proj / "dist" / "_entry_cli_helloworld.py").is_file()
    assert "standalone" in calls
    assert "dlopen" in calls["compile_source"]
    assert "libpython3.11.so" in calls["compile_source"]
    assert ".entry" in calls["compile_source"]


# ---------- _filter_by_python_version ----------


def test_filter_by_python_version_no_marker_kept() -> None:
    """无环境标记的依赖原样保留."""
    result = _filter_by_python_version(["numpy>=1.20", "requests"], "3.8.10")
    assert result == ["numpy>=1.20", "requests"]


def test_filter_by_python_version_skip_higher() -> None:
    """目标 3.8 时跳过 python_version >= '3.11' 的依赖."""
    pkgs = [
        "PySide2>=5.15.2.1; python_version <= '3.10'",
        "PySide6>=6.5.0; python_version >= '3.11'",
        "PyYAML>=6.0",
    ]
    result = _filter_by_python_version(pkgs, "3.8.10")
    assert result == ["PySide2>=5.15.2.1", "PyYAML>=6.0"]


def test_filter_by_python_version_keep_when_matches() -> None:
    """目标 3.11 时保留 python_version >= '3.11' 的依赖（去标记）."""
    pkgs = [
        "PySide2>=5.15.2.1; python_version <= '3.10'",
        "PySide6>=6.5.0; python_version >= '3.11'",
    ]
    result = _filter_by_python_version(pkgs, "3.11.9")
    assert result == ["PySide6>=6.5.0"]


def test_filter_by_python_version_keep_lower_bound_match() -> None:
    """边界值匹配：python_version <= '3.10' 在目标 3.10 时保留."""
    result = _filter_by_python_version(["PySide2>=5.15.2.1; python_version <= '3.10'"], "3.10.11")
    assert result == ["PySide2>=5.15.2.1"]


def test_filter_by_python_version_keep_non_python_marker() -> None:
    """非 python_version 标记保守保留（去标记）."""
    result = _filter_by_python_version(["foo>=1.0; platform_system == 'Windows'"], "3.8.10")
    assert result == ["foo>=1.0"]


def test_filter_by_python_version_and_combination() -> None:
    """and 组合：两个条件都满足才保留."""
    pkgs = ["bar>=1.0; python_version >= '3.8' and python_version < '3.12'"]
    assert _filter_by_python_version(pkgs, "3.10.11") == ["bar>=1.0"]
    assert _filter_by_python_version(pkgs, "3.12.0") == []


def test_filter_by_python_version_or_combination() -> None:
    """or 组合：任一条件满足即保留."""
    pkgs = ["baz>=1.0; python_version < '3.9' or python_version >= '3.12'"]
    assert _filter_by_python_version(pkgs, "3.8.10") == ["baz>=1.0"]
    assert _filter_by_python_version(pkgs, "3.11.9") == []
    assert _filter_by_python_version(pkgs, "3.12.0") == ["baz>=1.0"]


def test_filter_by_python_version_empty_input() -> None:
    """空列表输入返回空列表."""
    assert _filter_by_python_version([], "3.8.10") == []


def test_filter_by_python_version_all_filtered() -> None:
    """所有依赖都被标记过滤时返回空列表."""
    pkgs = ["PySide6>=6.5.0; python_version >= '3.11'"]
    assert _filter_by_python_version(pkgs, "3.8.10") == []


# ---------- _eval_single_marker / _eval_python_version_marker ----------


def test_eval_single_marker_ge() -> None:
    py = (3, 8)
    assert _eval_single_marker("python_version >= '3.8'", py) is True
    assert _eval_single_marker("python_version >= '3.9'", py) is False


def test_eval_single_marker_le() -> None:
    py = (3, 10)
    assert _eval_single_marker("python_version <= '3.10'", py) is True
    assert _eval_single_marker("python_version <= '3.9'", py) is False


def test_eval_single_marker_lt_gt() -> None:
    py = (3, 9)
    assert _eval_single_marker("python_version < '3.10'", py) is True
    assert _eval_single_marker("python_version > '3.8'", py) is True
    assert _eval_single_marker("python_version < '3.9'", py) is False
    assert _eval_single_marker("python_version > '3.9'", py) is False


def test_eval_single_marker_eq_ne() -> None:
    py = (3, 11)
    assert _eval_single_marker("python_version == '3.11'", py) is True
    assert _eval_single_marker("python_version != '3.10'", py) is True
    assert _eval_single_marker("python_version == '3.10'", py) is False


def test_eval_single_marker_non_python_returns_true() -> None:
    """非 python_version 标记保守返回 True."""
    assert _eval_single_marker("platform_system == 'Windows'", (3, 8)) is True


def test_eval_single_marker_double_quotes() -> None:
    """双引号标记值也能匹配."""
    assert _eval_single_marker('python_version >= "3.8"', (3, 9)) is True


def test_eval_python_version_marker_and() -> None:
    py = (3, 10)
    assert _eval_python_version_marker("python_version >= '3.8' and python_version <= '3.10'", py) is True
    assert _eval_python_version_marker("python_version >= '3.8' and python_version <= '3.9'", py) is False


def test_eval_python_version_marker_or() -> None:
    py = (3, 8)
    assert _eval_python_version_marker("python_version < '3.9' or python_version >= '3.12'", py) is True
    assert _eval_python_version_marker("python_version >= '3.9' or python_version >= '3.12'", py) is False


def test_eval_python_version_marker_case_insensitive() -> None:
    """and/or 大小写不敏感."""
    py = (3, 10)
    assert _eval_python_version_marker("python_version >= '3.8' AND python_version <= '3.10'", py) is True
    assert _eval_python_version_marker("python_version < '3.8' OR python_version >= '3.12'", py) is False


def test_eval_python_version_marker_non_python_returns_true() -> None:
    """纯非 python_version 标记保守返回 True."""
    assert _eval_python_version_marker("platform_system == 'Windows'", (3, 8)) is True


# ---------- _parse_missing_packages ----------


def test_parse_missing_packages_single() -> None:
    stderr = "ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
    assert _parse_missing_packages(stderr) == ["odfpy>=1.4.1"]


def test_parse_missing_packages_multiple() -> None:
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement PySide6>=6.5.0 (from versions: none)\n"
        "ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
    )
    assert _parse_missing_packages(stderr) == ["PySide6>=6.5.0", "odfpy>=1.4.1"]


def test_parse_missing_packages_dedup() -> None:
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
        "ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
    )
    assert _parse_missing_packages(stderr) == ["odfpy>=1.4.1"]


def test_parse_missing_packages_empty() -> None:
    """无匹配行返回空列表."""
    assert _parse_missing_packages("") == []
    assert _parse_missing_packages("no error here\n") == []


def test_parse_missing_packages_preserves_spec() -> None:
    """保留版本 specifier 供 pip wheel 使用."""
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement reportlab>=3.6.13,<4.0 (from versions: none)\n"
    )
    assert _parse_missing_packages(stderr) == ["reportlab>=3.6.13,<4.0"]


# ---------- _build_sdist_wheels ----------


def test_build_sdist_wheels_runs_pip_wheel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """对每个缺失包调用一次 pip wheel --no-deps."""
    captured: list[list[str]] = []

    def fake_stream(cmd: list[str]) -> _Completed:
        captured.append(cmd)
        return _Completed()

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    cache = tmp_path / "cache"
    cache.mkdir()
    _build_sdist_wheels(["odfpy>=1.4.1"], "/py/python", "https://idx/simple", cache)
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "/py/python"
    assert "wheel" in cmd
    assert "--no-deps" in cmd
    assert "-w" in cmd
    assert str(cache) in cmd
    assert "-i" in cmd
    assert "https://idx/simple" in cmd
    assert "odfpy>=1.4.1" in cmd


def test_build_sdist_wheels_multiple_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多个缺失包各调用一次 pip wheel."""
    calls: list[str] = []

    def fake_stream(cmd: list[str]) -> _Completed:
        calls.append(cmd[-1])
        return _Completed()

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    _build_sdist_wheels(["odfpy>=1.4.1", "foo>=1.0"], "/py/python", "https://idx", tmp_path / "cache")
    assert calls == ["odfpy>=1.4.1", "foo>=1.0"]


def test_build_sdist_wheels_failure_only_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip wheel 构建失败仅 warning 不抛异常（让后续重试失败时抛原始错误）."""

    def fake_stream(cmd: list[str]) -> _Completed:
        raise subprocess.CalledProcessError(1, cmd, stderr="build failed")

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    # 不抛异常即通过
    _build_sdist_wheels(["odfpy>=1.4.1"], "/py/python", "https://idx", tmp_path / "cache")


def test_build_sdist_wheels_pip_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FileNotFoundError 包装为 DependencyError（pip 解释器不存在）."""
    monkeypatch.setattr("fspack.builder._stream_subprocess", lambda cmd: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(DependencyError, match="未找到 pip"):
        _build_sdist_wheels(["odfpy>=1.4.1"], "/missing/python", "https://idx", tmp_path / "cache")


# ---------- download_wheels 标记过滤 / sdist 回退分支 ----------


def test_download_wheels_filters_python_version_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """带 python_version >= '3.11' 的依赖在目标 3.8 时被剔除，不传给 pip."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    pkgs = (
        "PySide2>=5.15.2.1; python_version <= '3.10'",
        "PySide6>=6.5.0; python_version >= '3.11'",
        "PyYAML>=6.0",
    )
    download_wheels(pkgs, "3.8.10", "https://idx/simple", tmp_path / "cache")
    cmd = captured["cmd"]
    # PySide6 不应出现在命令中，PySide2 去掉标记后传入
    assert any(a == "PySide2>=5.15.2.1" for a in cmd)
    assert not any(a.startswith("PySide6") for a in cmd)
    assert "PyYAML>=6.0" in cmd
    # 标记部分不应作为独立参数传入
    assert not any("python_version" in a for a in cmd)


def test_download_wheels_all_filtered_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """所有依赖被标记过滤时返回空列表，不调用 pip."""
    pip_called = False

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        nonlocal pip_called
        pip_called = True
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    pkgs = ("PySide6>=6.5.0; python_version >= '3.11'",)
    result = download_wheels(pkgs, "3.8.10", "https://idx/simple", tmp_path / "cache")
    assert result == []
    assert not pip_called


def test_download_wheels_sdist_fallback_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-index 失败 → -i index 失败（含 missing 包）→ pip wheel 构建 → 重试成功."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "odfpy-1.4.1-py3-none-any.whl"
    call_count = {"index_download": 0, "pip_wheel": 0}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    # --no-index 路径走 subprocess.run（stream=False）
    def fake_run(cmd: list[str], **kw: Any) -> _Result:
        raise subprocess.CalledProcessError(1, cmd, stderr="not in cache")

    # -i index 下载和 pip wheel 走 _stream_subprocess（stream=True）
    def fake_stream(cmd: list[str]) -> _Result:
        if "wheel" in cmd and "--no-deps" in cmd:
            call_count["pip_wheel"] += 1
            # 模拟从 sdist 构建 wheel 写入 cache_dir
            (cache / whl_name).write_bytes(b"odfpy")
            return _Result()
        # -i index 下载
        call_count["index_download"] += 1
        if call_count["index_download"] == 1:
            # 第一次 -i index 下载失败：报 odfpy 无 wheel
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
                "ERROR: No matching distribution found for odfpy>=1.4.1",
            )
        # 第二次重试成功
        r = _Result()
        r.stdout = f"Saved {whl_name}\n"
        return r

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.builder._find_uv", lambda: None)
    result = download_wheels(("odfpy>=1.4.1",), "3.8.10", "https://idx/simple", cache)
    assert call_count["index_download"] == 2
    assert call_count["pip_wheel"] == 1
    assert any(p.name == whl_name for p in result)


def test_download_wheels_sdist_fallback_no_missing_reraises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载失败但无 missing 包时直接抛出原始错误（不进入 sdist 回退）."""
    err = subprocess.CalledProcessError(1, "pip", stderr="network error, no missing pkg line")

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        # --no-index 缓存解析失败
        raise subprocess.CalledProcessError(1, cmd, stderr="not in cache")

    def fake_stream(cmd: list[str]) -> _Completed:
        # -i index 下载失败（无 missing 包）
        raise err

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.builder._find_uv", lambda: None)
    with pytest.raises(DependencyError, match="依赖下载失败"):
        download_wheels(("numpy",), "3.8.10", "https://idx/simple", tmp_path / "cache")


# ---------- _find_uv / _resolve_with_uv / _download_online ----------


def test_find_uv_returns_path_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 可用时返回路径."""
    monkeypatch.setattr("fspack.builder.shutil.which", lambda name: "/usr/local/bin/uv")
    assert _find_uv() == "/usr/local/bin/uv"


def test_find_uv_returns_none_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 不可用时返回 None."""
    monkeypatch.setattr("fspack.builder.shutil.which", lambda name: None)
    assert _find_uv() is None


def test_resolve_with_uv_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip compile 成功时返回 name==version 列表."""
    monkeypatch.setattr("fspack.builder._find_uv", lambda: "/usr/bin/uv")
    # uv pip compile 输出格式：每行 "name==version"，含注释行（# 开头）
    fake_output = "numpy==1.24.0\n  # via -r -\nrequests==2.31.0\n  # via -r -\n"
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["input"] = kw.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_output, stderr="")

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    result = _resolve_with_uv(("numpy>=1.0", "requests"), "3.11.9", ("win_amd64",), "https://idx/simple")
    assert result == ["numpy==1.24.0", "requests==2.31.0"]
    # 验证命令含 uv pip compile 和目标参数
    assert "pip" in captured["cmd"]
    assert "compile" in captured["cmd"]
    assert "--python-version" in captured["cmd"]
    assert "3.11" in captured["cmd"]
    assert "--python-platform" in captured["cmd"]
    assert "windows" in captured["cmd"]
    assert "--index-url" in captured["cmd"]
    # stdin 传入需求列表
    assert "numpy>=1.0" in captured["input"]
    assert "requests" in captured["input"]


def test_resolve_with_uv_linux_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台标签映射到 --python-platform linux."""
    monkeypatch.setattr("fspack.builder._find_uv", lambda: "/usr/bin/uv")
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="pkg==1.0\n", stderr="")

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    _resolve_with_uv(("pkg",), "3.11.9", ("manylinux2014_x86_64",), "https://idx/simple")
    assert "linux" in captured["cmd"]


def test_resolve_with_uv_no_uv_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 不可用时抛 DependencyError."""
    monkeypatch.setattr("fspack.builder._find_uv", lambda: None)
    with pytest.raises(DependencyError, match="未找到 uv"):
        _resolve_with_uv(("numpy",), "3.11.9", ("win_amd64",), "https://idx/simple")


def test_resolve_with_uv_empty_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 输出无匹配行时抛 DependencyError."""
    monkeypatch.setattr("fspack.builder._find_uv", lambda: "/usr/bin/uv")

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="only comments\n# no packages\n", stderr="")

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    with pytest.raises(DependencyError, match="未解析出任何依赖"):
        _resolve_with_uv(("numpy",), "3.11.9", ("win_amd64",), "https://idx/simple")


def test_resolve_with_uv_calledprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv pip compile 非零退出时抛 CalledProcessError（供 _download_online 捕获回退）."""
    monkeypatch.setattr("fspack.builder._find_uv", lambda: "/usr/bin/uv")

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="resolution failed")

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        _resolve_with_uv(("numpy",), "3.11.9", ("win_amd64",), "https://idx/simple")


def test_download_online_uv_resolved_uses_no_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 解析成功时用 pip download --no-deps -r 下载，含 --progress-bar on."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.builder._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        "fspack.builder._resolve_with_uv",
        lambda pkgs, pv, pt, idx: ["numpy==1.24.0", "requests==2.31.0"],
    )
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy>=1.0"], base_args, "/py/python", "3.11.9", ("win_amd64",), "https://idx/simple", cache)
    cmd = captured["cmd"]
    assert "--no-deps" in cmd
    assert "--progress-bar" in cmd
    assert "on" in cmd
    assert "-r" in cmd
    # 临时 requirements 文件已删除
    assert not (cache / ".requirements-resolved.txt").exists()


def test_download_online_uv_fails_falls_back_to_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 解析失败时回退到 pip 完整解析+下载."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.builder._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        "fspack.builder._resolve_with_uv",
        lambda pkgs, pv, pt, idx: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "uv", stderr="fail")),
    )
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy"], base_args, "/py/python", "3.11.9", ("win_amd64",), "https://idx/simple", cache)
    cmd = captured["cmd"]
    assert "--no-deps" not in cmd
    assert "-i" in cmd
    assert "https://idx/simple" in cmd


def test_download_online_no_uv_uses_pip_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 不可用时直接用 pip 完整解析+下载."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.builder._find_uv", lambda: None)
    captured: dict[str, list[str]] = {}

    def fake_stream(cmd: list[str]) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["numpy"], base_args, "/py/python", "3.11.9", ("win_amd64",), "https://idx/simple", cache)
    cmd = captured["cmd"]
    assert "--no-deps" not in cmd
    assert "-i" in cmd
    assert "https://idx/simple" in cmd


def test_download_online_sdist_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip 下载失败且含 missing 包时走 sdist 回退."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr("fspack.builder._find_uv", lambda: None)
    call_count = {"index_download": 0, "pip_wheel": 0}
    whl_name = "odfpy-1.4.1-py3-none-any.whl"

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_stream(cmd: list[str]) -> _Result:
        if "wheel" in cmd and "--no-deps" in cmd:
            call_count["pip_wheel"] += 1
            (cache / whl_name).write_bytes(b"odfpy")
            return _Result()
        call_count["index_download"] += 1
        if call_count["index_download"] == 1:
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="ERROR: Could not find a version that satisfies the requirement odfpy>=1.4.1 (from versions: none)\n"
                "ERROR: No matching distribution found for odfpy>=1.4.1",
            )
        r = _Result()
        r.stdout = f"Saved {whl_name}\n"
        return r

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache)]
    _download_online(["odfpy>=1.4.1"], base_args, "/py/python", "3.8.10", ("win_amd64",), "https://idx/simple", cache)
    assert call_count["index_download"] == 2
    assert call_count["pip_wheel"] == 1


def test_download_wheels_uv_path_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """download_wheels 集成测试：--no-index 失败 → uv 解析 → pip --no-deps 下载."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "numpy-1.24.0-cp311-cp311-win_amd64.whl"
    monkeypatch.setattr("fspack.builder._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.builder._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        "fspack.builder._resolve_with_uv",
        lambda pkgs, pv, pt, idx: ["numpy==1.24.0"],
    )

    # --no-index 走 subprocess.run 失败，pip --no-deps 走 _stream_subprocess 成功
    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        raise subprocess.CalledProcessError(1, cmd, stderr="not in cache")

    def fake_stream(cmd: list[str]) -> _Completed:
        # pip download --no-deps 下载成功
        (cache / whl_name).write_bytes(b"numpy")
        r = _Completed()
        r.stdout = f"Saved {whl_name}\n"
        return r

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    result = download_wheels(("numpy>=1.0",), "3.11.9", "https://idx/simple", cache)
    assert any(p.name == whl_name for p in result)


def test_download_online_uv_sdist_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv 路径 sdist 回退：pip download --no-deps 失败 → pip wheel 构建 → 重试成功."""
    cache = tmp_path / "cache"
    cache.mkdir()
    whl_name = "win-unicode-console-0.5-py3-none-any.whl"
    monkeypatch.setattr("fspack.builder._find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        "fspack.builder._resolve_with_uv",
        lambda pkgs, pv, pt, idx: ["win-unicode-console==0.5"],
    )
    call_count = {"pip_download": 0, "pip_wheel": 0}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_stream(cmd: list[str]) -> _Result:
        if "wheel" in cmd and "--no-deps" in cmd and "-w" in cmd:
            # pip wheel --no-deps 构建路径
            call_count["pip_wheel"] += 1
            (cache / whl_name).write_bytes(b"wuc")
            return _Result()
        call_count["pip_download"] += 1
        if call_count["pip_download"] == 1:
            # 第一次 pip download --no-deps 失败（无 wheel）
            raise subprocess.CalledProcessError(
                1,
                cmd,
                stderr="ERROR: Could not find a version that satisfies the requirement win-unicode-console==0.5 (from versions: none)\n"
                "ERROR: No matching distribution found for win-unicode-console==0.5",
            )
        # 第二次 pip download --no-deps -i index 重试成功（sdist 构建的 wheel 在缓存）
        r = _Result()
        r.stdout = f"Saved {whl_name}\n"
        return r

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    base_args = ["/py/python", "-m", "pip", "download", "-d", str(cache), "--only-binary=:all:"]
    result = _download_online(
        ["win-unicode-console"],
        base_args,
        "/py/python",
        "3.8.10",
        ("win_amd64",),
        "https://idx/simple",
        cache,
    )
    assert call_count["pip_download"] == 2  # 第一次失败，第二次成功
    assert call_count["pip_wheel"] == 1  # sdist 构建一次
    assert f"Saved {whl_name}" in result.stdout


# ---------- _stream_subprocess ----------


_FAKE_STDOUT_FD = 3
_FAKE_STDERR_FD = 4


class _FakePipe:
    """模拟管道，提供 ``read()``、``fileno()`` 和分块读取."""

    def __init__(self, data: bytes, fd: int) -> None:
        self._data = data
        self._pos = 0
        self._fd = fd

    def read(self) -> bytes:
        result = self._data[self._pos :]
        self._pos = len(self._data)
        return result

    def fileno(self) -> int:
        return self._fd

    def read_chunk(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakePopen:
    """模拟 ``subprocess.Popen``，配合 ``_stream_subprocess`` 测试."""

    def __init__(self, cmd: list[str], stdout_bytes: bytes, stderr_bytes: bytes, returncode: int) -> None:
        self.args = cmd
        self.stdout = _FakePipe(stdout_bytes, _FAKE_STDOUT_FD)
        self.stderr = _FakePipe(stderr_bytes, _FAKE_STDERR_FD)
        self._returncode = returncode

    def wait(self) -> int:
        return self._returncode


def _patch_os_read_for(monkeypatch: pytest.MonkeyPatch, popen: _FakePopen) -> None:
    """mock ``os.read`` 按 fd 从 ``popen`` 的管道取数据."""
    pipes = {popen.stdout._fd: popen.stdout, popen.stderr._fd: popen.stderr}

    def fake_read(fd: int, n: int) -> bytes:
        pipe = pipes.get(fd)
        if pipe is None:
            return b""
        return pipe.read_chunk(n)

    monkeypatch.setattr("fspack.builder.os.read", fake_read)


def _patch_stderr_buffer(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """替换 ``sys.stderr.buffer``，返回写入的字节块列表."""
    written: list[bytes] = []

    class _FakeBuffer:
        def write(self, data: bytes) -> int:
            written.append(data)
            return len(data)

        def flush(self) -> None:
            pass

    fake_stderr = types.SimpleNamespace(buffer=_FakeBuffer(), write=lambda s: None, flush=lambda: None)
    monkeypatch.setattr("fspack.builder.sys.stderr", fake_stderr)
    return written


def test_stream_subprocess_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功时返回 CompletedProcess，stdout/stderr 正确捕获，stderr 实时写入终端."""
    written = _patch_stderr_buffer(monkeypatch)

    def fake_popen(cmd: list[str], **kw: Any) -> _FakePopen:
        popen = _FakePopen(cmd, stdout_bytes=b"saved wheel\n", stderr_bytes=b"Downloading pkg", returncode=0)
        _patch_os_read_for(monkeypatch, popen)
        return popen

    monkeypatch.setattr("fspack.builder.subprocess.Popen", fake_popen)
    result = _stream_subprocess(["pip", "download"])
    assert result.returncode == 0
    assert result.stdout == "saved wheel\n"
    assert result.stderr == "Downloading pkg"
    # stderr 被实时写入 sys.stderr.buffer
    assert b"".join(written) == b"Downloading pkg"


def test_stream_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败时抛出 CalledProcessError，含 stdout/stderr."""
    _patch_stderr_buffer(monkeypatch)

    def fake_popen(cmd: list[str], **kw: Any) -> _FakePopen:
        popen = _FakePopen(cmd, stdout_bytes=b"out", stderr_bytes=b"err msg", returncode=1)
        _patch_os_read_for(monkeypatch, popen)
        return popen

    monkeypatch.setattr("fspack.builder.subprocess.Popen", fake_popen)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        _stream_subprocess(["pip"])
    assert exc_info.value.returncode == 1
    assert exc_info.value.stdout == "out"
    assert exc_info.value.stderr == "err msg"


def test_stream_subprocess_file_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Popen 抛 FileNotFoundError 时透传（pip 解释器不存在）."""
    monkeypatch.setattr("fspack.builder.subprocess.Popen", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(FileNotFoundError):
        _stream_subprocess(["/missing/cmd"])


def test_stream_subprocess_multibyte_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """多字节 stderr（中文）正确解码，不抛 UnicodeDecodeError."""
    _patch_stderr_buffer(monkeypatch)

    def fake_popen(cmd: list[str], **kw: Any) -> _FakePopen:
        popen = _FakePopen(cmd, stdout_bytes=b"", stderr_bytes="下载中\n".encode(), returncode=0)
        _patch_os_read_for(monkeypatch, popen)
        return popen

    monkeypatch.setattr("fspack.builder.subprocess.Popen", fake_popen)
    result = _stream_subprocess(["cmd"])
    assert result.stderr == "下载中\n"


# ---------- _run_pip stream 参数 ----------


def test_run_pip_stream_uses_stream_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=True 时调用 _stream_subprocess 而非 subprocess.run."""
    stream_called = False

    def fake_stream(cmd: list[str]) -> _Completed:
        nonlocal stream_called
        stream_called = True
        return _Completed()

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    monkeypatch.setattr(
        "fspack.builder.subprocess.run",
        lambda cmd, **kw: (_ for _ in ()).throw(AssertionError("不应调用 subprocess.run")),
    )
    result = _run_pip(["pip"], "label", stream=True)
    assert stream_called is True
    assert result is not None
    assert result.returncode == 0


def test_run_pip_stream_false_uses_subprocess_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=False 时调用 subprocess.run 而非 _stream_subprocess."""
    run_called = False

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        nonlocal run_called
        run_called = True
        return _Completed()

    monkeypatch.setattr("fspack.builder.subprocess.run", fake_run)
    monkeypatch.setattr(
        "fspack.builder._stream_subprocess",
        lambda cmd: (_ for _ in ()).throw(AssertionError("不应调用 _stream_subprocess")),
    )
    _run_pip(["pip"], "label", stream=False)
    assert run_called is True


def test_run_pip_stream_suppress_error_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=True + suppress_error=True 时 CalledProcessError 返回 None."""

    def fake_stream(cmd: list[str]) -> _Completed:
        raise subprocess.CalledProcessError(1, cmd, stderr="fail")

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    result = _run_pip(["pip"], "label", stream=True, suppress_error=True)
    assert result is None


def test_run_pip_stream_failure_raises_dependency_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=True + suppress_error=False 时 CalledProcessError 转为 DependencyError."""

    def fake_stream(cmd: list[str]) -> _Completed:
        raise subprocess.CalledProcessError(1, cmd, stderr="download failed")

    monkeypatch.setattr("fspack.builder._stream_subprocess", fake_stream)
    with pytest.raises(DependencyError, match="依赖下载失败"):
        _run_pip(["pip"], "label", stream=True)


def test_run_pip_stream_file_not_found_raises_dependency_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stream=True 时 FileNotFoundError 转为 DependencyError."""
    monkeypatch.setattr("fspack.builder._stream_subprocess", lambda cmd: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(DependencyError, match="未找到 pip"):
        _run_pip(["/missing/pip"], "label", stream=True)
