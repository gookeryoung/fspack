"""``fsp doctor`` 环境诊断子命令单元测试.

覆盖 :mod:`fspack.cli_doctor` 的核心场景：

- :class:`CheckResult`/``CheckStatus``/``DoctorReport`` 数据结构
- :func:`_check_tool_version` 通用工具检查（成功/未找到/超时/退出码非零）
- :func:`_check_pillow`/``_check_pip``/``_check_mingw`` 等具体工具检查
- :func:`_check_cache_dir` 缓存目录扫描（不存在/正常/扫描失败）
- :func:`_format_size`/``_format_status`` 辅助函数
- :func:`run_doctor` 聚合报告（按平台过滤工具）
- :func:`print_doctor_report` 渲染不抛异常
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from fspack.cli_doctor import (
    CheckResult,
    CheckStatus,
    DoctorReport,
    TemplateBuildResult,
    TemplateRunResult,
    _bench_history_group_dir,
    _build_debug_cmd,
    _build_run_cmd,
    _check_cache_dir,
    _check_pillow,
    _check_pip,
    _check_tool_version,
    _collect_machine_info,
    _deserialize_bench_results,
    _dir_size,
    _find_debug_python,
    _find_dist_exe,
    _find_wrapper,
    _format_bench_delta,
    _format_run_status,
    _format_size,
    _format_status,
    _load_previous_bench_history,
    _machine_id,
    _print_bench_comparison,
    _print_performance_analysis,
    _print_template_build_summary,
    _run_template,
    _save_and_compare_bench,
    _save_bench_history,
    _serialize_bench_results,
    print_doctor_report,
    run_doctor,
)
from fspack.console import console
from fspack.platform import Platform


@pytest.fixture(autouse=True)
def _fixed_rich_width() -> Iterator[None]:
    """固定 Rich Console 宽度，避免窄终端环境下 word wrap 导致断言失败.

    多个测试渲染含 8 列的 Rich Table（bench 模式汇总表），窄终端（width<80）下
    Rich 会截断长文本（如 ``ModuleNotFoundError`` → ``ModuleNot…``）或丢弃列，
    导致断言偶发失败。固定 width=200 确保所有环境渲染一致。
    """
    original = console.rich.width
    console.rich.width = 200
    yield
    console.rich.width = original


# ---- 数据结构 ----


def test_check_status_values() -> None:
    """CheckStatus 三态枚举：OK/WARN/ERROR."""
    assert CheckStatus.OK.value == "ok"
    assert CheckStatus.WARN.value == "warn"
    assert CheckStatus.ERROR.value == "error"


def test_check_result_frozen() -> None:
    """CheckResult 是 frozen dataclass，不可变."""
    result = CheckResult(name="test", status=CheckStatus.OK, detail="v1.0")
    with pytest.raises(AttributeError):
        result.name = "other"  # type: ignore[misc]


def test_check_result_default_suggestion_empty() -> None:
    """CheckResult suggestion 默认为空字符串."""
    result = CheckResult(name="test", status=CheckStatus.OK, detail="v1.0")
    assert result.suggestion == ""


def test_doctor_report_has_error() -> None:
    """DoctorReport.has_error 检测 ERROR 级别."""
    report = DoctorReport(
        env_info=(),
        tool_checks=(
            CheckResult("a", CheckStatus.OK, "v1"),
            CheckResult("b", CheckStatus.ERROR, "缺失", "安装"),
        ),
    )
    assert report.has_error is True
    assert report.has_warn is False


def test_doctor_report_has_warn() -> None:
    """DoctorReport.has_warn 检测 WARN 级别."""
    report = DoctorReport(
        env_info=(),
        tool_checks=(
            CheckResult("a", CheckStatus.OK, "v1"),
            CheckResult("b", CheckStatus.WARN, "可选", "建议安装"),
        ),
    )
    assert report.has_error is False
    assert report.has_warn is True


def test_doctor_report_all_ok() -> None:
    """DoctorReport 全 OK 时 has_error/has_warn 均 False."""
    report = DoctorReport(
        env_info=(),
        tool_checks=(CheckResult("a", CheckStatus.OK, "v1"),),
    )
    assert report.has_error is False
    assert report.has_warn is False


# ---- _format_size ----


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (1024 * 1024 * 1024, "1.0 GiB"),
        (1024 * 1024 * 1024 * 1024, "1.0 TiB"),
    ],
)
def test_format_size(size_bytes: int, expected: str) -> None:
    """_format_size 按单位阶梯格式化字节数."""
    assert _format_size(size_bytes) == expected


# ---- _format_status ----


@pytest.mark.parametrize(
    ("status", "label", "style"),
    [
        (CheckStatus.OK, "√ OK", "green"),
        (CheckStatus.WARN, "! WARN", "yellow"),
        (CheckStatus.ERROR, "× ERROR", "red"),
    ],
)
def test_format_status(status: CheckStatus, label: str, style: str) -> None:
    """_format_status 返回中文标签 + rich 样式."""
    assert _format_status(status) == (label, style)


# ---- _check_tool_version ----


def test_check_tool_version_success() -> None:
    """工具版本查询成功返回 OK + 版本首行."""

    # 模拟成功结果
    class _FakeCompleted:
        returncode = 0
        stdout = "gcc (Ubuntu 11.4.0) 11.4.0\nCopyright (C) 2021\n"
        stderr = ""

    with patch("fspack.cli_doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.cli_doctor.subprocess.run", return_value=_FakeCompleted()
    ):
        result = _check_tool_version("gcc", ["gcc", "--version"])
    assert result.status is CheckStatus.OK
    assert "11.4.0" in result.detail


def test_check_tool_version_not_found() -> None:
    """工具未安装在 PATH 时返回 ERROR + 修复建议."""
    with patch("fspack.cli_doctor.shutil.which", return_value=None):
        result = _check_tool_version(
            "gcc",
            ["gcc", "--version"],
            error_suggestion="请安装 gcc",
        )
    assert result.status is CheckStatus.ERROR
    assert result.detail == "未找到"
    assert result.suggestion == "请安装 gcc"


def test_check_tool_version_not_found_warn_only() -> None:
    """warn_only=True 时未安装降级为 WARN."""
    with patch("fspack.cli_doctor.shutil.which", return_value=None):
        result = _check_tool_version(
            "wine",
            ["wine", "--version"],
            error_suggestion="可选工具",
            warn_only=True,
        )
    assert result.status is CheckStatus.WARN
    assert result.suggestion == "可选工具"


def test_check_tool_version_timeout() -> None:
    """工具执行超时返回 ERROR（不阻塞测试，patch 抛 TimeoutExpired）."""
    with patch("fspack.cli_doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.cli_doctor.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["gcc"], timeout=5),
    ):
        result = _check_tool_version("gcc", ["gcc", "--version"], error_suggestion="超时")
    assert result.status is CheckStatus.ERROR
    assert "执行失败" in result.detail


def test_check_tool_version_oserror() -> None:
    """工具执行 OSError 返回 ERROR."""
    with patch("fspack.cli_doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.cli_doctor.subprocess.run", side_effect=OSError("permission denied")
    ):
        result = _check_tool_version("gcc", ["gcc", "--version"], error_suggestion="权限")
    assert result.status is CheckStatus.ERROR
    assert "执行失败" in result.detail


def test_check_tool_version_nonzero_returncode() -> None:
    """工具退出码非零返回 ERROR + stderr 首行."""

    class _FakeFailed:
        returncode = 2
        stdout = ""
        stderr = "Error: invalid option\nsecond line\n"

    with patch("fspack.cli_doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.cli_doctor.subprocess.run", return_value=_FakeFailed()
    ):
        result = _check_tool_version("gcc", ["gcc", "--version"], error_suggestion="失败")
    assert result.status is CheckStatus.ERROR
    assert "退出码 2" in result.detail
    assert "invalid option" in result.detail


def test_check_tool_version_no_parse_version() -> None:
    """parse_version=False 时仅返回"可用"不取版本首行."""

    class _FakeOk:
        returncode = 0
        stdout = "wine-8.0\nsome extra\n"
        stderr = ""

    with patch("fspack.cli_doctor.shutil.which", return_value="/usr/bin/wine"), patch(
        "fspack.cli_doctor.subprocess.run", return_value=_FakeOk()
    ):
        result = _check_tool_version(
            "wine",
            ["wine", "--version"],
            parse_version=False,
        )
    assert result.status is CheckStatus.OK
    assert result.detail == "可用"


def test_check_tool_version_empty_stdout() -> None:
    """工具 stdout 为空时返回"可用"（兜底，避免 IndexError）."""

    class _EmptyStdout:
        returncode = 0
        stdout = ""
        stderr = ""

    with patch("fspack.cli_doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.cli_doctor.subprocess.run", return_value=_EmptyStdout()
    ):
        result = _check_tool_version("gcc", ["gcc", "--version"])
    assert result.status is CheckStatus.OK
    assert result.detail == "可用"


# ---- _check_pillow ----


def test_check_pillow_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pillow 已安装且版本 >= 9.4.0 返回 OK."""

    class _FakePIL:
        __version__ = "10.2.0"

    monkeypatch.setitem(__import__("sys").modules, "PIL", _FakePIL)
    result = _check_pillow()
    assert result.status is CheckStatus.OK
    assert result.detail == "10.2.0"


def test_check_pillow_version_too_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pillow < 9.4.0 返回 WARN（bitmap_format 参数缺失）."""

    class _FakePIL:
        __version__ = "9.3.0"

    monkeypatch.setitem(__import__("sys").modules, "PIL", _FakePIL)
    result = _check_pillow()
    assert result.status is CheckStatus.WARN
    assert "9.3.0" in result.detail
    assert "9.4.0" in result.suggestion


def test_check_pillow_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pillow 未安装返回 ERROR."""

    def _raise_import(name: str, *args: object, **kwargs: object) -> None:
        raise ImportError(name)

    monkeypatch.setitem(__import__("sys").modules, "PIL", None)
    monkeypatch.setattr("builtins.__import__", _raise_import)
    result = _check_pillow()
    assert result.status is CheckStatus.ERROR
    assert result.detail == "未安装"
    assert "Pillow>=9.4.0" in result.suggestion


def test_check_pillow_version_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pillow 版本号无法解析时跳过版本检查仅报告已安装."""

    class _FakePIL:
        __version__ = "unknown"

    monkeypatch.setitem(__import__("sys").modules, "PIL", _FakePIL)
    result = _check_pillow()
    assert result.status is CheckStatus.OK
    assert result.detail == "unknown"


# ---- _check_pip ----


def test_check_pip_via_pip_command() -> None:
    """pip 命令在 PATH 时直接调用 pip --version."""

    class _FakePip:
        returncode = 0
        stdout = "pip 24.0 from /usr/lib/python3/dist-packages/pip (python 3.11)\n"
        stderr = ""

    with patch("fspack.cli_doctor.shutil.which", return_value="/usr/bin/pip"), patch(
        "fspack.cli_doctor.subprocess.run", return_value=_FakePip()
    ):
        result = _check_pip()
    assert result.status is CheckStatus.OK
    assert "pip 24.0" in result.detail


def test_check_pip_via_python_module() -> None:
    """pip 命令不在 PATH 但 python -m pip 可用时回退成功."""

    class _FakePipModule:
        returncode = 0
        stdout = "pip 23.0 from /usr/lib/python3.11/site-packages/pip (python 3.11)\n"
        stderr = ""

    def _which(name: str) -> None:
        return None

    with patch("fspack.cli_doctor.shutil.which", side_effect=_which), patch(
        "fspack.cli_doctor.subprocess.run", return_value=_FakePipModule()
    ):
        result = _check_pip()
    assert result.status is CheckStatus.OK
    assert "pip 23.0" in result.detail


def test_check_pip_not_found() -> None:
    """pip 命令与 python -m pip 均不可用时返回 ERROR."""

    class _FakeFail:
        returncode = 1
        stdout = ""
        stderr = "No module named pip"

    def _which(name: str) -> None:
        return None

    with patch("fspack.cli_doctor.shutil.which", side_effect=_which), patch(
        "fspack.cli_doctor.subprocess.run", return_value=_FakeFail()
    ):
        result = _check_pip()
    assert result.status is CheckStatus.ERROR
    assert result.detail == "未找到"
    assert "ensurepip" in result.suggestion


def test_check_pip_python_module_oserror() -> None:
    """python -m pip 执行 OSError 时返回 ERROR."""

    def _which(name: str) -> None:
        return None

    with patch("fspack.cli_doctor.shutil.which", side_effect=_which), patch(
        "fspack.cli_doctor.subprocess.run", side_effect=OSError("denied")
    ):
        result = _check_pip()
    assert result.status is CheckStatus.ERROR


# ---- _check_cache_dir ----


def test_check_cache_dir_not_exists(tmp_path: Path) -> None:
    """缓存目录不存在视为 OK（首次使用尚未下载）."""
    nonexistent = tmp_path / "no-cache"
    result = _check_cache_dir(nonexistent)
    assert result.status is CheckStatus.OK
    assert "尚未创建" in result.detail


def test_check_cache_dir_with_files(tmp_path: Path) -> None:
    """缓存目录有文件时返回 OK + 大小统计."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.txt").write_bytes(b"x" * 1024)
    (cache / "sub").mkdir()
    (cache / "sub" / "b.bin").write_bytes(b"y" * 2048)

    result = _check_cache_dir(cache)
    assert result.status is CheckStatus.OK
    assert "KiB" in result.detail
    # 1024 + 2048 = 3072 B = 3.0 KiB
    assert "3.0 KiB" in result.detail


def test_check_cache_dir_scan_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """扫描缓存目录 OSError 时降级为 WARN（不影响打包）."""
    cache = tmp_path / "cache"
    cache.mkdir()

    def _raise_oserror(path: Path) -> int:
        raise OSError("permission denied")

    monkeypatch.setattr("fspack.doctor_envs._dir_size", _raise_oserror)
    result = _check_cache_dir(cache)
    assert result.status is CheckStatus.WARN
    assert "扫描缓存目录失败" in result.suggestion


# ---- _dir_size ----


def test_dir_size_empty(tmp_path: Path) -> None:
    """空目录大小为 0."""
    assert _dir_size(tmp_path) == 0


def test_dir_size_with_files(tmp_path: Path) -> None:
    """_dir_size 递归累加所有文件大小."""
    (tmp_path / "a.txt").write_bytes(b"hello")  # 5 B
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"x" * 100)  # 100 B
    assert _dir_size(tmp_path) == 105


def test_dir_size_ignores_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_dir_size 跳过 stat 失败的文件，不抛异常."""
    (tmp_path / "ok.txt").write_bytes(b"abc")

    # 模拟 stat 失败：patch Path.stat 仅对 unreadable.txt 抛 OSError
    real_stat = Path.stat

    def _mocked_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "unreadable.txt":
            raise OSError("denied")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    (tmp_path / "unreadable.txt").write_bytes(b"xyz")
    monkeypatch.setattr(Path, "stat", _mocked_stat)
    # 应只统计 ok.txt 的 3 B，unreadable.txt 跳过
    assert _dir_size(tmp_path) == 3


# ---- run_doctor ----


def test_run_doctor_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_doctor 在 Windows 平台检查 mingw/NSIS，不查 gcc/wine."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)

    # mock 所有工具检查返回 OK，避免实际依赖系统工具
    def _fake_ok(name: str, cmd: list[str], **kwargs: object) -> CheckResult:
        return CheckResult(name=name, status=CheckStatus.OK, detail="mocked")

    monkeypatch.setattr("fspack.cli_doctor._check_tool_version", _fake_ok)
    monkeypatch.setattr("fspack.cli_doctor._check_pillow", lambda: CheckResult("Pillow", CheckStatus.OK, "10.0"))
    monkeypatch.setattr("fspack.cli_doctor._check_pip", lambda: CheckResult("pip", CheckStatus.OK, "24.0"))
    monkeypatch.setattr("fspack.cli_doctor._check_uv", lambda: CheckResult("uv", CheckStatus.OK, "0.4"))
    monkeypatch.setattr("fspack.cli_doctor._check_mingw", lambda: CheckResult("mingw-w64", CheckStatus.OK, "13.2.0"))
    monkeypatch.setattr("fspack.cli_doctor._check_nsis", lambda: CheckResult("NSIS", CheckStatus.OK, "3.09"))

    report = run_doctor()

    # 环境信息应有 5 项
    assert len(report.env_info) == 5
    env_names = {r.name for r in report.env_info}
    assert env_names == {"Python", "平台", "fspack", "镜像源", "缓存目录"}

    # Windows 工具检查应含 mingw + NSIS，不含 gcc/wine
    tool_names = {r.name for r in report.tool_checks}
    assert "mingw-w64" in tool_names
    assert "NSIS" in tool_names
    assert "gcc" not in tool_names
    assert "wine" not in tool_names


def test_run_doctor_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_doctor 在 Linux 平台检查 gcc/wine，不查 mingw."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)

    monkeypatch.setattr("fspack.cli_doctor._check_pillow", lambda: CheckResult("Pillow", CheckStatus.OK, "10.0"))
    monkeypatch.setattr("fspack.cli_doctor._check_pip", lambda: CheckResult("pip", CheckStatus.OK, "24.0"))
    monkeypatch.setattr("fspack.cli_doctor._check_uv", lambda: CheckResult("uv", CheckStatus.OK, "0.4"))
    monkeypatch.setattr("fspack.cli_doctor._check_gcc", lambda: CheckResult("gcc", CheckStatus.OK, "11.4"))
    monkeypatch.setattr("fspack.cli_doctor._check_wine", lambda: CheckResult("wine", CheckStatus.WARN, "未安装"))
    monkeypatch.setattr(
        "fspack.cli_doctor._check_makensis_on_linux",
        lambda: CheckResult("NSIS (交叉打包)", CheckStatus.WARN, "未安装"),
    )

    report = run_doctor()

    tool_names = {r.name for r in report.tool_checks}
    assert "gcc" in tool_names
    assert "wine" in tool_names
    assert "NSIS (交叉打包)" in tool_names
    assert "mingw-w64" not in tool_names
    assert "NSIS" not in tool_names  # 不含 Windows 专属 NSIS

    # wine 警告应让 has_warn=True
    assert report.has_warn is True


def test_run_doctor_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_doctor 在 macOS 平台检查 clang，不查 mingw/gcc/wine/NSIS."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.MACOS)

    monkeypatch.setattr("fspack.cli_doctor._check_pillow", lambda: CheckResult("Pillow", CheckStatus.OK, "10.0"))
    monkeypatch.setattr("fspack.cli_doctor._check_pip", lambda: CheckResult("pip", CheckStatus.OK, "24.0"))
    monkeypatch.setattr("fspack.cli_doctor._check_uv", lambda: CheckResult("uv", CheckStatus.OK, "0.4"))
    monkeypatch.setattr("fspack.cli_doctor._check_clang", lambda: CheckResult("clang", CheckStatus.OK, "15.0"))

    report = run_doctor()

    tool_names = {r.name for r in report.tool_checks}
    assert "clang" in tool_names
    assert "mingw-w64" not in tool_names
    assert "gcc" not in tool_names
    assert "wine" not in tool_names
    assert "NSIS" not in tool_names
    assert "NSIS (交叉打包)" not in tool_names


def test_run_doctor_has_error_when_tool_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_doctor 必备工具缺失时 has_error=True."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)

    monkeypatch.setattr("fspack.cli_doctor._check_pillow", lambda: CheckResult("Pillow", CheckStatus.OK, "10.0"))
    monkeypatch.setattr(
        "fspack.cli_doctor._check_pip",
        lambda: CheckResult("pip", CheckStatus.ERROR, "未找到", "安装 pip"),
    )
    monkeypatch.setattr("fspack.cli_doctor._check_uv", lambda: CheckResult("uv", CheckStatus.OK, "0.4"))
    monkeypatch.setattr(
        "fspack.cli_doctor._check_gcc",
        lambda: CheckResult("gcc", CheckStatus.ERROR, "未找到", "安装 gcc"),
    )
    monkeypatch.setattr("fspack.cli_doctor._check_wine", lambda: CheckResult("wine", CheckStatus.WARN, "未安装"))
    monkeypatch.setattr(
        "fspack.cli_doctor._check_makensis_on_linux",
        lambda: CheckResult("NSIS (交叉打包)", CheckStatus.WARN, "未安装"),
    )

    report = run_doctor()
    assert report.has_error is True


# ---- print_doctor_report ----


def test_print_doctor_report_all_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """print_doctor_report 全 OK 时输出"环境就绪"."""
    report = DoctorReport(
        env_info=(CheckResult("Python", CheckStatus.OK, "3.11.9"),),
        tool_checks=(CheckResult("pip", CheckStatus.OK, "24.0"),),
    )
    print_doctor_report(report)
    output = capsys.readouterr().out
    assert "环境信息" in output
    assert "工具检查" in output
    assert "环境就绪" in output


def test_print_doctor_report_with_error(capsys: pytest.CaptureFixture[str]) -> None:
    """print_doctor_report 含 ERROR 时输出"存在错误"."""
    report = DoctorReport(
        env_info=(),
        tool_checks=(
            CheckResult("gcc", CheckStatus.ERROR, "未找到", "安装 gcc"),
            CheckResult("pip", CheckStatus.OK, "24.0"),
        ),
    )
    print_doctor_report(report)
    output = capsys.readouterr().out
    assert "1 错误" in output
    assert "可能导致打包失败" in output


def test_print_doctor_report_with_warn(capsys: pytest.CaptureFixture[str]) -> None:
    """print_doctor_report 含 WARN 时输出"存在警告"."""
    report = DoctorReport(
        env_info=(),
        tool_checks=(
            CheckResult("wine", CheckStatus.WARN, "未安装", "可选"),
            CheckResult("pip", CheckStatus.OK, "24.0"),
        ),
    )
    print_doctor_report(report)
    output = capsys.readouterr().out
    assert "1 警告" in output
    assert "不阻塞打包但建议处理" in output


def test_print_doctor_report_renders_table(capsys: pytest.CaptureFixture[str]) -> None:
    """print_doctor_report 渲染表格包含所有诊断项名称."""
    report = DoctorReport(
        env_info=(CheckResult("Python", CheckStatus.OK, "3.11.9"),),
        tool_checks=(
            CheckResult("gcc", CheckStatus.ERROR, "未找到", "安装 gcc"),
            CheckResult("Pillow", CheckStatus.OK, "10.0"),
        ),
    )
    print_doctor_report(report)
    output = capsys.readouterr().out
    assert "Python" in output
    assert "gcc" in output
    assert "Pillow" in output


# ---- CLI 集成测试 ----


def test_cli_doctor_dispatches_to_run_doctor() -> None:
    """``fsp doctor`` 命令分发到 _run_doctor → run_doctor + print_doctor_report."""
    from fspack.cli import main

    # mock run_doctor 返回全 OK 报告，避免依赖系统工具
    fake_report = DoctorReport(
        env_info=(CheckResult("Python", CheckStatus.OK, "3.11.9"),),
        tool_checks=(CheckResult("pip", CheckStatus.OK, "24.0"),),
    )
    with patch("fspack.cli_doctor.run_doctor", return_value=fake_report) as mock_run, patch(
        "fspack.cli_doctor.print_doctor_report"
    ) as mock_print:
        main(["doctor"])
    mock_run.assert_called_once()
    mock_print.assert_called_once_with(fake_report)


def test_cli_doctor_in_help() -> None:
    """``fsp --help`` 输出含 doctor 子命令."""
    from fspack.cli import build_parser

    parser = build_parser()
    help_text = parser.format_help()
    assert "doctor" in help_text
    assert "环境诊断" in help_text


# ---- TemplateBuildResult / 模板构建汇总 ----


def test_template_build_result_frozen() -> None:
    """TemplateBuildResult 是 frozen dataclass，不可变."""
    r = TemplateBuildResult(template_id="test", success=True, duration_sec=1.0)
    with pytest.raises(AttributeError):
        r.success = False  # type: ignore[misc]


def test_template_build_result_defaults() -> None:
    """TemplateBuildResult 默认值：error/dist_size/entry_count 为零值."""
    r = TemplateBuildResult(template_id="test", success=True, duration_sec=1.0)
    assert r.error == ""
    assert r.dist_size == 0
    assert r.entry_count == 0


def test_print_template_build_summary_all_success(capsys: pytest.CaptureFixture[str]) -> None:
    """全部成功时汇总输出含"全部成功"与总耗时."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.5, dist_size=1024, entry_count=1),
        TemplateBuildResult(template_id="b", success=True, duration_sec=2.5, dist_size=2048, entry_count=1),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "全部成功" in out
    assert "2/2" in out
    assert "4.0s" in out  # 1.5 + 2.5


def test_print_template_build_summary_with_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """有失败时汇总输出含"失败"（错误信息仅在 bench 模式显示）."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1),
        TemplateBuildResult(template_id="b", success=False, duration_sec=0.5, error="网络超时"),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "1 成功" in out
    assert "1 失败" in out
    # 非 bench 模式无错误信息列，错误信息不输出
    assert "网络超时" not in out


def test_print_template_build_summary_with_failure_bench(capsys: pytest.CaptureFixture[str]) -> None:
    """bench 模式有失败时输出含错误信息."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1),
        TemplateBuildResult(template_id="b", success=False, duration_sec=0.5, error="网络超时"),
    ]
    _print_template_build_summary(results, bench=True)
    out = capsys.readouterr().out
    assert "1 失败" in out
    assert "网络超时" in out


def test_print_template_build_summary_bench_mode(capsys: pytest.CaptureFixture[str]) -> None:
    """bench 模式输出性能分析（耗时排名、产物大小排名）."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1),
        TemplateBuildResult(template_id="b", success=True, duration_sec=3.0, dist_size=500, entry_count=1),
        TemplateBuildResult(template_id="c", success=False, duration_sec=0.1, error="失败"),
    ]
    _print_template_build_summary(results, bench=True)
    out = capsys.readouterr().out
    assert "性能分析" in out
    assert "耗时排名" in out
    assert "产物大小排名" in out
    assert "最慢" in out
    assert "最大" in out


def test_print_performance_analysis_single_success(capsys: pytest.CaptureFixture[str]) -> None:
    """仅 1 个成功时不输出性能分析（需 >1 才排名）."""
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=1.0, dist_size=100),
    ]
    _print_performance_analysis(results)
    out = capsys.readouterr().out
    # 单个成功不输出分析表格
    assert "耗时排名" not in out


def test_print_performance_analysis_all_failed(capsys: pytest.CaptureFixture[str]) -> None:
    """全部失败时不输出性能分析."""
    results = [
        TemplateBuildResult(template_id="a", success=False, duration_sec=0.1, error="err1"),
        TemplateBuildResult(template_id="b", success=False, duration_sec=0.2, error="err2"),
    ]
    _print_performance_analysis(results)
    out = capsys.readouterr().out
    assert "耗时排名" not in out


# ---- TemplateRunResult 数据结构 ----


def test_template_run_result_frozen() -> None:
    """TemplateRunResult 是 frozen dataclass，不可变."""
    r = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.5)
    with pytest.raises(AttributeError):
        r.success = False  # type: ignore[misc]


def test_template_run_result_defaults() -> None:
    """TemplateRunResult 默认 error 为空字符串."""
    r = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.5)
    assert r.error == ""


def test_template_build_result_run_result_default_none() -> None:
    """TemplateBuildResult.run_result 默认 None（构建失败或未运行验证）."""
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0)
    assert r.run_result is None


# ---- _find_dist_exe ----


def test_find_dist_exe_linux_native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 优先返回原生无后缀可执行文件."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    (dist / "app.exe").write_bytes(b"")
    found = _find_dist_exe(tmp_path, "app")
    assert found == dist / "app"


def test_find_dist_exe_linux_fallback_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 无原生可执行文件时回退 .exe（用 wine 运行）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    found = _find_dist_exe(tmp_path, "app")
    assert found == dist / "app.exe"


def test_find_dist_exe_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 仅查 .exe."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    found = _find_dist_exe(tmp_path, "app")
    assert found == dist / "app.exe"


def test_find_dist_exe_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dist 下无可执行文件时返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    (tmp_path / "dist").mkdir()
    assert _find_dist_exe(tmp_path, "missing") is None


def test_find_dist_exe_no_dist_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dist 目录不存在时返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    assert _find_dist_exe(tmp_path, "app") is None


# ---- _build_run_cmd ----


def test_build_run_cmd_native_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 原生可执行文件（无 .exe 后缀）直跑."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    exe = tmp_path / "app"
    assert _build_run_cmd(exe) == [str(exe)]


def test_build_run_cmd_exe_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 下 .exe 直跑，不调 wine."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    exe = tmp_path / "app.exe"
    assert _build_run_cmd(exe) == [str(exe)]


def test_build_run_cmd_exe_on_linux_with_wine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 下 .exe 用 wine 运行（wine 在 PATH）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("fspack.cli_doctor.shutil.which", lambda name: "/usr/bin/wine")
    exe = tmp_path / "app.exe"
    assert _build_run_cmd(exe) == ["/usr/bin/wine", str(exe)]


def test_build_run_cmd_exe_on_linux_no_wine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 下 .exe 但 wine 未安装时回退字符串 'wine'（_run_template 捕获 OSError）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("fspack.cli_doctor.shutil.which", lambda name: None)
    exe = tmp_path / "app.exe"
    assert _build_run_cmd(exe) == ["wine", str(exe)]


# ---- _find_debug_python / _find_wrapper / _build_debug_cmd ----


def test_find_debug_python_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux debug 模式查 dist/runtime/python/bin/python3.X."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    bin_dir = tmp_path / "dist" / "runtime" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.11").write_bytes(b"")
    (bin_dir / "python3.12").write_bytes(b"")
    py = _find_debug_python(tmp_path)
    assert py == bin_dir / "python3.11"  # sorted 取首个


def test_find_debug_python_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows debug 模式查 dist/runtime/python.exe."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    runtime = tmp_path / "dist" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"")
    py = _find_debug_python(tmp_path)
    assert py == runtime / "python.exe"


def test_find_debug_python_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runtime 目录不存在时返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    assert _find_debug_python(tmp_path) is None


def test_find_wrapper_present(tmp_path: Path) -> None:
    """存在 dist/_entry_<name>.py 时返回路径."""
    dist = tmp_path / "dist"
    dist.mkdir()
    wrapper = dist / "_entry_app.py"
    wrapper.write_text("")
    assert _find_wrapper(tmp_path, "app") == wrapper


def test_find_wrapper_absent(tmp_path: Path) -> None:
    """wrapper 不存在时返回 None."""
    assert _find_wrapper(tmp_path, "app") is None


def test_build_debug_cmd_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux debug 命令 = [python, wrapper]，env 含 PYTHONHOME + PYTHONUNBUFFERED."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    bin_dir = tmp_path / "dist" / "runtime" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    py_file = bin_dir / "python3.11"
    py_file.write_bytes(b"")
    wrapper = tmp_path / "dist" / "_entry_app.py"
    wrapper.write_text("")
    result = _build_debug_cmd(tmp_path, "app")
    assert result is not None
    cmd, env = result
    assert cmd == [str(py_file), str(wrapper)]
    assert env["PYTHONHOME"] == str(tmp_path / "dist" / "runtime" / "python")
    assert env["PYTHONUNBUFFERED"] == "1"


def test_build_debug_cmd_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows debug 命令不含 PYTHONHOME（embed python 用 _pth 定位标准库）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    runtime = tmp_path / "dist" / "runtime"
    runtime.mkdir(parents=True)
    py_file = runtime / "python.exe"
    py_file.write_bytes(b"")
    wrapper = tmp_path / "dist" / "_entry_app.py"
    wrapper.write_text("")
    result = _build_debug_cmd(tmp_path, "app")
    assert result is not None
    cmd, env = result
    assert cmd == [str(py_file), str(wrapper)]
    assert "PYTHONHOME" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


def test_build_debug_cmd_wrapper_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wrapper 缺失时返回 None（调用方回退直跑 exe）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    bin_dir = tmp_path / "dist" / "runtime" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3.11").write_bytes(b"")
    # 不创建 _entry_app.py
    assert _build_debug_cmd(tmp_path, "app") is None


def test_build_debug_cmd_python_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """embed python 缺失时返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "_entry_app.py").write_text("")
    # runtime 目录不存在
    assert _build_debug_cmd(tmp_path, "app") is None


# ---- _run_template ----


class _FakeProc:
    """模拟 subprocess.Popen 返回的进程对象."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.terminated = False
        self.killed = False
        self.communicate_calls = 0
        self._raise_timeout = False

    def communicate(self, timeout: float | None = None):  # type: ignore[no-untyped-def]
        self.communicate_calls += 1
        if timeout is not None and self._raise_timeout:
            raise subprocess.TimeoutExpired(cmd=[], timeout=timeout)
        return (self._stdout, self._stderr)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_run_template_success_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程退出码 0 → 运行成功（CLI 正常执行完成）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(returncode=0, stdout="hello, world\n", stderr="")
    monkeypatch.setattr("fspack.cli_doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is True
    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.error == ""


def test_run_template_failure_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程退出码非 0 → 运行失败，捕获 stderr 首行."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(
        returncode=1,
        stdout="",
        stderr="ModuleNotFoundError: No module named 'modules'\nTraceback follows",
    )
    monkeypatch.setattr("fspack.cli_doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is False
    assert result.timed_out is False
    assert result.exit_code == 1
    assert "ModuleNotFoundError" in result.error


def test_run_template_failure_empty_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程退出码非 0 且 stderr 为空时 error 显示退出码."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(returncode=2, stdout="", stderr="")
    monkeypatch.setattr("fspack.cli_doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is False
    assert result.exit_code == 2
    assert "退出码 2" in result.error


def test_run_template_timeout_treated_as_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时未退出视为 GUI/Web 事件循环正常，主动 terminate 后返回成功."""

    class _TimeoutProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self._raise_timeout = True

    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _TimeoutProc()
    monkeypatch.setattr("fspack.cli_doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=0.5)
    assert result.success is True
    assert result.timed_out is True
    assert result.exit_code is None
    assert proc.terminated is True


def test_run_template_timeout_terminate_then_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时后 terminate 仍超时则升级为 kill."""

    class _StubbornProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self._raise_timeout = True

    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _StubbornProc()
    monkeypatch.setattr("fspack.cli_doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=0.3)
    assert result.success is True
    assert result.timed_out is True
    # terminate 后再次 communicate 超时 → 调 kill
    assert proc.terminated is True
    assert proc.killed is True


def test_run_template_oserror_startup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Popen 抛 OSError（如 wine 未安装）→ 运行失败，error 含启动失败."""

    def _raise_popen(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("[Errno 2] No such file or directory: 'wine'")

    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("fspack.cli_doctor.subprocess.Popen", _raise_popen)
    result = _run_template(["wine", str(tmp_path / "app.exe")], timeout=1.0)
    assert result.success is False
    assert result.timed_out is False
    assert result.exit_code is None
    assert "启动失败" in result.error
    assert "wine" in result.error


def test_run_template_passes_env_to_popen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式传入 env 时，env 透传给 subprocess.Popen（PYTHONHOME 等）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    captured: dict[str, object] = {}
    proc = _FakeProc(returncode=0)

    def _capture_popen(*args: object, **kwargs: object) -> _FakeProc:
        captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr("fspack.cli_doctor.subprocess.Popen", _capture_popen)
    env = {"PYTHONHOME": "/tmp/python", "PYTHONUNBUFFERED": "1"}
    _run_template([str(tmp_path / "app")], env, timeout=1.0)  # type: ignore[arg-type]
    assert captured["env"] == env


# ---- _format_run_status ----


def test_format_run_status_build_failed() -> None:
    """构建失败时运行状态显示 '-'（不运行验证）."""
    r = TemplateBuildResult(template_id="t", success=False, duration_sec=0.1, error="构建错误")
    status, err = _format_run_status(r)
    assert status == "-"
    assert err == ""


def test_format_run_status_no_run_result() -> None:
    """构建成功但未运行验证（多入口或未找到 exe）显示 '跳过'."""
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0)
    status, err = _format_run_status(r)
    assert "跳过" in status
    assert err == ""


def test_format_run_status_success_no_timeout() -> None:
    """运行成功且未超时（CLI 正常退出码 0）显示 '√ 成功'."""
    rr = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.2)
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0, run_result=rr)
    status, err = _format_run_status(r)
    assert "成功" in status
    assert err == ""


def test_format_run_status_success_timeout() -> None:
    """运行成功但超时（GUI/Web 事件循环）显示 '√ 超时'."""
    rr = TemplateRunResult(success=True, timed_out=True, exit_code=None, duration_sec=5.0)
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0, run_result=rr)
    status, _ = _format_run_status(r)
    assert "超时" in status


def test_format_run_status_failure() -> None:
    """运行失败显示 '× 失败' 并返回 stderr 错误."""
    rr = TemplateRunResult(success=False, timed_out=False, exit_code=1, duration_sec=0.1, error="ModuleNotFoundError")
    r = TemplateBuildResult(template_id="t", success=True, duration_sec=1.0, run_result=rr)
    status, err = _format_run_status(r)
    assert "失败" in status
    assert err == "ModuleNotFoundError"


# ---- _print_template_build_summary 运行列 ----


def test_print_summary_run_column_success(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总表含运行列：构建+运行均成功."""
    rr = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.2)
    results = [
        TemplateBuildResult(
            template_id="a", success=True, duration_sec=1.5, dist_size=1024, entry_count=1, run_result=rr
        ),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "运行" in out
    assert "全部成功" in out
    assert "运行验证" in out
    assert "全部通过" in out


def test_print_summary_run_column_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总表含运行列：构建成功但运行失败，输出运行失败统计."""
    rr = TemplateRunResult(success=False, timed_out=False, exit_code=1, duration_sec=0.1, error="ImportError")
    results = [
        TemplateBuildResult(
            template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1, run_result=rr
        ),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "运行验证" in out
    assert "1 失败" in out
    assert "ImportError" not in out  # 非 bench 模式不显示错误信息列


def test_print_summary_run_column_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总表含运行列：构建成功但跳过运行验证（多入口）。"""
    results = [
        TemplateBuildResult(template_id="multi", success=True, duration_sec=1.0, dist_size=100, entry_count=2),
    ]
    _print_template_build_summary(results, bench=False)
    out = capsys.readouterr().out
    assert "跳过" in out
    assert "运行验证" in out


def test_print_summary_bench_shows_run_error(capsys: pytest.CaptureFixture[str]) -> None:
    """bench 模式下显示运行错误信息列."""
    rr = TemplateRunResult(
        success=False, timed_out=False, exit_code=1, duration_sec=0.1, error="ModuleNotFoundError: x"
    )
    results = [
        TemplateBuildResult(
            template_id="a", success=True, duration_sec=1.0, dist_size=100, entry_count=1, run_result=rr
        ),
        TemplateBuildResult(
            template_id="b",
            success=True,
            duration_sec=2.0,
            dist_size=200,
            entry_count=1,
            run_result=TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.1),
        ),
    ]
    _print_template_build_summary(results, bench=True)
    out = capsys.readouterr().out
    assert "ModuleNotFoundError" in out
    assert "运行验证" in out
    assert "1 失败" in out


# ---- 基准历史持久化与横向对比 ----


def test_bench_history_group_dir_format(tmp_path: Path) -> None:
    """分组目录格式：{base}/{System}-CPython-{major}.{minor}-{bits}bit-doctor."""
    result = _bench_history_group_dir(tmp_path)
    # 直接在 base 下，无 doctor/ 子目录层级
    assert result.parent == tmp_path
    # 目录名含 CPython、bit 和 -doctor 后缀
    group_name = result.name
    assert "CPython" in group_name
    assert "bit" in group_name
    assert group_name.endswith("-doctor")


def test_serialize_deserialize_roundtrip() -> None:
    """序列化/反序列化往返测试：数据完整保留（含启动耗时）."""
    rr = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.5)
    results = [
        TemplateBuildResult(
            template_id="tpl_a",
            success=True,
            duration_sec=12.5,
            dist_size=102400,
            entry_count=1,
            run_result=rr,
        ),
        TemplateBuildResult(
            template_id="tpl_b",
            success=False,
            duration_sec=0.1,
            error="构建失败",
        ),
    ]
    data = _serialize_bench_results(results)
    # 验证 run_duration_sec 被序列化
    assert data["results"][0]["run_duration_sec"] == 0.5
    assert data["results"][1]["run_duration_sec"] is None

    restored, ts = _deserialize_bench_results(data)

    assert isinstance(ts, str)
    assert len(restored) == 2
    assert restored[0].template_id == "tpl_a"
    assert restored[0].success is True
    assert restored[0].duration_sec == 12.5
    assert restored[0].dist_size == 102400
    assert restored[0].entry_count == 1
    assert restored[0].run_result is not None
    assert restored[0].run_result.success is True
    assert restored[0].run_result.exit_code == 0
    assert restored[0].run_result.duration_sec == 0.5  # 启动耗时保留

    assert restored[1].template_id == "tpl_b"
    assert restored[1].success is False
    assert restored[1].error == "构建失败"
    assert restored[1].run_result is None


def test_serialize_includes_machine_info() -> None:
    """序列化结果含匿名化 machine 信息（node_id/system/python_version/cpu）."""
    results = [TemplateBuildResult(template_id="x", success=True, duration_sec=1.0, dist_size=100)]
    data = _serialize_bench_results(results)
    assert "machine" in data
    machine = data["machine"]
    # node_id 是匿名编码（8 位 hex），不含真实机器名
    assert "node_id" in machine
    assert len(machine["node_id"]) == 8
    assert "node" not in machine  # 不含真实机器名（隐私）
    assert "system" in machine
    assert "python_version" in machine
    # cpu 性能配置
    assert "cpu" in machine
    assert "brand" in machine["cpu"]
    assert "count" in machine["cpu"]
    assert "arch" in machine["cpu"]
    assert "bits" in machine["cpu"]


def test_machine_id_is_deterministic_and_anonymous() -> None:
    """_machine_id 返回 8 位 hex 编码，同一进程内确定性，不含真实机器名."""
    id1 = _machine_id()
    id2 = _machine_id()
    assert id1 == id2  # 确定性
    assert len(id1) == 8  # 8 位
    # 仅含 hex 字符
    import re

    assert re.match(r"^[0-9a-f]{8}$", id1)
    # 不含真实机器名
    import platform

    assert platform.node() not in id1


def test_collect_machine_info_no_privacy() -> None:
    """_collect_machine_info 不含真实机器名（隐私），含 CPU 性能配置."""
    info = _collect_machine_info()
    import platform

    # 不含真实机器名
    assert "node" not in info
    if platform.node():
        assert platform.node() not in str(info)
    # 含匿名编码
    assert "node_id" in info
    # 含 CPU 性能配置
    assert info["cpu"]["count"] > 0
    assert info["cpu"]["bits"] in (32, 64)


def test_save_bench_history_creates_file(tmp_path: Path) -> None:
    """保存基准结果创建 JSON 文件到分组目录."""
    results = [TemplateBuildResult(template_id="x", success=True, duration_sec=1.0, dist_size=100)]
    group_dir = tmp_path / "doctor" / "Test-CPython-3.11-64bit"
    path = _save_bench_history(results, group_dir)

    assert path.is_file()
    assert path.suffix == ".json"
    assert path.parent == group_dir
    # 文件名含时间戳（YYYYMMDDTHHMMSS 格式）
    import re

    assert re.match(r"\d{8}T\d{6}\.json", path.name)


def test_save_bench_history_creates_dir(tmp_path: Path) -> None:
    """保存时目录不存在则自动创建."""
    group_dir = tmp_path / "deeply" / "nested" / "group"
    results = [TemplateBuildResult(template_id="x", success=True, duration_sec=1.0, dist_size=100)]
    path = _save_bench_history(results, group_dir)
    assert path.is_file()
    assert group_dir.is_dir()


def test_load_previous_bench_history_no_dir_returns_none(tmp_path: Path) -> None:
    """目录不存在时返回 None."""
    assert _load_previous_bench_history(tmp_path / "nonexistent") is None


def test_load_previous_bench_history_empty_dir_returns_none(tmp_path: Path) -> None:
    """空目录返回 None."""
    assert _load_previous_bench_history(tmp_path) is None


def test_load_previous_bench_history_returns_latest(tmp_path: Path) -> None:
    """加载最近一次历史（按文件名降序第一个有效文件）."""
    results1 = [TemplateBuildResult(template_id="old", success=True, duration_sec=1.0, dist_size=100)]
    results2 = [TemplateBuildResult(template_id="new", success=True, duration_sec=2.0, dist_size=200)]

    # 手动创建两个不同时间戳的文件
    (tmp_path / "20260101T000000.json").write_text(
        json.dumps(_serialize_bench_results(results1), ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "20260201T000000.json").write_text(
        json.dumps(_serialize_bench_results(results2), ensure_ascii=False), encoding="utf-8"
    )

    previous = _load_previous_bench_history(tmp_path)
    assert previous is not None
    prev_results, _ts = previous
    assert prev_results[0].template_id == "new"  # 返回最新的


def test_load_previous_bench_history_exclude_current(tmp_path: Path) -> None:
    """exclude 参数跳过指定文件，返回上一个历史."""
    results1 = [TemplateBuildResult(template_id="old", success=True, duration_sec=1.0, dist_size=100)]
    results2 = [TemplateBuildResult(template_id="new", success=True, duration_sec=2.0, dist_size=200)]

    old_path = tmp_path / "20260101T000000.json"
    new_path = tmp_path / "20260201T000000.json"
    old_path.write_text(json.dumps(_serialize_bench_results(results1), ensure_ascii=False), encoding="utf-8")
    new_path.write_text(json.dumps(_serialize_bench_results(results2), ensure_ascii=False), encoding="utf-8")

    # 排除 new_path，应返回 old
    previous = _load_previous_bench_history(tmp_path, exclude=new_path)
    assert previous is not None
    prev_results, _ = previous
    assert prev_results[0].template_id == "old"


def test_load_previous_bench_history_skips_corrupt(tmp_path: Path) -> None:
    """损坏的 JSON 文件被跳过，返回下一个有效文件."""
    results = [TemplateBuildResult(template_id="ok", success=True, duration_sec=1.0, dist_size=100)]
    (tmp_path / "20260101T000000.json").write_text("not valid json", encoding="utf-8")
    (tmp_path / "20260201T000000.json").write_text(
        json.dumps(_serialize_bench_results(results), ensure_ascii=False), encoding="utf-8"
    )

    previous = _load_previous_bench_history(tmp_path)
    assert previous is not None
    prev_results, _ = previous
    assert prev_results[0].template_id == "ok"


def test_format_bench_delta_slower() -> None:
    """变慢（current > previous）返回红色标记."""
    result = _format_bench_delta(12.0, 10.0, lambda v: f"{v:.1f}s")
    assert "[red]" in result
    assert "+2.0s" in result
    assert "+20.0%" in result


def test_format_bench_delta_faster() -> None:
    """变快（current < previous）返回绿色标记."""
    result = _format_bench_delta(8.0, 10.0, lambda v: f"{v:.1f}s")
    assert "[green]" in result
    assert "-2.0s" in result
    assert "-20.0%" in result
    assert "+20.0%" not in result  # 确保不是 +- 混合


def test_format_bench_delta_equal() -> None:
    """持平（delta < 0.01）返回灰色 =."""
    result = _format_bench_delta(10.001, 10.0, lambda v: f"{v:.1f}s")
    assert result == "[dim]=[/dim]"


def test_format_bench_delta_no_history() -> None:
    """previous <= 0 返回灰色 --."""
    result = _format_bench_delta(10.0, 0.0, lambda v: f"{v:.1f}s")
    assert result == "[dim]--[/dim]"


def test_format_bench_delta_size_uses_format_size() -> None:
    """大小变化用 _format_size 格式化."""
    result = _format_bench_delta(2048, 1024, lambda v: _format_size(int(v)))
    assert "[red]" in result
    assert "1.0 KiB" in result  # delta=1024 → 1.0 KiB（_format_size 用 1024 进制）
    assert "+100.0%" in result


def test_print_bench_comparison_renders_table(capsys: pytest.CaptureFixture[str]) -> None:
    """对比表渲染含模板名、构建变化、启动变化、大小变化."""
    cur_rr_a = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.8)
    cur_rr_b = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.3)
    prev_rr_a = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.5)
    prev_rr_b = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.5)
    current = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=12.0, dist_size=102400, run_result=cur_rr_a),
        TemplateBuildResult(template_id="b", success=True, duration_sec=8.0, dist_size=51200, run_result=cur_rr_b),
    ]
    previous = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=10.0, dist_size=102400, run_result=prev_rr_a),
        TemplateBuildResult(template_id="b", success=True, duration_sec=10.0, dist_size=51200, run_result=prev_rr_b),
    ]
    _print_bench_comparison(current, previous, "2026-07-28T14:30")
    out = capsys.readouterr().out
    assert "性能对比" in out
    assert "横向对比" in out
    assert "a" in out
    assert "b" in out
    # 构建耗时变化：a 变慢（12 vs 10），b 变快（8 vs 10）
    assert "+2.0s" in out
    assert "-2.0s" in out
    # 启动耗时变化：a 变慢（0.8 vs 0.5），b 变快（0.3 vs 0.5）
    assert "+0.30s" in out
    assert "-0.20s" in out
    # 列标题
    assert "本次启动" in out
    assert "上次启动" in out
    assert "启动变化" in out


def test_print_bench_comparison_skips_failed_current(capsys: pytest.CaptureFixture[str]) -> None:
    """对比表跳过构建失败的当前模板."""
    current = [
        TemplateBuildResult(template_id="ok", success=True, duration_sec=10.0, dist_size=100),
        TemplateBuildResult(template_id="fail", success=False, duration_sec=0.1, error="err"),
    ]
    previous: list[TemplateBuildResult] = []
    _print_bench_comparison(current, previous, "2026-07-28T14:30")
    out = capsys.readouterr().out
    assert "ok" in out
    assert "fail" not in out


def test_print_bench_comparison_no_match_shows_dash(capsys: pytest.CaptureFixture[str]) -> None:
    """当前模板在上次历史中不存在时显示 --."""
    current = [TemplateBuildResult(template_id="new_tpl", success=True, duration_sec=10.0, dist_size=100)]
    previous = [TemplateBuildResult(template_id="old_tpl", success=True, duration_sec=10.0, dist_size=100)]
    _print_bench_comparison(current, previous, "2026-07-28T14:30")
    out = capsys.readouterr().out
    assert "new_tpl" in out
    assert "--" in out  # 无历史对比


def test_print_bench_comparison_empty_current_no_output(capsys: pytest.CaptureFixture[str]) -> None:
    """当前结果全部失败时不输出对比表."""
    current = [TemplateBuildResult(template_id="x", success=False, duration_sec=0.1, error="err")]
    previous = [TemplateBuildResult(template_id="x", success=True, duration_sec=10.0, dist_size=100)]
    _print_bench_comparison(current, previous, "2026-07-28T14:30")
    out = capsys.readouterr().out
    assert "性能对比" not in out


def test_save_and_compare_bench_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """首次运行无历史，保存结果并提示首次基准."""
    monkeypatch.chdir(tmp_path)
    results = [TemplateBuildResult(template_id="x", success=True, duration_sec=10.0, dist_size=100)]

    _save_and_compare_bench(results)
    out = capsys.readouterr().out

    assert "基准已保存" in out
    assert "首次基准" in out
    # 确认文件已保存
    group_dir = _bench_history_group_dir(tmp_path / ".benchmarks")
    assert group_dir.is_dir()
    assert len(list(group_dir.glob("*.json"))) == 1


def test_save_and_compare_bench_with_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """有历史时保存当前结果并打印对比表."""
    monkeypatch.chdir(tmp_path)
    group_dir = _bench_history_group_dir(tmp_path / ".benchmarks")
    group_dir.mkdir(parents=True)

    # 保存历史基准
    prev_results = [TemplateBuildResult(template_id="x", success=True, duration_sec=10.0, dist_size=100)]
    (group_dir / "20260101T000000.json").write_text(
        json.dumps(_serialize_bench_results(prev_results), ensure_ascii=False), encoding="utf-8"
    )

    # 当前结果（耗时变慢）
    current_results = [TemplateBuildResult(template_id="x", success=True, duration_sec=12.0, dist_size=100)]
    _save_and_compare_bench(current_results)
    out = capsys.readouterr().out

    assert "基准已保存" in out
    assert "性能对比" in out
    assert "+2.0s" in out  # 12 vs 10，变慢
    # 确认两个文件存在（历史 + 当前）
    assert len(list(group_dir.glob("*.json"))) == 2


def test_print_summary_bench_shows_startup_column(capsys: pytest.CaptureFixture[str]) -> None:
    """bench 模式汇总表含'启动耗时'列，显示应用调用响应速度."""
    rr = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.42)
    results = [
        TemplateBuildResult(
            template_id="tpl_a",
            success=True,
            duration_sec=10.0,
            dist_size=102400,
            entry_count=1,
            run_result=rr,
        ),
        TemplateBuildResult(template_id="tpl_b", success=True, duration_sec=5.0, dist_size=51200),
    ]
    _print_template_build_summary(results, bench=True)
    out = capsys.readouterr().out
    assert "启动耗时" in out
    assert "0.42s" in out  # tpl_a 的启动耗时
    assert "-" in out  # tpl_b 无 run_result，显示 -


def test_print_performance_analysis_includes_startup_ranking(capsys: pytest.CaptureFixture[str]) -> None:
    """性能分析含'启动耗时排名'表，按应用调用响应速度降序."""
    rr_a = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.8)
    rr_b = TemplateRunResult(success=True, timed_out=False, exit_code=0, duration_sec=0.3)
    results = [
        TemplateBuildResult(template_id="a", success=True, duration_sec=10.0, dist_size=100, run_result=rr_a),
        TemplateBuildResult(template_id="b", success=True, duration_sec=8.0, dist_size=200, run_result=rr_b),
    ]
    _print_performance_analysis(results)
    out = capsys.readouterr().out
    assert "启动耗时排名" in out
    assert "应用调用响应速度" in out
    # a (0.8s) 最慢排第一，b (0.3s) 排第二
    assert "0.80s" in out
    assert "0.30s" in out
