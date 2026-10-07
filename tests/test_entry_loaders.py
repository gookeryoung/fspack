"""``pipeline/stages.py`` 入口 loader 并行编译测试：_build_entry_loaders 线程池行为."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest

from fspack.config import AppType, BuildOptions, EntryPoint, ProjectInfo, get_mirror
from fspack.exceptions import LoaderError
from fspack.packaging.pipeline.stages import _MAX_LOADER_WORKERS, BuildContext, _build_entry_loaders
from fspack.platform import Platform

# --- _build_entry_loaders 并行编译测试（iter-133）---


def _make_multi_entry_context(
    tmp_path: Path,
    entry_names: tuple[str, ...] = ("cli", "gui", "web", "api"),
    *,
    target: Platform = Platform.WINDOWS,
) -> BuildContext:
    """构造多入口 BuildContext 用于 _build_entry_loaders 测试.

    在 ``tmp_path/src`` 下为每个 entry name 创建 ``<name>.py``，生成对应 EntryPoint
    元组（cli/web/api 为 CLI，gui 为 GUI），返回 BuildContext。
    """
    from fspack.config import BuildConfig
    from fspack.progress import BuildTracker

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    entries: list[EntryPoint] = []
    for name in entry_names:
        script = src_dir / f"{name}.py"
        script.write_text("def main():\n    pass\n")
        app_type = AppType.GUI if name == "gui" else AppType.CLI
        entries.append(EntryPoint(name=name, module=name, file=script, app_type=app_type))
    info = ProjectInfo(
        name="multi",
        version="0.1",
        src_dir=src_dir,
        entry_module=entry_names[0],
        entry_file=src_dir / f"{entry_names[0]}.py",
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
        entries=tuple(entries),
    )
    cfg = BuildConfig(
        project_dir=tmp_path,
        dist_dir=tmp_path / "dist",
        embed_cache_dir=tmp_path / "cache",
        mirror=get_mirror("huawei"),
        target=target,
    )
    # dist_dir 需预先存在：_write_entry_files 直接 write_text 到 dist_dir/<wrapper>
    cfg.dist_dir.mkdir(parents=True, exist_ok=True)
    return BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=BuildOptions(),
        runtime_dir=tmp_path / "dist" / "runtime",
    )


def test_max_loader_workers_constant() -> None:
    """_MAX_LOADER_WORKERS 常量值为 4（平衡并行收益与资源限制）."""
    assert _MAX_LOADER_WORKERS == 4


def test_build_entry_loaders_parallel_multi_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """4 入口并行编译：所有 exe/wrapper/.entry 生成，顺序与 entries 一致."""
    ctx = _make_multi_entry_context(tmp_path)
    work_dirs: list[Path] = []

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        work_dirs.append(work_dir)
        return out_exe

    monkeypatch.setattr("fspack.packaging.pipeline.stages.compile_loader", fake_compile)

    exes = _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert len(exes) == 4
    # exes 顺序与 entries 一致（按 submit 顺序取 result）
    assert [e.name for e in ctx.info.all_entries] == ["cli", "gui", "web", "api"]
    for ep in ctx.info.all_entries:
        exe_name = f"{ep.name}.exe"
        assert (ctx.cfg.dist_dir / exe_name).is_file()
        wrapper = ctx.cfg.dist_dir / f"_entry_{ep.name}.py"
        assert wrapper.is_file()
        assert "fspack 生成的入口包装器" in wrapper.read_text(encoding="utf-8")
        entry_file = ctx.cfg.dist_dir / f"{ep.name}.entry"
        assert entry_file.is_file()
        assert entry_file.read_text(encoding="utf-8") == f"_entry_{ep.name}.py"
    # 每个入口独立子工作目录（避免 loader.c/icon.rc 冲突）
    assert len(work_dirs) == 4
    assert len({str(d) for d in work_dirs}) == 4
    # 所有子目录共享同一父目录（TemporaryDirectory）
    parents = {d.parent for d in work_dirs}
    assert len(parents) == 1


def test_build_entry_loaders_parallel_shared_work_dir_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并行编译共享 TemporaryDirectory：所有 work_dir 子目录在同一父目录下."""
    ctx = _make_multi_entry_context(tmp_path, ("a", "b", "c"))
    work_dirs: list[Path] = []

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        work_dirs.append(work_dir)
        return out_exe

    monkeypatch.setattr("fspack.packaging.pipeline.stages.compile_loader", fake_compile)

    _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert len(work_dirs) == 3
    parents = {d.parent for d in work_dirs}
    assert len(parents) == 1, f"所有 work_dir 应共享父目录，实际: {parents}"
    # 子目录名与入口名一致
    assert {d.name for d in work_dirs} == {"a", "b", "c"}


def test_build_entry_loaders_parallel_exception_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 内 compile_loader 抛 LoaderError 时 future.result() 重抛."""
    ctx = _make_multi_entry_context(tmp_path, ("ok1", "fail", "ok2"))
    call_count = [0]

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        call_count[0] += 1
        if out_exe.stem == "fail":
            raise LoaderError("模拟编译失败")
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        return out_exe

    monkeypatch.setattr("fspack.packaging.pipeline.stages.compile_loader", fake_compile)

    with pytest.raises(LoaderError, match="模拟编译失败"):
        _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)


def test_build_entry_loaders_parallel_max_workers_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_workers = min(cpu_count, _MAX_LOADER_WORKERS)，cpu > 4 时 cap 为 4."""
    from concurrent.futures import ThreadPoolExecutor

    ctx = _make_multi_entry_context(tmp_path, ("a", "b", "c", "d", "e"))
    captured: list[int] = []

    class _SpyPool(ThreadPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            mw = kwargs.get("max_workers", args[0] if args else 1)
            captured.append(cast(int, mw))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("fspack.packaging.pipeline.stages.ThreadPoolExecutor", _SpyPool)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )
    monkeypatch.setattr(os, "cpu_count", lambda: 8)

    _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert len(captured) == 1
    assert captured[0] == _MAX_LOADER_WORKERS


def test_build_entry_loaders_parallel_max_workers_below_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cpu_count < _MAX_LOADER_WORKERS 时 max_workers = cpu_count."""
    from concurrent.futures import ThreadPoolExecutor

    ctx = _make_multi_entry_context(tmp_path, ("a", "b", "c"))
    captured: list[int] = []

    class _SpyPool(ThreadPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            mw = kwargs.get("max_workers", args[0] if args else 1)
            captured.append(cast(int, mw))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("fspack.packaging.pipeline.stages.ThreadPoolExecutor", _SpyPool)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )
    monkeypatch.setattr(os, "cpu_count", lambda: 2)

    _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert captured[0] == 2


def test_build_entry_loaders_single_entry_no_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单入口走串行路径，不创建 ThreadPoolExecutor."""
    from concurrent.futures import ThreadPoolExecutor

    # 单入口：entries 为空，all_entries 构造单一入口
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    from fspack.config import BuildConfig, ProjectInfo
    from fspack.progress import BuildTracker

    info = ProjectInfo.from_dir(tmp_path, "3.11.9")
    cfg = BuildConfig(
        project_dir=tmp_path,
        dist_dir=tmp_path / "dist",
        embed_cache_dir=tmp_path / "cache",
        mirror=get_mirror("huawei"),
        target=Platform.WINDOWS,
    )
    cfg.dist_dir.mkdir(parents=True, exist_ok=True)
    ctx = BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=BuildOptions(),
        runtime_dir=tmp_path / "dist" / "runtime",
    )

    pool_created = [False]
    original_init = ThreadPoolExecutor.__init__

    def spy_init(self: ThreadPoolExecutor, *args: Any, **kwargs: Any) -> None:
        pool_created[0] = True
        original_init(self, *args, **kwargs)

    monkeypatch.setattr("fspack.packaging.pipeline.stages.ThreadPoolExecutor", spy_init)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    exes = _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert len(exes) == 1
    assert not pool_created[0], "单入口不应创建 ThreadPoolExecutor"
    assert (ctx.cfg.dist_dir / "app.exe").is_file()
    assert (ctx.cfg.dist_dir / ".entry").is_file()


def test_build_entry_loaders_parallel_preserves_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并行编译后 exes 顺序与 all_entries 一致（按 submit 顺序取 result）."""
    names = ("alpha", "beta", "gamma", "delta", "epsilon")
    ctx = _make_multi_entry_context(tmp_path, names)

    # 模拟不同编译耗时，确保完成顺序与提交顺序不同
    import time

    compile_times = dict(zip(names, [0.05, 0.01, 0.04, 0.02, 0.03]))

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        time.sleep(compile_times.get(out_exe.stem, 0.01))
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        return out_exe

    monkeypatch.setattr("fspack.packaging.pipeline.stages.compile_loader", fake_compile)

    exes = _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert [e.stem for e in exes] == list(names), "exes 顺序应与 entries 提交顺序一致"


# --- 配置分类去重测试：同 (app_type, version_info) 只编译代表，其余复制 ---


def _spy_compile(monkeypatch: pytest.MonkeyPatch, calls: list[Path], content: str = "compiled-loader") -> None:
    """记录 compile_loader 调用时的 out_exe，模拟编译产物写入."""

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        calls.append(out_exe)
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(content)
        return out_exe

    monkeypatch.setattr("fspack.packaging.pipeline.stages.compile_loader", fake_compile)


def test_build_entry_loaders_dedup_same_app_type_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标同 app_type 多入口分类去重：只编译代表一次，其余入口复制代表 exe."""
    ctx = _make_multi_entry_context(tmp_path, ("a", "b", "c"), target=Platform.LINUX)
    calls: list[Path] = []
    _spy_compile(monkeypatch, calls)

    exes = _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert len(exes) == 3
    assert len(calls) == 1, "同 app_type 多入口只应编译代表入口一次"
    for exe in exes:
        assert exe.is_file()
        assert exe.read_text() == "compiled-loader", "同组入口 exe 应与代表产物一致"


def test_build_entry_loaders_dedup_group_by_app_type_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标不同 app_type 分属不同配置类：各编译一次，GUI/CLI 互不复制."""
    ctx = _make_multi_entry_context(tmp_path, ("a", "gui", "b"), target=Platform.LINUX)
    calls: list[Path] = []
    _spy_compile(monkeypatch, calls)

    exes = _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert len(exes) == 3
    assert len(calls) == 2, "CLI 与 GUI 两个配置类各编译一次"
    # 代表产物：a（CLI）与 gui（GUI）被编译，b 从 a 复制
    assert {c.name for c in calls} == {"a", "gui"}


def test_build_entry_loaders_dedup_all_same_config_no_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标全部入口同配置类时走串行路径，不创建 ThreadPoolExecutor."""
    from concurrent.futures import ThreadPoolExecutor

    ctx = _make_multi_entry_context(tmp_path, ("a", "b", "c"), target=Platform.LINUX)
    calls: list[Path] = []
    _spy_compile(monkeypatch, calls)

    pool_created = [False]
    original_init = ThreadPoolExecutor.__init__

    def spy_init(self: ThreadPoolExecutor, *args: Any, **kwargs: Any) -> None:
        pool_created[0] = True
        original_init(self, *args, **kwargs)

    monkeypatch.setattr("fspack.packaging.pipeline.stages.ThreadPoolExecutor", spy_init)
    _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert len(calls) == 1
    assert not pool_created[0], "单配置类不应创建 ThreadPoolExecutor"


def test_build_entry_loaders_windows_compiles_per_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标 version_info.exe_filename 按入口区分：多入口仍每入口编译（方案 A 边界）."""
    ctx = _make_multi_entry_context(tmp_path, ("a", "b", "c"), target=Platform.WINDOWS)
    calls: list[Path] = []
    _spy_compile(monkeypatch, calls)

    exes = _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    assert len(exes) == 3
    assert len(calls) == 3, "Windows 每个 exe 资源段（OriginalFilename）不同，不做构建内去重"


def test_build_entry_loaders_dedup_entries_files_still_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """去重不影响入口文件生成：每个入口的 wrapper 与 .entry 仍全部写出."""
    ctx = _make_multi_entry_context(tmp_path, ("a", "b", "c"), target=Platform.LINUX)
    calls: list[Path] = []
    _spy_compile(monkeypatch, calls)

    _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)

    for ep in ctx.info.all_entries:
        wrapper = ctx.cfg.dist_dir / f"_entry_{ep.name}.py"
        assert wrapper.is_file(), f"{ep.name} 的 wrapper 必须写出"
        assert "fspack 生成的入口包装器" in wrapper.read_text(encoding="utf-8")
        entry_file = ctx.cfg.dist_dir / f"{ep.name}.entry"
        assert entry_file.is_file(), f"{ep.name} 的 .entry 必须写出"
        assert entry_file.read_text(encoding="utf-8") == f"_entry_{ep.name}.py"


def test_build_entry_loaders_dedup_representative_failure_skips_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux 目标代表编译失败时重抛异常，同组入口不执行复制."""
    ctx = _make_multi_entry_context(tmp_path, ("a", "b"), target=Platform.LINUX)
    copied: list[Path] = []

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        raise LoaderError("模拟编译失败")

    def fake_copy2(src: Path, dst: Path) -> None:
        copied.append(dst)

    monkeypatch.setattr("fspack.packaging.pipeline.stages.compile_loader", fake_compile)
    monkeypatch.setattr("fspack.packaging.pipeline.compile_stage.shutil.copy2", fake_copy2)

    with pytest.raises(LoaderError, match="模拟编译失败"):
        _build_entry_loaders(ctx, resolved_icon=None, has_tkinter=False)
    assert not copied, "代表编译失败时不应复制同组入口"
