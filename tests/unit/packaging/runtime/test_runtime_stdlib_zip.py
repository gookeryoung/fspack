"""stdlib_zip 模块测试：Linux/macOS 标准库 zip 化（构建期）.

覆盖：完整流程（compileall → zip legacy 条目 → 删源）、平台跳过、幂等、
compileall 失败/超时降级、zip 写入失败降级、t 后缀布局、排除目录。
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.config import BuildConfig, BuildOptions, ProjectInfo, get_mirror
from fspack.packaging.pipeline.context import BuildContext
from fspack.packaging.pipeline.runtime_stage import _zip_stdlib
from fspack.packaging.runtime import stdlib_zip
from fspack.packaging.runtime.stdlib_zip import zip_stdlib
from fspack.platform import Platform
from fspack.progress import BuildTracker, StageRecorder


def _make_runtime(tmp: Path, py_version: str = "3.11.9") -> tuple[Path, Path, Path, Path]:
    """构造 standalone runtime 布局，返回 (runtime, stdlib, zip 路径, python 二进制)."""
    is_t = py_version.endswith("t")
    base = py_version[:-1] if is_t else py_version
    major, minor = base.split(".")[:2]
    suffix = "t" if is_t else ""
    runtime = tmp / "runtime"
    stdlib = runtime / "python" / "lib" / f"python{major}.{minor}{suffix}"
    stdlib.mkdir(parents=True)
    # zip 名与 CPython POSIX getpath 约定一致：python311.zip / python313t.zip
    # （major+minor 无点拼接，t 后缀紧随），带点的 python3.11.zip 不会进 sys.path
    zip_path = runtime / "python" / "lib" / f"python{major}{minor}{suffix}.zip"
    py_exe = runtime / "python" / "bin" / f"python{major}.{minor}{suffix}"
    py_exe.parent.mkdir(parents=True)
    py_exe.write_bytes(b"#!/bin/sh\n")
    return runtime, stdlib, zip_path, py_exe


def _fake_compileall(
    monkeypatch: pytest.MonkeyPatch,
    tag: str = "cpython-311-x86_64-linux-gnu",
    returncode: int = 0,
    raise_exc: Exception | None = None,
) -> list[list[str]]:
    """替换 stdlib_zip.subprocess.run：模拟 compileall 生成 __pycache__/<stem>.<tag>.pyc.

    返回记录的调用命令列表，供断言命令构造正确。
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if raise_exc is not None:
            raise raise_exc
        # stdlib 路径是命令中唯一的目录参数（cmd = [python, -m, compileall, <stdlib>, -q, -j, 0]）
        stdlib = next(Path(a) for a in cmd[1:] if Path(a).is_dir())
        for py in stdlib.rglob("*.py"):
            rel_parts = py.relative_to(stdlib).parts[:-1]
            if any(p in stdlib_zip._STDLIB_ZIP_EXCLUDE_DIRS for p in rel_parts):
                continue
            if any(p.startswith("config-") for p in rel_parts):
                continue
            cache = py.parent / "__pycache__"
            cache.mkdir(exist_ok=True)
            (cache / f"{py.stem}.{tag}.pyc").write_bytes(b"\x42" * 16)
        return subprocess.CompletedProcess(cmd, returncode)

    monkeypatch.setattr(stdlib_zip.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# zip_stdlib 核心流程
# ---------------------------------------------------------------------------


def test_zip_stdlib_linux_full_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 完整流程：compileall → zip legacy 条目 → 删 .py 与 __pycache__."""
    runtime, stdlib, zip_path, _ = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("import sys\n")
    (stdlib / "json").mkdir()
    (stdlib / "json" / "__init__.py").write_text("def dumps(): pass\n")

    calls = _fake_compileall(monkeypatch)
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    # 命令：runtime 自身 python -m compileall <stdlib> -q -j 0
    py_exe = runtime / "python" / "bin" / "python3.11"
    assert calls == [[str(py_exe), "-m", "compileall", str(stdlib), "-q", "-j", "0"]]
    # zip 存在且条目为 legacy 布局名（剥离平台标签）
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == ["json/__init__.pyc", "os.pyc"]
    # 源 .py 与 __pycache__ 已删除
    assert not (stdlib / "os.py").exists()
    assert not (stdlib / "json" / "__init__.py").exists()
    assert not (stdlib / "json" / "__pycache__").exists()
    record = st._finalize()
    assert record.items == 1
    assert "打包 2 模块" in record.detail


def test_zip_stdlib_macos_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS 目标同样执行 zip 化."""
    runtime, stdlib, zip_path, _ = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("x = 1\n")

    _fake_compileall(monkeypatch, tag="cpython-311-darwin")
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.MACOS, st)

    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["os.pyc"]
    assert not (stdlib / "os.py").exists()


def test_zip_stdlib_windows_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标跳过（embed zip 标准库已在 python3XX.zip 内）."""
    runtime, stdlib, _, _ = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("x = 1\n")

    calls = _fake_compileall(monkeypatch)
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.WINDOWS, st)

    assert calls == []
    assert (stdlib / "os.py").exists()
    assert st._finalize().detail == "仅 Linux/macOS，跳过"


def test_zip_stdlib_missing_stdlib_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdlib 目录不存在（缓存命中前/异常布局）时跳过."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    calls = _fake_compileall(monkeypatch)
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    assert calls == []
    assert st._finalize().detail == "标准库目录不存在，跳过"


def test_zip_stdlib_missing_python_exe_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """python 二进制不存在（已被精简/交叉构建）时跳过."""
    runtime, stdlib, _, py_exe = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("x = 1\n")
    py_exe.unlink()

    calls = _fake_compileall(monkeypatch)
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    assert calls == []
    assert st._finalize().detail == "runtime python 不存在，跳过"


def test_zip_stdlib_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复构建：stdlib 无 .py 可编译（已 zip 化）时跳过."""
    runtime, stdlib, zip_path, _ = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("x = 1\n")
    _fake_compileall(monkeypatch)

    st1 = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st1)
    assert zip_path.is_file()

    calls = _fake_compileall(monkeypatch)
    st2 = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st2)

    assert calls == []
    assert st2._finalize().detail == "已 zip 化，跳过"
    # zip 未被破坏
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["os.pyc"]


def test_zip_stdlib_freethreaded_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """t 后缀版本：stdlib/python 二进制目录名带点，zip 名无点（python313t.zip）."""
    runtime, stdlib, zip_path, py_exe = _make_runtime(tmp_path, py_version="3.13.14t")
    assert stdlib.name == "python3.13t"
    assert zip_path.name == "python313t.zip"
    assert py_exe.name == "python3.13t"
    (stdlib / "os.py").write_text("x = 1\n")

    _fake_compileall(monkeypatch, tag="cpython-313t-x86_64-linux-gnu")
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.13.14t", Platform.LINUX, st)

    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["os.pyc"]


def test_zip_stdlib_excludes_special_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """site-packages/lib-dynload/config-* 不打包不删源（保留目录形态）."""
    runtime, stdlib, zip_path, _ = _make_runtime(tmp_path)
    for rel in ("site-packages/foo.py", "lib-dynload/bar.py", "config-3.11/baz.py", "os.py"):
        p = stdlib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")

    _fake_compileall(monkeypatch)
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["os.pyc"]
    # 排除目录源文件保留
    assert (stdlib / "site-packages" / "foo.py").is_file()
    assert (stdlib / "lib-dynload" / "bar.py").is_file()
    assert (stdlib / "config-3.11" / "baz.py").is_file()
    # stdlib 正常模块已删
    assert not (stdlib / "os.py").exists()


def test_zip_stdlib_skips_untagged_pyc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无平台标签的异常命名 pyc 保守跳过（不打包）."""
    runtime, stdlib, zip_path, _ = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("x = 1\n")

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cache = stdlib / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "os.cpython-311-x86_64-linux-gnu.pyc").write_bytes(b"\x42" * 16)
        (cache / "plain.pyc").write_bytes(b"\x43" * 16)  # 异常命名
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(stdlib_zip.subprocess, "run", fake_run)
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["os.pyc"]


# ---------------------------------------------------------------------------
# 降级路径：compileall 失败/异常、zip 写入失败 → 保留目录形态
# ---------------------------------------------------------------------------


def test_zip_stdlib_compileall_failure_keeps_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compileall 退出码非 0：降级保留目录形态（warning 不阻断构建）."""
    runtime, stdlib, zip_path, _ = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("import sys\n")

    _fake_compileall(monkeypatch, returncode=1)
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    assert not zip_path.exists()
    assert (stdlib / "os.py").is_file()
    assert st._finalize().detail == "compileall 失败，跳过 zip 化"


def test_zip_stdlib_compileall_timeout_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compileall 超时/OSError：降级保留目录形态."""
    runtime, stdlib, zip_path, _ = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("import sys\n")

    _fake_compileall(monkeypatch, raise_exc=subprocess.TimeoutExpired(cmd=["x"], timeout=1))
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    assert not zip_path.exists()
    assert (stdlib / "os.py").is_file()
    assert st._finalize().detail == "compileall 异常，跳过 zip 化"


def test_zip_stdlib_zip_write_failure_keeps_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """zip 写入失败：清理临时文件并保留源 .py（下次构建幂等重试）."""
    runtime, stdlib, zip_path, _ = _make_runtime(tmp_path)
    (stdlib / "os.py").write_text("import sys\n")
    _fake_compileall(monkeypatch)

    class _FailZip:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("disk full")

    monkeypatch.setattr(stdlib_zip.zipfile, "ZipFile", _FailZip)
    st = StageRecorder("标准库 zip 化")
    zip_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    assert not zip_path.exists()
    assert not zip_path.with_suffix(".zip.tmp").exists()
    assert (stdlib / "os.py").is_file()
    assert st._finalize().detail == "zip 写入失败，跳过"


# ---------------------------------------------------------------------------
# _zip_stdlib 阶段包装（runtime_stage）
# ---------------------------------------------------------------------------


def _make_zip_stdlib_context(tmp_path: Path, target: Platform, no_stdlib_zip: bool = False):
    """构造最小 BuildContext 用于 _zip_stdlib 阶段测试."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = ProjectInfo.from_dir(tmp_path, "3.11.9")
    cfg = BuildConfig(
        project_dir=tmp_path,
        dist_dir=tmp_path / "dist",
        embed_cache_dir=tmp_path / "cache",
        mirror=get_mirror("huawei"),
        target=target,
    )
    ctx = BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=BuildOptions(no_stdlib_zip=no_stdlib_zip),
        runtime_dir=tmp_path / "dist" / "runtime",
    )
    return ctx


def test_zip_stdlib_stage_no_stdlib_zip_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """no_stdlib_zip=True 时阶段直接跳过，不调用底层 zip_stdlib."""
    ctx = _make_zip_stdlib_context(tmp_path, Platform.LINUX, no_stdlib_zip=True)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("不应调用 zip_stdlib")

    monkeypatch.setattr("fspack.packaging.pipeline.stages.zip_stdlib", _boom)
    _zip_stdlib(ctx)


def test_zip_stdlib_stage_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认开启：经 stages facade dispatch 调用 zip_stdlib（可被 monkeypatch 拦截）."""
    ctx = _make_zip_stdlib_context(tmp_path, Platform.LINUX)
    received: dict[str, Any] = {}

    def _fake_zip(runtime_dir: Path, py_version: str, target: Platform, stage: Any) -> None:
        received["runtime_dir"] = runtime_dir
        received["py_version"] = py_version
        received["target"] = target
        stage.set_detail("fake")

    monkeypatch.setattr("fspack.packaging.pipeline.stages.zip_stdlib", _fake_zip)
    _zip_stdlib(ctx)

    assert received["runtime_dir"] == ctx.runtime_dir
    assert received["py_version"] == "3.11.9"
    assert received["target"] is Platform.LINUX
    # 阶段记录进入 tracker
    records = ctx.tracker.records
    assert any(r.name == "标准库 zip 化" and r.detail == "fake" for r in records)
