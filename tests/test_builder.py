"""builder 流水线编排测试."""

from __future__ import annotations

import shutil
import zipfile
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
from fspack.config import BuildOptions, DependencyReport, get_mirror
from fspack.console import console
from fspack.exceptions import DependencyError
from fspack.packaging.pipeline import _warn_dist_incomplete
from fspack.packaging.pipeline.stages import BuildContext
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

    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_standalone", fake_download_standalone)
    monkeypatch.setattr("fspack.packaging.pipeline.stages.extract_standalone", fake_extract_standalone)
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
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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


def test_build_orchestration_helloworld(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj, ignore=shutil.ignore_patterns("dist", "__pycache__"))
    calls: dict[str, Any] = {}

    def fake_extract_embed(zip_path: object, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "python311.dll").write_bytes(b"")
        (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
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

    with console.rich.capture() as capture:
        info = build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert info.name == "cli_helloworld_pyall"
    assert (proj / "dist" / "cli_helloworld_pyall.exe").is_file()
    assert (proj / "dist" / "runtime" / "python311._pth").is_file()
    assert (proj / "dist" / "src" / "helloworld.py").is_file()
    assert (proj / "dist" / "runtime" / "python311.dll").is_file()
    assert (proj / "dist" / ".entry").is_file()
    assert (proj / "dist" / ".entry").read_text(encoding="utf-8") == "_entry_cli_helloworld_pyall.py"
    wrapper = proj / "dist" / "_entry_cli_helloworld_pyall.py"
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
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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
        sp = runtime_dir / "Lib" / "site-packages"
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
    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj, ignore=shutil.ignore_patterns("dist", "__pycache__"))
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
    # mock 预编译阶段的 subprocess.run（Linux python3.11 二进制在 Windows 上无法执行）
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileCompleted())

    info = build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX)
    assert info.name == "cli_helloworld_pyall"
    assert (proj / "dist" / "cli_helloworld_pyall").is_file()
    assert not (proj / "dist" / "cli_helloworld_pyall.exe").exists()
    assert not (proj / "dist" / "runtime" / "python311._pth").exists()
    assert (proj / "dist" / "src" / "helloworld.py").is_file()
    assert (proj / "dist" / ".entry").is_file()
    assert (proj / "dist" / "_entry_cli_helloworld_pyall.py").is_file()
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
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
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


def test_build_skips_win7_compat_dll_for_py38(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            (runtime_dir / "python" / "lib" / "python3.11" / "site-packages").mkdir(parents=True, exist_ok=True),
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


def test_trim_stdlib_windows_skips(tmp_path: Path) -> None:
    """Windows embed 标准库在 zip 内已精简，跳过不剥离."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)  # 构造验证跳过

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.WINDOWS, st)

    # Windows 模式不剥离
    assert (stdlib / "test").exists()
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

    模拟 _scandir_tree 返回的条目中，stat(follow_symlinks=False) 抛 OSError
    （并发删除/权限问题）。_dir_size 用 os.scandir 替代 rglob 后，DirEntry.stat
    复用枚举时缓存，但仍可能因文件被并发删除而抛 OSError。
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
    # _dir_size 用 _scandir_tree（os.scandir）替代 Path.rglob，mock _scandir_tree
    # 直接返回自定义条目列表，验证 stat 异常被跳过。
    monkeypatch.setattr(
        "fspack.packaging.sync._scandir_tree",
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
    """Windows 目标用 runtime/python.exe 单次调 compileall 同时编译 src 与 site-packages."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 合并为单次 compileall 调用，同时编译 src 与 site-packages
    assert len(captured) == 1
    cmd = captured[0]
    assert "compileall" in cmd
    assert str(dist / "src") in cmd
    assert str(runtime / "Lib" / "site-packages") in cmd
    assert str(runtime / "python.exe") in cmd[0]


def test_precompile_pyc_linux_uses_python3_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标用 runtime/python/bin/python{ver} 调 compileall."""
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_bytes(b"")
    (runtime / "python" / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
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
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
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
    需从命令中解析 ``-o <optimize>`` 与目标目录，py_version 由调用方在 ``cmd`` 中
    无法获取，故用模块级 ``_FAKE_COMPILE_PY_VERSION`` 变量传递（默认 "3.11"）。

    支持多目录合并调用：``compileall dir1 dir2 -q -j 0 -o N``
    一次编译多目录，本函数收集所有非 flag 的目录参数逐个编译。
    """
    optimize = 0
    target_dirs: list[Path] = []
    for i, arg in enumerate(cmd):
        if arg == "-o":
            optimize = int(cmd[i + 1])
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
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
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
    """optimize 参数透传为 compileall `-o` 标志，控制字节码优化级别."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st, optimize=2)

    # 每次 compileall 调用都含 `-o 2`
    for cmd in captured:
        assert "-o" in cmd
        assert cmd[cmd.index("-o") + 1] == "2"


def test_precompile_pyc_optimize_default_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """optimize 默认 0，compileall 命令含 `-o 0`."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    for cmd in captured:
        assert cmd[cmd.index("-o") + 1] == "0"


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
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
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
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    # 预先写入匹配的 stamp
    stamp_key = _pyc_stamp_key(dist / "src", runtime / "Lib" / "site-packages", strip_py=False, optimize=0)
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
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

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
    ) -> None:
        nuitka_called["src_dir"] = src_dir
        nuitka_called["dist_dir"] = dist_dir
        nuitka_called["py_version"] = py_version
        nuitka_called["target"] = target
        nuitka_called["cache_root"] = cache_root
        nuitka_called["entry_rels"] = entry_rels
        nuitka_called["ccache"] = ccache
        nuitka_called["nuitka_packages"] = nuitka_packages
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


# --- clean_dist 测试（原 tests/test_commands.py 的 clean 测试） ---


def test_clean_dist_removes_dist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "x.txt").write_text("x")
    clean_dist(tmp_path)
    # clean 后 dist 目录重建为空（保留目录结构便于重新构建）
    assert dist.is_dir()
    assert not (dist / "x.txt").exists()


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


# --- _warn_dist_incomplete 测试（iter-130 dist 半成品检测） ---


def test_warn_dist_incomplete_no_dist(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 目录不存在时不告警."""
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _warn_dist_incomplete(tmp_path / "nonexistent")
    assert not caplog.records


def test_warn_dist_incomplete_empty_dist(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 目录为空时不告警（无构建产物）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _warn_dist_incomplete(dist)
    assert not caplog.records


def test_warn_dist_incomplete_only_nsi(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 仅含 installer.nsi（clean_dist 保留）时不告警."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _warn_dist_incomplete(dist)
    assert not caplog.records


def test_warn_dist_incomplete_artifacts_no_stamp_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含构建产物但无 stamp 文件时告警（中断/失败的上次构建）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "src").mkdir()
    (dist / "app.exe").write_bytes(b"")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _warn_dist_incomplete(dist)
    assert any("残留产物" in r.message and "fsp c" in r.message for r in caplog.records)


def test_warn_dist_incomplete_with_pyc_stamp_no_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含产物且有 .pyc_stamp 时不告警（上次构建至少完成到编译阶段）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "src").mkdir()
    (dist / ".pyc_stamp").write_text("fingerprint", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _warn_dist_incomplete(dist)
    assert not caplog.records


def test_warn_dist_incomplete_with_nuitka_stamp_no_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含产物且有 .nuitka_compile_stamp 时不告警."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / ".nuitka_compile_stamp").write_text("fingerprint", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _warn_dist_incomplete(dist)
    assert not caplog.records


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
