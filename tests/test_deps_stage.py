"""依赖阶段测试：site-packages 探测、wheel 解包与依赖分析缓存（deps_stage.py）."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Callable, cast

import pytest

from fspack.builder import (
    _dep_cache_load,
    _dep_cache_path,
    _dep_cache_save,
    _site_packages_has_deps,
    build,
    unpack_wheels,
)
from fspack.config import DependencyReport, get_mirror
from fspack.exceptions import DependencyError
from fspack.platform import Platform
from tests._stubs import CompletedStub, setup_embed_mocks


def test_site_packages_has_deps_true(tmp_path: Path) -> None:
    """声明的依赖均在 site-packages 中已安装时返回 True."""
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "numpy-1.0.dist-info").mkdir()
    assert _site_packages_has_deps(sp, ["numpy"]) is True
    # 含版本 specifier 也应匹配
    assert _site_packages_has_deps(sp, ["numpy>=1.0"]) is True


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
    # 无用户依赖时（packages 为空）vacuously True
    assert _site_packages_has_deps(sp, []) is True


def test_site_packages_has_deps_false_empty(tmp_path: Path) -> None:
    """site-packages 为空目录时返回 False."""
    sp = tmp_path / "sp"
    sp.mkdir()
    assert _site_packages_has_deps(sp, ["numpy"]) is False


def test_site_packages_has_deps_false_no_dir(tmp_path: Path) -> None:
    """site-packages 不存在时返回 False."""
    assert _site_packages_has_deps(tmp_path / "nonexistent", ["numpy"]) is False


def test_site_packages_has_deps_name_normalization(tmp_path: Path) -> None:
    """包名规范化：``-``/``_``/``.`` 互通，大小写不敏感."""
    sp = tmp_path / "sp"
    sp.mkdir()
    (sp / "ordered_set-1.1.dist-info").mkdir()
    # 导入名 orderedset 与 PyPI 名 ordered-set 不匹配（无分隔符），但
    # ordered_set / ordered-set / Ordered.Set 均规范化为 ordered-set 互通
    assert _site_packages_has_deps(sp, ["ordered-set"]) is True
    assert _site_packages_has_deps(sp, ["ordered.set"]) is True
    assert _site_packages_has_deps(sp, ["Ordered_Set"]) is True


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
    original_from_src = cast(Callable[..., DependencyReport], DependencyReport.from_src.__func__)

    def tracking_from_src(cls: Any, *args: Any, **kwargs: Any) -> DependencyReport:
        nonlocal analyze_called
        analyze_called = True
        return original_from_src(*args, **kwargs)

    monkeypatch.setattr("fspack.config.DependencyReport.from_src", classmethod(tracking_from_src))
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert not analyze_called, "缓存命中时不应调用 AST 分析"
