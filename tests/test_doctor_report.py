"""doctor 数据结构与报告测试：CheckResult/DoctorReport 模型、run_doctor 聚合、报告渲染与 CLI 派发."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from fspack.console import console
from fspack.doctor import (
    CheckResult,
    CheckStatus,
    DoctorReport,
    _format_status,
    print_doctor_report,
    run_doctor,
)
from fspack.packaging.profile_log import ProfileOptions
from fspack.platform import Platform


@pytest.fixture(autouse=True)
def _fixed_rich_width(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """固定 Rich Console 宽度，避免窄终端环境下 word wrap 导致断言失败.

    多个测试渲染含 8 列的 Rich Table（bench 模式汇总表），窄终端（width<80）下
    Rich 会截断长文本（如 ``ModuleNotFoundError`` → ``ModuleNot…``）或丢弃列，
    导致断言偶发失败。固定 width=200 确保所有环境渲染一致。

    必须直接 patch ``_width`` 而非走 ``width`` 属性往返：rich 的 ``width`` getter
    在 ``_width``/``_height`` 均已设置时（shell 导出 ``COLUMNS``/``LINES`` 环境变量
    即如此）返回 ``_width - legacy_windows``，而 setter 存原始值——往返一次宽度净
    减 1（legacy Windows 控制台）。本文件数百个测试逐个缩水，跑完后宽度变负数，
    rich 会把后续所有文本裁剪为空，殃及后续文件 27 个 capsys 断言。
    ``monkeypatch`` 记录的是原始 ``_width`` 值，恢复无损。
    """
    monkeypatch.setattr(console.rich, "_width", 200)
    yield


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


def test_run_doctor_cache_check_renders_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_doctor_cache_check 调 _check_cache_integrity 并渲染表格，返回 CheckResult."""
    from fspack.doctor import run_doctor_cache_check

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")  # 创建 wheel 避免 stale（iter-139）

    # patch wheel_cache_dir 返回测试目录（run_doctor_cache_check 内部局部 import
    # 查 fspack.config.cache 模块属性，monkeypatch 替换模块属性生效）
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    result = run_doctor_cache_check()
    assert result.status is CheckStatus.OK
    assert "扫描 1 个 deps 缓存" in result.detail


def test_cli_doctor_check_cache_flag_dispatches() -> None:
    """``fsp doctor --check-cache`` 触发 run_doctor_cache_check 调用（iter-128）."""
    from fspack.cli import main

    fake_report = DoctorReport(
        env_info=(CheckResult("Python", CheckStatus.OK, "3.11.9"),),
        tool_checks=(CheckResult("pip", CheckStatus.OK, "24.0"),),
    )
    with patch("fspack.doctor.run_doctor", return_value=fake_report), patch("fspack.doctor.print_doctor_report"), patch(
        "fspack.doctor.run_doctor_cache_check"
    ) as mock_check:
        main(["doctor", "--check-cache"])
    mock_check.assert_called_once()


# ---- CacheHealthReport 通用字段 ----


def test_cache_health_report_issues_count_wheels() -> None:
    """wheels 类型 issues_count 合计 corrupt/stale deps + orphan wheels."""
    from fspack.doctor.models import CacheHealthReport

    report = CacheHealthReport(
        cache_dir=Path("/tmp"),
        cache_type="wheels",
        corrupt_deps_files=("a.json",),
        stale_deps_files=("b.json",),
        orphan_wheels=("c.whl", "d.whl"),
    )
    assert report.issues_count == 4
    assert report.has_issues


def test_cache_health_report_issues_count_generic() -> None:
    """非 wheels 类型 issues_count 合计 corrupt/stale/orphan files."""
    from fspack.doctor.models import CacheHealthReport

    report = CacheHealthReport(
        cache_dir=Path("/tmp"),
        cache_type="embed",
        corrupt_files=("a.zip",),
        stale_files=("b.zip", "c.zip"),
    )
    assert report.issues_count == 3
    assert report.has_issues


def test_cache_health_report_no_issues() -> None:
    """所有字段为空时 has_issues=False, issues_count=0."""
    from fspack.doctor.models import CacheHealthReport

    report = CacheHealthReport(cache_dir=Path("/tmp"), cache_type="embed")
    assert not report.has_issues
    assert report.issues_count == 0


# ---- run_doctor ----


def _mock_cache_content_fns(monkeypatch: pytest.MonkeyPatch) -> None:
    """整体替换缓存内容盘点分发入口，避免 run_doctor 测试真实扫描缓存目录."""
    monkeypatch.setattr(
        "fspack.doctor.runner._cache_content_fns",
        lambda platform: [lambda: CheckResult("缓存内容", CheckStatus.OK, "mocked")],
    )


def test_run_doctor_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_doctor 在 Windows 平台检查 mingw/NSIS，不查 gcc/wine."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)

    # mock 所有工具检查返回 OK，避免实际依赖系统工具
    def _fake_ok(name: str, cmd: list[str], **kwargs: object) -> CheckResult:
        return CheckResult(name=name, status=CheckStatus.OK, detail="mocked")

    monkeypatch.setattr("fspack.doctor.tools._check_tool_version", _fake_ok)
    monkeypatch.setattr("fspack.doctor.runner._check_pillow", lambda: CheckResult("Pillow", CheckStatus.OK, "10.0"))
    monkeypatch.setattr("fspack.doctor.runner._check_pip", lambda: CheckResult("pip", CheckStatus.OK, "24.0"))
    monkeypatch.setattr("fspack.doctor.runner._check_uv", lambda: CheckResult("uv", CheckStatus.OK, "0.4"))
    monkeypatch.setattr("fspack.doctor.runner._check_mingw", lambda: CheckResult("mingw-w64", CheckStatus.OK, "13.2.0"))
    monkeypatch.setattr("fspack.doctor.runner._check_nsis", lambda: CheckResult("NSIS", CheckStatus.OK, "3.09"))
    monkeypatch.setattr(
        "fspack.doctor.runner._check_win7_compat", lambda: CheckResult("Win7 兼容", CheckStatus.OK, "mocked")
    )
    # 缓存内容盘点经 cache_contents 模块全局名字解析分发，逐项 mock 避免
    # 真实扫描缓存目录与 MSVC 探测
    _cache_items = {
        "_check_nuitka_contents": "nuitka 缓存",
        "_check_embed_contents": "embed 缓存",
        "_check_standalone_windows_contents": "standalone-windows 缓存",
        "_check_tkinter_contents": "tkinter 缓存",
        "_check_winlibs_contents": "winlibs 工具链",
    }
    for fn_name, label in _cache_items.items():
        monkeypatch.setattr(
            f"fspack.doctor.cache_contents.{fn_name}", lambda label=label: CheckResult(label, CheckStatus.OK, "mocked")
        )

    report = run_doctor()

    # 环境信息应有 11 项（Win7 兼容自检 + 5 项压缩包缓存内容盘点）
    assert len(report.env_info) == 11
    env_names = {r.name for r in report.env_info}
    assert env_names == {
        "Python",
        "平台",
        "fspack",
        "镜像源",
        "缓存目录",
        "Win7 兼容",
        "nuitka 缓存",
        "embed 缓存",
        "standalone-windows 缓存",
        "tkinter 缓存",
        "winlibs 工具链",
    }

    # Windows 工具检查应含 mingw + NSIS，不含 gcc/wine
    tool_names = {r.name for r in report.tool_checks}
    assert "mingw-w64" in tool_names
    assert "NSIS" in tool_names
    assert "gcc" not in tool_names
    assert "wine" not in tool_names


def test_run_doctor_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_doctor 在 Linux 平台检查 gcc/wine，不查 mingw."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)

    monkeypatch.setattr("fspack.doctor.runner._check_pillow", lambda: CheckResult("Pillow", CheckStatus.OK, "10.0"))
    monkeypatch.setattr("fspack.doctor.runner._check_pip", lambda: CheckResult("pip", CheckStatus.OK, "24.0"))
    monkeypatch.setattr("fspack.doctor.runner._check_uv", lambda: CheckResult("uv", CheckStatus.OK, "0.4"))
    monkeypatch.setattr("fspack.doctor.runner._check_gcc", lambda: CheckResult("gcc", CheckStatus.OK, "11.4"))
    monkeypatch.setattr("fspack.doctor.runner._check_wine", lambda: CheckResult("wine", CheckStatus.WARN, "未安装"))
    monkeypatch.setattr(
        "fspack.doctor.runner._check_makensis_on_linux",
        lambda: CheckResult("NSIS (交叉打包)", CheckStatus.WARN, "未安装"),
    )
    _mock_cache_content_fns(monkeypatch)

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

    monkeypatch.setattr("fspack.doctor.runner._check_pillow", lambda: CheckResult("Pillow", CheckStatus.OK, "10.0"))
    monkeypatch.setattr("fspack.doctor.runner._check_pip", lambda: CheckResult("pip", CheckStatus.OK, "24.0"))
    monkeypatch.setattr("fspack.doctor.runner._check_uv", lambda: CheckResult("uv", CheckStatus.OK, "0.4"))
    monkeypatch.setattr("fspack.doctor.runner._check_clang", lambda: CheckResult("clang", CheckStatus.OK, "15.0"))
    _mock_cache_content_fns(monkeypatch)

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

    _mock_cache_content_fns(monkeypatch)
    monkeypatch.setattr("fspack.doctor.runner._check_pillow", lambda: CheckResult("Pillow", CheckStatus.OK, "10.0"))
    monkeypatch.setattr(
        "fspack.doctor.runner._check_pip",
        lambda: CheckResult("pip", CheckStatus.ERROR, "未找到", "安装 pip"),
    )
    monkeypatch.setattr("fspack.doctor.runner._check_uv", lambda: CheckResult("uv", CheckStatus.OK, "0.4"))
    monkeypatch.setattr(
        "fspack.doctor.runner._check_gcc",
        lambda: CheckResult("gcc", CheckStatus.ERROR, "未找到", "安装 gcc"),
    )
    monkeypatch.setattr("fspack.doctor.runner._check_wine", lambda: CheckResult("wine", CheckStatus.WARN, "未安装"))
    monkeypatch.setattr(
        "fspack.doctor.runner._check_makensis_on_linux",
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
    with patch("fspack.doctor.run_doctor", return_value=fake_report) as mock_run, patch(
        "fspack.doctor.print_doctor_report"
    ) as mock_print:
        main(["doctor"])
    mock_run.assert_called_once()
    mock_print.assert_called_once_with(fake_report)


def test_cli_doctor_bench_removed() -> None:
    """``fsp doctor --bench`` 已移除（并入 --test -P）：未知参数报错退出."""
    from fspack.cli import main

    with patch("fspack.doctor.run_doctor") as mock_run, pytest.raises(SystemExit) as exc_info:
        main(["doctor", "--test", "--bench"])
    # argparse 对未知 --bench 报错（退出码 2），不进入诊断
    mock_run.assert_not_called()
    assert exc_info.value.code == 2


def test_cli_doctor_in_help() -> None:
    """``fsp --help`` 输出含 doctor 子命令."""
    from fspack.cli import build_parser

    parser = build_parser()
    help_text = parser.format_help()
    assert "doctor" in help_text
    assert "环境诊断" in help_text


def test_cli_doctor_profile_requires_test() -> None:
    """``fsp d -P`` 未指定 --test 时报错（校验前置，不跑诊断）。"""
    from fspack.cli import main

    with patch("fspack.doctor.run_doctor") as mock_run, pytest.raises(SystemExit) as exc_info:
        main(["doctor", "--profile"])
    # 校验前置：诊断未执行；FspackError 转退出码 2
    mock_run.assert_not_called()
    assert exc_info.value.code == 2


def test_cli_doctor_profile_compare_requires_profile() -> None:
    """``fsp d --test -PC last`` 未指定 -P 时报错。"""
    from fspack.cli import main

    with patch("fspack.doctor.run_doctor") as mock_run, pytest.raises(SystemExit) as exc_info:
        main(["doctor", "--test", "--profile-compare", "last"])
    mock_run.assert_not_called()
    assert exc_info.value.code == 2


def test_cli_doctor_test_passes_profile_options() -> None:
    """``fsp d --test -P -PC`` 构造 ProfileOptions 传给 run_doctor_test。"""
    from fspack.cli import main

    fake_report = DoctorReport(
        env_info=(CheckResult("Python", CheckStatus.OK, "3.11.9"),),
        tool_checks=(CheckResult("pip", CheckStatus.OK, "24.0"),),
    )
    with patch("fspack.doctor.run_doctor", return_value=fake_report), patch("fspack.doctor.print_doctor_report"), patch(
        "fspack.doctor.run_doctor_test"
    ) as mock_test:
        main(["doctor", "--test", "--profile", "--profile-compare", "last"])
    mock_test.assert_called_once()
    opts = mock_test.call_args[0][0]
    assert isinstance(opts, ProfileOptions)
    assert opts.enabled is True
    assert opts.compare == "last"
