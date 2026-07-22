"""builder 流水线编排测试."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.builder import (
    _site_packages_has_deps,
    build,
    copy_source,
    unpack_wheels,
)
from fspack.console import console
from fspack.exceptions import DependencyError
from fspack.mirror import get_mirror
from fspack.platform import Platform

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
