"""``pipeline/runtime_stage.py`` 测试：Win7 dll 注入/替换、_slim_runtime、_prepare_runtime 与 t 版布局打平."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.builder import (
    _inject_win7_compat_dll,
    _needs_win7_compat_dll,
    _slim_runtime,
    build,
)
from fspack.config import BuildOptions, ProjectInfo, get_mirror
from fspack.packaging.pipeline.stages import BuildContext
from fspack.platform import Platform
from tests._stubs import CompletedStub, make_standalone_runtime, setup_embed_mocks

# ---- Win7 兼容 DLL 注入测试 ----


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("3.8.10", False),
        ("3.8.20", False),
        ("3.9.0", True),
        ("3.9.13", True),
        ("3.10.11", True),
        ("3.11.9", True),
        ("3.12.0", True),
        ("3.13.0", True),
        ("3.14.0", True),
    ],
)
def test_needs_win7_compat_dll(version: str, expected: bool) -> None:
    """Python 3.9+ 需注入兼容 DLL，3.8 不需要."""
    assert _needs_win7_compat_dll(version) is expected


def test_inject_win7_compat_dll_copies_from_assets(tmp_path: Path) -> None:
    """runtime 无 DLL 时从 fspack assets 复制到 runtime 根目录."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    _inject_win7_compat_dll(runtime_dir)
    dll = runtime_dir / "api-ms-win-core-path-l1-1-0.dll"
    assert dll.is_file()
    # DLL 应为非空二进制（~114KB x64 构建）
    assert dll.stat().st_size > 10000


def test_inject_win7_compat_dll_skips_when_exists(tmp_path: Path) -> None:
    """runtime 已有 DLL 时跳过复制，原文件内容不变."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    dest = runtime_dir / "api-ms-win-core-path-l1-1-0.dll"
    dest.write_bytes(b"FAKE_EXISTING_DLL")
    _inject_win7_compat_dll(runtime_dir)
    # 内容应保持不变（未被覆盖）
    assert dest.read_bytes() == b"FAKE_EXISTING_DLL"


def test_inject_win7_compat_dll_warns_when_source_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """源 DLL 缺失时仅 warning 不报错（向后兼容旧 fspack 安装）."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    # 将模块级常量改为不存在的文件名，使源路径查找失败
    monkeypatch.setattr("fspack.packaging.pyc._WIN7_COMPAT_DLL_NAME", "nonexistent-dll.dll")
    _inject_win7_compat_dll(runtime_dir)  # 不应抛异常
    assert not (runtime_dir / "nonexistent-dll.dll").exists()
    assert any("缺失" in r.message for r in caplog.records)


def test_build_injects_win7_compat_dll_for_py39_plus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.11.9 + Windows 目标构建后 runtime 含 api-ms-win-core-path-l1-1-0.dll."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert (proj / "dist" / "runtime" / "api-ms-win-core-path-l1-1-0.dll").is_file()


def test_build_skips_win7_compat_dll_for_py38_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.8.10 + Windows 目标构建后 runtime 不含兼容 DLL（3.8 官方支持 Win7）."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    setup_embed_mocks(tmp_path, monkeypatch, "3.8.10")
    build(proj, get_mirror("huawei"), "3.8.10", target=Platform.WINDOWS)
    assert not (proj / "dist" / "runtime" / "api-ms-win-core-path-l1-1-0.dll").exists()


def test_build_skips_win7_compat_dll_for_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.11.9 + Linux 目标构建后 runtime 不含兼容 DLL（Linux 无此问题）."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    # Linux 用 standalone mock
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.download_standalone", lambda v, r, c, **kw: tmp_path / "fake.tar.gz"
    )
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_standalone",
        lambda tar_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python" / "bin").mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python" / "bin" / "python3.11").write_text(""),
            (runtime_dir.parent / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )
    # 守卫要求 Linux 目标在 Linux 构建机上（测试可在任意宿主运行）
    monkeypatch.setattr("fspack.packaging.pipeline.executor.detect_platform", lambda: Platform.LINUX)
    # mock 预编译阶段的 subprocess.run（Linux python3.11 二进制在 Windows 上无法执行）
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX)
    assert not (proj / "dist" / "runtime" / "api-ms-win-core-path-l1-1-0.dll").exists()


# --- _slim_runtime 测试 ---


def _make_slim_runtime_context(
    tmp_path: Path,
    *,
    target: Platform = Platform.LINUX,
    no_slim_runtime: bool = False,
) -> tuple[BuildContext, Path]:
    """构造最小 BuildContext 用于 _slim_runtime 测试."""
    from fspack.config import BuildConfig, BuildOptions, ProjectInfo
    from fspack.progress import BuildTracker

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
    runtime_dir = tmp_path / "dist" / "runtime"
    ctx = BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=BuildOptions(no_slim_runtime=no_slim_runtime),
        runtime_dir=runtime_dir,
    )
    return ctx, runtime_dir


def test_slim_runtime_no_slim_runtime_skips(tmp_path: Path) -> None:
    """no_slim_runtime=True 时跳过精简."""
    ctx, runtime_dir = _make_slim_runtime_context(tmp_path, no_slim_runtime=True)
    make_standalone_runtime(runtime_dir.parent)  # runtime_dir = dist/runtime

    _slim_runtime(ctx, has_tkinter=False)

    bin_dir = runtime_dir / "python" / "bin"
    assert (bin_dir / "python3.11").is_file()
    assert (bin_dir / "2to3").is_file()
    assert (runtime_dir / "python" / "include").is_dir()
    assert (runtime_dir / "python" / "lib" / "libtcl9.0.so").is_file()


def test_slim_runtime_linux_calls_trim(tmp_path: Path) -> None:
    """Linux 目标调用 _trim_standalone_runtime 删文件."""
    ctx, runtime_dir = _make_slim_runtime_context(tmp_path, target=Platform.LINUX)
    make_standalone_runtime(runtime_dir.parent)  # runtime_dir = dist/runtime

    _slim_runtime(ctx, has_tkinter=False)

    bin_dir = runtime_dir / "python" / "bin"
    assert not (bin_dir / "python3.11").exists()
    assert not (bin_dir / "2to3").exists()
    assert not (runtime_dir / "python" / "include").exists()
    assert not (runtime_dir / "python" / "share").exists()
    assert not (runtime_dir / "python" / "lib" / "libtcl9.0.so").exists()


def test_slim_runtime_windows_skips(tmp_path: Path) -> None:
    """Windows 目标时 _trim_standalone_runtime 内部跳过."""
    ctx, runtime_dir = _make_slim_runtime_context(tmp_path, target=Platform.WINDOWS)
    bin_dir = runtime_dir / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.11").write_text("fake")

    _slim_runtime(ctx, has_tkinter=False)

    assert (bin_dir / "python3.11").is_file()


# --- _prepare_runtime Win7 dll 替换集成测试 ---


def _make_win7_runtime_context(
    tmp_path: Path,
    py_version: str,
    *,
    target: Platform = Platform.WINDOWS,
) -> tuple[BuildContext, Path]:
    """构造 runtime 已就绪（官方 dll 存在）的 BuildContext 用于 _prepare_runtime 测试."""
    from fspack.config import BuildConfig
    from fspack.progress import BuildTracker

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = ProjectInfo.from_dir(tmp_path, py_version)
    cfg = BuildConfig(
        project_dir=tmp_path,
        dist_dir=tmp_path / "dist",
        embed_cache_dir=tmp_path / "cache",
        mirror=get_mirror("huawei"),
        target=target,
    )
    runtime_dir = tmp_path / "dist" / "runtime"
    runtime_dir.mkdir(parents=True)
    major, minor = py_version.split(".")[:2]
    (runtime_dir / f"python{major}{minor}.dll").write_bytes(b"official")
    ctx = BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=BuildOptions(),
        runtime_dir=runtime_dir,
    )
    return ctx, runtime_dir


def test_prepare_runtime_replaces_dll_on_windows_312(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 3.12+ 目标：官方 embed 解压后调 ensure_win7_dll（replace_invalid）替换."""
    from fspack.packaging.pipeline import runtime_stage

    ctx, runtime_dir = _make_win7_runtime_context(tmp_path, "3.12.10")
    calls: dict[str, object] = {}

    def fake_ensure(version: str, cache_dir: Path, dest_dir: Path, **kwargs: object) -> Path:
        calls["version"] = version
        calls["cache_dir"] = cache_dir
        calls["dest_dir"] = dest_dir
        calls["kwargs"] = kwargs
        dll = dest_dir / "python312.dll"
        dll.write_bytes(b"win7")
        return dll

    monkeypatch.setattr(runtime_stage, "ensure_win7_dll", fake_ensure)
    inject_calls: list[Path] = []
    monkeypatch.setattr(runtime_stage, "_inject_win7_compat_dll", inject_calls.append)
    from fspack.config import win7_dll_cache_dir

    result = runtime_stage._prepare_runtime(ctx)
    assert calls["version"] == "3.12.10"
    assert calls["dest_dir"] == runtime_dir
    assert calls["cache_dir"] == win7_dll_cache_dir()
    # kwargs 含 replace_invalid=True 即可（stage 为 StageRecorder 实例）
    assert calls["kwargs"]["replace_invalid"] is True  # type: ignore[index]
    assert (runtime_dir / "python312.dll").read_bytes() == b"win7"
    # 3.12 >= 3.9，shim 注入同样触发
    assert inject_calls == [runtime_dir]
    assert result == tmp_path / "dist" / "site-packages"


def test_prepare_runtime_skips_dll_replace_on_311(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 3.11 目标：shim 注入即可，不触发 dll 替换."""
    from fspack.packaging.pipeline import runtime_stage

    ctx, runtime_dir = _make_win7_runtime_context(tmp_path, "3.11.9")
    called = {"ensure": False}
    monkeypatch.setattr(runtime_stage, "ensure_win7_dll", lambda *a, **k: called.__setitem__("ensure", True))
    inject_calls: list[Path] = []
    monkeypatch.setattr(runtime_stage, "_inject_win7_compat_dll", inject_calls.append)
    runtime_stage._prepare_runtime(ctx)
    assert not called["ensure"]
    assert inject_calls == [runtime_dir]
    assert (runtime_dir / "python311.dll").read_bytes() == b"official"


def test_prepare_runtime_skips_dll_replace_on_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标：非 Windows 不触发 dll 替换与 shim 注入."""
    from fspack.packaging.pipeline import runtime_stage

    ctx, _ = _make_win7_runtime_context(tmp_path, "3.12.10", target=Platform.LINUX)
    # Linux 分支走 standalone 下载：runtime 内无 python/bin 会被判未就绪而下载，
    # patch 下载/解压避免网络
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.download_standalone", lambda *a, **k: tmp_path / "fake.tar.gz"
    )
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_standalone", lambda *a, **k: None)
    called = {"ensure": False, "inject": False}
    monkeypatch.setattr(runtime_stage, "ensure_win7_dll", lambda *a, **k: called.__setitem__("ensure", True))
    monkeypatch.setattr(runtime_stage, "_inject_win7_compat_dll", lambda *a, **k: called.__setitem__("inject", True))
    runtime_stage._prepare_runtime(ctx)
    assert not called["ensure"]
    assert not called["inject"]


# --- _flatten_python_dir & _prepare_windows_t_runtime 测试 ---


def test_flatten_python_dir_moves_entries_to_root(tmp_path: Path) -> None:
    """_flatten_python_dir 把 python/ 子目录内容上移到 runtime_dir 根并删 python/."""
    from fspack.packaging.pipeline.runtime_stage import _flatten_python_dir

    runtime = tmp_path / "runtime"
    python_sub = runtime / "python"
    python_sub.mkdir(parents=True)
    (python_sub / "python.exe").write_bytes(b"exe")
    (python_sub / "python313t.dll").write_bytes(b"dll")
    (python_sub / "Lib").mkdir()
    (python_sub / "Lib" / "os.py").write_text("")
    (python_sub / "DLLs").mkdir()
    (python_sub / "DLLs" / "_tkinter.pyd").write_bytes(b"pyd")

    _flatten_python_dir(runtime)

    assert not python_sub.exists()  # python/ 子目录已删
    assert (runtime / "python.exe").is_file()
    assert (runtime / "python313t.dll").is_file()
    assert (runtime / "Lib" / "os.py").is_file()
    assert (runtime / "DLLs" / "_tkinter.pyd").is_file()


def test_flatten_python_dir_idempotent_no_python_subdir(tmp_path: Path) -> None:
    """_flatten_python_dir 幂等：python/ 不存在时直接返回不报错（缓存命中场景）."""
    from fspack.packaging.pipeline.runtime_stage import _flatten_python_dir

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python313t.dll").write_bytes(b"dll")  # runtime 已扁平化

    _flatten_python_dir(runtime)
    assert (runtime / "python313t.dll").is_file()
    assert not (runtime / "python").exists()


def test_flatten_python_dir_overrides_existing_dest(tmp_path: Path) -> None:
    """_flatten_python_dir 遇到 dest 已存在时先清理再移动（重复构建残留场景）."""
    from fspack.packaging.pipeline.runtime_stage import _flatten_python_dir

    runtime = tmp_path / "runtime"
    python_sub = runtime / "python"
    python_sub.mkdir(parents=True)
    (python_sub / "python.exe").write_bytes(b"new")
    # runtime 根已有残留的 python.exe（旧构建未清）
    (runtime / "python.exe").write_bytes(b"old")
    (python_sub / "Lib").mkdir()
    (python_sub / "Lib" / "x.py").write_text("")
    (runtime / "Lib").mkdir()
    (runtime / "Lib" / "stale.py").write_text("")  # 残留目录

    _flatten_python_dir(runtime)

    assert (runtime / "python.exe").read_bytes() == b"new"  # 覆盖为最新
    assert not (runtime / "python").exists()
    assert (runtime / "Lib" / "x.py").is_file()
    assert not (runtime / "Lib" / "stale.py").exists()  # 残留被清理


def _make_windows_t_context(
    tmp_path: Path,
    py_version: str = "3.13.14t",
    *,
    no_stdlib_trim: bool = False,
) -> tuple[BuildContext, Path]:
    """构造 Windows 自由线程版本 BuildContext 用于 _prepare_windows_t_runtime 测试."""
    from fspack.config import BuildConfig
    from fspack.progress import BuildTracker

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    info = ProjectInfo.from_dir(tmp_path, py_version)
    cfg = BuildConfig(
        project_dir=tmp_path,
        dist_dir=tmp_path / "dist",
        embed_cache_dir=tmp_path / "cache",
        mirror=get_mirror("huawei"),
        target=Platform.WINDOWS,
    )
    runtime_dir = tmp_path / "dist" / "runtime"
    runtime_dir.mkdir(parents=True)
    ctx = BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=BuildOptions(no_stdlib_trim=no_stdlib_trim),
        runtime_dir=runtime_dir,
    )
    return ctx, runtime_dir


def test_prepare_windows_t_runtime_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime 已就绪（python3XXt.dll 存在）时两 stage 均 hit_cache，不下载不解压."""
    from fspack.packaging.pipeline import runtime_stage
    from fspack.packaging.runtime import embed_dirname

    ctx, runtime_dir = _make_windows_t_context(tmp_path, "3.13.14t")
    # 标记 runtime 已就绪：写入 python313t.dll
    (runtime_dir / f"{embed_dirname('3.13.14t')}.dll").write_bytes(b"ready")

    download_calls: list[str] = []
    extract_calls: list[Path] = []

    def fake_download(*args: object, **kwargs: object) -> Path:
        download_calls.append("download")
        return tmp_path / "fake.tar.gz"

    def fake_extract(_tar: Path, _dest: Path) -> None:
        extract_calls.append(_tar)

    monkeypatch.setattr(runtime_stage, "_default_download_standalone", fake_download)
    monkeypatch.setattr(runtime_stage, "_default_extract_standalone", fake_extract)
    monkeypatch.setattr(runtime_stage, "needs_win7_dll", lambda v: False)
    monkeypatch.setattr(runtime_stage, "_needs_win7_compat_dll", lambda v: False)

    site_packages = runtime_stage._prepare_windows_t_runtime(ctx)
    assert download_calls == []  # runtime 已就绪，不下载
    assert extract_calls == []  # 不解压
    assert site_packages == ctx.cfg.dist_dir / "site-packages"


def test_prepare_windows_t_runtime_download_extract_flatten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime 未就绪时下载 standalone freethreaded tarball 并扁平化 python/ 子目录."""
    from fspack.packaging.pipeline import runtime_stage
    from fspack.packaging.runtime import embed_dirname

    ctx, runtime_dir = _make_windows_t_context(tmp_path, "3.13.14t")
    tar_path = tmp_path / "fake-standalone.tar.gz"
    tar_path.write_bytes(b"tarball")

    def fake_download(*args: object, **kwargs: object) -> Path:
        # 验证 windows=True 被传递
        assert kwargs.get("windows") is True
        return tar_path

    def fake_extract(_tar: Path, dest: Path) -> None:
        # 模拟 python-build-standalone tarball 解压：顶层是 python/ 子目录
        python_sub = dest / "python"
        python_sub.mkdir(parents=True)
        (python_sub / "python.exe").write_bytes(b"exe")
        (python_sub / f"{embed_dirname('3.13.14t')}.dll").write_bytes(b"dll")
        (python_sub / "Lib").mkdir()
        (python_sub / "Lib" / "os.py").write_text("")
        (python_sub / "DLLs").mkdir()

    monkeypatch.setattr(runtime_stage, "_default_download_standalone", fake_download)
    monkeypatch.setattr(runtime_stage, "_default_extract_standalone", fake_extract)
    monkeypatch.setattr(runtime_stage, "needs_win7_dll", lambda v: False)
    monkeypatch.setattr(runtime_stage, "_needs_win7_compat_dll", lambda v: False)

    site_packages = runtime_stage._prepare_windows_t_runtime(ctx)
    assert site_packages == ctx.cfg.dist_dir / "site-packages"
    # 扁平化后 python/ 子目录被删，DLL/exe/Lib 移到 runtime_dir 根
    assert not (runtime_dir / "python").exists()
    assert (runtime_dir / "python.exe").is_file()
    assert (runtime_dir / f"{embed_dirname('3.13.14t')}.dll").is_file()
    assert (runtime_dir / "Lib" / "os.py").is_file()
    assert (runtime_dir / "DLLs").is_dir()


def test_prepare_runtime_dispatches_to_windows_t_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_prepare_runtime 对 Windows+t 版本调用 _prepare_windows_t_runtime（独立分支）."""
    from fspack.packaging.pipeline import runtime_stage

    ctx, _ = _make_windows_t_context(tmp_path, "3.13.14t", no_stdlib_trim=True)
    called: dict[str, object] = {}

    def fake_prepare_t(ctx: BuildContext) -> Path:
        called["t_branch"] = True
        return ctx.cfg.dist_dir / "site-packages"

    monkeypatch.setattr(runtime_stage, "_prepare_windows_t_runtime", fake_prepare_t)
    monkeypatch.setattr(runtime_stage, "needs_win7_dll", lambda v: False)
    monkeypatch.setattr(runtime_stage, "_needs_win7_compat_dll", lambda v: False)

    runtime_stage._prepare_runtime(ctx)
    assert called.get("t_branch") is True
