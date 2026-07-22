"""cli 子命令分发测试."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack import __version__, cli
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


def test_build_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(  # noqa: PLR0913
        project: Path,
        mirror: str | None = None,
        py_version: str | None = None,
        target: object = None,
        keep_modules: set[str] | None = None,
        icon: Path | None = None,
    ) -> None:
        called["project"] = project
        called["mirror"] = mirror

    monkeypatch.setattr(cli.build_cmd, "run", fake_run)
    cli.main(["b", str(tmp_path), "--mirror", "aliyun"])
    assert called["project"] == tmp_path.resolve()
    assert called["mirror"] == "aliyun"


def test_build_default_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Path] = {}
    monkeypatch.setattr(
        cli.build_cmd,
        "run",
        lambda project, mirror=None, py_version=None, target=None, keep_modules=None, icon=None: called.__setitem__(
            "p", project
        ),
    )
    monkeypatch.chdir(tmp_path)
    cli.main(["build"])
    assert called["p"] == tmp_path.resolve()


def test_build_custom_py_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}
    monkeypatch.setattr(
        cli.build_cmd,
        "run",
        lambda project, mirror=None, py_version=None, target=None, keep_modules=None, icon=None: called.__setitem__(
            "pv", py_version
        ),
    )
    cli.main(["b", str(tmp_path), "--py-version", "3.12.3"])
    assert called["pv"] == "3.12.3"


def test_build_target_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(  # noqa: PLR0913
        project: Path,
        mirror: str | None = None,
        py_version: str | None = None,
        target: object = None,
        keep_modules: set[str] | None = None,
        icon: Path | None = None,
    ) -> None:
        called["target"] = target

    monkeypatch.setattr(cli.build_cmd, "run", fake_run)
    cli.main(["b", str(tmp_path), "--target", "linux"])
    assert called["target"] is Platform.LINUX


def test_build_target_windows_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(  # noqa: PLR0913
        project: Path,
        mirror: str | None = None,
        py_version: str | None = None,
        target: object = None,
        keep_modules: set[str] | None = None,
        icon: Path | None = None,
    ) -> None:
        called["target"] = target

    monkeypatch.setattr(cli.build_cmd, "run", fake_run)
    cli.main(["b", str(tmp_path), "--target", "windows"])
    assert called["target"] is Platform.WINDOWS


def test_build_keep_module_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(  # noqa: PLR0913
        project: Path,
        mirror: str | None = None,
        py_version: str | None = None,
        target: object = None,
        keep_modules: set[str] | None = None,
        icon: Path | None = None,
    ) -> None:
        called["keep_modules"] = keep_modules

    monkeypatch.setattr(cli.build_cmd, "run", fake_run)
    cli.main(["b", str(tmp_path), "--keep-module", "PySide2.QtGui", "--keep-module", "PySide2.QtNetwork"])
    assert called["keep_modules"] == {"PySide2.QtGui", "PySide2.QtNetwork"}


def test_build_icon_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fspack b <project> --icon <path>` 解析为绝对路径并传递给 build_cmd.run."""
    called: dict[str, Any] = {}

    def fake_run(  # noqa: PLR0913
        project: Path,
        mirror: str | None = None,
        py_version: str | None = None,
        target: object = None,
        keep_modules: set[str] | None = None,
        icon: Path | None = None,
    ) -> None:
        called["icon"] = icon

    monkeypatch.setattr(cli.build_cmd, "run", fake_run)
    # 用绝对路径避免 CWD 依赖
    icon_abs = tmp_path / "custom.ico"
    cli.main(["b", str(tmp_path), "--icon", str(icon_abs)])
    assert called["icon"] == icon_abs.resolve()


def test_build_no_icon_passes_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 --icon 时传递 None（由 builder 回退到默认 app.ico）."""
    called: dict[str, Any] = {}

    def fake_run(  # noqa: PLR0913
        project: Path,
        mirror: str | None = None,
        py_version: str | None = None,
        target: object = None,
        keep_modules: set[str] | None = None,
        icon: Path | None = None,
    ) -> None:
        called["icon"] = icon

    monkeypatch.setattr(cli.build_cmd, "run", fake_run)
    cli.main(["b", str(tmp_path)])
    assert called["icon"] is None


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

    monkeypatch.setattr(cli.run_cmd, "run", fake_run)
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

    monkeypatch.setattr(cli.run_cmd, "run", fake_run)
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

    monkeypatch.setattr(cli.run_cmd, "run", fake_run)
    cli.main(["r", str(tmp_path), "--entry", "cli"])
    assert called["entry"] == "cli"


def test_clean_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Path] = {}
    monkeypatch.setattr(cli.clean_cmd, "run", lambda project: called.__setitem__("p", project))
    cli.main(["c", str(tmp_path)])
    assert called["p"] == tmp_path.resolve()


def test_package_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(
        project: Path,
        mirror: str | None = None,
        py_version: str | None = None,
        no_build: bool = False,
        target: object = None,
    ) -> None:
        called["project"] = project
        called["mirror"] = mirror
        called["no_build"] = no_build

    monkeypatch.setattr(cli.package_cmd, "run", fake_run)
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
