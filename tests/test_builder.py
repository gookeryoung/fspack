"""builder 流水线编排测试."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.builder import (
    _dep_cache_load,
    _dep_cache_path,
    _dep_cache_save,
    _inject_win7_compat_dll,
    _needs_win7_compat_dll,
    _precompile_pyc,
    _site_packages_has_deps,
    _sync_tree,
    _trim_stdlib,
    build,
    copy_source,
    fspack_wheel_cache_dir,
    unpack_wheels,
)
from fspack.config import DependencyReport, get_mirror
from fspack.console import console
from fspack.exceptions import DependencyError
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
    # mock 预编译阶段的 subprocess.run（Linux python3.11 二进制在 Windows 上无法执行）
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

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

    with console.rich.capture() as capture:
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

    with console.rich.capture() as capture:
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
    # mock 预编译阶段的 subprocess.run（Linux python3.11 二进制在 Windows 上无法执行）
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

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


def test_build_supplements_tkinter_when_needed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AST 检出 tkinter 且目标 Windows 时触发 TkinterBundler.ensure，wrapper 注入 TCL/TK 环境变量."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("import tkinter\ndef main():\n    pass\n")

    monkeypatch.setattr("fspack.builder.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr(
        "fspack.builder.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    monkeypatch.setattr("fspack.builder.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    ensure_called: dict[str, bool] = {}
    monkeypatch.setattr(
        "fspack.builder.TkinterBundler.ensure",
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

    monkeypatch.setattr("fspack.builder.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    monkeypatch.setattr(
        "fspack.builder.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python311.dll").write_bytes(b""),
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    monkeypatch.setattr("fspack.builder.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )

    ensure_called: dict[str, bool] = {}
    monkeypatch.setattr(
        "fspack.builder.TkinterBundler.ensure",
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
    monkeypatch.setattr("fspack.builder._WIN7_COMPAT_DLL_NAME", "nonexistent-dll.dll")
    _inject_win7_compat_dll(runtime_dir)  # 不应抛异常
    assert not (runtime_dir / "nonexistent-dll.dll").exists()
    assert any("缺失" in r.message for r in caplog.records)


def _setup_embed_mocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, py_version: str) -> None:
    """为 Windows embed 构建注入公共 mock（download/extract/wheels/loader）."""
    monkeypatch.setattr("fspack.builder.download_embed", lambda v, m, c, **kw: tmp_path / "fake.zip")
    parts = py_version.split(".", maxsplit=2)
    pyxy = f"python{parts[0]}{parts[1]}"
    monkeypatch.setattr(
        "fspack.builder.extract_embed",
        lambda zip_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / f"{pyxy}.dll").write_bytes(b""),
            (runtime_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    monkeypatch.setattr("fspack.builder.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
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
    monkeypatch.setattr("fspack.builder.download_standalone", lambda v, r, c, **kw: tmp_path / "fake.tar.gz")
    monkeypatch.setattr(
        "fspack.builder.extract_standalone",
        lambda tar_path, runtime_dir: (
            runtime_dir.mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python" / "bin").mkdir(parents=True, exist_ok=True),
            (runtime_dir / "python" / "bin" / "python3.11").write_text(""),
            (runtime_dir / "python" / "lib" / "python3.11" / "site-packages").mkdir(parents=True, exist_ok=True),
        )[-1],
    )
    monkeypatch.setattr("fspack.builder.download_wheels", lambda *a, **k: [])
    monkeypatch.setattr("fspack.builder.unpack_wheels", lambda *a, **k: 0)
    monkeypatch.setattr(
        "fspack.builder.compile_loader",
        lambda source, out_exe, app_type, work_dir, platform, **kw: (
            out_exe.parent.mkdir(parents=True, exist_ok=True),
            out_exe.write_text(source),
        )[-1],
    )
    # mock 预编译阶段的 subprocess.run（Linux python3.11 二进制在 Windows 上无法执行）
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

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


def test_trim_stdlib_windows_skips(tmp_path: Path) -> None:
    """Windows embed 标准库在 zip 内已精简，跳过不剥离."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)  # 构造验证跳过

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.WINDOWS, st)

    # Windows 模式不剥离
    assert (stdlib / "test").exists()


def test_trim_stdlib_missing_stdlib_skips(tmp_path: Path) -> None:
    """标准库目录不存在时不报错."""
    runtime = tmp_path / "runtime"
    # 不创建 stdlib 目录

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)
    # 不报错即通过


def test_trim_stdlib_idempotent(tmp_path: Path) -> None:
    """重复调用幂等：已剥离的目录不存在时跳过."""
    runtime = tmp_path / "runtime"
    stdlib = runtime / "python" / "lib" / "python3.11"
    (stdlib / "test").mkdir(parents=True)

    st = StageRecorder("精简标准库")
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)
    _trim_stdlib(runtime, "3.11.9", Platform.LINUX, st)  # 二次调用不报错
    assert not (stdlib / "test").exists()


# ---- _precompile_pyc 测试 ----


class _CompileCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def test_precompile_pyc_windows_calls_compileall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标用 runtime/python.exe 调 compileall 预编译 src 与 site-packages."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("print('hi')")

    captured: list[list[str]] = []
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 调用 2 次（src + site-packages）
    assert len(captured) == 2
    assert "compileall" in captured[0]
    assert str(dist / "src") in captured[0]
    assert str(runtime / "python.exe") in captured[0][0]


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
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: captured.append(cmd) or _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.LINUX, strip_py=False, stage=st)

    assert "python3.11" in str(captured[0][0])


def test_precompile_pyc_strip_deletes_non_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """strip_py=True 删除非 __init__.py 的 .py，保留 __init__.py 维持包结构."""
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

    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=True, stage=st)

    # __init__.py 保留（包标识）
    assert (src / "__init__.py").is_file()
    assert (src / "sub" / "__init__.py").is_file()
    # 非 __init__.py 被删
    assert not (src / "app.py").exists()
    assert not (src / "sub" / "mod.py").exists()


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

    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=True, stage=st)

    assert (src / "__init__.py").is_file()
    assert not (src / "main.py").exists()


def test_precompile_pyc_python_missing_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime python 未就绪时跳过 compileall，不调 subprocess."""
    runtime = tmp_path / "runtime"
    # 不创建 python.exe
    dist = tmp_path / "dist"
    (dist / "src").mkdir(parents=True)
    (dist / "src" / "app.py").write_text("")

    called: list[object] = []
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: called.append(cmd))

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    assert not called  # 未调 subprocess


def test_precompile_pyc_compileall_failure_warns_not_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compileall 非零退出码时仅 warning 不抛异常，继续处理后续目录."""
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

    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _Failed())

    st = StageRecorder("预编译字节码")
    with caplog.at_level("WARNING", logger="fspack.builder"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    assert any("compileall 失败" in r.message for r in caplog.records)


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
    monkeypatch.setattr("fspack.builder.download_wheels", lambda *a, **k: [tmp_path / "fake.whl"])
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

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
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

    stage_names = _capture_stage_names(monkeypatch)
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, no_pyc=True)

    assert "预编译字节码" not in stage_names
    assert "精简标准库" in stage_names  # 精简标准库仍执行


def test_build_no_stdlib_trim_skips_trim_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """no_stdlib_trim=True 时跳过「精简标准库」阶段."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

    stage_names = _capture_stage_names(monkeypatch)
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, no_stdlib_trim=True)

    assert "精简标准库" not in stage_names
    assert "预编译字节码" in stage_names  # 预编译仍执行


def test_build_pyc_strip_deletes_non_init_py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pyc_strip=True 时 build() 调 _precompile_pyc 剥离非 __init__.py 源码."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    _setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    # 让 python.exe 就绪，使 _precompile_pyc 真正执行 strip
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, pyc_strip=True)

    src = proj / "dist" / "src"
    # app.py 被剥离
    assert not (src / "app.py").exists()
    # 但 _entry_app.py 是 wrapper（在 dist 根，非 src），不受影响


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
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

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
    monkeypatch.setattr("fspack.builder.subprocess.run", lambda cmd, **kw: _CompileCompleted())

    # 第一次构建：生成缓存
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    cache = _dep_cache_path(proj / "dist")
    assert cache.is_file(), "第一次构建应生成 .dep_cache.json"

    # 第二次构建：缓存命中
    analyze_called = False
    original_from_src = DependencyReport.from_src.__func__  # type: ignore[attr-defined]

    def tracking_from_src(cls: object, *args: object, **kwargs: object) -> DependencyReport:
        nonlocal analyze_called
        analyze_called = True
        return original_from_src(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("fspack.config.DependencyReport.from_src", classmethod(tracking_from_src))
    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)
    assert not analyze_called, "缓存命中时不应调用 AST 分析"
