"""builder 流水线编排测试."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, cast

import pytest

from fspack.builder import (
    _dep_cache_load,
    _dep_cache_path,
    _dep_cache_save,
    _dir_size,
    _inject_win7_compat_dll,
    _needs_win7_compat_dll,
    _precompile_pyc,
    _site_packages_fingerprint,
    _site_packages_has_deps,
    _slim_runtime,
    _strip_elf_symbols,
    _strip_tcl_tk_counted,
    _sync_tree,
    _trim_standalone_runtime,
    _trim_stdlib,
    build,
    clean_dist,
    copy_source,
    fspack_wheel_cache_dir,
    unpack_wheels,
)
from fspack.config import AppType, BuildOptions, DependencyReport, EntryPoint, ProjectInfo, get_mirror
from fspack.console import console
from fspack.exceptions import DependencyError, FspackError, LoaderError
from fspack.packaging.pipeline import (
    _BUILD_FAILED,
    _BUILD_OK,
    _clean_dist_dir,
    _handle_dist_incomplete,
    _has_build_stamps,
    _load_build_failure,
    _remove_build_failure,
    _remove_build_ok,
    _save_build_failure,
    _save_build_ok,
)
from fspack.packaging.pipeline.frontend_stage import (
    _build_frontend,
    _detect_frontends,
    _frontend_prune_map,
    _is_wsl_windows_mount,
    _run_cmd,
)
from fspack.packaging.pipeline.stages import _MAX_LOADER_WORKERS, BuildContext, _build_entry_loaders
from fspack.platform import Platform
from fspack.progress import StageRecorder
from fspack.templates.project_template import ProjectTemplate

_EXAMPLES = ProjectTemplate.root_dir()


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
    (runtime_dir.parent / "site-packages").mkdir(parents=True)

    download_called = False
    extract_called = False

    def fake_download_embed(*a: Any, **kw: Any) -> Path:
        nonlocal download_called
        download_called = True
        return tmp_path / "fake.zip"

    def fake_extract_embed(*a: Any, **kw: Any) -> None:
        nonlocal extract_called
        extract_called = True

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", fake_download_embed)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_embed", fake_extract_embed)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
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
    (runtime_dir.parent / "site-packages").mkdir(parents=True)

    download_called = False
    extract_called = False

    def fake_download_standalone(*a: Any, **kw: Any) -> Path:
        nonlocal download_called
        download_called = True
        return tmp_path / "fake.tar.gz"

    def fake_extract_standalone(*a: Any, **kw: Any) -> None:
        nonlocal extract_called
        extract_called = True

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_standalone", fake_download_standalone)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_standalone", fake_extract_standalone)
    # 守卫要求 Linux 目标在 Linux 构建机上（测试可在任意宿主运行）
    monkeypatch.setattr("fspack.packaging.pipeline.executor.detect_platform", lambda: Platform.LINUX)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )
    # mock 预编译阶段的 subprocess.run（Linux python3.11 二进制在 Windows 上无法执行）
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX)
    assert not download_called
    assert not extract_called


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


def test_build_forwards_keep_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build() 将 keep_modules 和 ast_submodules 透传给 unpack_wheels."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\nfrom requests import get\ndef main():\n    pass\n")

    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.download_embed",
        lambda v, m, c, **kw: tmp_path / "fake.zip",
    )
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir.parent / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.download_wheels",
        lambda packages, py_version, index, cache_dir, platform_tags=("win_amd64",), **kw: [],
    )

    captured: dict[str, Any] = {}

    def fake_unpack(wheels: object, sp: object, submodule_usage: object, keep_modules: object, **kw: Any) -> int:
        captured["submodule_usage"] = submodule_usage
        captured["keep_modules"] = keep_modules
        return 0

    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", fake_unpack)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    build(
        proj,
        get_mirror("huawei"),
        "3.11.9",
        target=Platform.WINDOWS,
        options=BuildOptions(keep_modules={"requests.adapters"}),
    )
    assert captured["keep_modules"] == {"requests.adapters"}
    assert isinstance(captured["submodule_usage"], dict)
    assert "requests" in captured["submodule_usage"]


def test_build_orchestration_tk_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无第三方依赖的真实模板（tk_app）走完整 Windows 编排."""
    proj = tmp_path / "tk_app"
    shutil.copytree(_EXAMPLES / "gui" / "tk_app", proj, ignore=shutil.ignore_patterns("dist", "__pycache__"))
    calls: dict[str, Any] = {}

    def fake_extract_embed(zip_path: object, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "python311.dll").write_bytes(b"")
        # embed 布局判据：python3XX.zip 存在（stdlib 载体），write_pth 据此写 zip 行
        (runtime_dir / "python311.zip").write_bytes(b"")
        (runtime_dir.parent / "site-packages").mkdir(parents=True, exist_ok=True)
        calls["extract"] = True

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_embed", fake_extract_embed)

    def fake_download(packages: object, py_version: str, index: str, cache_dir: Path, **kw: Any) -> list[Path]:
        calls["download"] = True
        return []

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", fake_download)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        calls["compile_source"] = source
        return out_exe

    monkeypatch.setattr("fspack.packaging.pipeline.stages.compile_loader", fake_compile)
    # tk_app 使用 tkinter：Windows embed 需补充内置库，mock 避免真实下载 standalone tarball
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.TkinterBundler.ensure",
        lambda runtime_dir, version, cache_dir, stage: None,
    )

    with console.rich.capture() as capture:
        info = build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert info.name == "tk_app"
    assert (proj / "dist" / "tk_app.exe").is_file()
    assert (proj / "dist" / "runtime" / "python311._pth").is_file()
    assert (proj / "dist" / "src" / "tk_app.py").is_file()
    assert (proj / "dist" / "runtime" / "python311.dll").is_file()
    assert (proj / "dist" / ".entry").is_file()
    assert (proj / "dist" / ".entry").read_text(encoding="utf-8") == "_entry_tk_app.py"
    wrapper = proj / "dist" / "_entry_tk_app.py"
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
        "fspack.packaging.pipeline.stages.download_embed",
        lambda v, m, c, **kw: tmp_path / "fake.zip",
    )
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir.parent / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    downloaded: dict[str, bool] = {}
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.download_wheels",
        lambda packages, py_version, index, cache_dir, platform_tags=("win_amd64",), **kw: (
            downloaded.__setitem__("called", True) or []
        ),
    )
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
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

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir.parent / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    captured: dict[str, Any] = {}

    def fake_download(
        packages: tuple[str, ...] | list[str], py_version: str, index: str, cache_dir: Path, **kw: Any
    ) -> list[Path]:
        captured["packages"] = tuple(packages)
        return []

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", fake_download)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    # 下载用的是 declared 的 PyPI 包名（ordered-set），而非 AST 扫描的导入名（orderedset）
    assert captured["packages"] == ("ordered-set", "lxml")


def test_build_merges_cli_private_sources_with_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build() 将 CLI 私有包源与 [tool.fspack] 配置合并（CLI 追加在后，去重）."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\ndependencies = ["requests"]\n\n'
        "[tool.fspack]\n"
        'extra-index-urls = ["https://pypi.company.com/simple/", "https://mirror.example.com/pypi"]\n'
        'find-links = ["./wheels"]\n'
    )
    (proj / "app.py").write_text("import requests\ndef main():\n    pass\n")

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir.parent / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    captured: dict[str, Any] = {}

    def fake_download(
        packages: tuple[str, ...] | list[str], py_version: str, index: str, cache_dir: Path, **kw: Any
    ) -> list[Path]:
        captured["extra_index_urls"] = kw.get("extra_index_urls", ())
        captured["find_links"] = kw.get("find_links", ())
        return []

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", fake_download)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    # CLI 追加一个新的 extra-index-url 与 find-links，与配置合并
    build(
        proj,
        get_mirror("huawei"),
        "3.11.9",
        target=Platform.WINDOWS,
        extra_index_urls=("https://pypi.company.com/simple/", "https://cli.example.com/pypi"),
        find_links=("./wheels", "./cli-wheels"),
    )
    # 去重保留首次出现：配置的在前，CLI 的在后
    assert captured["extra_index_urls"] == (
        "https://pypi.company.com/simple/",
        "https://mirror.example.com/pypi",
        "https://cli.example.com/pypi",
    )
    assert captured["find_links"] == ("./wheels", "./cli-wheels")


def test_build_passes_config_private_sources_without_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 CLI 私有包源时，build() 仅用 [tool.fspack] 配置的私有包源."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\ndependencies = ["requests"]\n\n'
        "[tool.fspack]\n"
        'extra-index-urls = ["https://pypi.company.com/simple/"]\n'
        'find-links = ["./wheels"]\n'
    )
    (proj / "app.py").write_text("import requests\ndef main():\n    pass\n")

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir.parent / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    captured: dict[str, Any] = {}

    def fake_download(
        packages: tuple[str, ...] | list[str], py_version: str, index: str, cache_dir: Path, **kw: Any
    ) -> list[Path]:
        captured["extra_index_urls"] = kw.get("extra_index_urls", ())
        captured["find_links"] = kw.get("find_links", ())
        return []

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", fake_download)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert captured["extra_index_urls"] == ("https://pypi.company.com/simple/",)
    assert captured["find_links"] == ("./wheels",)


def test_build_skips_download_when_site_packages_has_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """site-packages 已有 dist-info 时跳过下载解压，记录跳过数."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import requests\ndef main():\n    pass\n")

    def fake_extract_embed(zip_path: object, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "python311.dll").write_bytes(b"")
        sp = runtime_dir.parent / "site-packages"
        sp.mkdir(parents=True)
        (sp / "requests-2.31.0.dist-info").mkdir()

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_embed", fake_extract_embed)

    download_called = False

    def fake_download(*a: Any, **kw: Any) -> list[Path]:
        nonlocal download_called
        download_called = True
        return []

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", fake_download)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    with console.rich.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert not download_called
    out = capture.get()
    assert "已存在跳过" in out


def test_build_orchestration_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无第三方依赖的真实模板（tk_app）走完整 Linux 编排（standalone runtime）."""
    proj = tmp_path / "tk_app"
    shutil.copytree(_EXAMPLES / "gui" / "tk_app", proj, ignore=shutil.ignore_patterns("dist", "__pycache__"))
    calls: dict[str, Any] = {}

    def fake_extract_standalone(tar_path: object, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        major, minor = "3", "11"
        pydir = runtime_dir / "python"
        (pydir / "bin").mkdir(parents=True)
        (pydir / "bin" / f"python{major}.{minor}").write_text("")
        (pydir / "lib" / f"python{major}.{minor}" / "site-packages").mkdir(parents=True)
        calls["standalone"] = "3.11.9"

    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.download_standalone", lambda v, r, c, **kw: tmp_path / "fake.tar.gz"
    )
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_standalone", fake_extract_standalone)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.packaging.pipeline.stages.unpack_wheels", lambda *a, **k: 0)

    def fake_compile(source: str, out_exe: Path, app_type: object, work_dir: Path, platform: object, **kw: Any) -> Path:
        out_exe.parent.mkdir(parents=True, exist_ok=True)
        out_exe.write_text(source)
        calls["compile_platform"] = platform
        calls["compile_source"] = source
        return out_exe

    monkeypatch.setattr("fspack.packaging.pipeline.stages.compile_loader", fake_compile)
    # 守卫要求 Linux 目标在 Linux 构建机上（测试可在任意宿主运行）
    monkeypatch.setattr("fspack.packaging.pipeline.executor.detect_platform", lambda: Platform.LINUX)
    # mock 预编译阶段的 subprocess.run（Linux python3.11 二进制在 Windows 上无法执行）
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())

    info = build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX)
    assert info.name == "tk_app"
    assert (proj / "dist" / "tk_app").is_file()
    assert not (proj / "dist" / "tk_app.exe").exists()
    assert not (proj / "dist" / "runtime" / "python311._pth").exists()
    assert (proj / "dist" / "src" / "tk_app.py").is_file()
    assert (proj / "dist" / ".entry").is_file()
    assert (proj / "dist" / "_entry_tk_app.py").is_file()
    assert "standalone" in calls
    assert "dlopen" in calls["compile_source"]
    assert "libpython3.11.so" in calls["compile_source"]
    assert ".entry" in calls["compile_source"]


def test_build_supplements_tkinter_when_needed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AST 检出 tkinter 且目标 Windows 时触发 TkinterBundler.ensure，wrapper 注入 TCL/TK 环境变量."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import tkinter\ndef main():\n    pass\n")

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
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

    ensure_called: dict[str, bool] = {}
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.TkinterBundler.ensure",
        lambda runtime_dir, version, cache_dir, stage: ensure_called.__setitem__("called", True),
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert ensure_called.get("called") is True
    # wrapper 应注入 `if True:` 启用 Tcl/Tk 环境变量
    wrapper = (proj / "dist" / "_entry_app.py").read_text(encoding="utf-8")
    assert "if True:" in wrapper
    assert "TCL_LIBRARY" in wrapper


def test_build_skips_tkinter_when_not_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AST 未检出 tkinter 时不触发 TkinterBundler.ensure，wrapper 注入 `if False:`."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import os\ndef main():\n    pass\n")

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
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

    ensure_called: dict[str, bool] = {}
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.TkinterBundler.ensure",
        lambda runtime_dir, version, cache_dir, stage: ensure_called.__setitem__("called", True),
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert ensure_called.get("called") is None
    wrapper = (proj / "dist" / "_entry_app.py").read_text(encoding="utf-8")
    assert "if False:" in wrapper


def test_fspack_wheel_cache_dir_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """fspack wheel 缓存目录路径结构 ``~/.fspack/cache/wheels/``."""
    # 本机设置 FSPACK_CACHE_DIR 时会覆盖 home 推导，须隔离保证断言语义
    monkeypatch.delenv("FSPACK_CACHE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = fspack_wheel_cache_dir()
    assert result == tmp_path / ".fspack" / "cache" / "wheels"


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


def _setup_embed_mocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, py_version: str) -> None:
    """为 Windows embed 构建注入公共 mock（download/extract/wheels/loader/mingw dll）."""
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    parts = py_version.split(".", maxsplit=2)
    pyxy = f"python{parts[0]}{parts[1]}"
    monkeypatch.setattr(
        "fspack.packaging.pipeline.stages.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / f"{pyxy}.dll").write_bytes(b""),
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


def test_build_injects_win7_compat_dll_for_py39_plus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.11.9 + Windows 目标构建后 runtime 含 api-ms-win-core-path-l1-1-0.dll."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert (proj / "dist" / "runtime" / "api-ms-win-core-path-l1-1-0.dll").is_file()


def test_build_skips_win7_compat_dll_for_py38_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.8.10 + Windows 目标构建后 runtime 不含兼容 DLL（3.8 官方支持 Win7）."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.8.10")
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
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX)
    assert not (proj / "dist" / "runtime" / "api-ms-win-core-path-l1-1-0.dll").exists()


# ---- _trim_stdlib 测试 ----


def test_trim_stdlib_linux_strips_unwanted_dirs(tmp_path: Path) -> None:
    """Linux 模式剥离 test/ensurepip/idlelib/pydoc_data/turtledemo 等无用目录，保留有用模块."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    for d in ("test", "ensurepip", "idlelib", "pydoc_data", "turtledemo", "json"):
        (stdlib / d).mkdir(parents=True)
    (stdlib / "json" / "__init__.py").write_text("")  # 有用模块应保留

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    assert not (stdlib / "test").exists()
    assert not (stdlib / "ensurepip").exists()
    assert not (stdlib / "idlelib").exists()
    assert not (stdlib / "pydoc_data").exists()
    assert not (stdlib / "turtledemo").exists()
    assert (stdlib / "json").exists()  # 保留有用模块


def test_trim_stdlib_linux_records_saved_bytes(tmp_path: Path) -> None:
    """Linux 模式剥离目录时累加节省字节数到 stage.add_saved_bytes."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)
    (stdlib / "test" / "data.bin").write_bytes(b"x" * 1024)  # 1KB
    (stdlib / "test" / "sub").mkdir()
    (stdlib / "test" / "sub" / "more.bin").write_bytes(b"y" * 512)  # 0.5KB
    (stdlib / "ensurepip").mkdir(parents=True)
    (stdlib / "ensurepip" / "pkg.py").write_bytes(b"z" * 256)  # 0.25KB

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)

    record = st._finalize()
    # 1KB + 0.5KB + 0.25KB = 1792 字节
    assert record.bytes_saved == 1792
    assert record.skipped == 2  # test + ensurepip


def test_trim_stdlib_windows_standard_skips(tmp_path: Path) -> None:
    """Windows 标准版无 embed stdlib zip（缓存未就绪等场景）时跳过不剥离."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)  # 构造验证跳过

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.WINDOWS, st)

    # 无 python311.zip 时跳过，散装目录不动
    assert (stdlib / "test").exists()
    record = st._finalize()
    assert record.bytes_saved == 0


# ---- _rewrite_embed_stdlib_zip 测试 ----


def _make_embed_zip(runtime: Path, entries: dict[str, bytes]) -> Path:
    """构造伪 embed stdlib zip（python3XX.zip）供重写测试."""
    runtime.mkdir(parents=True, exist_ok=True)
    zip_path = runtime / "python311.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return zip_path


def test_rewrite_embed_stdlib_zip_conservative(tmp_path: Path) -> None:
    """保守档重写：删 pydoc_data/__phello__ 等纯文档条目，保留 xml/json/logging.

    embed zip 内条目为 .pyc 形态（官方全量冻结），测试条目同真实形态。
    """
    from fspack.packaging.runtime.trim import _rewrite_embed_stdlib_zip

    runtime = tmp_path / "runtime"
    zip_path = _make_embed_zip(
        runtime,
        {
            "pydoc_data/topics.pyc": b"x" * 100,  # 纯文档数据，删
            "pydoc_data/__init__.pyc": b"",
            "__phello__/__init__.pyc": b"",  # 嵌入示例，删
            "xml/__init__.pyc": b"y" * 100,  # 保守档保留
            "json/__init__.pyc": b"z" * 100,  # 保留
            "logging/__init__.pyc": b"w" * 100,  # 保留
            "os.pyc": b"os",  # 保留
        },
    )
    size_before = zip_path.stat().st_size

    st = StageRecorder("精简标准库")
    _rewrite_embed_stdlib_zip(runtime, "3.11.9", st)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert not any(n.startswith("pydoc_data/") for n in names)
    assert not any(n.startswith("__phello__") for n in names)
    assert any(n.startswith("xml/") for n in names)
    assert any(n.startswith("json/") for n in names)
    record = st._finalize()
    assert record.bytes_saved > 0
    assert record.bytes_saved <= size_before  # 净节省不超过原大小
    assert record.skipped == 3  # pydoc_data 2 条 + __phello__ 1 条


def test_rewrite_embed_stdlib_zip_aggressive(tmp_path: Path) -> None:
    """激进档重写：再删 xml/unittest/asyncio 与开发工具单文件（.pyc 按 stem 匹配）."""
    from fspack.packaging.runtime.trim import _rewrite_embed_stdlib_zip

    runtime = tmp_path / "runtime"
    zip_path = _make_embed_zip(
        runtime,
        {
            "pydoc_data/topics.pyc": b"x" * 100,
            "xml/__init__.pyc": b"y" * 100,  # 激进档删
            "unittest/__init__.pyc": b"u" * 100,  # 激进档删
            "asyncio/__init__.pyc": b"a" * 100,  # 激进档删
            "pdb.pyc": b"p" * 100,  # 激进档删（开发工具单文件，stem 匹配）
            "json/__init__.pyc": b"z" * 100,  # 保留
            "logging/__init__.pyc": b"w" * 100,  # 保留（常用）
            "concurrent/futures/__init__.pyc": b"c" * 100,  # 保留（通用基础设施）
        },
    )

    st = StageRecorder("精简标准库")
    _rewrite_embed_stdlib_zip(runtime, "3.11.9", st, aggressive=True)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert not any(n.startswith(("pydoc_data/", "xml/", "unittest/", "asyncio/")) for n in names)
    assert "pdb.pyc" not in names
    assert any(n.startswith("json/") for n in names)
    assert any(n.startswith("logging/") for n in names)
    assert any(n.startswith("concurrent/") for n in names)
    record = st._finalize()
    # 激进档节省多于仅保守档（xml/unittest/asyncio/pdb 共 400+ 字节原始数据）
    assert record.bytes_saved > 400
    assert record.skipped == 5


def test_rewrite_embed_stdlib_zip_idempotent(tmp_path: Path) -> None:
    """幂等：重写后黑名单条目不在 zip 内，二次调用剥离数为 0 跳过."""
    from fspack.packaging.runtime.trim import _rewrite_embed_stdlib_zip

    runtime = tmp_path / "runtime"
    zip_path = _make_embed_zip(runtime, {"pydoc_data/topics.pyc": b"x" * 100, "os.pyc": b"os"})

    st1 = StageRecorder("精简标准库")
    _rewrite_embed_stdlib_zip(runtime, "3.11.9", st1)
    saved1 = st1._finalize().bytes_saved
    assert saved1 > 0

    st2 = StageRecorder("精简标准库")
    _rewrite_embed_stdlib_zip(runtime, "3.11.9", st2)
    record2 = st2._finalize()
    assert record2.bytes_saved == 0
    assert record2.skipped == 0
    # zip 完整保留非黑名单条目
    with zipfile.ZipFile(zip_path) as zf:
        assert "os.pyc" in zf.namelist()


def test_rewrite_embed_stdlib_zip_bad_zip_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """畸形 zip 警告跳过不抛异常，原文件保留."""
    from fspack.packaging.runtime.trim import _rewrite_embed_stdlib_zip

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    zip_path = runtime / "python311.zip"
    zip_path.write_bytes(b"{not a zip")

    st = StageRecorder("精简标准库")
    with caplog.at_level("WARNING"):
        _rewrite_embed_stdlib_zip(runtime, "3.11.9", st)

    assert "读取 embed stdlib zip 失败" in caplog.text
    assert zip_path.stat().st_size == len(b"{not a zip")  # 原文件未动
    assert st._finalize().bytes_saved == 0


def test_trim_stdlib_windows_standard_rewrites_zip(tmp_path: Path) -> None:
    """_trim_stdlib Windows 标准版分支走 embed zip 重写（集成）."""
    runtime = tmp_path / "runtime"
    _make_embed_zip(
        runtime,
        {"pydoc_data/topics.pyc": b"x" * 100, "xml/__init__.pyc": b"y" * 100, "os.pyc": b"os"},
    )

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.WINDOWS, st)

    with zipfile.ZipFile(runtime / "python311.zip") as zf:
        names = zf.namelist()
    assert not any(n.startswith("pydoc_data/") for n in names)
    assert any(n.startswith("xml/") for n in names)  # 保守档保留
    assert "os.pyc" in names
    assert st._finalize().bytes_saved > 0


def test_trim_stdlib_windows_standard_aggressive_via_flag(tmp_path: Path) -> None:
    """_trim_stdlib aggressive=True 透传激进档剥离清单."""
    runtime = tmp_path / "runtime"
    _make_embed_zip(runtime, {"xml/__init__.pyc": b"y" * 100, "json/__init__.pyc": b"z" * 100})

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.WINDOWS, st, aggressive=True)

    with zipfile.ZipFile(runtime / "python311.zip") as zf:
        names = zf.namelist()
    assert not any(n.startswith("xml/") for n in names)  # 激进档删除
    assert any(n.startswith("json/") for n in names)


def test_trim_stdlib_windows_t_strips_lib_at_root(tmp_path: Path) -> None:
    """Windows 自由线程版（t 后缀）走 standalone 路径，剥离 runtime/Lib/ 无用目录."""
    runtime = tmp_path / "runtime"
    # python-build-standalone Windows freethreaded tarball 解压扁平化后标准库在 runtime/Lib/
    stdlib = runtime / "Lib"
    for d in ("test", "ensurepip", "idlelib", "pydoc_data", "turtledemo", "json"):
        (stdlib / d).mkdir(parents=True)
    (stdlib / "json" / "__init__.py").write_text("")  # 有用模块应保留

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.13.14t", Platform.WINDOWS, st)

    assert not (stdlib / "test").exists()
    assert not (stdlib / "ensurepip").exists()
    assert not (stdlib / "idlelib").exists()
    assert not (stdlib / "pydoc_data").exists()
    assert not (stdlib / "turtledemo").exists()
    assert (stdlib / "json").exists()  # 保留有用模块


def test_trim_stdlib_windows_t_missing_lib_skips(tmp_path: Path) -> None:
    """Windows 自由线程版 runtime/Lib/ 不存在时不报错."""
    runtime = tmp_path / "runtime"
    # 不创建 Lib 目录（缓存命中场景或扁平化前已删除）

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.14.6t", Platform.WINDOWS, st)
    record = st._finalize()
    assert record.bytes_saved == 0


def test_trim_stdlib_missing_stdlib_skips(tmp_path: Path) -> None:
    """标准库目录不存在时不报错."""
    runtime = tmp_path / "runtime"
    # 不创建 stdlib 目录

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)
    # 不报错即通过
    record = st._finalize()
    assert record.bytes_saved == 0


def test_trim_stdlib_idempotent(tmp_path: Path) -> None:
    """重复调用幂等：已剥离的目录不存在时跳过."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)
    (stdlib / "test" / "data.bin").write_bytes(b"x" * 100)

    st1 = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st1)
    st2 = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st2)  # 二次调用不报错
    assert not (stdlib / "test").exists()
    # 二次调用目录已不存在，bytes_saved 为 0
    assert st2._finalize().bytes_saved == 0
    # 首次调用记录了 100 字节
    assert st1._finalize().bytes_saved == 100


def test_dir_size_empty_dir(tmp_path: Path) -> None:
    """_dir_size 对空目录返回 0."""
    d = tmp_path / "empty"
    d.mkdir()
    assert _dir_size(d) == 0


def test_dir_size_nested_files(tmp_path: Path) -> None:
    """_dir_size 递归累加所有文件大小."""
    d = tmp_path / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(b"x" * 100)
    (d / "sub" / "b.bin").write_bytes(b"y" * 200)
    (d / "sub" / "c.bin").write_bytes(b"z" * 300)
    assert _dir_size(d) == 600


def test_dir_size_handles_concurrent_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_dir_size 遇到 OSError（stat 失败）时跳过，不阻断计算.

    模拟 scandir_tree 返回的条目中，stat(follow_symlinks=False) 抛 OSError
    （并发删除/权限问题）。_dir_size 委托 _util.fsutil.scandir_dir_size，后者用
    scandir_tree 枚举，DirEntry.stat 复用枚举缓存但仍可能因文件被并发删除抛 OSError。
    """

    class _StatResult:
        def __init__(self, size: int) -> None:
            self.st_size = size

    class _GoodEntry:
        def __init__(self, size: int) -> None:
            self._size = size

        def stat(self, *, follow_symlinks: bool = True) -> _StatResult:
            return _StatResult(self._size)

    class _BrokenEntry:
        def stat(self, *, follow_symlinks: bool = True) -> _StatResult:
            raise OSError("file removed by another process")

    d = tmp_path / "tree"
    d.mkdir()
    # _dir_size 委托 fspack._util.fsutil.scandir_dir_size，后者用 scandir_tree 枚举。
    # 按"patch 定义所在底层模块"约定 patch _util.fsutil.scandir_tree，验证
    # stat 抛 OSError 的条目被跳过。
    monkeypatch.setattr(
        "fspack._util.fsutil.scandir_tree",
        lambda root: [_GoodEntry(100), _BrokenEntry(), _GoodEntry(200)] if root == d else [],
    )
    # BrokenEntry 的 OSError 被跳过，仅累加两个 GoodEntry 的 100 + 200 = 300
    assert _dir_size(d) == 300


# ---- _precompile_pyc 测试 ----


class _CompileCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def test_precompile_pyc_windows_calls_compileall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标用 runtime/python.exe 拆分两次调 compileall 分别编译 src 与 site-packages.

    src 与 site-packages 用 ``ThreadPoolExecutor`` 并行编译，完成顺序不保证，
    断言两个目录都出现且都使用 runtime python.exe。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 拆分为两次 compileall 调用：src 与 site-packages 分别编译
    # （src 用 optimize，site-packages 用 min(optimize,1) 保留 docstring）
    # 并行执行，完成顺序不保证，用集合断言两个目标都出现
    assert len(captured) == 2
    target_dirs = {cmd[3] for cmd in captured}
    assert str(dist / "src") in target_dirs
    assert str(tmp_path / "dist" / "site-packages") in target_dirs
    for cmd in captured:
        assert "compileall" in cmd
        assert str(runtime / "python.exe") in cmd[0]


def test_precompile_pyc_parallel_executes_in_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """双目录场景下两个 compileall 在不同线程并行执行（验证 ThreadPoolExecutor 启用）.

    使用 :class:`threading.Barrier` 强制两个 compileall 调用同时活跃：若两者
    在同一线程串行执行，Barrier 永远等不到 2 个 parties，超时抛
    :class:`BrokenBarrierError` 让测试失败。
    """
    import threading

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")
    (dist / "site-packages").mkdir(parents=True)
    (dist / "site-packages" / "pkg.py").write_text("x = 1")

    thread_ids: set[int] = set()
    # Barrier(2) 强制两个 compileall 同时活跃：只有两个任务都在不同线程执行时才能通过
    barrier: threading.Barrier = threading.Barrier(2)

    def capture_thread(cmd: list[str], **kw: object) -> object:
        """记录调用线程 ID，等待另一个 compileall 也到达后返回."""
        thread_ids.add(threading.get_ident())
        barrier.wait(timeout=2.0)
        return _CompileCompleted()

    monkeypatch.setattr("subprocess.run", capture_thread)

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 两个 compileall 在不同线程执行（ThreadPoolExecutor worker 线程）
    assert len(thread_ids) >= 2


def test_precompile_pyc_parallel_one_failure_skips_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """并行编译时任一目录失败则不写 stamp，且记录一条 compileall 失败 warning."""
    import logging

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")
    (dist / "site-packages").mkdir(parents=True)
    (dist / "site-packages" / "pkg.py").write_text("x = 1")

    class _CompileFail:
        returncode = 2
        stderr = "SyntaxError: invalid syntax"
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileFail())

    from fspack.packaging.pyc import _precompile_pyc

    st = StageRecorder("预编译字节码")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.pyc.compile"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    stamp = dist / ".pyc_stamp"
    assert not stamp.is_file()
    fail_logs = [r for r in caplog.records if "compileall 失败" in r.message]
    # 至少一条失败日志（并行下两个都失败，as_completed 顺序不保证）
    assert len(fail_logs) >= 1
    assert "SyntaxError" in fail_logs[0].message


def test_precompile_pyc_cleans_data_dirs_pycache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_precompile_pyc`` 编译后删除 data_dirs 下的 __pycache__（数据资源不留 .pyc）.

    compileall 会为 data_dirs 内 .py 生成 __pycache__/*.pyc，但 data_dirs 视为完整
    资源原样保留，这些字节码是污染（尤其 fspack 模板目录内 $entry_module.py 编译出
    的 .pyc 会被 fsp init 模板加载器误读）。本测试模拟 compileall 已生成 __pycache__，
    验证 _precompile_pyc 调用后 data_dirs 下的 __pycache__ 被清理，src 其余 .pyc 保留。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "app.py").write_text("print('app')")
    # 模拟 data_dir：assets/init_templates 下含占位符 .py 的模板目录
    tpl_dir = src / "fspack" / "assets" / "init_templates" / "cli" / "helloworld"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "$entry_module.py").write_text("def main():\n    pass\n")
    # 模拟 compileall 已为 data_dir 与普通 src 生成 __pycache__/*.pyc
    tpl_pycache = tpl_dir / "__pycache__"
    tpl_pycache.mkdir()
    (tpl_pycache / "$entry_module.cpython-311.opt-2.pyc").write_bytes(b"\xa7\x00\x01")
    src_pycache = src / "__pycache__"
    src_pycache.mkdir()
    (src_pycache / "app.cpython-311.pyc").write_bytes(b"\x00\x01\x02")

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(
        dist,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        strip_py=False,
        stage=st,
        data_dirs=(tpl_dir.resolve(),),
    )

    # data_dir 下的 __pycache__ 被清理，占位符 .py 保留
    assert not tpl_pycache.exists()
    assert (tpl_dir / "$entry_module.py").is_file()
    # src 其余（非 data_dir）__pycache__ 不受影响
    assert src_pycache.is_dir()
    assert (src_pycache / "app.cpython-311.pyc").is_file()


def test_precompile_pyc_linux_uses_python3_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标用 runtime/python/bin/python{ver} 调 compileall."""
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.LINUX, strip_py=False, stage=st)

    # pyrefly: ignore [unnecessary-type-conversion]
    assert "python3.11" in str(captured[0][0])


def test_precompile_pyc_strip_deletes_non_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip_py=True 删除非 __init__.py 的 .py，保留 __init__.py 维持包结构.

    PEP 3147 迁移：删除 .py 前将 __pycache__/{stem}.cpython-{ver}.pyc 移到 {stem}.pyc。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('hi')")
    (src / "sub").mkdir()
    (src / "sub" / "__init__.py").write_text("")
    (src / "sub" / "mod.py").write_text("x")

    monkeypatch.setattr("subprocess.run", _fake_compileall_runner)

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=True, stage=st)

    # __init__.py 保留（包标识）
    assert (src / "__init__.py").is_file()
    assert (src / "sub" / "__init__.py").is_file()
    # 非 __init__.py 被删
    assert not (src / "app.py").exists()
    assert not (src / "sub" / "mod.py").exists()
    # .pyc 已迁移到 legacy 布局
    assert (src / "app.pyc").is_file()
    assert (src / "sub" / "mod.pyc").is_file()


def _fake_compileall_runner(cmd: list[str], **kw: Any) -> Any:
    """模拟 subprocess.run 调用 compileall：解析命令并生成真实 .pyc 文件.

    供 ``_precompile_pyc`` 测试使用，使 ``_strip_py_sources`` 能迁移真实的 .pyc。
    用 :func:`py_compile.compile` 生成指定 Python 版本标签的 .pyc 文件名
    （``cpython-{major}{minor}[-opt-N].pyc``），而非当前解释器版本。
    从命令中解析解释器优化标志 ``-O``/``-OO``（等价 ``compileall -o 1/2``）与
    目标目录；py_version 由调用方在 ``cmd`` 中无法获取，故用模块级
    ``_FAKE_COMPILE_PY_VERSION`` 变量传递（默认 "3.11"）。

    支持多目录合并调用：``python [-O|-OO] -m compileall dir1 dir2 -q -j 0``
    一次编译多目录，本函数收集所有非 flag 的目录参数逐个编译。
    """
    optimize = 0
    target_dirs: list[Path] = []
    for arg in cmd:
        if arg == "-OO":
            optimize = 2
        elif arg == "-O":
            optimize = 1
        elif not arg.startswith("-") and Path(arg).is_dir():
            target_dirs.append(Path(arg))
    for target_dir in target_dirs:
        _compile_dir_with_pyc(target_dir, _FAKE_COMPILE_PY_VERSION, optimize)
    return _CompileCompleted()


_FAKE_COMPILE_PY_VERSION = "3.11"


def _compile_dir_with_pyc(target_dir: Path, py_version: str, optimize: int) -> None:
    """用 py_compile 为 target_dir 下所有 .py 生成指定版本标签的 .pyc 文件."""
    import py_compile

    major, minor = py_version.split(".")[:2]
    ver_tag = f"cpython-{major}{minor}"
    opt_suffix = "" if optimize == 0 else f".opt-{optimize}"
    for py in target_dir.rglob("*.py"):
        pycache = py.parent / "__pycache__"
        pycache.mkdir(exist_ok=True)
        pyc_file = pycache / f"{py.stem}.{ver_tag}{opt_suffix}.pyc"
        py_compile.compile(str(py), cfile=str(pyc_file), optimize=optimize)


def test_precompile_pyc_strip_keeps_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip_py=True 时不删 __init__.py（避免 PEP 420 命名空间包导致 .pyc 不加载）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("PKG = 1")
    (src / "main.py").write_text("print('main')")

    monkeypatch.setattr("subprocess.run", _fake_compileall_runner)

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=True, stage=st)

    assert (src / "__init__.py").is_file()
    assert not (src / "main.py").exists()
    # main.py 的 .pyc 已迁移到 legacy 布局
    assert (src / "main.pyc").is_file()


def test_precompile_pyc_strip_keeps_entry_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip_py=True 时保留 entry_rels 中的入口文件（runpy.run_module 需 .py 定位模块）.

    入口文件跳过 Nuitka 编译（保留 .py），若 pyc_strip 再删除入口 .py，
    ``runpy.run_module`` 会因 ``find_spec`` 找不到模块而 ``ImportError``：
    ``__pycache__`` 下的 ``.pyc`` 不在 ``FileFinder`` 搜索范围。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    # 模拟 fspack 包结构：src/fspack/__init__.py + cli.py（入口）+ utils.py（非入口）
    pkg = src / "fspack"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "cli.py").write_text("def main(): pass")  # 入口文件
    (pkg / "utils.py").write_text("x = 1")  # 非入口文件

    monkeypatch.setattr("subprocess.run", _fake_compileall_runner)

    st = StageRecorder("预编译字节码")
    _precompile_pyc(
        dist,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        strip_py=True,
        stage=st,
        entry_rels=frozenset({"fspack/cli.py"}),
    )

    # __init__.py 保留（包标识）
    assert (pkg / "__init__.py").is_file()
    # 入口文件保留（runpy.run_module 需 .py 定位）
    assert (pkg / "cli.py").is_file()
    # 非入口 .py 被删除
    assert not (pkg / "utils.py").exists()
    # utils.py 的 .pyc 已迁移到 legacy 布局
    assert (pkg / "utils.pyc").is_file()


def test_strip_py_sources_skips_entry_rels(tmp_path: Path) -> None:
    """``_strip_py_sources`` 单元测试：entry_rels 中的文件跳过剥离.

    新增 PEP 3147 迁移：删除 .py 前需有对应 .pyc 才会剥离，否则保留 .py。
    """
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    pkg = src / "app"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "main.py").write_text("m")  # 入口
    (pkg / "helper.py").write_text("h")  # 非入口
    # 为 helper.py 预生成 .pyc（模拟 compileall 输出），否则新逻辑保留 .py
    _make_pyc_file(pkg / "helper.py", "3.11", optimize=0)

    stripped = _strip_py_sources([src], frozenset({"app/main.py"}), optimize=0, py_version="3.11.9")

    assert stripped == 1  # 仅 helper.py 被删
    assert (pkg / "main.py").is_file()  # 入口保留
    assert not (pkg / "helper.py").exists()  # 非入口删除
    assert (pkg / "__init__.py").is_file()  # __init__.py 保留
    # .pyc 已迁移到 legacy 布局（helper.pyc）
    assert (pkg / "helper.pyc").is_file()


def _make_pyc_file(py_file: Path, py_version: str = "3.11", optimize: int = 0) -> Path:
    """生成 ``__pycache__/{stem}.cpython-{ver}{opt}.pyc`` 文件，返回路径.

    用 :func:`py_compile.compile` 生成真实的 .pyc 字节码（非空文件），
    供 ``_strip_py_sources`` 的 PEP 3147 迁移逻辑测试使用。
    """
    import py_compile

    major, minor = py_version.split(".")[:2]
    ver_tag = f"cpython-{major}{minor}"
    opt_suffix = "" if optimize == 0 else f".opt-{optimize}"
    pycache = py_file.parent / "__pycache__"
    pycache.mkdir(exist_ok=True)
    pyc_file = pycache / f"{py_file.stem}.{ver_tag}{opt_suffix}.pyc"
    py_compile.compile(str(py_file), cfile=str(pyc_file), optimize=optimize)
    return pyc_file


def test_strip_py_sources_migrates_pyc_to_legacy_layout(tmp_path: Path) -> None:
    """``_strip_py_sources`` 删除 .py 前将 __pycache__ 中的 .pyc 迁移到 legacy 布局.

    PEP 3147 规定 __pycache__ 中的 .pyc 仅在源码 .py 存在时才被加载，
    删除 .py 后必须迁移到 {stem}.pyc 才能被 SourcelessFileLoader 加载。
    """
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "mod.py").write_text("VALUE = 42")
    # 预生成 .pyc（optimize=2，对应 .opt-2.pyc）
    _make_pyc_file(src / "mod.py", "3.11", optimize=2)

    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=2)

    assert stripped == 1
    assert not (src / "mod.py").exists()  # .py 已删
    assert (src / "mod.pyc").is_file()  # .pyc 迁移到 legacy 布局
    # __pycache__ 中的 .pyc 已被移走
    pycache_dir = src / "__pycache__"
    pycache_files: list[Path] = list(pycache_dir.glob("mod.*.pyc")) if pycache_dir.exists() else []
    assert not pycache_files


def test_strip_py_sources_keeps_py_when_pyc_missing(tmp_path: Path) -> None:
    """``.pyc`` 不存在（编译失败）时保留 ``.py``，避免模块完全丢失."""
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "broken.py").write_text("syntax error!!!")
    # 不生成 .pyc（模拟 compileall 失败）

    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0)

    assert stripped == 0  # 无 .pyc 不剥离
    assert (src / "broken.py").is_file()  # .py 保留


def test_strip_py_sources_optimize_level_matches_pyc(tmp_path: Path) -> None:
    """optimize 级别必须匹配 .pyc 文件名后缀（.opt-N），否则不剥离."""
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "mod.py").write_text("x = 1")
    # 生成 optimize=2 的 .pyc，但调用时 optimize=0 → 文件名不匹配
    _make_pyc_file(src / "mod.py", "3.11", optimize=2)

    # optimize=0 查找 mod.cpython-311.pyc，但实际是 mod.cpython-311.opt-2.pyc
    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0)

    assert stripped == 0  # 文件名不匹配，不剥离
    assert (src / "mod.py").is_file()  # .py 保留


def test_strip_py_sources_skips_data_dirs(tmp_path: Path) -> None:
    """``data_dirs`` 内的 .py 不剥离（数据资源目录原样保留）.

    模拟 fspack 自身打包：``src/fspack/assets/templates/<each>/tk_app.py``
    是项目模板源码，``fsp doctor --test`` 复制后需 .py 存在才能 build。
    """
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('app')")
    # 模拟 assets/templates/gui/tk_app/tk_app.py
    tpl_dir = src / "fspack" / "assets" / "templates" / "gui" / "tk_app"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "tk_app.py").write_text("def main():\n    print('hi')\n")
    # 为两个 .py 都生成 .pyc（确保 PEP 3147 迁移条件满足，区别仅在 data_dirs 跳过）
    _make_pyc_file(src / "app.py", "3.11", optimize=0)
    _make_pyc_file(tpl_dir / "tk_app.py", "3.11", optimize=0)

    data_dirs = (tpl_dir.resolve(),)
    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0, data_dirs=data_dirs)

    # 仅 app.py 被剥离，tk_app.py 保留
    assert stripped == 1
    assert not (src / "app.py").exists()
    assert (src / "app.pyc").is_file()  # app.pyc 迁移到 legacy 布局
    assert (tpl_dir / "tk_app.py").is_file()  # data_dirs 内保留 .py
    # data_dirs 内的 __pycache__/.pyc 不迁移（.py 未删除）
    pycache_files: list[Path] = list((tpl_dir / "__pycache__").glob("*.pyc"))
    assert pycache_files  # __pycache__ 下 .pyc 仍在


def test_strip_py_sources_data_dirs_empty_default_behavior(tmp_path: Path) -> None:
    """``data_dirs`` 为空时与不传一致：所有非 __init__.py/.pyc 缺失的 .py 都剥离."""
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "mod.py").write_text("x = 1")
    _make_pyc_file(src / "mod.py", "3.11", optimize=0)

    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0, data_dirs=())

    assert stripped == 1
    assert not (src / "mod.py").exists()


def test_precompile_pyc_python_missing_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime python 未就绪时跳过 compileall，不调 subprocess."""
    runtime = tmp_path / "runtime"
    # 不创建 python.exe
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")

    called: list[object] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: called.append(cmd))

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    assert not called  # 未调 subprocess


def test_precompile_pyc_compileall_failure_warns_not_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compileall 非零退出码时仅 warning 不抛异常，且不写 stamp（下次构建重试）.

    iter-128 扩展"失败不缓存"策略：returncode != 0 与超时一致都不写 stamp，
    避免失败的编译被 stamp 跳过导致用户长期运行未编译的 .py。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")

    class _Failed:
        returncode = 1
        stderr = "syntax error"
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _Failed())

    st = StageRecorder("预编译字节码")
    with caplog.at_level("WARNING", logger="fspack.builder"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    assert any("compileall 失败" in r.message for r in caplog.records)
    # 编译失败不写 stamp（iter-128）：下次构建重试，避免失败的编译被缓存跳过
    assert not (dist / ".pyc_stamp").is_file()


def test_precompile_pyc_optimize_passes_o_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """optimize 参数透传为解释器 ``-O``/``-OO`` 标志，控制字节码优化级别.

    用解释器标志而非 ``compileall -o N``：后者仅 Python 3.9+ CLI 支持，
    embed Python 3.8 会报 "unrecognized arguments: -o"。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st, optimize=2)

    # 拆分两次调用：src 用 optimize=2（-OO），site-packages 降级到 1（-O，保留 docstring）
    assert len(captured) == 2
    src_cmd = next(cmd for cmd in captured if str(dist / "src") in cmd)
    assert "-OO" in src_cmd
    sp_cmd = next(cmd for cmd in captured if str(tmp_path / "dist" / "site-packages") in cmd)
    assert "-O" in sp_cmd
    assert "-OO" not in sp_cmd


def test_pyc_stamp_key_includes_sp_optimize(tmp_path: Path) -> None:
    """_pyc_stamp_key 纳入 sp_optimize，切换 site-packages 优化级别时强制重编译.

    site-packages 用 ``min(optimize, 1)`` 降级（保留 docstring），老 stamp（无
    sp_optimize 字段）自然失效，避免旧的剥离 docstring 的 .pyc 被加载触发
    第三方库 C 扩展兼容问题（numpy ``add_docstring`` 等）。
    """
    from fspack.builder import _pyc_stamp_key

    sp = tmp_path / "sp"
    sp.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("")
    # 同 optimize 不同 sp_optimize 产生不同 key
    key_sp0 = _pyc_stamp_key(src, sp, strip_py=False, optimize=2, sp_optimize=0)
    key_sp1 = _pyc_stamp_key(src, sp, strip_py=False, optimize=2, sp_optimize=1)
    assert key_sp0 != key_sp1
    # 默认 sp_optimize=0
    assert _pyc_stamp_key(src, sp, strip_py=False, optimize=2) == key_sp0


def test_precompile_pyc_optimize_default_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """optimize 默认 0，compileall 命令不含优化标志（生成无 opt 后缀的 .pyc）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    for cmd in captured:
        assert "-O" not in cmd
        assert "-OO" not in cmd


def test_pyc_stamp_key_includes_optimize(tmp_path: Path) -> None:
    """_pyc_stamp_key 纳入 optimize，切换级别时强制重编译."""
    from fspack.builder import _pyc_stamp_key

    sp = tmp_path / "sp"
    sp.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("")
    key0 = _pyc_stamp_key(src, sp, strip_py=False, optimize=0)
    key1 = _pyc_stamp_key(src, sp, strip_py=False, optimize=1)
    key2 = _pyc_stamp_key(src, sp, strip_py=False, optimize=2)
    assert key0 != key1
    assert key0 != key2
    assert key1 != key2
    # 同级别稳定
    assert _pyc_stamp_key(src, sp, strip_py=False, optimize=0) == key0


def test_precompile_pyc_optimize_invalidates_old_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """切换 optimize 时旧 stamp 不命中，触发重编译."""
    from fspack.builder import _pyc_stamp_path

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    # 先用 optimize=0 编译，写 stamp
    captured_first: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured_first.append(cmd) or _CompileCompleted())
    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st, optimize=0)
    assert captured_first  # 实际调用了 compileall
    assert _pyc_stamp_path(dist).is_file()

    # 切换 optimize=2，应触发重编译
    captured_second: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured_second.append(cmd) or _CompileCompleted())
    st2 = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st2, optimize=2)
    assert captured_second  # 重新调用 compileall，stamp 未命中


# ---- build() 集成新阶段测试 ----


def _capture_stage_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """包装 StageRecorder._finalize 记录所有阶段名."""
    captured: list[str] = []
    original_finalize = StageRecorder._finalize

    def recording_finalize(self: StageRecorder) -> object:
        rec = original_finalize(self)
        captured.append(rec.name)
        return rec

    monkeypatch.setattr(StageRecorder, "_finalize", recording_finalize)
    return captured


def test_build_includes_new_stages_in_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build() 阶段汇总含「精简标准库」「预编译字节码」「解压 wheel(精简)」."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\ndependencies = ["rich"]\n')
    (proj / "app.py").write_text("import rich\n\ndef main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    # 覆盖 download_wheels 返回非空列表，触发「解压 wheel(精简)」阶段
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", lambda *a, **k: [tmp_path / "fake.whl"])
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())
    # 模拟同平台构建（CI 可能在 Linux 上跑 Windows 目标测试，交叉构建会跳过预编译）
    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.WINDOWS)

    stage_names = _capture_stage_names(monkeypatch)
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    assert "精简标准库" in stage_names
    assert "预编译字节码" in stage_names
    assert "解压 wheel(精简)" in stage_names


def test_build_no_pyc_skips_precompile_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """no_pyc=True 时跳过「预编译字节码」阶段."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())

    stage_names = _capture_stage_names(monkeypatch)
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, options=BuildOptions(no_pyc=True))

    assert "预编译字节码" not in stage_names
    assert "精简标准库" in stage_names  # 精简标准库仍执行


def test_build_no_stdlib_trim_skips_trim_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """no_stdlib_trim=True 时跳过「精简标准库」阶段."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())
    # 模拟同平台构建（CI 可能在 Linux 上跑 Windows 目标测试，交叉构建会跳过预编译）
    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.WINDOWS)

    stage_names = _capture_stage_names(monkeypatch)
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, options=BuildOptions(no_stdlib_trim=True))

    assert "精简标准库" not in stage_names
    assert "预编译字节码" in stage_names  # 预编译仍执行


def test_build_pyc_strip_deletes_non_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pyc_strip=True 时 build() 剥离非 __init__.py 非入口的 .py，但保留入口文件.

    入口文件需保留 .py 以供 ``runpy.run_module`` 定位（``__pycache__`` 下 .pyc
    不在 ``FileFinder`` 搜索范围，.pyd 无字节码无法被 runpy 执行）。
    PEP 3147 迁移：剥离 .py 前将 __pycache__ 中的 .pyc 移到 legacy 布局。
    """
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")
    # 非入口 .py 文件，应被剥离
    (proj / "helper.py").write_text("x = 1")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    # 让 python.exe 就绪，使 _precompile_pyc 真正执行 strip
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", _fake_compileall_runner)
    # 模拟同平台构建（CI 可能在 Linux 上跑 Windows 目标测试，交叉构建会跳过预编译）
    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.WINDOWS)

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, options=BuildOptions(pyc_strip=True))

    src = proj / "dist" / "src"
    # app.py 是入口文件，保留（runpy.run_module 需 .py 定位）
    assert (src / "app.py").is_file()
    # helper.py 非入口，被剥离
    assert not (src / "helper.py").exists()
    # helper.py 的 .pyc 已迁移到 legacy 布局
    assert (src / "helper.pyc").is_file()


def test_build_default_keeps_py_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认（无 pyc_strip）保留 .py 源码，仅生成 .pyc 加速."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    # app.py 保留
    assert (proj / "dist" / "src" / "app.py").is_file()


# ---- 增量同步（copy_source 保留 __pycache__）----


def test_copy_source_preserves_pycache(tmp_path: Path) -> None:
    """copy_source 增量同步时保留 dst 的 __pycache__ 目录以复用 .pyc 缓存."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('v1')\n")
    dst = tmp_path / "out" / "src"
    dst.mkdir(parents=True)
    (dst / "old.py").write_text("old")
    pycache = dst / "__pycache__"
    pycache.mkdir()
    (pycache / "app.cpython-311.pyc").write_bytes(b"\x00\x00")

    copy_source(src, dst)

    # __pycache__ 保留
    assert pycache.is_dir()
    assert (pycache / "app.cpython-311.pyc").is_file()
    # old.py（src 中不存在）被删除
    assert not (dst / "old.py").exists()
    # app.py 覆盖复制
    assert (dst / "app.py").read_text() == "print('v1')\n"


def test_sync_tree_recursive_preserves_nested_pycache(tmp_path: Path) -> None:
    """_sync_tree 递归保留子目录中的 __pycache__."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "pkg").mkdir()
    (src / "pkg" / "mod.py").write_text("x=1\n")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "pkg").mkdir()
    (dst / "pkg" / "__pycache__").mkdir()
    (dst / "pkg" / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00")
    (dst / "pkg" / "stale.py").write_text("stale")

    import shutil

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert (dst / "pkg" / "__pycache__" / "mod.cpython-311.pyc").is_file()
    assert not (dst / "pkg" / "stale.py").exists()
    assert (dst / "pkg" / "mod.py").read_text() == "x=1\n"


def test_copy_source_syncs_deleted_files(tmp_path: Path) -> None:
    """src 删除文件后 copy_source 同步删除 dst 中对应文件（保留 __pycache__）."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("v1")
    dst = tmp_path / "out" / "src"

    # 第一次复制
    copy_source(src, dst)
    assert (dst / "app.py").is_file()

    # src 删除 app.py，添加 main.py
    (src / "app.py").unlink()
    (src / "main.py").write_text("v2")

    # 第二次同步
    copy_source(src, dst)
    assert not (dst / "app.py").exists(), "src 已删除的文件应从 dst 移除"
    assert (dst / "main.py").is_file()


def test_sync_tree_file_to_dir_type_swap(tmp_path: Path) -> None:
    """src 同名条目由文件改为目录时，先删 dst 残留文件再建目录，不抛 FileExistsError."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo").write_text("v1")
    dst = tmp_path / "dst"
    dst.mkdir()
    _sync_tree(src, dst, shutil.ignore_patterns())
    assert (dst / "foo").is_file()

    # src 侧 foo 由文件改为目录
    (src / "foo").unlink()
    (src / "foo").mkdir()
    (src / "foo" / "bar.py").write_text("in dir")

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert (dst / "foo").is_dir(), "dst/foo 应被替换为目录"
    assert (dst / "foo" / "bar.py").read_text() == "in dir"


def test_sync_tree_dir_to_file_type_swap(tmp_path: Path) -> None:
    """src 同名条目由目录改为文件时，先删 dst 残留目录再复制，不抛 IsADirectoryError."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo").mkdir()
    (src / "foo" / "bar.py").write_text("in dir")
    dst = tmp_path / "dst"
    dst.mkdir()
    _sync_tree(src, dst, shutil.ignore_patterns())
    assert (dst / "foo").is_dir()

    # src 侧 foo 由目录改为文件
    shutil.rmtree(src / "foo")
    (src / "foo").write_text("now a file")

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert (dst / "foo").is_file(), "dst/foo 应被替换为文件"
    assert (dst / "foo").read_text() == "now a file"


def test_sync_tree_deletes_stale_directory(tmp_path: Path) -> None:
    """_sync_tree 删除 dst 中 src 不存在的目录（rmtree 分支）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("v1")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "app.py").write_text("old")
    (dst / "stale_dir").mkdir()
    (dst / "stale_dir" / "file.txt").write_text("stale")

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert not (dst / "stale_dir").exists(), "src 不存在的目录应被删除"
    assert (dst / "app.py").read_text() == "v1"


def test_sync_tree_overwrites_changed_file(tmp_path: Path) -> None:
    """_sync_tree 对 dst 已存在但内容变动的文件调 copy2 覆盖（mtime/size 不同分支）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("new content")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "app.py").write_text("old")

    _sync_tree(src, dst, shutil.ignore_patterns())

    assert (dst / "app.py").read_text() == "new content"


def test_sync_tree_skips_unchanged_file(tmp_path: Path) -> None:
    """_sync_tree 对 mtime+size 相同的文件跳过 copy2（避免不必要磁盘写）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("same")
    dst = tmp_path / "dst"
    dst.mkdir()
    # 先复制一次确保 mtime/size 一致
    shutil.copy2(src / "app.py", dst / "app.py")
    src_stat_before = (src / "app.py").stat()
    dst_stat_before = (dst / "app.py").stat()

    _sync_tree(src, dst, shutil.ignore_patterns())

    # dst 文件未被重写（mtime 不变）
    dst_stat_after = (dst / "app.py").stat()
    assert dst_stat_after.st_mtime_ns == dst_stat_before.st_mtime_ns
    assert src_stat_before.st_mtime_ns == dst_stat_after.st_mtime_ns


def test_site_packages_fingerprint_empty_when_no_dir(tmp_path: Path) -> None:
    """_site_packages_fingerprint 目录不存在时返回空串."""
    assert _site_packages_fingerprint(tmp_path / "nonexistent") == ""


def test_site_packages_fingerprint_empty_when_empty(tmp_path: Path) -> None:
    """_site_packages_fingerprint 目录存在但无 dist-info 时返回非空哈希（空输入）."""
    sp = tmp_path / "site-packages"
    sp.mkdir()
    fp = _site_packages_fingerprint(sp)
    assert isinstance(fp, str)
    assert len(fp) == 64  # sha256 hexdigest 长度


def test_site_packages_fingerprint_changes_with_dist_info(tmp_path: Path) -> None:
    """_site_packages_fingerprint 随 dist-info 目录名变化."""
    sp = tmp_path / "site-packages"
    sp.mkdir()
    fp_empty = _site_packages_fingerprint(sp)
    (sp / "rich-13.0.0.dist-info").mkdir()
    fp_rich = _site_packages_fingerprint(sp)
    (sp / "click-8.1.0.dist-info").mkdir()
    fp_both = _site_packages_fingerprint(sp)
    assert fp_empty != fp_rich
    assert fp_rich != fp_both
    assert len(fp_both) == 64


def test_site_packages_fingerprint_order_independent(tmp_path: Path) -> None:
    """_site_packages_fingerprint 对 dist-info 排序后哈希，顺序无关."""
    sp1 = tmp_path / "sp1"
    sp1.mkdir()
    (sp1 / "aaa-1.0.dist-info").mkdir()
    (sp1 / "zzz-1.0.dist-info").mkdir()
    sp2 = tmp_path / "sp2"
    sp2.mkdir()
    (sp2 / "zzz-1.0.dist-info").mkdir()
    (sp2 / "aaa-1.0.dist-info").mkdir()
    assert _site_packages_fingerprint(sp1) == _site_packages_fingerprint(sp2)


def test_copy_source_with_extra_excludes(tmp_path: Path) -> None:
    """extra_excludes 额外排除 [tool.fspack] exclude 配置的目录."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "examples").mkdir()
    (src / "examples" / "demo.py").write_text("print('demo')")
    (src / "mydata").mkdir()
    (src / "mydata" / "data.txt").write_text("data")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, extra_excludes=("examples", "mydata"))
    assert (dst / "app.py").is_file()
    assert not (dst / "examples").exists()
    assert not (dst / "mydata").exists()


def test_copy_source_extra_excludes_merged_with_builtin(tmp_path: Path) -> None:
    """extra_excludes 与内置 _EXCLUDE 合并：内置排除仍生效 + 额外排除生效."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "dist").mkdir()  # 内置排除
    (src / "dist" / "junk.txt").write_text("x")
    (src / "custom_excl").mkdir()  # 额外排除
    (src / "custom_excl" / "file.py").write_text("x")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, extra_excludes=("custom_excl",))
    assert (dst / "app.py").is_file()
    assert not (dst / "dist").exists()
    assert not (dst / "custom_excl").exists()


def test_copy_source_data_dirs_keeps_metadata_in_data_dirs(tmp_path: Path) -> None:
    """data_dirs 内的元数据/文档文件保留（pyproject.toml/README.md/uv.lock 等）.

    模拟 fspack 自身打包场景：``src/fspack/assets/templates/<each>/`` 是完整
    项目模板，其内的 ``pyproject.toml``/``README.md``/``uv.lock`` 是模板必需
    文件，必须原样保留供 ``fsp doctor --test`` 复制后构建。
    """
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    # 模拟 assets/templates/gui/tk_app/ 完整项目模板
    tpl = src / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app"
    tpl.mkdir(parents=True)
    (tpl / "pyproject.toml").write_text('[project]\nname = "tk_app"\n')
    (tpl / "tk_app.py").write_text("def main():\n    print('hi')\n")
    (tpl / "README.md").write_text("# tk_app\n")
    (tpl / "uv.lock").write_text("version = 1\n")
    (tpl / ".python-version").write_text("3.11\n")
    # 项目根目录的元数据文件仍应被剥离
    (src / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (src / "README.md").write_text("# app\n")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, data_dirs=("src/fspack/assets/templates",))
    # data_dirs 内的元数据/文档文件保留
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / "pyproject.toml").is_file()
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / "README.md").is_file()
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / "uv.lock").is_file()
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / ".python-version").is_file()
    # 应用源码保留
    assert (dst / "app.py").is_file()
    assert (dst / "src" / "fspack" / "assets" / "templates" / "gui" / "tk_app" / "tk_app.py").is_file()
    # 项目根目录的元数据文件仍被剥离（data_dirs 只保护子树内的元数据）
    assert not (dst / "pyproject.toml").exists()
    assert not (dst / "README.md").exists()


def test_copy_source_data_dirs_still_excludes_build_artifacts(tmp_path: Path) -> None:
    """data_dirs 内仍排除构建产物/缓存/IDE 等（_EXCLUDE_ALWAYS 始终生效）."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    tpl = src / "assets" / "templates" / "demo"
    tpl.mkdir(parents=True)
    (tpl / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    (tpl / "main.py").write_text("print('demo')\n")
    # 构建产物与缓存：data_dirs 内仍应排除
    (tpl / "__pycache__").mkdir()
    (tpl / "__pycache__" / "main.cpython-311.pyc").write_text("x")
    (tpl / "dist").mkdir()
    (tpl / "dist" / "junk.txt").write_text("x")
    (tpl / ".venv").mkdir()
    (tpl / ".venv" / "pyvenv.cfg").write_text("x")
    (tpl / "node_modules").mkdir()
    (tpl / "node_modules" / ".pnpm").mkdir()
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, data_dirs=("assets/templates",))
    # 元数据保留（data_dirs 保护）
    assert (dst / "assets" / "templates" / "demo" / "pyproject.toml").is_file()
    # 构建产物/缓存排除（_EXCLUDE_ALWAYS 始终生效）
    assert not (dst / "assets" / "templates" / "demo" / "__pycache__").exists()
    assert not (dst / "assets" / "templates" / "demo" / "dist").exists()
    assert not (dst / "assets" / "templates" / "demo" / ".venv").exists()
    # 前端依赖缓存排除：pnpm install 可再生，.pnpm 超长路径会导致 fsp c 清理失败
    assert not (dst / "assets" / "templates" / "demo" / "node_modules").exists()


def test_copy_source_data_dirs_empty_keeps_default_behavior(tmp_path: Path) -> None:
    """data_dirs 为空时行为与不传一致：元数据/文档照常剥离（向后兼容）."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (src / "README.md").write_text("# app\n")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, data_dirs=())
    assert (dst / "app.py").is_file()
    assert not (dst / "pyproject.toml").exists()
    assert not (dst / "README.md").exists()


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

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())

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


# ---- Nuitka 编译模式与 stamp 缓存命中测试 ----


def test_precompile_pyc_stamp_cache_hit_skips_compileall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 命中时跳过 compileall 调用，stage 标注缓存命中."""
    from fspack.builder import _pyc_stamp_key, _pyc_stamp_path

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (tmp_path / "dist" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    # 预先写入匹配的 stamp
    stamp_key = _pyc_stamp_key(dist / "src", tmp_path / "dist" / "site-packages", strip_py=False, optimize=0)
    _pyc_stamp_path(dist).parent.mkdir(parents=True, exist_ok=True)
    _pyc_stamp_path(dist).write_text(stamp_key, encoding="utf-8")

    call_count = {"n": 0}
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: call_count.__setitem__("n", call_count["n"] + 1) or _CompileCompleted(),
    )

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # stamp 命中，不调用 compileall
    assert call_count["n"] == 0
    assert st._hits == 1
    assert "缓存命中" in st._detail


def test_build_with_nuitka_invokes_compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """nuitka=True 时 build() 调用 NuitkaCompiler.compile_with_stamp 编译用户源码."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\n\n[tool.fspack]\ndata-dirs = ["assets"]\n'
    )
    (proj / "app.py").write_text("def main():\n    pass\n")
    # data-dirs 数据资源目录：含示例 .py，Nuitka 编译应跳过
    (proj / "assets" / "templates").mkdir(parents=True)
    (proj / "assets" / "templates" / "demo.py").write_text("x = 1")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())
    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.WINDOWS)

    # 拦截 NuitkaCompiler.compile_with_stamp 验证调用
    nuitka_called: dict[str, object] = {}

    def fake_compile_with_stamp(  # noqa: PLR0913
        cls: type,
        src_dir: Path,
        dist_dir: Path,
        runtime_dir: Path,
        py_version: str,
        target: Platform,
        mirror: object,
        cache_root: Path,
        *,
        stage: StageRecorder,
        entry_rels: frozenset[str] | None = None,
        ccache: bool = False,
        nuitka_packages: tuple[str, ...] = (),
        data_dirs: tuple[Path, ...] = (),
        compiler: str = "auto",
    ) -> None:
        nuitka_called["src_dir"] = src_dir
        nuitka_called["dist_dir"] = dist_dir
        nuitka_called["py_version"] = py_version
        nuitka_called["target"] = target
        nuitka_called["cache_root"] = cache_root
        nuitka_called["entry_rels"] = entry_rels
        nuitka_called["ccache"] = ccache
        nuitka_called["nuitka_packages"] = nuitka_packages
        nuitka_called["data_dirs"] = data_dirs
        nuitka_called["compiler"] = compiler
        stage.processed()
        stage.set_detail("mock 编译")

    monkeypatch.setattr(
        "fspack.packaging.nuitka.NuitkaCompiler.compile_with_stamp",
        classmethod(fake_compile_with_stamp),
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, options=BuildOptions(nuitka=True))

    assert nuitka_called["py_version"] == "3.11.9"
    assert nuitka_called["target"] is Platform.WINDOWS
    # src_dir 是 dist/src
    assert Path(str(nuitka_called["src_dir"])).name == "src"
    # dist_dir 是 proj/dist
    assert Path(str(nuitka_called["dist_dir"])).name == "dist"
    # cache_root 指向本地缓存（~/.fspack/cache/nuitka），不污染 dist/runtime
    assert Path(str(nuitka_called["cache_root"])).name == "nuitka"
    # entry_rels 包含入口文件 app.py（入口文件跳过编译，保留 .py 供 runpy.run_path 调用）
    assert nuitka_called["entry_rels"] == {"app.py"}
    # data_dirs 解析为 dist/src 下的绝对路径（assets 数据资源目录不编译）
    assert nuitka_called["data_dirs"] == (proj / "dist" / "src" / "assets",)


def test_build_nuitka_skipped_on_cross_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """交叉构建时（构建机平台 ≠ 目标平台）Nuitka 跳过，不调用编译器."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())
    # 构建机是 Linux，目标是 Windows → 交叉构建
    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.LINUX)

    nuitka_called = {"n": 0}

    def fake_compile_with_stamp(*args: object, **kwargs: object) -> None:
        nuitka_called["n"] += 1

    monkeypatch.setattr(
        "fspack.packaging.nuitka.NuitkaCompiler.compile_with_stamp",
        classmethod(fake_compile_with_stamp),
    )

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, options=BuildOptions(nuitka=True))

    # 交叉构建跳过 Nuitka
    assert nuitka_called["n"] == 0


def test_compile_user_sources_skips_nuitka_on_win7_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """win7 重编译版 runtime（.win7_runtime 标记存在）时跳过 Nuitka 编译.

    官方工具链编译的 .pyd 与重编译版 python3XX.dll ABI 不兼容（加载即
    访问违例），编译必败（verify 全判损坏回退 .pyc），前置跳过。同一
    上下文无标记时正常调用（对照组）。
    """
    from fspack.config import BuildConfig
    from fspack.packaging.pipeline.compile_stage import _compile_user_sources
    from fspack.progress import BuildTracker

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    src_dst = tmp_path / "dist" / "src"
    src_dst.mkdir(parents=True)
    (src_dst / "app.py").write_text("def main():\n    pass\n")
    runtime_dir = tmp_path / "dist" / "runtime"
    runtime_dir.mkdir(parents=True)

    def _make_ctx() -> BuildContext:
        info = ProjectInfo.from_dir(tmp_path, "3.13.14")
        cfg = BuildConfig(
            project_dir=tmp_path,
            dist_dir=tmp_path / "dist",
            embed_cache_dir=tmp_path / "cache",
            mirror=get_mirror("huawei"),
            target=Platform.WINDOWS,
        )
        return BuildContext(
            tracker=BuildTracker(),
            info=info,
            cfg=cfg,
            opts=BuildOptions(nuitka=True, no_pyc=True),
            runtime_dir=runtime_dir,
        )

    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.WINDOWS)
    nuitka_calls = {"n": 0}

    def fake_compile_with_stamp(*args: object, **kwargs: object) -> None:
        nuitka_calls["n"] += 1

    monkeypatch.setattr(
        "fspack.packaging.nuitka.NuitkaCompiler.compile_with_stamp",
        classmethod(fake_compile_with_stamp),
    )

    # 对照组：官方 runtime（无标记）→ Nuitka 正常调用
    _compile_user_sources(_make_ctx(), src_dst)
    assert nuitka_calls["n"] == 1

    # win7 重编译版标记存在 → 跳过 Nuitka（回退 .pyc）
    (runtime_dir / ".win7_runtime").write_text("3.13.14", encoding="ascii")
    _compile_user_sources(_make_ctx(), src_dst)
    assert nuitka_calls["n"] == 1


def test_normalize_exclusive_options_matrix() -> None:
    """互斥归一化矩阵：仅 nuitka + py>=3.12 标准版 Windows + 未显式关闭时翻转 no_win7_dll.

    py3.11（shim 注入不替换 runtime，ABI 兼容）/ 非 Windows / t 版 /
    显式 no_win7_dll / 非 nuitka 场景均不触发，二者可共存或原样保留。
    """
    from fspack.packaging.pipeline.executor import _normalize_exclusive_options

    # 命中：nuitka + 3.13 + Windows → 自动关闭（显式意图 > 隐式默认）
    opts = _normalize_exclusive_options(BuildOptions(nuitka=True), "3.13.14", Platform.WINDOWS)
    assert opts.no_win7_dll is True
    # py3.11：shim 注入路径与 Nuitka 产物 ABI 兼容，不互斥
    opts = _normalize_exclusive_options(BuildOptions(nuitka=True), "3.11.9", Platform.WINDOWS)
    assert opts.no_win7_dll is False
    # 非 Windows 目标：win7 替换不适用
    opts = _normalize_exclusive_options(BuildOptions(nuitka=True), "3.13.14", Platform.LINUX)
    assert opts.no_win7_dll is False
    # free-threaded（t）：needs_win7_dll 恒 False，不互斥
    opts = _normalize_exclusive_options(BuildOptions(nuitka=True), "3.13.14t", Platform.WINDOWS)
    assert opts.no_win7_dll is False
    # 已显式关闭：原样保留
    opts = _normalize_exclusive_options(BuildOptions(nuitka=True, no_win7_dll=True), "3.13.14", Platform.WINDOWS)
    assert opts.no_win7_dll is True
    # 非 nuitka：默认 win7 替换不受影响
    opts = _normalize_exclusive_options(BuildOptions(), "3.13.14", Platform.WINDOWS)
    assert opts.no_win7_dll is False


def test_normalize_exclusive_options_compiler_msvc_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """compiler=msvc 校验矩阵：Windows + nuitka + 无 MSVC 时 fail-fast，其余场景放行."""
    from fspack.exceptions import ProjectError
    from fspack.packaging.pipeline.executor import _normalize_exclusive_options

    # Windows + nuitka + compiler=msvc + 无 MSVC：raise ProjectError
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: False)
    with pytest.raises(ProjectError, match="compiler=msvc"):
        _normalize_exclusive_options(BuildOptions(nuitka=True, compiler="msvc"), "3.13.14", Platform.WINDOWS)

    # win7 归一化命中后仍执行 compiler 校验（控制流不提前 return）
    with pytest.raises(ProjectError, match="compiler=msvc"):
        _normalize_exclusive_options(BuildOptions(nuitka=True, compiler="msvc"), "3.12.10", Platform.WINDOWS)

    # 有 MSVC：通过，且 win7 归一化仍生效
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: True)
    opts = _normalize_exclusive_options(BuildOptions(nuitka=True, compiler="msvc"), "3.13.14", Platform.WINDOWS)
    assert opts.compiler == "msvc"
    assert opts.no_win7_dll is True

    # 非 Windows 目标 / 非 nuitka / 非 msvc：不校验（无 MSVC 也放行）
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: False)
    opts = _normalize_exclusive_options(BuildOptions(nuitka=True, compiler="msvc"), "3.13.14", Platform.LINUX)
    assert opts.compiler == "msvc"
    opts = _normalize_exclusive_options(BuildOptions(compiler="msvc"), "3.13.14", Platform.WINDOWS)
    assert opts.compiler == "msvc"
    opts = _normalize_exclusive_options(BuildOptions(nuitka=True, compiler="mingw"), "3.13.14", Platform.WINDOWS)
    assert opts.compiler == "mingw"


def test_build_nuitka_disables_win7_dll_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """3.13 + Windows + --nuitka 构建自动关闭 win7 组件替换（互斥归一化）并告警.

    无归一化时会先整套替换 runtime 再被编译守卫跳过——Win7 替换是无用功，
    归一化后 ensure_win7_dll 不被调用、标记不存在、warning 提示产物仅支持 Win8+。
    """
    import logging

    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.13.14")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())
    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.WINDOWS)
    monkeypatch.setattr(
        "fspack.packaging.nuitka.NuitkaCompiler.compile_with_stamp",
        classmethod(lambda cls, *a, **k: None),
    )
    win7_calls = {"n": 0}
    monkeypatch.setattr(
        "fspack.packaging.pipeline.runtime_stage.ensure_win7_dll",
        lambda *a, **k: win7_calls.__setitem__("n", win7_calls["n"] + 1),
    )

    with caplog.at_level(logging.WARNING, logger="fspack.packaging.pipeline.executor"):
        build(proj, get_mirror("huawei"), "3.13.14", target=Platform.WINDOWS, options=BuildOptions(nuitka=True))

    assert win7_calls["n"] == 0
    assert not (runtime / ".win7_runtime").exists()
    assert "互斥" in caplog.text


def test_prepare_windows_runtime_recovers_win7_residue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-win7-dll 构建遇 .win7_runtime 残留时清空 runtime 重解压官方 embed.

    dist 复用场景：上次默认构建替换的 win7 重编译版 runtime 若不恢复，
    官方工具链产物（Nuitka .pyd）在残留 runtime 内加载即崩溃，且标记
    残留会误触编译守卫跳过。恢复后标记消失、旧残留文件被清。
    """
    from fspack.config import BuildConfig
    from fspack.packaging.pipeline.runtime_stage import _prepare_windows_runtime
    from fspack.progress import BuildTracker

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    runtime_dir = tmp_path / "dist" / "runtime"
    runtime_dir.mkdir(parents=True)
    # 构造 win7 残留：官方 dll 占位（runtime_ready 判定）+ 标记 + 残留文件
    (runtime_dir / "python313.dll").write_bytes(b"")
    (runtime_dir / ".win7_runtime").write_text("3.13.14", encoding="ascii")
    (runtime_dir / "win7_only_residue.txt").write_text("stale", encoding="ascii")

    info = ProjectInfo.from_dir(tmp_path, "3.13.14")
    cfg = BuildConfig(
        project_dir=tmp_path,
        dist_dir=tmp_path / "dist",
        embed_cache_dir=tmp_path / "cache",
        mirror=get_mirror("huawei"),
        target=Platform.WINDOWS,
    )
    ctx = BuildContext(
        tracker=BuildTracker(),
        info=info,
        cfg=cfg,
        opts=BuildOptions(no_win7_dll=True),
        runtime_dir=runtime_dir,
    )

    extract_calls = {"n": 0}

    def fake_extract(zip_path: Path, dest: Path) -> None:
        extract_calls["n"] += 1
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "python313.dll").write_bytes(b"official")

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_embed", lambda v, m, c, **kw: tmp_path / "embed.zip")
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_embed", fake_extract)

    _prepare_windows_runtime(ctx)

    assert extract_calls["n"] == 1
    assert not (runtime_dir / ".win7_runtime").exists()
    assert not (runtime_dir / "win7_only_residue.txt").exists()
    assert (runtime_dir / "python313.dll").read_bytes() == b"official"


# --- clean_dist 测试（原 tests/test_commands.py 的 clean 测试） ---


def test_clean_dist_removes_dist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "x.txt").write_text("x")
    clean_dist(tmp_path)
    # 无保留文件时 dist 整体移除（不重建空目录），清理彻底
    assert not dist.exists()


def test_clean_dist_preserves_nsi(tmp_path: Path) -> None:
    """clean 保留 installer.nsi 便于改代码后重新打包分发."""
    dist = tmp_path / "dist"
    dist.mkdir()
    nsi = dist / "installer.nsi"
    nsi.write_text('Name "app"', encoding="utf-8")
    (dist / "x.txt").write_text("x")
    clean_dist(tmp_path)
    assert dist.is_dir()
    assert nsi.is_file()
    assert nsi.read_text(encoding="utf-8") == 'Name "app"'
    assert not (dist / "x.txt").exists()


def test_clean_dist_no_dist(tmp_path: Path) -> None:
    clean_dist(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH 260 长路径场景")
def test_clean_dist_removes_over_maxpath(tmp_path: Path) -> None:
    """dist 内含超 MAX_PATH 260 的深层路径时 clean 仍能整体删除.

    场景来源：模板 frontend/node_modules/.pnpm 下路径超 260，普通
    ``shutil.rmtree`` 抛 ``WinError 3`` 中途残留（fsp c 清理失败的根因）。
    """
    deep = tmp_path / "dist"
    for i in range(18):
        deep = deep / f"level_{i:02d}_padding_padding_padding"
    assert len(str(deep)) > 260  # 前置：确认已触发长路径场景
    Path("\\\\?\\" + str(deep)).mkdir(parents=True)
    (Path("\\\\?\\" + str(deep)) / "f.js").write_text("x")

    clean_dist(tmp_path)
    assert not (tmp_path / "dist").exists()


# --- _handle_dist_incomplete 测试（iter-140 扩展 iter-130 dist 半成品检测） ---


def test_handle_dist_incomplete_no_dist(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 目录不存在时不告警."""
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(tmp_path / "nonexistent", auto_clean=False)
    assert not caplog.records


def test_handle_dist_incomplete_empty_dist(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 目录为空时不告警（无构建产物）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert not caplog.records


def test_handle_dist_incomplete_only_nsi(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 仅含 installer.nsi（clean_dist 保留）时不告警."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert not caplog.records


def test_handle_dist_incomplete_artifacts_no_stamp_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含构建产物但无 stamp 文件时告警（中断/失败的上次构建）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "src").mkdir()
    (dist / "app.exe").write_bytes(b"")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert any("残留" in r.message and "fsp c" in r.message for r in caplog.records)


def test_handle_dist_incomplete_with_pyc_stamp_no_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含产物且有 .pyc_stamp 时不告警（上次构建至少完成到编译阶段）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "src").mkdir()
    (dist / ".pyc_stamp").write_text("fingerprint", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert not caplog.records


def test_handle_dist_incomplete_with_nuitka_stamp_no_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含产物且有 .nuitka_compile_stamp 时不告警."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / ".nuitka_compile_stamp").write_text("fingerprint", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert not caplog.records


# --- _handle_dist_incomplete auto_clean 与 .build_failed 测试（iter-140） ---


def test_handle_dist_incomplete_auto_clean_removes_artifacts(tmp_path: Path) -> None:
    """auto_clean=True 时清空 dist 残留产物（不保留 .build_failed）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "src").mkdir()
    (dist / "app.exe").write_bytes(b"")
    (dist / _BUILD_FAILED).write_text('{"stage":"x"}', encoding="utf-8")

    _handle_dist_incomplete(dist, auto_clean=True)

    # 无保留文件（无 installer.nsi）时 dist 整体移除
    assert not dist.exists()


def test_handle_dist_incomplete_auto_clean_preserves_nsi(tmp_path: Path) -> None:
    """auto_clean=True 仍保留 installer.nsi（便于重新打包）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")

    _handle_dist_incomplete(dist, auto_clean=True)

    assert (dist / "installer.nsi").read_text(encoding="utf-8") == 'Name "app"'
    assert not (dist / "runtime").exists()


def test_handle_dist_incomplete_build_failed_shows_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含 .build_failed 时输出失败阶段与错误信息."""
    import json

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text(
        json.dumps({"stage": "编译源码", "error": "NuitkaError: compile failed", "timestamp": "2026-08-04T21:00:00"}),
        encoding="utf-8",
    )

    _handle_dist_incomplete(dist, auto_clean=False)

    assert any("残留" in r.message for r in caplog.records)


def test_handle_dist_incomplete_build_failed_auto_clean_removes_it(tmp_path: Path) -> None:
    """auto_clean=True 时 .build_failed 也被清除（全新开始）."""
    import json

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text(
        json.dumps({"stage": "编译源码", "error": "failed"}),
        encoding="utf-8",
    )

    _handle_dist_incomplete(dist, auto_clean=True)

    assert not (dist / _BUILD_FAILED).exists()


def test_handle_dist_incomplete_no_artifacts_with_build_failed_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """dist 无产物但有 .build_failed 时仍视为半成品并告警."""
    import json

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text(
        json.dumps({"stage": "下载依赖", "error": "NetworkError"}),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)

    assert any("残留" in r.message for r in caplog.records)


# --- _save/_load/_remove_build_failure 测试（iter-140） ---


def test_save_build_failure_writes_json(tmp_path: Path) -> None:
    """_save_build_failure 写入 JSON 含 stage/error/timestamp."""
    import json
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    dist = tmp_path / "dist"
    dist.mkdir()

    tracker = MagicMock()
    # SimpleNamespace 而非 MagicMock(name=...)：MagicMock 的 name 参数设置 repr
    # 名称而非属性，records[-1].name 返回 MagicMock 无法 JSON 序列化
    tracker.records = [SimpleNamespace(name="解析项目"), SimpleNamespace(name="下载依赖")]

    exc = RuntimeError("test error")
    _save_build_failure(dist, tracker, exc)

    data = json.loads((dist / _BUILD_FAILED).read_text(encoding="utf-8"))
    assert data["stage"] == "下载依赖"
    assert "RuntimeError" in data["error"]
    assert "test error" in data["error"]
    assert "timestamp" in data


def test_save_build_failure_no_records_uses_unknown(tmp_path: Path) -> None:
    """tracker.records 为空时 stage 记为'未知'."""
    import json
    from unittest.mock import MagicMock

    dist = tmp_path / "dist"
    dist.mkdir()

    tracker = MagicMock()
    tracker.records = []  # type: ignore[list-item]

    _save_build_failure(dist, tracker, ValueError("err"))

    data = json.loads((dist / _BUILD_FAILED).read_text(encoding="utf-8"))
    assert data["stage"] == "未知"


def test_save_build_failure_truncates_long_error(tmp_path: Path) -> None:
    """错误信息超 500 字符时截断."""
    import json
    from unittest.mock import MagicMock

    dist = tmp_path / "dist"
    dist.mkdir()

    tracker = MagicMock()
    tracker.records = []  # type: ignore[list-item]
    long_msg = "x" * 600
    _save_build_failure(dist, tracker, RuntimeError(long_msg))

    data = json.loads((dist / _BUILD_FAILED).read_text(encoding="utf-8"))
    assert len(data["error"]) <= 500
    assert data["error"].endswith("...")


def test_save_build_failure_dist_not_exists_skips(tmp_path: Path) -> None:
    """dist 目录不存在时跳过写入（构建可能在创建 dist 前失败）."""
    from unittest.mock import MagicMock

    tracker = MagicMock()
    tracker.records = []  # type: ignore[list-item]

    _save_build_failure(tmp_path / "nonexistent", tracker, RuntimeError("err"))

    assert not (tmp_path / "nonexistent").exists()


def test_load_build_failure_returns_dict(tmp_path: Path) -> None:
    """_load_build_failure 读取 JSON 返回 dict."""
    import json

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text(
        json.dumps({"stage": "编译", "error": "err", "timestamp": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )

    result = _load_build_failure(dist)
    assert result is not None
    assert result["stage"] == "编译"
    assert result["error"] == "err"


def test_load_build_failure_no_file_returns_none(tmp_path: Path) -> None:
    """文件不存在时返回 None."""
    dist = tmp_path / "dist"
    dist.mkdir()

    assert _load_build_failure(dist) is None


def test_load_build_failure_invalid_json_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """JSON 解析失败返回 None 并告警."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text("not json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        result = _load_build_failure(dist)

    assert result is None


def test_remove_build_failure_deletes_file(tmp_path: Path) -> None:
    """_remove_build_failure 删除 .build_failed 文件."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text("{}", encoding="utf-8")

    _remove_build_failure(dist)

    assert not (dist / _BUILD_FAILED).exists()


def test_remove_build_failure_no_file_noop(tmp_path: Path) -> None:
    """文件不存在时 _remove_build_failure 无操作."""
    dist = tmp_path / "dist"
    dist.mkdir()

    _remove_build_failure(dist)  # 不抛异常


# --- _clean_dist_dir 与 clean_dist 保留诊断测试（iter-140） ---


def test_clean_dist_dir_keeps_diagnostics_preserves_build_failed(tmp_path: Path) -> None:
    """keep_diagnostics=True 时保留 .build_failed 与 installer.nsi."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / _BUILD_FAILED).write_text('{"stage":"x"}', encoding="utf-8")
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")

    _clean_dist_dir(dist, keep_diagnostics=True)

    assert (dist / _BUILD_FAILED).read_text(encoding="utf-8") == '{"stage":"x"}'
    assert (dist / "installer.nsi").read_text(encoding="utf-8") == 'Name "app"'
    assert not (dist / "runtime").exists()


def test_clean_dist_dir_no_diagnostics_removes_build_failed(tmp_path: Path) -> None:
    """keep_diagnostics=False 时删除 .build_failed（全新开始）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / _BUILD_FAILED).write_text('{"stage":"x"}', encoding="utf-8")

    _clean_dist_dir(dist, keep_diagnostics=False)

    assert not (dist / _BUILD_FAILED).exists()
    assert not (dist / "runtime").exists()


def test_clean_dist_preserves_build_failed(tmp_path: Path) -> None:
    """fsp c (clean_dist) 保留 .build_failed 便于用户排查."""
    project = tmp_path / "proj"
    dist = project / "dist"
    dist.mkdir(parents=True)
    (dist / "runtime").mkdir()
    (dist / _BUILD_FAILED).write_text('{"stage":"编译"}', encoding="utf-8")
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")

    clean_dist(project)

    assert (dist / _BUILD_FAILED).read_text(encoding="utf-8") == '{"stage":"编译"}'
    assert (dist / "installer.nsi").read_text(encoding="utf-8") == 'Name "app"'
    assert not (dist / "runtime").exists()


# --- .build_ok 完成标记测试（no_pyc/交叉构建二次构建误判修复） ---


def test_has_build_stamps_recognizes_build_ok(tmp_path: Path) -> None:
    """仅存在 .build_ok（无编译 stamp）时也视为已完成构建（no_pyc/交叉构建场景）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    assert not _has_build_stamps(dist)

    _save_build_ok(dist)
    assert (dist / _BUILD_OK).is_file()
    assert _has_build_stamps(dist)


def test_remove_build_ok_deletes_marker(tmp_path: Path) -> None:
    """_remove_build_ok 删除标记文件（构建开始时清旧标记），不存在时无操作."""
    dist = tmp_path / "dist"
    dist.mkdir()
    _save_build_ok(dist)
    _remove_build_ok(dist)
    assert not (dist / _BUILD_OK).is_file()
    _remove_build_ok(dist)  # 不抛异常


def test_clean_dist_dir_keeps_diagnostics_removes_build_ok(tmp_path: Path) -> None:
    """.build_ok 是完成标记而非诊断信息，随清理删除（避免空 dist 被误判有效）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    _save_build_ok(dist)

    _clean_dist_dir(dist, keep_diagnostics=True)

    # 无保留文件时 dist 整体移除，.build_ok 一并消失
    assert not dist.exists()


def test_build_success_writes_build_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build() 成功完成后写入 dist/.build_ok 并清除 .build_failed."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())
    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.WINDOWS)

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    assert (proj / "dist" / _BUILD_OK).is_file()
    assert not (proj / "dist" / _BUILD_FAILED).exists()


def test_build_keyboard_interrupt_writes_build_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C（KeyboardInterrupt，非 Exception 子类）也写入 .build_failed 标记."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")
    # 预置 dist 目录：_save_build_failure 要求 dist 存在才写入
    (proj / "dist").mkdir()

    def raise_interrupt(ctx: object) -> Path:
        raise KeyboardInterrupt()

    monkeypatch.setattr("fspack.packaging.pipeline.executor._prepare_runtime", raise_interrupt)

    with pytest.raises(KeyboardInterrupt):
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    failed = _load_build_failure(proj / "dist")
    assert failed is not None, "KeyboardInterrupt 未写入 .build_failed"
    assert "KeyboardInterrupt" in failed["error"]
    assert not (proj / "dist" / _BUILD_OK).exists()


# --- _trim_standalone_runtime 测试 ---


def _symlink_or_skip(target: str, link: Path) -> None:
    """尝试创建符号链接，Windows 无权限时跳过测试.

    这些测试验证 Linux standalone runtime 的符号链接处理
    （``_trim_standalone_runtime`` 对 Windows 平台直接 return），
    Windows 非管理员环境无法创建符号链接时跳过而非失败。
    启用 Windows 开发者模式或以管理员运行可解除限制。
    """
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"无法创建符号链接（Windows 需开发者模式或管理员权限）: {e}")


def _make_standalone_runtime(tmp_path: Path, py_version: str = "3.11.9") -> Path:
    """构造最小 standalone runtime 目录树供 _trim_standalone_runtime 测试."""
    runtime = tmp_path / "runtime"
    major, minor = py_version.split(".")[:2]
    py_tag = f"python{major}.{minor}"

    bin_dir = runtime / "python" / "bin"
    bin_dir.mkdir(parents=True)
    py_bin = bin_dir / py_tag
    py_bin.write_bytes(b"\x7fELF" + b"x" * 1024)
    _symlink_or_skip(py_tag, bin_dir / "python3")
    _symlink_or_skip(py_tag, bin_dir / "python")
    (bin_dir / f"{py_tag}-config").write_text("#!/bin/sh\n")
    for name in ("2to3", "idle3", "pydoc3", "pip", "pip3"):
        (bin_dir / name).write_text("#!/bin/sh\n")
    (bin_dir / "pip3.11").write_text("#!/bin/sh\n")

    lib_dir = runtime / "python" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / f"libpython{major}.{minor}.so.1.0").write_bytes(b"\x7fELF" + b"y" * 2048)
    _symlink_or_skip(f"libpython{major}.{minor}.so.1.0", lib_dir / f"libpython{major}.{minor}.so")
    (lib_dir / "libtcl9.0.so").write_bytes(b"tcl" + b"t" * 512)
    (lib_dir / "libtk9.0.so").write_bytes(b"tk" + b"k" * 512)
    (lib_dir / "tcl9.0").mkdir()
    (lib_dir / "tcl9.0" / "init.tcl").write_text("# tcl")
    (lib_dir / "tk9.0").mkdir()
    (lib_dir / "tk9.0" / "tk.tcl").write_text("# tk")
    (lib_dir / "itcl4.3.5").mkdir()
    (lib_dir / "itcl4.3.5" / "itcl.tcl").write_text("# itcl")
    (lib_dir / "thread3.0.4").mkdir()
    (lib_dir / "thread3.0.4" / "thread.tcl").write_text("# thread")

    stdlib = lib_dir / py_tag
    stdlib.mkdir(parents=True)
    (stdlib / "site-packages").mkdir()

    include_dir = runtime / "python" / "include"
    include_dir.mkdir(parents=True)
    (include_dir / "Python.h").write_text("#define Py_Version")

    share_dir = runtime / "python" / "share"
    share_dir.mkdir(parents=True)
    (share_dir / "man" / "man1").mkdir(parents=True)
    (share_dir / "man" / "man1" / f"{py_tag}.1").write_text(".TH python")

    return runtime


def test_trim_standalone_runtime_windows_skips(tmp_path: Path) -> None:
    """Windows 目标跳过精简（embed python 无调试符号）."""
    runtime = _make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.WINDOWS, st, has_tkinter=False)

    assert (runtime / "python" / "bin" / "python3.11").is_file()
    assert (runtime / "python" / "lib" / "libpython3.11.so.1.0").is_file()
    record = st._finalize()
    assert record.bytes_saved == 0


def _make_windows_t_runtime(tmp_path: Path) -> Path:
    """构造扁平化布局的 Windows 自由线程 runtime 目录树.

    模拟 python-build-standalone freethreaded tarball 解压扁平化后的结构：
    runtime 根含 python.exe/python3.14t.exe/pdb、Lib/DLLs/include/libs/Scripts/tcl。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "Lib").mkdir()
    (runtime / "Lib" / "encodings").mkdir()
    (runtime / "DLLs").mkdir()
    (runtime / "include").mkdir()
    (runtime / "libs").mkdir()
    (runtime / "Scripts").mkdir()
    (runtime / "tcl").mkdir()
    (runtime / "python.exe").write_bytes(b"exe")
    (runtime / "pythonw.exe").write_bytes(b"exe")
    (runtime / "python3.14t.exe").write_bytes(b"exe")
    (runtime / "pythonw3.14t.exe").write_bytes(b"exe")
    (runtime / "python314t.pdb").write_bytes(b"pdb" * 100)
    (runtime / "python3t.pdb").write_bytes(b"pdb" * 50)
    (runtime / "DLLs" / "_ssl.cp314t-win_amd64.pdb").write_bytes(b"pdb" * 10)
    (runtime / "DLLs" / "_ssl.cp314t-win_amd64.pyd").write_bytes(b"pyd")
    (runtime / "DLLs" / "_socket.cp314t-win_amd64.pdb").write_bytes(b"pdb" * 10)
    (runtime / "include" / "Python.h").write_text("#include")
    (runtime / "libs" / "python314t.lib").write_bytes(b"lib")
    (runtime / "tcl" / "init.tcl").write_text("# tcl")
    return runtime


def test_trim_standalone_runtime_windows_t_strips_dev_files(tmp_path: Path) -> None:
    """Windows 自由线程版（standalone 布局）剥离 pdb/include/libs/别名 exe/tcl."""
    runtime = _make_windows_t_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.14.6t", Platform.WINDOWS, st, has_tkinter=False)

    # pdb 全部剥离（runtime 根与 DLLs/）
    assert not list(runtime.glob("*.pdb"))
    assert not list((runtime / "DLLs").glob("*.pdb"))
    # 开发期目录剥离
    assert not (runtime / "include").exists()
    assert not (runtime / "libs").exists()
    assert not (runtime / "Scripts").exists()
    # 非 tkinter 项目剥离 tcl/
    assert not (runtime / "tcl").exists()
    # 版本别名 exe 剥离，python.exe/pythonw.exe 保留（fsp r --debug 用）
    assert (runtime / "python.exe").is_file()
    assert (runtime / "pythonw.exe").is_file()
    assert not (runtime / "python3.14t.exe").exists()
    assert not (runtime / "pythonw3.14t.exe").exists()
    # pyd 与 stdlib 保留
    assert (runtime / "DLLs" / "_ssl.cp314t-win_amd64.pyd").is_file()
    assert (runtime / "Lib" / "encodings").is_dir()
    record = st._finalize()
    assert record.bytes_saved > 0


def test_trim_standalone_runtime_windows_t_keeps_tcl_for_tkinter(tmp_path: Path) -> None:
    """tkinter 项目保留 tcl/（_tkinter.pyd 运行时需要 Tcl/Tk 脚本库）."""
    runtime = _make_windows_t_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.14.6t", Platform.WINDOWS, st, has_tkinter=True)

    assert (runtime / "tcl").is_dir()
    assert not list(runtime.glob("*.pdb"))


def test_trim_standalone_runtime_windows_t_idempotent(tmp_path: Path) -> None:
    """重复精简幂等：二次调用无报错、不再累计节省字节."""
    runtime = _make_windows_t_runtime(tmp_path)
    st1 = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.14.6t", Platform.WINDOWS, st1, has_tkinter=False)
    saved1 = st1._finalize().bytes_saved
    st2 = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.14.6t", Platform.WINDOWS, st2, has_tkinter=False)
    assert st2._finalize().bytes_saved == 0
    assert saved1 > 0


def test_trim_standalone_runtime_missing_python_dir_skips(tmp_path: Path) -> None:
    """standalone runtime 目录不存在时不报错."""
    runtime = tmp_path / "runtime"
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=False)

    record = st._finalize()
    assert record.bytes_saved == 0


def test_trim_standalone_runtime_strips_libpython(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标 strip libpython 调试符号."""
    runtime = _make_standalone_runtime(tmp_path)

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        target_path = Path(cmd[-1])
        if target_path.is_file():
            original = target_path.read_bytes()
            target_path.write_bytes(original[:100])
        return _CompileCompleted()

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", fake_run)

    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True)

    record = st._finalize()
    assert record.bytes_saved > 0
    assert (runtime / "python" / "lib" / "libpython3.11.so").is_symlink()


def test_trim_standalone_runtime_deletes_python_binary(tmp_path: Path) -> None:
    """Linux 目标删除 python3.X 二进制与符号链接."""
    runtime = _make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True, strip_symbols=False)

    bin_dir = runtime / "python" / "bin"
    assert not (bin_dir / "python3.11").exists()
    assert not (bin_dir / "python3").exists()
    assert not (bin_dir / "python").exists()
    assert not (bin_dir / "python3.11-config").exists()


def test_trim_standalone_runtime_deletes_dev_bin_files(tmp_path: Path) -> None:
    """Linux 目标删除 2to3/idle3/pip3/pydoc3 等开发工具脚本."""
    runtime = _make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True, strip_symbols=False)

    bin_dir = runtime / "python" / "bin"
    assert not (bin_dir / "2to3").exists()
    assert not (bin_dir / "idle3").exists()
    assert not (bin_dir / "pydoc3").exists()
    assert not (bin_dir / "pip").exists()
    assert not (bin_dir / "pip3").exists()
    assert not (bin_dir / "pip3.11").exists()


def test_trim_standalone_runtime_deletes_include_share(tmp_path: Path) -> None:
    """Linux 目标删除 include/ 与 share/ 目录."""
    runtime = _make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True, strip_symbols=False)

    assert not (runtime / "python" / "include").exists()
    assert not (runtime / "python" / "share").exists()


def test_trim_standalone_runtime_strips_tcl_tk_when_no_tkinter(tmp_path: Path) -> None:
    """非 tkinter 项目剥离 Tcl/Tk 运行时文件."""
    runtime = _make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=False, strip_symbols=False)

    lib_dir = runtime / "python" / "lib"
    assert not (lib_dir / "libtcl9.0.so").exists()
    assert not (lib_dir / "libtk9.0.so").exists()
    assert not (lib_dir / "tcl9.0").exists()
    assert not (lib_dir / "tk9.0").exists()
    assert not (lib_dir / "itcl4.3.5").exists()
    assert not (lib_dir / "thread3.0.4").exists()


def test_trim_standalone_runtime_keeps_tcl_tk_when_tkinter(tmp_path: Path) -> None:
    """tkinter 项目保留 Tcl/Tk 运行时."""
    runtime = _make_standalone_runtime(tmp_path)
    st = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st, has_tkinter=True, strip_symbols=False)

    lib_dir = runtime / "python" / "lib"
    assert (lib_dir / "libtcl9.0.so").is_file()
    assert (lib_dir / "libtk9.0.so").is_file()
    assert (lib_dir / "tcl9.0").is_dir()
    assert (lib_dir / "tk9.0").is_dir()
    assert (lib_dir / "itcl4.3.5").is_dir()
    assert (lib_dir / "thread3.0.4").is_dir()


def test_trim_standalone_runtime_idempotent(tmp_path: Path) -> None:
    """重复调用幂等：二次调用 bytes_saved 为 0."""
    runtime = _make_standalone_runtime(tmp_path)
    st1 = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st1, has_tkinter=False, strip_symbols=False)
    saved1 = st1._finalize().bytes_saved
    assert saved1 > 0

    st2 = StageRecorder("精简 runtime")
    _trim_standalone_runtime(runtime, "3.11.9", Platform.LINUX, st2, has_tkinter=False, strip_symbols=False)
    saved2 = st2._finalize().bytes_saved
    assert saved2 == 0


# --- _strip_elf_symbols 测试 ---


class _StripFailed:
    """模拟 strip 命令失败（非零退出码）."""

    returncode = 1
    stdout = ""
    stderr = "strip: bad file"


def test_strip_elf_symbols_strip_missing_silently_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip 命令缺失（FileNotFoundError）静默跳过返回 (0, 0)."""
    lib = tmp_path / "libpython3.11.so.1.0"
    lib.write_bytes(b"\x7fELF" + b"x" * 100)

    def fake_run(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError("strip not found")

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", fake_run)

    ok, saved = _strip_elf_symbols(lib, "linux")
    assert ok == 0
    assert saved == 0
    assert lib.read_bytes() == b"\x7fELF" + b"x" * 100


def test_strip_elf_symbols_strip_fails_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip 命令返回非零退出码时返回 (0, 0) 不抛异常."""
    lib = tmp_path / "libpython3.11.so.1.0"
    lib.write_bytes(b"\x7fELF" + b"x" * 100)

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", lambda *a, **kw: _StripFailed())

    ok, saved = _strip_elf_symbols(lib, "linux")
    assert ok == 0
    assert saved == 0


def test_strip_elf_symbols_success_returns_saved_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip 成功时返回 (1, saved_bytes)."""
    lib = tmp_path / "libpython3.11.so.1.0"
    lib.write_bytes(b"\x7fELF" + b"x" * 200)

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        target = Path(cmd[-1])
        target.write_bytes(b"\x7fELF" + b"x" * 46)
        return _CompileCompleted()

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", fake_run)

    ok, saved = _strip_elf_symbols(lib, "linux")
    assert ok == 1
    assert saved == 204 - 50


def test_strip_elf_symbols_already_stripped_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已 stripped 文件 strip 后体积未变返回 saved=0."""
    lib = tmp_path / "libpython3.11.so.1.0"
    lib.write_bytes(b"\x7fELF" + b"x" * 100)

    monkeypatch.setattr("fspack.packaging.pyc.subprocess.run", lambda *a, **kw: _CompileCompleted())

    ok, saved = _strip_elf_symbols(lib, "linux")
    assert ok == 1
    assert saved == 0


# --- _strip_tcl_tk_counted 测试 ---


def test_strip_tcl_tk_counted_no_lib_dir(tmp_path: Path) -> None:
    """lib 目录不存在时返回 (0, 0, 0)."""
    python_dir = tmp_path / "python"
    python_dir.mkdir()
    saved, dirs, files = _strip_tcl_tk_counted(python_dir)
    assert (saved, dirs, files) == (0, 0, 0)


def test_strip_tcl_tk_counted_strips_files_and_dirs(tmp_path: Path) -> None:
    """剥离 Tcl/Tk 共享库、脚本目录、itcl/thread 扩展."""
    python_dir = tmp_path / "python"
    lib_dir = python_dir / "lib"
    lib_dir.mkdir(parents=True)

    (lib_dir / "libtcl9.0.so").write_bytes(b"tcl" * 100)
    (lib_dir / "libtk9.0.so").write_bytes(b"tk" * 50)
    (lib_dir / "tcl9.0").mkdir()
    (lib_dir / "tcl9.0" / "init.tcl").write_bytes(b"x" * 50)
    (lib_dir / "tk9.0").mkdir()
    (lib_dir / "tk9.0" / "tk.tcl").write_bytes(b"y" * 30)
    (lib_dir / "itcl4.3.5").mkdir()
    (lib_dir / "itcl4.3.5" / "itcl.tcl").write_bytes(b"z" * 20)
    (lib_dir / "thread3.0.4").mkdir()
    (lib_dir / "thread3.0.4" / "thread.tcl").write_bytes(b"w" * 10)
    (lib_dir / "libpython3.11.so.1.0").write_bytes(b"py" * 200)
    (lib_dir / "libc.so.6").write_bytes(b"c" * 100)

    saved, dirs, files = _strip_tcl_tk_counted(python_dir)

    assert dirs == 4
    assert files == 2
    assert saved == 510
    assert (lib_dir / "libpython3.11.so.1.0").is_file()
    assert (lib_dir / "libc.so.6").is_file()
    assert not (lib_dir / "libtcl9.0.so").exists()
    assert not (lib_dir / "libtk9.0.so").exists()
    assert not (lib_dir / "tcl9.0").exists()
    assert not (lib_dir / "tk9.0").exists()


def test_strip_tcl_tk_counted_handles_symlinks(tmp_path: Path) -> None:
    """符号链接被 unlink 不计 saved_bytes."""
    python_dir = tmp_path / "python"
    lib_dir = python_dir / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "libtcl9.0.so").write_bytes(b"tcl" * 10)
    _symlink_or_skip("libtcl9.0.so", lib_dir / "libtcl9.0.so.1")

    saved, dirs, files = _strip_tcl_tk_counted(python_dir)

    assert saved == 30
    assert files == 2
    assert dirs == 0


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
    _make_standalone_runtime(runtime_dir.parent)  # runtime_dir = dist/runtime

    _slim_runtime(ctx, has_tkinter=False)

    bin_dir = runtime_dir / "python" / "bin"
    assert (bin_dir / "python3.11").is_file()
    assert (bin_dir / "2to3").is_file()
    assert (runtime_dir / "python" / "include").is_dir()
    assert (runtime_dir / "python" / "lib" / "libtcl9.0.so").is_file()


def test_slim_runtime_linux_calls_trim(tmp_path: Path) -> None:
    """Linux 目标调用 _trim_standalone_runtime 删文件."""
    ctx, runtime_dir = _make_slim_runtime_context(tmp_path, target=Platform.LINUX)
    _make_standalone_runtime(runtime_dir.parent)  # runtime_dir = dist/runtime

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
    # dist_dir 需预先存在：_build_one_loader 直接 write_text 到 dist_dir/<wrapper>
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
    import os
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
    import os
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


# --- iter-148 前后端分离 Web 打包：web_static_dirs 保护 ---


def test_copy_source_web_static_dirs_keeps_metadata(tmp_path: Path) -> None:
    """web_static_dirs 内的元数据/文档文件保留（与 data_dirs 同等保护）."""
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    # 模拟前端构建产物目录 dist/
    dist_dir = src / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html></html>")
    (dist_dir / "pyproject.toml").write_text('[project]\nname = "frontend"\n')
    (dist_dir / "README.md").write_text("# frontend\n")
    # 项目根目录的元数据文件仍应被剥离
    (src / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (src / "README.md").write_text("# app\n")
    dst = tmp_path / "out" / "src"

    copy_source(src, dst, web_static_dirs=("dist",))
    # web_static_dirs 内的元数据/文档文件保留
    assert (dst / "dist" / "index.html").is_file()
    assert (dst / "dist" / "pyproject.toml").is_file()
    assert (dst / "dist" / "README.md").is_file()
    # 应用源码保留
    assert (dst / "app.py").is_file()
    # 项目根目录的元数据文件仍被剥离
    assert not (dst / "pyproject.toml").exists()
    assert not (dst / "README.md").exists()


def test_strip_py_sources_skips_web_static_dirs(tmp_path: Path) -> None:
    """``web_static_dirs`` 内的 .py 不剥离（前端产物目录原样保留）."""
    from fspack.builder import _strip_py_sources

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('app')")
    # 模拟前端构建产物目录 dist/ 下的 .py 文件（如 JS 工具脚本）
    web_dir = src / "dist"
    web_dir.mkdir()
    (web_dir / "tool.py").write_text("def run():\n    pass\n")
    # 为两个 .py 都生成 .pyc（确保 PEP 3147 迁移条件满足，区别仅在 web_static_dirs 跳过）
    _make_pyc_file(src / "app.py", "3.11", optimize=0)
    _make_pyc_file(web_dir / "tool.py", "3.11", optimize=0)

    web_static_dirs = (web_dir.resolve(),)
    stripped = _strip_py_sources([src], py_version="3.11.9", optimize=0, web_static_dirs=web_static_dirs)

    # 仅 app.py 被剥离，tool.py 保留
    assert stripped == 1
    assert not (src / "app.py").exists()
    assert (web_dir / "tool.py").is_file()


# ---- 前端构建阶段（fsp b 自动识别 web 结构） ----


def _write_frontend_pkg(root: Path, *, build: bool = True) -> Path:
    """写入前端项目骨架：package.json（build 脚本按需）."""
    root.mkdir(parents=True, exist_ok=True)
    scripts = {"build": "vite build"} if build else {"dev": "vite"}
    (root / "package.json").write_text(json.dumps({"name": root.name, "scripts": scripts}), encoding="utf-8")
    return root


def test_detect_frontends_configured_walk_up(tmp_path: Path) -> None:
    """web-static-dirs 配置的产物目录向上定位最近 package.json（flask/fastapi 布局）."""
    fe = _write_frontend_pkg(tmp_path / "frontend")
    fps = _detect_frontends(tmp_path, ("frontend/deploy",))
    assert len(fps) == 1
    assert fps[0].root == fe.resolve()
    assert fps[0].output_dirs == ((tmp_path / "frontend" / "deploy").resolve(),)


def test_detect_frontends_auto_scan_nested(tmp_path: Path) -> None:
    """未配置项目结构扫描：src/<pkg>/frontend 命中，node_modules 与超深目录剪枝."""
    fe = _write_frontend_pkg(tmp_path / "src" / "webview_app" / "frontend")
    # node_modules 内的 package.json 不触发识别
    nm_pkg = fe / "node_modules" / "left-pad"
    nm_pkg.mkdir(parents=True)
    (nm_pkg / "package.json").write_text('{"scripts": {"build": "x"}}', encoding="utf-8")
    # 超过扫描深度的目录不触发识别
    _write_frontend_pkg(tmp_path / "a" / "b" / "c" / "d" / "frontend")

    fps = _detect_frontends(tmp_path, ())
    assert [fp.root for fp in fps] == [fe]
    assert fps[0].output_dirs == (fe / "deploy", fe / "dist")


def test_detect_frontends_pure_static_dir_not_detected(tmp_path: Path) -> None:
    """纯手写 html 的最小模板（无 package.json/build 脚本）不识别、不构建."""
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "index.html").write_text("<html/>", encoding="utf-8")
    # 配置路径：向上找不到 package.json；扫描路径：无 build 脚本
    assert _detect_frontends(tmp_path, ("frontend",)) == []
    _write_frontend_pkg(tmp_path / "other" / "fe", build=False)
    assert _detect_frontends(tmp_path, ()) == []


def test_detect_frontends_configured_preferred_over_auto(tmp_path: Path) -> None:
    """同根目录命中两条路径时按根目录去重，保留配置来源（产物目录精确）."""
    _write_frontend_pkg(tmp_path / "frontend")
    fps = _detect_frontends(tmp_path, ("frontend/deploy",))
    assert len(fps) == 1
    assert fps[0].output_dirs == ((tmp_path / "frontend" / "deploy").resolve(),)


def test_build_frontend_skips_when_output_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物目录非空时跳过构建（增量语义，不执行任何命令）."""
    fe = _write_frontend_pkg(tmp_path / "frontend")
    deploy = fe / "deploy"
    deploy.mkdir()
    (deploy / "index.html").write_text("<html/>", encoding="utf-8")

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._run_cmd", lambda *a: calls.append(a))
    detail = _build_frontend(_detect_frontends(tmp_path, ()))
    assert calls == []
    assert "跳过" in detail


def test_build_frontend_installs_and_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物缺失时先 install（node_modules 不存在）再 build，产物就绪."""
    fe = _write_frontend_pkg(tmp_path / "frontend")
    calls: list[tuple[str, ...]] = []

    def fake_run_cmd(exe: str, args: Sequence[str], cwd: Path) -> None:
        calls.append(tuple(args))
        if list(args) == ["run", "build"]:
            deploy = fe / "deploy"
            deploy.mkdir(parents=True, exist_ok=True)
            (deploy / "index.html").write_text("<html/>", encoding="utf-8")

    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._run_cmd", fake_run_cmd)
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._resolve_pm", lambda: ("pnpm", "C:/fake/pnpm.cmd"))
    detail = _build_frontend(_detect_frontends(tmp_path, ()))
    assert ("install",) in calls
    assert ("run", "build") in calls
    assert "pnpm" in detail and "frontend" in detail


def test_build_frontend_existing_node_modules_skips_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """node_modules 已存在时直接 build（不重复 install）."""
    fe = _write_frontend_pkg(tmp_path / "frontend")
    (fe / "node_modules").mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_run_cmd(exe: str, args: Sequence[str], cwd: Path) -> None:
        calls.append(tuple(args))
        if list(args) == ["run", "build"]:
            (fe / "dist").mkdir()
            (fe / "dist" / "index.html").write_text("<html/>", encoding="utf-8")

    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._run_cmd", fake_run_cmd)
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._resolve_pm", lambda: ("npm", "npm"))
    _build_frontend(_detect_frontends(tmp_path, ()))
    assert ("install",) not in calls
    assert calls == [("run", "build")]


def test_build_frontend_no_pm_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物缺失且无包管理器时报错并给出指引."""
    _write_frontend_pkg(tmp_path / "frontend")
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._resolve_pm", lambda: None)
    with pytest.raises(FspackError, match="Node"):
        _build_frontend(_detect_frontends(tmp_path, ()))


def test_build_frontend_empty_output_after_build_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建命令成功但产物目录仍为空时报错（fail-fast，防打包出坏应用）."""
    _write_frontend_pkg(tmp_path / "frontend")
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._run_cmd", lambda *a: None)
    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage._resolve_pm", lambda: ("npm", "npm"))
    with pytest.raises(FspackError, match="产物目录仍为空"):
        _build_frontend(_detect_frontends(tmp_path, ()))


def test_is_wsl_windows_mount() -> None:
    """``/mnt/<盘符>/...`` 命中 WSL Windows 挂载，其余路径不命中."""
    assert _is_wsl_windows_mount("/mnt/c/Users/foo/nodejs/pnpm")
    assert _is_wsl_windows_mount("/mnt/d/env/node/pnpm.cmd")
    # 非 Windows 盘符挂载：多字母卷名、普通 Linux 路径、相对路径均不命中
    assert not _is_wsl_windows_mount("/mnt/usb/bin/pnpm")
    assert not _is_wsl_windows_mount("/usr/bin/pnpm")
    assert not _is_wsl_windows_mount("C:/fake/pnpm.cmd")
    assert not _is_wsl_windows_mount("pnpm")


def test_resolve_pm_skips_wsl_windows_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """``which`` 命中 WSL Windows 盘符路径时跳过，改用后续 Linux 候选."""
    from fspack.packaging.pipeline import frontend_stage

    def fake_which(name: str) -> str | None:
        return {
            "pnpm": "/mnt/c/Users/foo/AppData/pnpm",
            "npm": "/usr/bin/npm",
        }.get(name)

    monkeypatch.setattr(frontend_stage.shutil, "which", fake_which)
    assert frontend_stage._resolve_pm() == ("npm", "/usr/bin/npm")


def test_resolve_pm_all_wsl_mounts_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有候选均只存在于 Windows 盘符挂载时返回 None（明确的未找到报错）."""
    from fspack.packaging.pipeline import frontend_stage

    monkeypatch.setattr(frontend_stage.shutil, "which", lambda name: f"/mnt/c/tools/{name}")
    assert frontend_stage._resolve_pm() is None


class _FakePipeStream:
    """os.pipe 读端包装：drain 线程经 ``fileno`` + ``os.read`` 消费，可预置内容."""

    def __init__(self, content: bytes = b"") -> None:
        self._r, w = os.pipe()
        if content:
            os.write(w, content)
        os.close(w)

    def fileno(self) -> int:
        return self._r


class _FakeProc:
    """``_run_cmd`` 的 Popen 替身：管道流 + 可编程 wait/kill 行为."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        stdout: bytes = b"",
        first_wait_exc: Exception | None = None,
    ) -> None:
        self.pid = 4242
        self.stdout = _FakePipeStream(stdout)
        self.stderr = _FakePipeStream(stderr)
        self._returncode = returncode
        self._first_wait_exc = first_wait_exc
        self.kill_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        if self._first_wait_exc is not None:
            exc, self._first_wait_exc = self._first_wait_exc, None
            raise exc
        return self._returncode

    def kill(self) -> None:
        self.kill_calls += 1


def test_run_cmd_success_passes_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """退出码 0 时正常返回（无异常），命令与工作目录透传 Popen."""
    seen: dict[str, object] = {}

    def fake_popen(cmd: list[str], cwd: str, **kwargs: object) -> _FakeProc:
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        return _FakeProc()

    monkeypatch.setattr("fspack.packaging.pipeline.frontend_stage.subprocess.Popen", fake_popen)
    _run_cmd("C:/fake/npm", ["run", "build"], tmp_path)
    assert seen["cmd"] == ["C:/fake/npm", "run", "build"]
    assert seen["cwd"] == str(tmp_path)


def test_run_cmd_failure_raises_with_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非零退出码抛 FspackError，含 stderr 尾部（截断到 800 字符）."""
    proc = _FakeProc(returncode=1, stderr=b"E" * 1000)
    monkeypatch.setattr(
        "fspack.packaging.pipeline.frontend_stage.subprocess.Popen",
        lambda *a, **k: proc,
    )
    with pytest.raises(FspackError, match="前端命令失败") as exc_info:
        _run_cmd("npm", ["run", "build"], tmp_path)
    assert "E" * 800 in str(exc_info.value)
    assert "E" * 801 not in str(exc_info.value)


def test_run_cmd_timeout_kills_process_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时抛 FspackError 并终止进程树：Windows taskkill /T /F，POSIX kill."""
    import sys as _sys

    proc = _FakeProc(
        returncode=1,
        first_wait_exc=subprocess.TimeoutExpired(cmd=["fake"], timeout=600),
    )
    kill_cmds: list[list[str]] = []
    monkeypatch.setattr(
        "fspack.packaging.pipeline.frontend_stage.subprocess.Popen",
        lambda *a, **k: proc,
    )
    monkeypatch.setattr(
        "fspack.packaging.pipeline.frontend_stage.subprocess.run",
        lambda cmd, **k: kill_cmds.append(list(cmd)),
    )
    with pytest.raises(FspackError, match="前端命令超时"):
        _run_cmd("npm", ["run", "build"], tmp_path)
    if _sys.platform == "win32":
        assert kill_cmds and kill_cmds[0][:1] == ["taskkill"]
        assert "/T" in kill_cmds[0] and "/F" in kill_cmds[0]
    else:
        assert proc.kill_calls == 1


# ---- copy_source 前端裁剪（dist 只发布产物，前端源码不进入） ----


def test_frontend_prune_map_assembly(tmp_path: Path) -> None:
    """_frontend_prune_map 组装：FrontendProject 集 → root 到产物映射."""
    fe = _write_frontend_pkg(tmp_path / "frontend")
    fps = _detect_frontends(tmp_path, ())
    assert _frontend_prune_map(fps) == {fe.resolve(): (fe / "deploy", fe / "dist")}


def test_copy_source_frontend_prune_keeps_only_output(tmp_path: Path) -> None:
    """前端根目录下只保留产物目录：src/public/package.json 等源码不进 dist."""
    src = tmp_path / "proj"
    fe = src / "src" / "webview_app" / "frontend"
    _write_frontend_pkg(fe)
    (fe / "src").mkdir()
    (fe / "src" / "App.vue").write_text("<template/>", encoding="utf-8")
    (fe / "public").mkdir()
    (fe / "vite.config.ts").write_text("export default {}", encoding="utf-8")
    (fe / "index.html").write_text("<html/>", encoding="utf-8")
    deploy = fe / "deploy"
    deploy.mkdir()
    (deploy / "index.html").write_text("<html>built</html>", encoding="utf-8")
    assets = deploy / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")

    dst = tmp_path / "dist_src"
    copy_source(src, dst, frontend_prune=_frontend_prune_map(_detect_frontends(src, ())))

    fe_dst = dst / "src" / "webview_app" / "frontend"
    assert sorted(p.name for p in fe_dst.iterdir()) == ["deploy"]
    assert (fe_dst / "deploy" / "index.html").read_text(encoding="utf-8") == "<html>built</html>"
    assert (fe_dst / "deploy" / "assets" / "app.js").is_file()
    assert not (fe_dst / "package.json").exists()
    assert not (fe_dst / "src").exists()
    assert not (fe_dst / "vite.config.ts").exists()


def test_copy_source_frontend_prune_output_name_dist_restored(tmp_path: Path) -> None:
    """产物目录名为 dist 时命中 _EXCLUDE_ALWAYS 构建产物模式，仍被保护恢复."""
    src = tmp_path / "proj"
    fe = _write_frontend_pkg(src / "frontend")
    out = fe / "dist"
    out.mkdir()
    (out / "index.html").write_text("<html/>", encoding="utf-8")

    dst = tmp_path / "dist_src"
    # 显式配置产物目录为 frontend/dist（配置路径识别的 output_dirs）
    prune = {fe.resolve(): (out.resolve(),)}
    copy_source(src, dst, frontend_prune=prune)

    assert sorted(p.name for p in (dst / "frontend").iterdir()) == ["dist"]
    assert (dst / "frontend" / "dist" / "index.html").is_file()


def test_copy_source_frontend_prune_nested_output(tmp_path: Path) -> None:
    """产物目录嵌套（build/www）：逐层裁剪，只保留通往产物的路径链."""
    src = tmp_path / "proj"
    fe = _write_frontend_pkg(src / "frontend")
    www = fe / "build" / "www"
    www.mkdir(parents=True)
    (www / "index.html").write_text("<html/>", encoding="utf-8")
    (fe / "build" / "cache.txt").write_text("x", encoding="utf-8")

    dst = tmp_path / "dist_src"
    copy_source(src, dst, frontend_prune={fe.resolve(): (www.resolve(),)})

    fe_dst = dst / "frontend"
    assert sorted(p.name for p in fe_dst.iterdir()) == ["build"]
    assert sorted(p.name for p in (fe_dst / "build").iterdir()) == ["www"]
    assert (fe_dst / "build" / "www" / "index.html").is_file()


def test_copy_source_frontend_prune_output_is_root_no_prune(tmp_path: Path) -> None:
    """产物目录即前端根本身（配置指向前端根，如 flask 手写 html）：不裁剪."""
    src = tmp_path / "proj"
    fe = _write_frontend_pkg(src / "frontend")
    (fe / "index.html").write_text("<html/>", encoding="utf-8")

    dst = tmp_path / "dist_src"
    copy_source(src, dst, frontend_prune={fe.resolve(): (fe.resolve(),)})

    # frontend 根即产物：原样复制（package.json 保留）
    assert (dst / "frontend" / "package.json").is_file()
    assert (dst / "frontend" / "index.html").is_file()


def test_copy_source_frontend_prune_incremental_sync(tmp_path: Path) -> None:
    """增量同步路径（dst 已存在）同样应用裁剪：dst 残留的前端源码被删除."""
    src = tmp_path / "proj"
    fe = _write_frontend_pkg(src / "frontend")
    (fe / "src").mkdir()
    (fe / "src" / "App.vue").write_text("<template/>", encoding="utf-8")
    deploy = fe / "deploy"
    deploy.mkdir()
    (deploy / "index.html").write_text("<html/>", encoding="utf-8")

    dst = tmp_path / "dist_src"
    # 首次复制（未裁剪，模拟旧版 fspack 打出的 dist 残留前端源码）
    copy_source(src, dst)
    assert (dst / "frontend" / "package.json").is_file()

    # 二次构建（带裁剪）：增量同步删除 dst 中源码侧已排除的文件
    copy_source(src, dst, frontend_prune=_frontend_prune_map(_detect_frontends(src, ())))
    fe_dst = dst / "frontend"
    assert sorted(p.name for p in fe_dst.iterdir()) == ["deploy"]
    assert not (fe_dst / "package.json").exists()
    assert not (fe_dst / "src").exists()
