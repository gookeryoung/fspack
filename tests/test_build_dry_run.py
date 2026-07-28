"""``--dry-run`` 预览模式单元测试.

覆盖 :func:`fspack.packaging.pipeline.build` 的 ``dry_run=True`` 分支与
:func:`fspack.packaging.pipeline._print_build_plan` 行为：

- ``dry_run=True``：仅执行项目解析与依赖分析，不触发下载/解压/复制/编译等写操作
- ``_print_build_plan``：渲染项目信息/依赖分析/构建选项表格不抛异常
- CLI 层：``--dry-run`` 标志正确解析并透传给 :func:`build`
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from fspack import cli
from fspack.config import get_mirror
from fspack.console import console
from fspack.packaging.pipeline import BuildContext, _print_build_plan, build
from fspack.platform import Platform

_EXAMPLES = Path(__file__).parent.parent / "examples"


# ---- pipeline.build(dry_run=True) 不执行写操作 ----


def test_build_dry_run_skips_runtime_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 不调用 download_embed/extract_embed."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    download_called = False
    extract_called = False

    def fake_download(*a: Any, **kw: Any) -> Path:
        nonlocal download_called
        download_called = True
        return tmp_path / "fake.zip"

    def fake_extract(*a: Any, **kw: Any) -> None:
        nonlocal extract_called
        extract_called = True

    monkeypatch.setattr("fspack.packaging.pipeline.download_embed", fake_download)
    monkeypatch.setattr("fspack.packaging.pipeline.extract_embed", fake_extract)
    monkeypatch.setattr("fspack.packaging.pipeline.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.packaging.pipeline.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.compile_loader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应编译 loader")),
    )
    monkeypatch.setattr(
        "fspack.packaging.pipeline.copy_source",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应复制源码")),
    )

    with console.rich.capture():
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, dry_run=True)

    assert not download_called
    assert not extract_called
    # dist 目录不应被创建（dry-run 不写盘）
    assert not (proj / "dist").exists()


def test_build_dry_run_skips_linux_runtime_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 在 Linux 目标也不调用 download_standalone/extract_standalone."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    download_called = False
    extract_called = False

    def fake_download(*a: Any, **kw: Any) -> Path:
        nonlocal download_called
        download_called = True
        return tmp_path / "fake.tar.gz"

    def fake_extract(*a: Any, **kw: Any) -> None:
        nonlocal extract_called
        extract_called = True

    monkeypatch.setattr("fspack.packaging.pipeline.download_standalone", fake_download)
    monkeypatch.setattr("fspack.packaging.pipeline.extract_standalone", fake_extract)
    monkeypatch.setattr("fspack.packaging.pipeline.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.packaging.pipeline.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.compile_loader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应编译 loader")),
    )
    monkeypatch.setattr(
        "fspack.packaging.pipeline.copy_source",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应复制源码")),
    )

    with console.rich.capture():
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX, dry_run=True)

    assert not download_called
    assert not extract_called
    assert not (proj / "dist").exists()


def test_build_dry_run_returns_project_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 返回 ProjectInfo，字段与项目配置一致."""
    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj)

    # 拦截所有写操作
    for fn in ("download_embed", "download_standalone", "download_wheels"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: tmp_path / "fake")
    for fn in ("extract_embed", "extract_standalone", "unpack_wheels", "copy_source"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: None)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.compile_loader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应编译 loader")),
    )

    with console.rich.capture():
        info = build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, dry_run=True)

    assert info.name == "cli_helloworld_pyall"
    assert info.version == "0.1.0"
    assert info.py_version == "3.11.9"


def test_build_dry_run_prints_plan_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 输出 '打包计划就绪' 汇总与 dry-run 提示."""
    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj)

    for fn in ("download_embed", "download_standalone", "download_wheels"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: tmp_path / "fake")
    for fn in ("extract_embed", "extract_standalone", "unpack_wheels", "copy_source"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: None)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.compile_loader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应编译 loader")),
    )

    with console.rich.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, dry_run=True)

    out = capture.get()
    assert "打包计划" in out
    assert "项目信息" in out
    assert "依赖分析" in out
    assert "构建选项" in out
    assert "打包计划就绪" in out
    assert "dry-run" in out.lower()
    assert "cli_helloworld_pyall" in out
    assert "3.11.9" in out
    assert "windows" in out.lower()


def test_build_dry_run_includes_missing_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 在依赖分析表中显示 AST 发现但未声明的依赖（missing）."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    # 源码 import rich 但 pyproject.toml 未声明 → missing
    (proj / "app.py").write_text(
        "import rich\n\n\ndef main():\n    print('hi')\n",
        encoding="utf-8",
    )

    for fn in ("download_embed", "download_wheels"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: tmp_path / "fake")
    for fn in ("extract_embed", "unpack_wheels", "copy_source"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: None)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.compile_loader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应编译 loader")),
    )

    with console.rich.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, dry_run=True)

    out = capture.get()
    assert "rich" in out  # missing 列应出现 rich
    assert "未声明" in out


def test_build_dry_run_no_write_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 不创建 dist/runtime、dist/src、dist/.entry 等任何产物."""
    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj)

    for fn in ("download_embed", "download_wheels"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: tmp_path / "fake")
    for fn in ("extract_embed", "unpack_wheels", "copy_source"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: None)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.compile_loader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应编译 loader")),
    )

    with console.rich.capture():
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, dry_run=True)

    dist = proj / "dist"
    assert not dist.exists()


def test_build_dry_run_merges_private_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 仍合并 CLI 私有包源到 info（在解析阶段完成，dry-run 不绕过）."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n[tool.fspack]\nextra-index-urls = ["https://config.example.com/"]\n',
        encoding="utf-8",
    )
    (proj / "app.py").write_text("def main():\n    pass\n")

    for fn in ("download_embed", "download_wheels"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: tmp_path / "fake")
    for fn in ("extract_embed", "unpack_wheels", "copy_source"):
        monkeypatch.setattr(f"fspack.packaging.pipeline.{fn}", lambda *a, **k: None)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.compile_loader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应编译 loader")),
    )

    with console.rich.capture() as capture:
        info = build(
            proj,
            get_mirror("huawei"),
            "3.11.9",
            target=Platform.WINDOWS,
            dry_run=True,
            extra_index_urls=("https://cli.example.com/",),
        )

    assert "https://config.example.com/" in info.extra_index_urls
    assert "https://cli.example.com/" in info.extra_index_urls
    # 配置在前，CLI 追加在后
    assert info.extra_index_urls.index("https://config.example.com/") < info.extra_index_urls.index(
        "https://cli.example.com/"
    )
    out = capture.get()
    assert "https://config.example.com/" in out
    assert "https://cli.example.com/" in out


# ---- _print_build_plan 单元测试 ----


def _make_build_context(tmp_path: Path, *, target: Platform = Platform.WINDOWS) -> BuildContext:
    """构造最小可用的 BuildContext 用于 _print_build_plan 测试."""
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
    return BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=BuildOptions(),
        runtime_dir=tmp_path / "dist" / "runtime",
    )


def test_print_build_plan_renders_without_error(tmp_path: Path) -> None:
    """_print_build_plan 渲染基本表格不抛异常."""
    ctx = _make_build_context(tmp_path)
    from fspack.config import DependencyReport

    report = DependencyReport(
        declared=("requests",),
        ast_third_party=("requests",),
        ast_stdlib=("os",),
        ast_local=(),
        ast_submodules={},
    )

    with console.rich.capture() as capture:
        _print_build_plan(ctx, report)

    out = capture.get()
    assert "打包计划" in out
    assert "项目信息" in out
    assert "依赖分析" in out
    assert "构建选项" in out
    assert "app" in out
    assert "0.1" in out
    assert "3.11.9" in out
    assert "windows" in out.lower()


def test_print_build_plan_linux_target(tmp_path: Path) -> None:
    """_print_build_plan 在 Linux 目标下显示 python-build-standalone 与 gcc."""
    ctx = _make_build_context(tmp_path, target=Platform.LINUX)
    from fspack.config import DependencyReport

    report = DependencyReport(
        declared=(),
        ast_third_party=(),
        ast_stdlib=(),
        ast_local=(),
        ast_submodules={},
    )

    with console.rich.capture() as capture:
        _print_build_plan(ctx, report)

    out = capture.get()
    assert "python-build-standalone" in out
    assert "gcc" in out
    assert "linux" in out.lower()


def test_print_build_plan_shows_nuitka_when_enabled(tmp_path: Path) -> None:
    """_print_build_plan 在 opts.nuitka=True 时显示 Nuitka 选项行."""
    from dataclasses import replace

    from fspack.config import BuildOptions, DependencyReport

    ctx = _make_build_context(tmp_path)
    ctx = replace(ctx, opts=BuildOptions(nuitka=True, ccache=True, nuitka_packages=("requests",)))
    report = DependencyReport(
        declared=(),
        ast_third_party=(),
        ast_stdlib=(),
        ast_local=(),
        ast_submodules={},
    )

    with console.rich.capture() as capture:
        _print_build_plan(ctx, report)

    out = capture.get()
    assert "Nuitka" in out
    assert "ccache" in out
    assert "requests" in out


def test_print_build_plan_shows_private_sources(tmp_path: Path) -> None:
    """_print_build_plan 在 info 含私有包源时显示 '私有包源' 表."""
    from dataclasses import replace

    from fspack.config import DependencyReport

    ctx = _make_build_context(tmp_path)
    info = replace(
        ctx.info,
        extra_index_urls=("https://pypi.company.com/simple/",),
        find_links=("./wheels",),
    )
    ctx = replace(ctx, info=info)
    report = DependencyReport(
        declared=(),
        ast_third_party=(),
        ast_stdlib=(),
        ast_local=(),
        ast_submodules={},
    )

    with console.rich.capture() as capture:
        _print_build_plan(ctx, report)

    out = capture.get()
    assert "私有包源" in out
    assert "https://pypi.company.com/simple/" in out
    assert "./wheels" in out


# ---- CLI 层 --dry-run 标志 ----


def _make_minimal_project(tmp_path: Path) -> Path:
    """创建最小可解析项目（pyproject.toml + 入口脚本）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return tmp_path


def test_cli_build_dry_run_flag_passed_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp b --dry-run`` 透传 dry_run=True 给 build()."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
    ) -> None:
        captured["dry_run"] = dry_run

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--dry-run"])
    assert captured["dry_run"] is True


def test_cli_build_without_dry_run_flag_defaults_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --dry-run 时 dry_run=False（默认行为）."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
    ) -> None:
        captured["dry_run"] = dry_run

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert captured["dry_run"] is False


def test_cli_build_dry_run_alias_b(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp b --dry-run`` 别名 b 同样透传 dry_run=True."""
    _make_minimal_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
    ) -> None:
        captured["dry_run"] = dry_run

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--dry-run"])
    assert captured["dry_run"] is True
