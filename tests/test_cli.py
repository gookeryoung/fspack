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
    ) -> None:
        captured["project"] = project
        captured["mirror"] = mirror
        captured["py_version"] = py_version
        captured["target"] = target
        captured["options"] = options

    return captured, fake_build


def test_build_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--mirror", "aliyun"])
    assert called["project"] == tmp_path.resolve()
    assert called["mirror"] == get_mirror("aliyun")


def test_build_default_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    monkeypatch.chdir(tmp_path)
    cli.main(["build"])
    assert called["project"] == tmp_path.resolve()


def test_build_custom_py_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--py-version", "3.12.3"])
    assert called["py_version"] == "3.12.3"


def test_build_target_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--target", "linux"])
    assert called["target"] is Platform.LINUX


def test_build_target_windows_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--target", "windows"])
    assert called["target"] is Platform.WINDOWS


def test_build_keep_module_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--keep-module", "PySide2.QtGui", "--keep-module", "PySide2.QtNetwork"])
    assert called["options"].keep_modules == {"PySide2.QtGui", "PySide2.QtNetwork"}


def test_build_icon_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack b <project> --icon <path>` 解析为绝对路径并封装到 BuildOptions.icon."""
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    icon_abs = tmp_path / "custom.ico"
    cli.main(["b", str(tmp_path), "--icon", str(icon_abs)])
    assert called["options"].icon == icon_abs.resolve()


def test_build_no_icon_passes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --icon 时 BuildOptions.icon 为 None（由 builder 回退到默认 app.ico）."""
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    assert called["options"].icon is None


def test_build_pyc_options_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--no-pyc`/`--pyc-strip`/`--no-stdlib-trim` 解析并封装到 BuildOptions."""
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--no-pyc", "--pyc-strip", "--no-stdlib-trim"])
    assert called["options"].no_pyc is True
    assert called["options"].pyc_strip is True
    assert called["options"].no_stdlib_trim is True


def test_build_pyc_options_default_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 pyc 选项时 BuildOptions 各开关均为默认值（开启精简与预编译）."""
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path)])
    opts = called["options"]
    assert isinstance(opts, BuildOptions)
    assert opts.no_pyc is False
    assert opts.pyc_strip is False
    assert opts.no_stdlib_trim is False
    assert opts.pyc_optimize == 0
    assert opts.no_site is False
    assert opts.nuitka is False


def test_build_new_options_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--pyc-optimize`/`--no-site`/`--nuitka` 解析并封装到 BuildOptions."""
    called, fake_build = _capture_build()
    monkeypatch.setattr("fspack.builder.build", fake_build)
    cli.main(["b", str(tmp_path), "--pyc-optimize", "2", "--no-site", "--nuitka"])
    assert called["options"].pyc_optimize == 2
    assert called["options"].no_site is True
    assert called["options"].nuitka is True


def test_build_pyc_optimize_invalid_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--pyc-optimize` 仅接受 0/1/2，非法值 argparse 报错."""
    monkeypatch.setattr("fspack.builder.build", lambda *a, **kw: None)
    with pytest.raises(SystemExit):
        cli.main(["b", str(tmp_path), "--pyc-optimize", "3"])


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

    monkeypatch.setattr("fspack.commands.run.run", fake_run)
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

    monkeypatch.setattr("fspack.commands.run.run", fake_run)
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

    monkeypatch.setattr("fspack.commands.run.run", fake_run)
    cli.main(["r", str(tmp_path), "--entry", "cli"])
    assert called["entry"] == "cli"


def test_clean_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Path] = {}
    monkeypatch.setattr("fspack.commands.clean.run", lambda project: called.__setitem__("p", project))
    cli.main(["c", str(tmp_path)])
    assert called["p"] == tmp_path.resolve()


def test_package_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(  # noqa: PLR0913
        project: Path,
        mirror: str | None = None,
        py_version: str | None = None,
        no_build: bool = False,
        target: object = None,
        fmt: str = "auto",
    ) -> None:
        called["project"] = project
        called["mirror"] = mirror
        called["no_build"] = no_build

    monkeypatch.setattr("fspack.commands.package.run", fake_run)
    cli.main(["p", str(tmp_path), "--mirror", "aliyun", "--no-build"])
    assert called["project"] == tmp_path.resolve()
    assert called["mirror"] == "aliyun"
    assert called["no_build"] is True


def test_drop_separator() -> None:
    assert cli._drop_separator(["--", "a", "b"]) == ["a", "b"]
    assert cli._drop_separator(["a", "b"]) == ["a", "b"]
    assert cli._drop_separator([]) == []


def test_invalid_mirror_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["b", str(tmp_path), "--mirror", "nope"])
