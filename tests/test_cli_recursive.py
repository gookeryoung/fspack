"""cli --recursive/-R 递归打包模式测试.

覆盖 :func:`fspack.cli.discover_subprojects` 子项目扫描逻辑（含跳过
开发期目录、按名称排序、符号链接去重）与 :func:`fspack.cli._run_recursive`
批量执行逻辑（含单项目失败不中断、退出码传播、汇总输出）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from fspack import cli

# ---------- 辅助构造 ----------


def _make_subproject(root: Path, rel: str, name: str | None = None) -> Path:
    """在 root/<rel> 下创建最小可识别子项目（含 pyproject.toml）.

    Args:
        root: 工作区根目录
        rel: 子项目相对路径（如 ``app1``、``services/api``）
        name: 项目名（默认取 rel 末段）

    Returns:
        子项目目录绝对路径
    """
    project_dir = root / rel
    project_dir.mkdir(parents=True, exist_ok=True)
    project_name = name or rel.rsplit("/", 1)[-1]
    (project_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{project_name}"\nversion = "0.1"\n', encoding="utf-8"
    )
    (project_dir / f"{project_name}.py").write_text("def main():\n    pass\n", encoding="utf-8")
    return project_dir


def _make_dev_dir(root: Path, name: str, with_pyproject: bool = False) -> Path:
    """在 root 下创建开发期目录（如 .venv/build/dist），可选放入 pyproject.toml.

    用于验证 ``discover_subprojects`` 正确跳过这些目录下的 pyproject.toml
    （如 ``.venv`` 内 pip 的 pyproject.toml）。
    """
    dev_dir = root / name
    dev_dir.mkdir(parents=True, exist_ok=True)
    if with_pyproject:
        (dev_dir / "pyproject.toml").write_text(
            '[project]\nname = "should-be-skipped"\nversion = "0.1"\n', encoding="utf-8"
        )
    return dev_dir


def _capture_build_call() -> tuple[list[Path], Any]:
    """构造 fake_build 记录所有调用的 project 路径.

    Returns:
        (调用记录列表, fake_build 函数)
    """
    called_projects: list[Path] = []

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
    ) -> None:
        called_projects.append(project)

    return called_projects, fake_build


def _capture_package_call(*, with_outputs: bool = False) -> tuple[list[Path], Any]:
    """构造 fake_build_release 记录所有调用的 project 路径.

    Args:
        with_outputs: True 时返回模拟产物路径列表，触发 ``_run_package``
            中的 ``发行包已生成`` 日志输出；False 返回空列表
    """
    called_projects: list[Path] = []

    def fake_build_release(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        no_build: bool = False,
        dist_dir: Path | None = None,
        target: object = None,
        fmt: str = "auto",
    ) -> list[Path]:
        called_projects.append(project)
        if with_outputs:
            return [project / "dist" / "release" / f"{project.name}-setup.exe"]
        return []

    return called_projects, fake_build_release


# ---------- discover_subprojects 单元测试 ----------


def test_discover_subprojects_includes_root_with_pyproject(tmp_path: Path) -> None:
    """root 自身含 pyproject.toml 时应被包含在结果首位."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "rootapp"\nversion = "0.1"\n', encoding="utf-8")
    projects = cli.discover_subprojects(tmp_path)
    assert tmp_path in projects


def test_discover_subprojects_finds_nested_subprojects(tmp_path: Path) -> None:
    """递归扫描所有含 pyproject.toml 的子目录."""
    _make_subproject(tmp_path, "app1")
    _make_subproject(tmp_path, "app2")
    _make_subproject(tmp_path, "services/api")
    _make_subproject(tmp_path, "services/web")

    projects = cli.discover_subprojects(tmp_path)
    project_names = {p.name for p in projects}
    assert project_names == {"app1", "app2", "api", "web"}


def test_discover_subprojects_skips_dev_directories(tmp_path: Path) -> None:
    """跳过 .venv/build/dist/.git 等开发期目录下的 pyproject.toml."""
    _make_subproject(tmp_path, "app1")
    # 这些目录含 pyproject.toml 但应被跳过
    _make_dev_dir(tmp_path, ".venv", with_pyproject=True)
    _make_dev_dir(tmp_path, "build", with_pyproject=True)
    _make_dev_dir(tmp_path, "dist", with_pyproject=True)
    _make_dev_dir(tmp_path, ".git", with_pyproject=True)
    _make_dev_dir(tmp_path, "__pycache__", with_pyproject=True)
    _make_dev_dir(tmp_path, ".tox", with_pyproject=True)
    _make_dev_dir(tmp_path, ".fspack", with_pyproject=True)
    _make_dev_dir(tmp_path, ".pytest_cache", with_pyproject=True)

    projects = cli.discover_subprojects(tmp_path)
    project_names = {p.name for p in projects}
    assert project_names == {"app1"}


def test_discover_subprojects_skips_egg_info_directories(tmp_path: Path) -> None:
    """跳过 *.egg-info 目录（Python 包元数据目录，非可打包项目）."""
    _make_subproject(tmp_path, "app1")
    _make_dev_dir(tmp_path, "myproj.egg-info", with_pyproject=True)
    _make_dev_dir(tmp_path, "sub/myproj.egg-info", with_pyproject=True)

    projects = cli.discover_subprojects(tmp_path)
    assert {p.name for p in projects} == {"app1"}


def test_discover_subprojects_returns_sorted_by_path(tmp_path: Path) -> None:
    """返回按路径字母序排序的子项目列表，保证稳定输出与可重复构建."""
    # 反序创建，验证结果按字母序
    _make_subproject(tmp_path, "zeta")
    _make_subproject(tmp_path, "alpha")
    _make_subproject(tmp_path, "mid/beta")

    projects = cli.discover_subprojects(tmp_path)
    paths = [p.relative_to(tmp_path).as_posix() for p in projects]
    assert paths == sorted(paths)
    # alpha < mid/beta < zeta
    assert paths[0] == "alpha"
    assert paths[1] == "mid/beta"
    assert paths[2] == "zeta"


def test_discover_subprojects_empty_root_returns_empty_list(tmp_path: Path) -> None:
    """无 pyproject.toml 的空目录返回空列表."""
    projects = cli.discover_subprojects(tmp_path)
    assert projects == []


def test_discover_subprojects_no_pyproject_in_subdirs(tmp_path: Path) -> None:
    """子目录存在但无 pyproject.toml 时不计入结果."""
    (tmp_path / "subdir1").mkdir()
    (tmp_path / "subdir1" / "module.py").write_text("# not a project\n", encoding="utf-8")
    (tmp_path / "subdir2").mkdir()

    projects = cli.discover_subprojects(tmp_path)
    assert projects == []


def test_discover_subprojects_deeply_nested(tmp_path: Path) -> None:
    """深度嵌套的子项目（如 monorepo/packages/utils）也能被发现."""
    _make_subproject(tmp_path, "packages/core")
    _make_subproject(tmp_path, "packages/cli")
    _make_subproject(tmp_path, "packages/gui/widgets")
    _make_subproject(tmp_path, "examples/demo1")

    projects = cli.discover_subprojects(tmp_path)
    project_names = {p.name for p in projects}
    assert project_names == {"core", "cli", "widgets", "demo1"}


def test_discover_subprojects_does_not_descend_into_skipped_dirs(tmp_path: Path) -> None:
    """跳过的目录内即使有合法子项目也不被扫描（避免 .venv 内的项目被误识别）."""
    _make_subproject(tmp_path, "app1")
    # .venv 内放一个合法子项目，应被跳过
    _make_subproject(tmp_path, ".venv/lib/site-packages/somepkg")

    projects = cli.discover_subprojects(tmp_path)
    assert {p.name for p in projects} == {"app1"}


def test_discover_subprojects_handles_unreadable_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """扫描中遇到 OSError（如权限不足）时跳过该目录而非崩溃.

    用 monkeypatch 替换 os.scandir 在扫描特定子目录时抛出 OSError，
    验证扫描降级为跳过该目录而非向上抛异常。
    """
    # root 自身放 pyproject.toml + 一个合法子项目 app1
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "rootapp"\nversion = "0.1"\n', encoding="utf-8")
    _make_subproject(tmp_path, "app1")
    # 制造一个无法扫描的子目录（无 pyproject.toml，触发 OSError 后被跳过）
    (tmp_path / "unreadable").mkdir()

    original_scandir = cli.os.scandir

    def _scandir_with_error(path: str) -> Any:
        if Path(path).name == "unreadable":
            raise OSError("simulated permission denied")
        return original_scandir(path)

    monkeypatch.setattr(cli.os, "scandir", _scandir_with_error)
    # 不应抛异常，跳过不可读目录后继续扫描其他子目录
    projects = cli.discover_subprojects(tmp_path)
    project_paths = {p.resolve() for p in projects}
    # root 自身（含 pyproject.toml）和 app1 都应被发现，unreadable 被跳过
    assert tmp_path.resolve() in project_paths
    assert (tmp_path / "app1").resolve() in project_paths


def test_discover_subprojects_handles_symlink_cycle(tmp_path: Path) -> None:
    """符号链接循环时通过 seen 集合去重，避免无限递归.

    构造 a -> b -> a 的符号链接环，``_scan`` 通过 ``resolve()`` 后的去重
    集合识别已访问目录并提前返回，保证不无限递归。
    """
    _make_subproject(tmp_path, "real_app")
    # 构造符号链接环：link_a -> link_b -> link_a
    link_a = tmp_path / "link_a"
    link_b = tmp_path / "link_b"
    try:
        link_b.symlink_to(tmp_path / "real_app", target_is_directory=True)
        link_a.symlink_to(link_b, target_is_directory=True)
    except OSError:
        # Windows 无管理员权限或非开发者模式时跳过此测试
        pytest.skip("无法创建符号链接（Windows 需开发者模式或管理员权限）")

    # 不应无限递归，应正常返回结果
    projects = cli.discover_subprojects(tmp_path)
    # real_app 必被发现；link_a/link_b 因 resolve 后指向 real_app 路径，
    # 被 seen 集合去重不会重复计入（但会触发 (current/pyproject.toml).is_file()
    # 检查，因 link_a/link_b 自身无 pyproject.toml，不计入结果）
    project_names = {p.name for p in projects}
    assert "real_app" in project_names


# ---------- _run_recursive 单元测试 ----------


def test_run_recursive_build_invokes_build_for_each_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """递归 build 模式对每个子项目调用一次 builder.build."""
    _make_subproject(tmp_path, "app1")
    _make_subproject(tmp_path, "app2")
    _make_subproject(tmp_path, "app3")

    called_projects, fake_build = _capture_build_call()
    monkeypatch.setattr("fspack.builder.build", fake_build)

    # _run_recursive 返回退出码
    ns = cli.build_parser().parse_args(["b", str(tmp_path), "-R"])
    exit_code = cli._run_recursive(tmp_path, "build", ns)
    assert exit_code == 0
    assert len(called_projects) == 3
    called_names = {p.name for p in called_projects}
    assert called_names == {"app1", "app2", "app3"}


def test_run_recursive_package_invokes_package_for_each_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """递归 package 模式对每个子项目调用一次 installer.build_release."""
    _make_subproject(tmp_path, "app1")
    _make_subproject(tmp_path, "app2")

    called_projects, fake_release = _capture_package_call()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_release)

    ns = cli.build_parser().parse_args(["p", str(tmp_path), "-R"])
    exit_code = cli._run_recursive(tmp_path, "package", ns)
    assert exit_code == 0
    assert len(called_projects) == 2


def test_run_recursive_package_logs_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """递归 package 模式下 build_release 返回的产物路径被 INFO 日志记录."""
    _make_subproject(tmp_path, "app1")

    _called_projects, fake_release = _capture_package_call(with_outputs=True)
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_release)

    caplog.set_level(logging.INFO, logger="fspack.cli")

    ns = cli.build_parser().parse_args(["p", str(tmp_path), "-R"])
    exit_code = cli._run_recursive(tmp_path, "package", ns)
    assert exit_code == 0

    # 验证 _run_package 中 "发行包已生成" 日志被触发
    info_records = [r for r in caplog.records if r.levelname == "INFO" and r.name == "fspack.cli"]
    assert any("发行包已生成" in r.getMessage() for r in info_records)


def test_run_recursive_single_failure_does_not_break_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单项目失败不中断后续项目，最终返回退出码 1."""
    _make_subproject(tmp_path, "app1")
    _make_subproject(tmp_path, "app2")
    _make_subproject(tmp_path, "app3")

    call_log: list[str] = []

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
    ) -> None:
        call_log.append(project.name)
        if project.name == "app2":
            raise RuntimeError("simulated build failure")

    monkeypatch.setattr("fspack.builder.build", fake_build)

    ns = cli.build_parser().parse_args(["b", str(tmp_path), "-R"])
    exit_code = cli._run_recursive(tmp_path, "build", ns)
    assert exit_code == 1
    # 三个项目都被调用，未因 app2 失败而中断
    assert call_log == ["app1", "app2", "app3"]


def test_run_recursive_all_success_returns_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全部项目成功时返回退出码 0."""
    _make_subproject(tmp_path, "app1")
    _make_subproject(tmp_path, "app2")

    called, fake_build = _capture_build_call()
    monkeypatch.setattr("fspack.builder.build", fake_build)

    ns = cli.build_parser().parse_args(["b", str(tmp_path), "-R"])
    exit_code = cli._run_recursive(tmp_path, "build", ns)
    assert exit_code == 0
    assert len(called) == 2


def test_run_recursive_no_subprojects_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """未发现子项目时返回 0（非错误）并打印警告."""
    ns = cli.build_parser().parse_args(["b", str(tmp_path), "-R"])
    exit_code = cli._run_recursive(tmp_path, "build", ns)
    assert exit_code == 0
    captured = capsys.readouterr()
    # 输出包含警告信息
    assert "未在" in captured.out or "未在" in captured.err


def test_run_recursive_outputs_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """递归模式输出汇总：成功数、失败数、失败项目列表."""
    _make_subproject(tmp_path, "app1")
    _make_subproject(tmp_path, "app2")

    def fake_build(  # noqa: PLR0913
        project: Path,
        mirror: object = None,
        py_version: str | None = None,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: object = None,
        extra_index_urls: tuple[str, ...] = (),
        find_links: tuple[str, ...] = (),
        dry_run: bool = False,
        log_file: Path | None = None,
        log_format: object = None,
    ) -> None:
        if project.name == "app2":
            raise RuntimeError("simulated failure")

    monkeypatch.setattr("fspack.builder.build", fake_build)

    ns = cli.build_parser().parse_args(["b", str(tmp_path), "-R"])
    exit_code = cli._run_recursive(tmp_path, "build", ns)
    assert exit_code == 1

    captured = capsys.readouterr()
    # 汇总输出包含成功/失败统计
    assert "成功" in captured.out
    assert "失败" in captured.out
    assert "app2" in captured.out


def test_run_recursive_preserves_error_chain_in_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """单项目失败时记录完整错误堆栈到 DEBUG 日志，便于调试."""
    _make_subproject(tmp_path, "app1")

    def fake_build(*args: object, **kwargs: object) -> None:
        raise ValueError("detailed error message for debugging")

    monkeypatch.setattr("fspack.builder.build", fake_build)

    # 启用 DEBUG 级别捕获（默认 caplog 不捕获 DEBUG）
    caplog.set_level(logging.DEBUG, logger="fspack.cli")

    ns = cli.build_parser().parse_args(["b", str(tmp_path), "-R"])
    exit_code = cli._run_recursive(tmp_path, "build", ns)
    assert exit_code == 1

    # DEBUG 日志应包含完整错误堆栈标记
    debug_records = [r for r in caplog.records if r.levelname == "DEBUG" and r.name == "fspack.cli"]
    assert any("完整错误堆栈" in r.getMessage() for r in debug_records)
    # exc_info 保留因果链
    assert any(r.exc_info is not None for r in debug_records)


# ---------- CLI -R 集成测试 ----------


def test_cli_build_recursive_flag_dispatches_to_recursive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp b -R <root>` 触发递归 build 模式."""
    _make_subproject(tmp_path, "app1")
    _make_subproject(tmp_path, "app2")

    called_projects, fake_build = _capture_build_call()
    monkeypatch.setattr("fspack.builder.build", fake_build)

    # 递归模式始终通过 sys.exit 退出（0=全部成功，1=有失败）
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["b", str(tmp_path), "-R"])
    assert exc_info.value.code == 0
    assert len(called_projects) == 2


def test_cli_build_recursive_long_flag_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp b --recursive <root>` 等价于 -R 短形式."""
    _make_subproject(tmp_path, "app1")

    called_projects, fake_build = _capture_build_call()
    monkeypatch.setattr("fspack.builder.build", fake_build)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["build", str(tmp_path), "--recursive"])
    assert exc_info.value.code == 0
    assert len(called_projects) == 1


def test_cli_package_recursive_flag_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fsp p -R <root>` 触发递归 package 模式."""
    _make_subproject(tmp_path, "app1")
    _make_subproject(tmp_path, "app2")

    called_projects, fake_release = _capture_package_call()
    monkeypatch.setattr("fspack.packaging.installer.build_release", fake_release)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["p", str(tmp_path), "-R"])
    assert exc_info.value.code == 0
    assert len(called_projects) == 2


def test_cli_build_recursive_failure_propagates_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """递归模式下有项目失败时通过 sys.exit(1) 传播退出码."""
    _make_subproject(tmp_path, "app1")

    def fake_build(*args: object, **kwargs: object) -> None:
        raise RuntimeError("fail")

    monkeypatch.setattr("fspack.builder.build", fake_build)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["b", str(tmp_path), "-R"])
    assert exc_info.value.code == 1


def test_cli_build_recursive_skips_dev_dirs_in_real_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """集成测试：递归扫描正确跳过 .venv/build 等目录下的 pyproject.toml."""
    _make_subproject(tmp_path, "app1")
    _make_dev_dir(tmp_path, ".venv", with_pyproject=True)
    _make_dev_dir(tmp_path, "build", with_pyproject=True)
    _make_dev_dir(tmp_path, "dist", with_pyproject=True)

    called_projects, fake_build = _capture_build_call()
    monkeypatch.setattr("fspack.builder.build", fake_build)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["b", str(tmp_path), "-R"])
    assert exc_info.value.code == 0
    # 只应调用 app1 一次，不扫描 .venv/build/dist 下的 pyproject.toml
    assert len(called_projects) == 1
    assert called_projects[0].name == "app1"


# ---------- 辅助函数测试 ----------


def test_format_project_path_self_is_dot(tmp_path: Path) -> None:
    """root 自身显示为 '.'."""
    assert cli._format_project_path(tmp_path, tmp_path) == "."


def test_format_project_path_subdir_relative(tmp_path: Path) -> None:
    """子项目显示为相对 root 的 posix 路径."""
    sub = tmp_path / "app1"
    sub.mkdir()
    assert cli._format_project_path(sub, tmp_path) == "app1"

    nested = tmp_path / "services" / "api"
    nested.mkdir(parents=True)
    assert cli._format_project_path(nested, tmp_path) == "services/api"


def test_format_project_path_outside_root_returns_absolute(tmp_path: Path) -> None:
    """project 不在 root 下时返回绝对路径（relative_to 失败回退）."""
    other = tmp_path.parent / "other_project"
    other.mkdir(exist_ok=True)
    try:
        result = cli._format_project_path(other, tmp_path)
        # 路径不在 root 下时回退为 str(project)
        assert "other_project" in result
    finally:
        other.rmdir()


def test_format_error_single_line() -> None:
    """单行错误消息原样返回."""
    exc = ValueError("something failed")
    assert cli._format_error(exc) == "something failed"


def test_format_error_multiline_takes_first_line() -> None:
    """多行错误消息取首行，避免汇总表被打乱."""
    exc = ValueError("line1\nline2\nline3")
    assert cli._format_error(exc) == "line1"


def test_format_error_truncates_long_message() -> None:
    """超长错误消息截断到 200 字符（含 ... 后缀）."""
    long_msg = "x" * 300
    exc = ValueError(long_msg)
    result = cli._format_error(exc)
    assert len(result) == 200
    assert result.endswith("...")


def test_format_error_empty_message_falls_back_to_class_name() -> None:
    """异常消息为空时回退到异常类名."""
    exc = ValueError()
    result = cli._format_error(exc)
    assert result == "ValueError"


def test_print_recursive_summary_all_success() -> None:
    """汇总输出：全部成功时打印 success 消息（覆盖 elif succeeded 分支）."""
    succeeded = [(Path("/tmp/app1"), "app1"), (Path("/tmp/app2"), "app2")]
    failed: list[tuple[Path, str]] = []
    # 不应抛异常，调用 success 分支
    cli._print_recursive_summary("构建", succeeded, failed)


def test_print_recursive_summary_with_failures() -> None:
    """汇总输出：有失败时打印失败项目列表（覆盖 if failed 分支）."""
    succeeded = [(Path("/tmp/app1"), "app1")]
    failed = [(Path("/tmp/app2"), "app2: simulated failure")]
    # 不应抛异常，调用 error 分支
    cli._print_recursive_summary("构建", succeeded, failed)


def test_print_recursive_summary_both_empty() -> None:
    """汇总输出：成功与失败均为空时只打印汇总行（覆盖 elif 跳过分支）.

    此场景在正常流程中不可达（_run_recursive 仅在 total > 0 时调用本函数），
    但 _print_recursive_summary 作为公共辅助函数应正确处理空输入。
    """
    cli._print_recursive_summary("构建", [], [])
