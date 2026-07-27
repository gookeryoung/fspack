"""cli 子命令分发测试."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack import __version__, cli
from fspack.config import BuildOptions, get_mirror
from fspack.platform import Platform


def test_build_parser_prog() -> None:
    assert cli.build_parser().prog == "fspack"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["-V"])
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main([])
    assert "fspack" in capsys.readouterr().out


def _make_minimal_project(tmp_path: Path) -> Path:
    """在 tmp_path 下创建最小可解析项目（pyproject.toml + 入口脚本）.

    cli.main 在 dispatch 到 builder.build 前会调用 ProjectInfo.from_dir
    读取 [tool.fspack] build_defaults，需要可解析的 pyproject.toml。
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n', encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return tmp_path


def _capture_build() -> tuple[dict[str, Any], Any]:
    """构造 fake_build 与 captured dict，fake_build 签名匹配 builder.build()."""
    captured: dict[str, Any] = {}

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: Platform | None = None,
        options: BuildOptions | None = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
    ) -> None:
        captured["project"] = project
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["target"] = target
        captured["options"] = options
        captured["extra_index_urls"] = extra_index_urls
        captured["find_links"] = find_links

    return captured, fake_build


def test_build_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--mirror", "aliyun"])
    assert called["project"] == tmp_path.resolve()
    assert called["mirror"] == get_mirror("aliyun")


def test_build_default_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    monkeypatch.chdir(tmp_path)
    cli.main(["build"])
    assert called["project"] == tmp_path.resolve()


def test_build_custom_py_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--py-version", "3.12.3"])
    assert called["py_version"] == "3.12.3"


def test_build_target_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--target", "linux"])
    assert called["target"] is Platform.LINUX


def test_build_target_windows_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--target", "windows"])
    assert called["target"] is Platform.WINDOWS


def test_build_keep_module_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--keep-module", "PySide2.QtGui", "--keep-module", "PySide2.QtNetwork"])
    assert called["options"].keep_modules == {"PySide2.QtGui", "PySide2.QtNetwork"}


def test_build_icon_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack b <project> --icon <path>` 解析为绝对路径并封装到 BuildOptions.icon."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    icon_abs = tmp_path / "custom.ico"
    cli.main(["b", str(tmp_path), "--icon", str(icon_abs)])
    assert called["options"].icon == icon_abs.resolve()


def test_build_no_icon_passes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --icon 时 BuildOptions.icon 为 None（由 builder 回退到默认 app.ico）."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].icon is None


def test_build_pyc_options_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--no-pyc`/`--pyc-strip`/`--no-stdlib-trim` 解析并封装到 BuildOptions."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--no-pyc", "--pyc-strip", "--no-stdlib-trim"])
    assert called["options"].no_pyc is True
    assert called["options"].pyc_strip is True
    assert called["options"].no_stdlib_trim is True


def test_build_pyc_options_default_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 pyc 选项时 BuildOptions 各开关均为默认值（开启精简与预编译）."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    opts = called["options"]
    assert isinstance(opts, BuildOptions)
    assert opts.no_pyc is False
    assert opts.pyc_strip is False
    assert opts.no_stdlib_trim is False
    # --pyc-optimize 默认 2（与 cli.py argparse default 一致，iter-35 决策）
    assert opts.pyc_optimize == 2
    assert opts.no_site is False
    assert opts.nuitka is False


def test_build_new_options_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--pyc-optimize`/`--no-site`/`--nuitka` 解析并封装到 BuildOptions."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--pyc-optimize", "2", "--no-site", "--nuitka"])
    assert called["options"].pyc_optimize == 2
    assert called["options"].no_site is True
    assert called["options"].nuitka is True


def test_build_pyc_optimize_invalid_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--pyc-optimize` 仅接受 0/1/2，非法值 argparse 报错."""
    _make_minimal_project(tmp_path)
    monkeypatch.setattr("fspack.builder.build", lambda *a, **kw: None)
    with pytest.raises(SystemExit):
        cli.main(["b", str(tmp_path), "--pyc-optimize", "3"])


def _make_project_with_fspack_config(tmp_path: Path, fspack_config: str) -> Path:
    """在 tmp_path 下创建带 [tool.fspack] 配置的最小项目."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "app"\nversion = "0.1"\n\n{fspack_config}\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return tmp_path


def test_build_config_defaults_applied_when_cli_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[tool.fspack] build_defaults 在 CLI 未指定对应标志时作为回退默认值."""
    _make_project_with_fspack_config(
        tmp_path,
        "[tool.fspack]\nnuitka = true\npyc_strip = true\nno_site = true\npyc_optimize = 1\nno_pyc = true\nno_stdlib_trim = true\n",
    )
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    opts = called["options"]
    assert opts.nuitka is True
    assert opts.pyc_strip is True
    assert opts.no_site is True
    assert opts.pyc_optimize == 1
    assert opts.no_pyc is True
    assert opts.no_stdlib_trim is True


def test_build_cli_flag_overrides_config_pyc_optimize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --pyc-optimize 显式指定优先于 [tool.fspack] pyc_optimize 配置."""
    _make_project_with_fspack_config(tmp_path, "[tool.fspack]\npyc_optimize = 1\n")
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--pyc-optimize", "0"])
    assert called["options"].pyc_optimize == 0


def test_build_cli_and_config_merged_with_any(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """布尔开关用 any([cli, config]) 合并：配置启用 + CLI 未指定 → 启用."""
    _make_project_with_fspack_config(tmp_path, "[tool.fspack]\nnuitka = true\n")
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].nuitka is True


def test_build_cli_nuitka_flag_with_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 [tool.fspack] 配置时 --nuitka CLI 标志正常生效."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--nuitka"])
    assert called["options"].nuitka is True


def test_build_config_pyc_optimize_default_when_both_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 与 [tool.fspack] 均未指定 pyc_optimize 时回退到默认值 2."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].pyc_optimize == 2


def test_run_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(
        project: Path,
        rest_args: list[str] | None = None,
        debug: bool = False,
        entry: str | None = None,
    ) -> None:
        called["project"] = project
        called["rest"] = rest_args
        called["debug"] = debug
        called["entry"] = entry

    monkeypatch.setattr("fspack.runner.run", fake_run)
    cli.main(["r", str(tmp_path), "--", "--foo", "bar"])
    assert called["rest"] == ["--foo", "bar"]


def test_run_debug_flag_after_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack r <project> --debug` 应解析为 debug 标志,而非透传参数。

    回归测试:曾用 argparse.REMAINDER 导致 --debug 被捕获到 rest_args,
    改用 nargs="*" 后 --debug 正确解析为 fspack 选项。
    """
    called: dict[str, Any] = {}

    def fake_run(
        project: Path,
        rest_args: list[str] | None = None,
        debug: bool = False,
        entry: str | None = None,
    ) -> None:
        called["project"] = project
        called["rest"] = rest_args
        called["debug"] = debug
        called["entry"] = entry

    monkeypatch.setattr("fspack.runner.run", fake_run)
    cli.main(["r", str(tmp_path), "--debug"])
    assert called["debug"] is True
    assert called["rest"] == []


def test_run_entry_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack r <project> --entry cli` 解析 entry 参数."""
    called: dict[str, Any] = {}

    def fake_run(
        project: Path,
        rest_args: list[str] | None = None,
        debug: bool = False,
        entry: str | None = None,
    ) -> None:
        called["entry"] = entry

    monkeypatch.setattr("fspack.runner.run", fake_run)
    cli.main(["r", str(tmp_path), "--entry", "cli"])
    assert called["entry"] == "cli"


def test_clean_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Path] = {}
    monkeypatch.setattr("fspack.builder.clean_dist", lambda project: called.__setitem__("p", project))
    cli.main(["c", str(tmp_path)])
    assert called["p"] == tmp_path.resolve()


def test_package_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """package 子命令直接调用 installer.build_release，校验参数透传."""
    called: dict[str, Any] = {}

    def fake_build_release(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        no_build: bool = False,
        dist_dir: Path | None = None,
        target: object = None,
        fmt: str = "auto",
    ) -> list[Path]:
        called["project"] = project
        called["mirror"] = mirror
        called["no_build"] = no_build
        return []

    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_build_release)
    cli.main(["p", str(tmp_path), "--mirror", "aliyun", "--no-build"])
    assert called["project"] == tmp_path.resolve()
    # mirror 经 get_mirror 转为 MirrorConfig，校验 name 字段
    assert called["mirror"].name == "阿里云"
    assert called["no_build"] is True


def test_drop_separator() -> None:
    assert cli._drop_separator(["--", "a", "b"]) == ["a", "b"]
    assert cli._drop_separator(["a", "b"]) == ["a", "b"]
    assert cli._drop_separator([]) == []


def test_invalid_mirror_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["b", str(tmp_path), "--mirror", "nope"])


# ---------- 私有包源（--extra-index-url / --find-links）----------


def test_build_extra_index_url_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--extra-index-url 可多次指定，作为元组透传给 build()."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(
        [
            "b",
            str(tmp_path),
            "--extra-index-url",
            "https://pypi.company.com/simple/",
            "--extra-index-url",
            "https://mirror.example.com/pypi",
        ]
    )
    assert called["extra_index_urls"] == (
        "https://pypi.company.com/simple/",
        "https://mirror.example.com/pypi",
    )


def test_build_find_links_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--find-links 可多次指定，作为元组透传给 build()."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--find-links", "./wheels", "--find-links", "https://example.com/wheels/"])
    assert called["find_links"] == ("./wheels", "https://example.com/wheels/")


def test_build_no_private_sources_defaults_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --extra-index-url/--find-links 时传空元组给 build()."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["extra_index_urls"] == ()
    assert called["find_links"] == ()


def test_build_extra_index_url_combined_with_find_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同时指定 --extra-index-url 与 --find-links，二者各自透传."""
    _make_minimal_project(tmp_path)
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(
        [
            "b",
            str(tmp_path),
            "--extra-index-url",
            "https://pypi.company.com/simple/",
            "--find-links",
            "./wheels",
        ]
    )
    assert called["extra_index_urls"] == ("https://pypi.company.com/simple/",)
    assert called["find_links"] == ("./wheels",)


# ---------- CLI 启动懒加载 ----------


def test_mirrors_choices_returns_valid_list() -> None:
    """_mirrors_choices 返回非空字符串列表（延迟导入 MIRRORS）."""
    choices = cli._mirrors_choices()
    assert isinstance(choices, list)
    assert len(choices) > 0
    assert all(isinstance(c, str) for c in choices)


def test_cli_module_no_top_level_console_import() -> None:
    """cli.py 顶部不再导入 fspack.console（懒加载优化）.

    验证 ``from fspack.console import console`` 不在模块顶层导入，
    确保 ``fsp --help`` 不触发 rich 模块加载（~17ms）。
    """
    import inspect

    source = inspect.getsource(cli)
    # 顶部导入区（build_parser 之前的全局代码）不应含 console
    top_section = source.split("def build_parser")[0]
    assert "from fspack.console" not in top_section
    assert "import fspack.console" not in top_section


def test_cli_module_no_top_level_platform_import() -> None:
    """cli.py 顶部不再导入 fspack.platform（懒加载优化）.

    Platform 仅在 TYPE_CHECKING 块和 _parse_target 函数内导入。
    """
    import inspect

    source = inspect.getsource(cli)
    top_section = source.split("def build_parser")[0]
    # TYPE_CHECKING 块内的导入允许
    assert "from fspack.platform import Platform" not in top_section.replace(
        "if TYPE_CHECKING:\n    from fspack.platform import Platform", ""
    )
