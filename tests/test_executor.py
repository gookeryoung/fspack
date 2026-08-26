"""``pipeline/executor.py`` 编排测试：build 阶段接线、依赖合并、Nuitka 互斥选项与 dist 残留恢复."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from fspack.builder import (
    build,
    fspack_wheel_cache_dir,
)
from fspack.config import BuildOptions, ProjectInfo, get_mirror
from fspack.console import console
from fspack.packaging.pipeline.stages import BuildContext
from fspack.platform import Platform
from fspack.progress import StageRecorder
from fspack.templates.project_template import ProjectTemplate
from tests._stubs import CompletedStub, fake_compileall_runner, setup_embed_mocks

_EXAMPLES = ProjectTemplate.root_dir()


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
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.LINUX)
    assert not download_called
    assert not extract_called


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
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())

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

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    # 覆盖 download_wheels 返回非空列表，触发「解压 wheel(精简)」阶段
    monkeypatch.setattr("fspack.packaging.pipeline.stages.download_wheels", lambda *a, **k: [tmp_path / "fake.whl"])
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())
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

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())

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

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())
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

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    # 让 python.exe 就绪，使 _precompile_pyc 真正执行 strip
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", fake_compileall_runner)
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

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    # app.py 保留
    assert (proj / "dist" / "src" / "app.py").is_file()


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

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())
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

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())
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

    setup_embed_mocks(tmp_path, monkeypatch, "3.13.14")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())
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
