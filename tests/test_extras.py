"""``[project.optional-dependencies]`` 与 ``--extra`` / ``[tool.fspack] extras`` 端到端测试.

覆盖单元测试（``test_config.py`` 中 expand_extras）之外的整合场景：

- CLI ``--extra`` 透传到 ``BuildOptions.extras``（build 子命令）
- CLI ``--extra`` 透传到 ``build_release(extras=...)``（package 子命令）
- ``[tool.fspack] extras`` 配置默认在未指定 CLI ``--extra`` 时生效
- CLI ``--extra`` 完全覆盖 ``[tool.fspack] extras`` 配置默认（集合语义，非合并）
- 未知 extras 分组在 build / package 子命令均抛 ProjectError
- 依赖分析缓存键含扩展后依赖：extras 变化触发缓存失效
- ``--dry-run`` 打印的依赖分析表含「启用 extras」与「扩展后依赖」行
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack import cli
from fspack.config import BuildOptions, get_mirror
from fspack.console import console
from fspack.exceptions import ProjectError
from fspack.packaging.pipeline import build
from fspack.packaging.pipeline_stages import _dep_cache_load, _dep_cache_save
from fspack.platform import Platform


def _make_project_with_extras(
    tmp_path: Path,
    *,
    name: str = "myapp",
    base_deps: tuple[str, ...] = (),
    optional_deps: dict[str, tuple[str, ...]] | None = None,
    fspack_extras: tuple[str, ...] = (),
) -> Path:
    """构造带 [project.optional-dependencies] 与可选 [tool.fspack] extras 的项目.

    Args:
        base_deps: ``[project] dependencies`` 基础依赖
        optional_deps: ``[project.optional-dependencies]`` 分组与依赖映射
        fspack_extras: ``[tool.fspack] extras`` 配置默认启用的分组名
    """
    parts = [f'[project]\nname = "{name}"\nversion = "0.1"\n']
    if base_deps:
        parts.append("dependencies = [\n")
        for dep in base_deps:
            parts.append(f'    "{dep}",\n')
        parts.append("]\n")
    if optional_deps:
        parts.append("\n[project.optional-dependencies]\n")
        for extra_name, deps in optional_deps.items():
            dep_list = ", ".join(f'"{d}"' for d in deps)
            parts.append(f"{extra_name} = [{dep_list}]\n")
    if fspack_extras:
        parts.append("\n[tool.fspack]\nextras = [")
        parts.append(", ".join(f'"{e}"' for e in fspack_extras))
        parts.append("]\n")
    (tmp_path / "pyproject.toml").write_text("".join(parts), encoding="utf-8")
    # 入口文件 import 依赖以触发 AST 扫描（避免 missing 误报）
    imports = "\n".join(f"import {d.split('>=')[0].split('<=')[0].split('==')[0].split('[')[0]}" for d in base_deps)
    (tmp_path / f"{name}.py").write_text(f"{imports}\n\n\ndef main():\n    pass\n", encoding="utf-8")
    return tmp_path


def _stub_write_operations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """桩掉所有写盘操作（download/extract/copy/compile），仅保留依赖分析."""
    for fn in ("download_embed", "download_standalone", "download_wheels"):
        monkeypatch.setattr(f"fspack.packaging.pipeline_stages.{fn}", lambda *a, **k: tmp_path / "fake")
    for fn in ("extract_embed", "extract_standalone", "unpack_wheels"):
        monkeypatch.setattr(f"fspack.packaging.pipeline_stages.{fn}", lambda *a, **k: None)
    monkeypatch.setattr("fspack.packaging.pipeline.copy_source", lambda *a, **k: None)
    monkeypatch.setattr(
        "fspack.packaging.pipeline_stages.compile_loader",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应编译 loader")),
    )


# ---- CLI --extra 透传（build 子命令）----


def test_cli_build_extra_flag_propagates_to_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp b <proj> --extra gui --extra web`` 透传到 BuildOptions.extras."""
    _make_project_with_extras(
        tmp_path,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",), "web": ("flask",)},
    )

    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: BuildOptions | None = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: bool = False,
    ) -> None:
        captured["options"] = options

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--extra", "gui", "--extra", "web"])

    opts = captured["options"]
    assert opts is not None
    assert opts.extras == frozenset({"gui", "web"})


def test_cli_build_extra_overrides_config_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --extra 完全覆盖 [tool.fspack] extras 配置默认（集合语义，非合并）."""
    _make_project_with_extras(
        tmp_path,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",), "web": ("flask",), "sci": ("numpy",)},
        fspack_extras=("gui", "web"),  # 配置默认启用 gui+web
    )

    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: BuildOptions | None = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: bool = False,
    ) -> None:
        captured["options"] = options

    monkeypatch.setattr("fspack.builder.build", fake_build)
    # CLI 仅指定 sci → 覆盖配置默认，extras 只含 sci（不含 gui/web）
    cli.main(["b", str(tmp_path), "--extra", "sci"])

    opts = captured["options"]
    assert opts is not None
    assert opts.extras == frozenset({"sci"})


def test_cli_build_config_default_used_when_no_extra_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --extra 时用 [tool.fspack] extras 配置默认."""
    _make_project_with_extras(
        tmp_path,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",), "web": ("flask",)},
        fspack_extras=("gui",),  # 配置默认启用 gui
    )

    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: BuildOptions | None = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
        profile: bool = False,
    ) -> None:
        captured["options"] = options

    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])

    opts = captured["options"]
    assert opts is not None
    assert opts.extras == frozenset({"gui"})


# ---- 未知 extras 报错 ----


def test_cli_build_unknown_extra_raises_project_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --extra 指定未声明的分组名抛 ProjectError."""
    _make_project_with_extras(
        tmp_path,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",)},
    )

    def fake_build(*a: object, **kw: object) -> None:
        raise AssertionError("不应调用 build（校验阶段应先抛错）")

    monkeypatch.setattr("fspack.builder.build", fake_build)
    with pytest.raises(ProjectError, match="未知的 extras 分组"):
        cli.main(["b", str(tmp_path), "--extra", "unknown_group"])


def test_cli_package_unknown_extra_raises_project_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """package 子命令 --extra 指定未声明的分组名同样抛 ProjectError."""
    _make_project_with_extras(
        tmp_path,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",)},
    )

    def fake_build_release(*a: object, **kw: object) -> list[Path]:
        raise AssertionError("不应调用 build_release（校验阶段应先抛错）")

    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    with pytest.raises(ProjectError, match="未知的 extras 分组"):
        cli.main(["p", str(tmp_path), "--extra", "unknown_group", "--no-build"])


# ---- CLI --extra 透传到 package 子命令 ----


def test_cli_package_extra_flag_propagates_to_build_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp p <proj> --extra gui`` 透传到 build_release(extras=["gui"])."""
    _make_project_with_extras(
        tmp_path,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",), "web": ("flask",)},
    )

    captured: dict[str, Any] = {}

    def fake_build_release(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        no_build: bool = False,
        dist_dir: Path | None = None,
        target: object = None,
        fmt: str = "auto",
        codesign: bool = False,
        extras: object = None,
        sign_exe: bool = False,
        sign_exe_certificate: Path | None = None,
        sign_exe_password: str | None = None,
        sign_deb: bool = False,
        sign_deb_key: str | None = None,
    ) -> list[Path]:
        captured["extras"] = extras
        return []

    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--extra", "gui", "--extra", "web", "--no-build"])

    # CLI --extra 作为 list 透传（build_release 内部转为 frozenset）
    assert captured["extras"] == ["gui", "web"]


# ---- 依赖缓存键含扩展后依赖 ----


def test_dependency_cache_key_differs_for_different_extras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_dep_cache_load`` 在 declared 不同时返回 None（缓存键不匹配）.

    直接测试缓存函数：同一指纹下，declared 含 PySide2 时写入的缓存，
    用 declared 含 flask 加载时返回 None。
    """
    from fspack.analyzer import source_fingerprint
    from fspack.config import DependencyReport

    proj = tmp_path / "app"
    proj.mkdir()
    _make_project_with_extras(
        proj,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",), "web": ("flask",)},
    )

    fingerprint = source_fingerprint(proj)
    dist_dir = proj / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    # 模拟启用 gui 时的缓存（declared 含 PySide2）
    report_gui = DependencyReport(
        declared=("rich", "PySide2"),
        ast_third_party=("rich", "PySide2"),
        ast_stdlib=(),
        ast_local=(),
    )
    _dep_cache_save(dist_dir, fingerprint, report_gui)
    assert _dep_cache_load(dist_dir, fingerprint, ("rich", "PySide2")) is not None

    # 用 declared 含 flask 加载 → 缓存键不匹配，返回 None
    assert _dep_cache_load(dist_dir, fingerprint, ("rich", "flask")) is None

    # 用 declared 完全相同加载 → 缓存命中
    assert _dep_cache_load(dist_dir, fingerprint, ("rich", "PySide2")) is not None


# ---- dry-run 打印启用 extras ----


def test_dry_run_prints_enabled_extras_in_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--dry-run`` 模式打印的依赖分析表含「启用 extras」与「扩展后依赖」行."""
    proj = tmp_path / "app"
    proj.mkdir()
    _make_project_with_extras(
        proj,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",), "web": ("flask",)},
    )

    _stub_write_operations(monkeypatch, tmp_path)

    from dataclasses import replace

    opts = replace(BuildOptions(), extras=frozenset({"gui", "web"}))
    with console.rich.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, options=opts, dry_run=True)

    out = capture.get()
    assert "启用 extras" in out
    assert "gui" in out
    assert "web" in out
    assert "扩展后依赖" in out
    # 扩展后依赖应含 base + extras 展开的包名
    assert "rich" in out
    assert "PySide2" in out
    assert "flask" in out


def test_dry_run_omits_extras_row_when_no_extras_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未启用 extras 时 dry-run 输出不含「启用 extras」行."""
    proj = tmp_path / "app"
    proj.mkdir()
    _make_project_with_extras(
        proj,
        base_deps=("rich",),
        optional_deps={"gui": ("PySide2",)},  # 有可选分组但未启用
    )

    _stub_write_operations(monkeypatch, tmp_path)

    with console.rich.capture() as capture:
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, dry_run=True)

    out = capture.get()
    assert "启用 extras" not in out
    assert "扩展后依赖" not in out
    # 声明依赖行仍存在
    assert "声明依赖" in out
    assert "rich" in out
