"""cli 子命令分发测试。."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack import __version__, cli


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

    def fake_run(project: Path, mirror: str | None = None, py_version: str | None = None) -> None:
        called["project"] = project
        called["mirror"] = mirror

    monkeypatch.setattr(cli.build_cmd, "run", fake_run)
    cli.main(["b", str(tmp_path), "--mirror", "aliyun"])
    assert called["project"] == tmp_path.resolve()
    assert called["mirror"] == "aliyun"


def test_build_default_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Path] = {}
    monkeypatch.setattr(
        cli.build_cmd, "run", lambda project, mirror=None, py_version=None: called.__setitem__("p", project)
    )
    monkeypatch.chdir(tmp_path)
    cli.main(["build"])
    assert called["p"] == tmp_path.resolve()


def test_build_custom_py_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}
    monkeypatch.setattr(
        cli.build_cmd, "run", lambda project, mirror=None, py_version=None: called.__setitem__("pv", py_version)
    )
    cli.main(["b", str(tmp_path), "--py-version", "3.12.3"])
    assert called["pv"] == "3.12.3"


def test_run_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_run(project: Path, rest_args: list[str] | None = None) -> None:
        called["project"] = project
        called["rest"] = rest_args

    monkeypatch.setattr(cli.run_cmd, "run", fake_run)
    cli.main(["r", str(tmp_path), "--", "--foo", "bar"])
    assert called["rest"] == ["--foo", "bar"]


def test_clean_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Path] = {}
    monkeypatch.setattr(cli.clean_cmd, "run", lambda project: called.__setitem__("p", project))
    cli.main(["c", str(tmp_path)])
    assert called["p"] == tmp_path.resolve()


def test_drop_separator() -> None:
    assert cli._drop_separator(["--", "a", "b"]) == ["a", "b"]
    assert cli._drop_separator(["a", "b"]) == ["a", "b"]
    assert cli._drop_separator([]) == []


def test_invalid_mirror_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["b", str(tmp_path), "--mirror", "nope"])
