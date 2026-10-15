"""依赖阶段测试：site-packages 探测、wheel 解包与依赖分析缓存（deps_stage.py）."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from fspack.builder import (
    _dep_cache_load,
    _dep_cache_path,
    _dep_cache_save,
    _site_packages_has_deps,
    build,
    unpack_wheels,
)
from fspack.config import BuildConfig, BuildOptions, DependencyReport, ProjectInfo, SlimRules, get_mirror
from fspack.exceptions import DependencyError
from fspack.packaging.pipeline.context import BuildContext
from fspack.packaging.pipeline.deps_stage import _download_dependencies, _save_installed_dependencies
from fspack.platform import Platform
from fspack.progress import BuildTracker
from tests._stubs import CompletedStub, setup_embed_mocks


def test_site_packages_metadata_alone_is_not_cache_proof(tmp_path: Path) -> None:
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "numpy-1.0.dist-info").mkdir()
    assert not _site_packages_has_deps(sp, ["numpy"])
    assert not _site_packages_has_deps(sp, ["numpy>=999.0"], build_key="key")


def test_site_packages_has_deps_false_missing_pkg(tmp_path: Path) -> None:
    """声明依赖未全部安装时返回 False（即使 site-packages 有其他 dist-info）."""
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "numpy-1.0.dist-info").mkdir()
    # pygame 未安装
    assert _site_packages_has_deps(sp, ["numpy", "pygame"]) is False


def test_site_packages_has_deps_false_ignores_pip(tmp_path: Path) -> None:
    """仅有预装的 pip dist-info 时，对用户依赖返回 False.

    python-build-standalone 与 embed python 均预装 pip（含 ``pip-*.dist-info``），
    不能因预装 pip 就误判用户依赖已安装。
    """
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "pip-26.1.2.dist-info").mkdir()
    assert _site_packages_has_deps(sp, ["pygame"]) is False
    assert _site_packages_has_deps(sp, []) is False


def test_site_packages_has_deps_false_empty(tmp_path: Path) -> None:
    """site-packages 为空目录时返回 False."""
    sp = tmp_path / "sp"
    sp.mkdir()
    assert _site_packages_has_deps(sp, ["numpy"]) is False


def test_site_packages_has_deps_false_no_dir(tmp_path: Path) -> None:
    """site-packages 不存在时返回 False."""
    assert _site_packages_has_deps(tmp_path / "nonexistent", ["numpy"]) is False


@pytest.mark.parametrize("change", ["none", "version", "key", "missing", "modified", "extra", "failed"])
def test_installed_state_validation(tmp_path: Path, change: str) -> None:
    from fspack.packaging.pipeline.deps_stage import _installed_fingerprint

    sp = tmp_path / "site-packages"
    sp.mkdir()
    artifact = sp / "numpy.py"
    artifact.write_bytes(b"original")
    payload = {"installed": {"key": "key", "packages": ["numpy>=1.0"], "fingerprint": _installed_fingerprint(sp)}}
    _dep_cache_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    if change == "missing":
        artifact.unlink()
    elif change == "modified":
        artifact.write_bytes(b"changed payload")
    elif change == "extra":
        (sp / "obsolete.dll").write_bytes(b"x")
    elif change == "failed":
        (tmp_path / ".build_failed").write_text("failed")
    packages = ["numpy>=999.0"] if change == "version" else ["numpy>=1.0"]
    key = "other" if change == "key" else "key"
    assert _site_packages_has_deps(sp, packages, build_key=key) is (change == "none")


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


# ---- 依赖分析缓存 ----


def _make_report() -> DependencyReport:
    """构造测试用 DependencyReport."""
    return DependencyReport(
        declared=("rich",),
        ast_third_party=("rich",),
        ast_stdlib=("os", "sys"),
        ast_local=("app",),
        ast_submodules={"PySide2": frozenset({"QtCore", "QtWidgets"})},
    )


def test_dep_cache_save_and_load_roundtrip(tmp_path: Path) -> None:
    """缓存保存后加载应返回等价的 DependencyReport."""
    report = _make_report()
    fingerprint = "abc123"

    _dep_cache_save(tmp_path, fingerprint, report)
    loaded = _dep_cache_load(tmp_path, fingerprint, ("rich",))

    assert loaded is not None
    assert loaded.declared == report.declared
    assert loaded.ast_third_party == report.ast_third_party
    assert loaded.ast_stdlib == report.ast_stdlib
    assert loaded.ast_local == report.ast_local
    assert loaded.ast_submodules == report.ast_submodules


def test_dep_cache_load_miss_on_fingerprint_change(tmp_path: Path) -> None:
    """指纹变化时缓存失效返回 None."""
    report = _make_report()
    _dep_cache_save(tmp_path, "fp1", report)
    assert _dep_cache_load(tmp_path, "fp2", ("rich",)) is None


def test_dep_cache_load_miss_on_declared_change(tmp_path: Path) -> None:
    """声明依赖变化时缓存失效返回 None."""
    report = _make_report()
    _dep_cache_save(tmp_path, "fp1", report)
    assert _dep_cache_load(tmp_path, "fp1", ("rich", "click")) is None


def test_dep_cache_load_miss_on_no_cache(tmp_path: Path) -> None:
    """缓存文件不存在时返回 None."""
    assert _dep_cache_load(tmp_path, "fp", ()) is None


def test_dep_cache_load_miss_on_corrupt_json(tmp_path: Path) -> None:
    """损坏的 JSON 文件返回 None 而非抛异常."""
    cache = _dep_cache_path(tmp_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{invalid json", encoding="utf-8")
    assert _dep_cache_load(tmp_path, "fp", ()) is None


def test_build_dep_cache_hit_skips_ast_analysis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复构建时分析依赖缓存命中，跳过 AST 分析."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\ndependencies = ["rich"]\n')
    (proj / "app.py").write_text("import rich\n\ndef main():\n    pass\n")

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())

    # 第一次构建：生成缓存
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    cache = _dep_cache_path(proj / "dist")
    assert cache.is_file(), "第一次构建应生成 .dep_cache.json"

    # 第二次构建：缓存命中
    analyze_called = False
    # cast 类型参数用字符串前向引用：3.8 的 collections.abc.Callable 不支持下标，
    # 非字符串形式会在运行时求值 TypeError（cast(str_type, ...) 不求值）
    original_from_src = cast("Callable[..., DependencyReport]", DependencyReport.from_src.__func__)

    def tracking_from_src(cls: Any, *args: Any, **kwargs: Any) -> DependencyReport:
        nonlocal analyze_called
        analyze_called = True
        return original_from_src(*args, **kwargs)

    monkeypatch.setattr("fspack.config.DependencyReport.from_src", classmethod(tracking_from_src))
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert not analyze_called, "缓存命中时不应调用 AST 分析"


@pytest.fixture
def dependency_context(tmp_path: Path) -> tuple[BuildContext, DependencyReport, Path]:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    (dist / "site-packages").mkdir(parents=True)
    ctx = BuildContext(
        tracker=BuildTracker(),
        info=ProjectInfo.from_dir(tmp_path, "3.11.9"),
        cfg=BuildConfig(tmp_path, dist, tmp_path / "cache", get_mirror("huawei"), Platform.WINDOWS),
        opts=BuildOptions(),
        runtime_dir=dist / "runtime",
    )
    report = DependencyReport(declared=("demo>=1.0",), ast_third_party=(), ast_stdlib=(), ast_local=())
    _dep_cache_save(dist, "fingerprint", report)
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("demo/__init__.py", "")
        zf.writestr("demo/module.py", "value = 1\n")
        zf.writestr("demo/extra.dat", "payload")
        zf.writestr("demo-1.0.dist-info/METADATA", "Name: demo\nVersion: 1.0\n")
    return ctx, report, wheel


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "version",
        "python",
        "platform",
        "index",
        "extra_index",
        "links",
        "include",
        "exclude",
        "keep",
        "usage",
        "pyc",
        "site",
        "hashes",
        "missing",
    ],
)
def test_dependency_reuse_identity(  # noqa: PLR0912 - 15 个身份要素变体共用一套分支构造差异
    dependency_context: tuple[BuildContext, DependencyReport, Path],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    ctx, report, wheel = dependency_context
    calls: list[dict[str, Any]] = []

    def download(*args: Any, **kwargs: Any) -> list[Path]:
        calls.append(kwargs)
        return [wheel]

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", download)
    sp = ctx.cfg.dist_dir / "site-packages"
    _download_dependencies(ctx, sp, report)
    _save_installed_dependencies(ctx, sp, report)
    if change == "version":
        report = replace(report, declared=("demo>=999.0",))
    elif change == "python":
        ctx = replace(ctx, info=replace(ctx.info, py_version="3.12.1"))
    elif change == "platform":
        ctx = replace(ctx, cfg=replace(ctx.cfg, target=Platform.LINUX))
    elif change == "index":
        ctx = replace(ctx, cfg=replace(ctx.cfg, mirror=get_mirror("tsinghua")))
    elif change == "extra_index":
        ctx = replace(ctx, info=replace(ctx.info, extra_index_urls=("https://example.com/simple/",)))
    elif change == "links":
        ctx = replace(ctx, info=replace(ctx.info, find_links=("./wheels",)))
    elif change == "include":
        ctx = replace(ctx, info=replace(ctx.info, slim_rules=SlimRules(include=("demo/*",))))
    elif change == "exclude":
        ctx = replace(ctx, info=replace(ctx.info, slim_rules=SlimRules(exclude=("demo/extra.dat",))))
    elif change == "keep":
        ctx = replace(ctx, opts=replace(ctx.opts, keep_modules={"demo.module"}))
    elif change == "usage":
        report = replace(report, ast_submodules={"demo": frozenset({"module"})})
    elif change == "pyc":
        ctx = replace(ctx, opts=replace(ctx.opts, pyc_strip=True))
    elif change == "site":
        ctx = replace(ctx, opts=replace(ctx.opts, no_site=True))
    elif change == "hashes":
        ctx = replace(ctx, opts=replace(ctx.opts, require_hashes=True))
    elif change == "missing":
        (sp / "demo/module.py").unlink()
    stamp = ctx.cfg.dist_dir / ".pyc_stamp"
    stamp.write_text("old stamp")
    _download_dependencies(ctx, sp, report)
    assert len(calls) == (1 if change == "none" else 2)
    assert stamp.exists() is (change == "none")
    assert (sp / "demo/module.py").is_file()
    assert (sp / "demo/extra.dat").exists() is (change != "exclude")
    assert calls[-1]["require_hashes"] is (change == "hashes")


def test_dependency_removal_clears_old_files(
    dependency_context: tuple[BuildContext, DependencyReport, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, report, wheel = dependency_context
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", lambda *a, **kw: [wheel])
    sp = ctx.cfg.dist_dir / "site-packages"
    _download_dependencies(ctx, sp, report)
    _save_installed_dependencies(ctx, sp, report)
    _download_dependencies(ctx, sp, DependencyReport(declared=(), ast_third_party=(), ast_stdlib=(), ast_local=()))
    assert list(sp.iterdir()) == []


def test_dependency_transformed_outputs_reuse_final_state(
    dependency_context: tuple[BuildContext, DependencyReport, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, report, wheel = dependency_context
    ctx = replace(ctx, opts=replace(ctx.opts, pyc_strip=True))
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", lambda *a, **kw: [wheel])
    sp = ctx.cfg.dist_dir / "site-packages"
    _download_dependencies(ctx, sp, report)
    (sp / "demo/module.py").unlink()
    (sp / "demo/module.pyc").write_bytes(b"compiled artifact")
    _save_installed_dependencies(ctx, sp, report)

    def unexpected_download(*a: Any, **kw: Any) -> list[Path]:
        pytest.fail("unchanged final outputs must reuse dependencies")

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", unexpected_download)
    _download_dependencies(ctx, sp, report)
    assert (sp / "demo/module.pyc").is_file()
    assert not (sp / "demo/module.py").exists()


@pytest.mark.parametrize("failure", ["unpack", "replace"])
def test_dependency_refresh_failure_preserves_old_files(
    dependency_context: tuple[BuildContext, DependencyReport, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    ctx, report, wheel = dependency_context
    sp = ctx.cfg.dist_dir / "site-packages"
    (sp / "previous.py").write_bytes(b"keep old tree")
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", lambda *a, **kw: [wheel])
    if failure == "unpack":

        def fail_unpack(wheels: object, dest: Path, *args: Any, **kwargs: Any) -> None:
            (dest / "partial.py").write_bytes(b"partial")
            raise OSError("unpack failed")

        monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", fail_unpack)
    else:
        original_replace = Path.replace

        def fail_replace(path: Path, target: Path) -> Path:
            if target == sp and path.name == "site-packages":
                raise OSError("replace failed")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="failed"):
        _download_dependencies(ctx, sp, report)
    assert (sp / "previous.py").read_bytes() == b"keep old tree"
    assert not (sp / "demo").exists()
    assert not list(ctx.cfg.dist_dir.glob(".fspack-deps-*"))
