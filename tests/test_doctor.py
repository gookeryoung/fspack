"""``fsp doctor`` 环境诊断子命令单元测试.

覆盖 :mod:`fspack.doctor` 的核心场景：

- :class:`CheckResult`/``CheckStatus``/``DoctorReport`` 数据结构
- :func:`_check_tool_version` 通用工具检查（成功/未找到/超时/退出码非零）
- :func:`_check_pillow`/``_check_pip``/``_check_mingw`` 等具体工具检查
- :func:`_check_cache_dir` 缓存目录扫描（不存在/正常/扫描失败）
- :func:`_format_size`/``_format_status`` 辅助函数
- :func:`run_doctor` 聚合报告（按平台过滤工具）
- :func:`print_doctor_report` 渲染不抛异常
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from fspack.console import console
from fspack.doctor import (
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
    _filter_platform_supported,
    _find_debug_python,
    _find_dist_exe,
    _find_wrapper,
    _format_bench_delta,
    _format_run_status,
    _format_size,
    _format_status,
    _load_previous_bench_history,
    _machine_id,
    _platform_skip_reason,
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
from fspack.platform import Platform
from fspack.templates.registry import Template


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

    with patch("fspack.doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.doctor.subprocess.run", return_value=_FakeCompleted()
    ):
        result = _check_tool_version("gcc", ["gcc", "--version"])
    assert result.status is CheckStatus.OK
    assert "11.4.0" in result.detail


def test_check_tool_version_not_found() -> None:
    """工具未安装在 PATH 时返回 ERROR + 修复建议."""
    with patch("fspack.doctor.shutil.which", return_value=None):
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
    with patch("fspack.doctor.shutil.which", return_value=None):
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
    with patch("fspack.doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.doctor.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["gcc"], timeout=5),
    ):
        result = _check_tool_version("gcc", ["gcc", "--version"], error_suggestion="超时")
    assert result.status is CheckStatus.ERROR
    assert "执行失败" in result.detail


def test_check_tool_version_oserror() -> None:
    """工具执行 OSError 返回 ERROR."""
    with patch("fspack.doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.doctor.subprocess.run", side_effect=OSError("permission denied")
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

    with patch("fspack.doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.doctor.subprocess.run", return_value=_FakeFailed()
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

    with patch("fspack.doctor.shutil.which", return_value="/usr/bin/wine"), patch(
        "fspack.doctor.subprocess.run", return_value=_FakeOk()
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

    with patch("fspack.doctor.shutil.which", return_value="/usr/bin/gcc"), patch(
        "fspack.doctor.subprocess.run", return_value=_EmptyStdout()
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

    with patch("fspack.doctor.shutil.which", return_value="/usr/bin/pip"), patch(
        "fspack.doctor.subprocess.run", return_value=_FakePip()
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

    with patch("fspack.doctor.shutil.which", side_effect=_which), patch(
        "fspack.doctor.subprocess.run", return_value=_FakePipModule()
    ):
        result = _check_pip()
    assert result.status is CheckStatus.OK
    assert "pip 23.0" in result.detail


def test_check_pip_via_pip3_command() -> None:
    """pip 不在 PATH 但 pip3 存在时用 pip3 --version 探测（不再误报缺失）."""

    class _FakePip3:
        returncode = 0
        stdout = "pip 24.1 from /usr/lib/python3.12/site-packages/pip (python 3.12)\n"
        stderr = ""

    def _which(name: str) -> str | None:
        return "/usr/bin/pip3" if name == "pip3" else None

    with patch("fspack.doctor.shutil.which", side_effect=_which), patch(
        "fspack.doctor.subprocess.run", return_value=_FakePip3()
    ) as mock_run:
        result = _check_pip()
    assert result.status is CheckStatus.OK
    assert "pip 24.1" in result.detail
    # 探测命令为 pip3 --version（而非 python -m pip）
    assert mock_run.call_args[0][0] == ["pip3", "--version"]


def test_check_pip_not_found() -> None:
    """pip 命令与 python -m pip 均不可用时返回 ERROR."""

    class _FakeFail:
        returncode = 1
        stdout = ""
        stderr = "No module named pip"

    def _which(name: str) -> None:
        return None

    with patch("fspack.doctor.shutil.which", side_effect=_which), patch(
        "fspack.doctor.subprocess.run", return_value=_FakeFail()
    ):
        result = _check_pip()
    assert result.status is CheckStatus.ERROR
    assert result.detail == "未找到"
    assert "ensurepip" in result.suggestion


def test_check_pip_python_module_oserror() -> None:
    """python -m pip 执行 OSError 时返回 ERROR."""

    def _which(name: str) -> None:
        return None

    with patch("fspack.doctor.shutil.which", side_effect=_which), patch(
        "fspack.doctor.subprocess.run", side_effect=OSError("denied")
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

    monkeypatch.setattr("fspack.doctor.envs._dir_size", _raise_oserror)
    result = _check_cache_dir(cache)
    assert result.status is CheckStatus.WARN
    assert "扫描缓存目录失败" in result.suggestion


# ---- _check_cache_integrity（iter-128，iter-139 扩展 stale/orphan 检测） ----


def test_check_cache_integrity_dir_not_exists(tmp_path: Path) -> None:
    """缓存目录不存在时返回 OK（无需检查）."""
    from fspack.doctor import _check_cache_integrity

    result = _check_cache_integrity(tmp_path / "no-cache")
    assert result.status is CheckStatus.OK
    assert "缓存目录不存在" in result.detail


def test_check_cache_integrity_empty_dir(tmp_path: Path) -> None:
    """缓存目录为空（无 deps 文件与 wheel 文件）时返回 OK."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.OK
    assert "无依赖解析缓存文件与 wheel 文件" in result.detail


def test_check_cache_integrity_orphan_wheel_only(tmp_path: Path) -> None:
    """只有 wheel 文件无 deps 引用时返回 WARN（孤儿 wheel，iter-139）."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "numpy-1.0.whl").write_bytes(b"x")

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.WARN
    assert "1 个 wheel" in result.detail
    assert "1 孤儿" in result.detail
    assert "fsp cache clean" in result.suggestion


def test_check_cache_integrity_all_valid(tmp_path: Path) -> None:
    """所有缓存文件结构有效且引用的 wheel 都存在时返回 OK."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key1.json").write_text('{"wheels": ["numpy-1.0.whl"]}', encoding="utf-8")
    (cache / ".deps-key2.json").write_text('{"wheels": ["rich-1.0.whl", "click-1.0.whl"]}', encoding="utf-8")
    # 创建对应的 wheel 文件，使 deps 引用有效（iter-139 扩展检查 wheel 存在性）
    (cache / "numpy-1.0.whl").write_bytes(b"x")
    (cache / "rich-1.0.whl").write_bytes(b"x")
    (cache / "click-1.0.whl").write_bytes(b"x")

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.OK
    assert "扫描 2 个 deps 缓存" in result.detail
    assert "2 有效" in result.detail
    assert "3 个 wheel" in result.detail
    # 有效文件保留
    assert (cache / ".deps-key1.json").is_file()
    assert (cache / ".deps-key2.json").is_file()


def test_check_cache_integrity_corrupt_json_deleted(tmp_path: Path) -> None:
    """JSON 损坏的缓存文件计入 WARN，诊断阶段不删除（只读路径）."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-good.json").write_text('{"wheels": ["numpy-1.0.whl"]}', encoding="utf-8")
    (cache / "numpy-1.0.whl").write_bytes(b"x")  # 创建 wheel 避免 stale
    corrupt = cache / ".deps-bad.json"
    corrupt.write_text("{bad json", encoding="utf-8")

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.WARN
    assert "1 有效" in result.detail
    assert "1 损坏" in result.detail
    assert "1 个损坏 deps 待清理" in result.suggestion
    # 诊断阶段不删除损坏文件（由 fsp cache clean 清理）
    assert corrupt.is_file()
    # 有效文件保留
    assert (cache / ".deps-good.json").is_file()


def test_check_cache_integrity_non_dict_root_deleted(tmp_path: Path) -> None:
    """JSON 根对象非 dict（如 list）计入损坏，诊断阶段不删除."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt = cache / ".deps-corrupt.json"
    corrupt.write_text("[1, 2, 3]", encoding="utf-8")

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.WARN
    assert "1 损坏" in result.detail
    assert corrupt.is_file()


def test_check_cache_integrity_wrong_wheels_type_deleted(tmp_path: Path) -> None:
    """wheels 字段非 list 的缓存文件计入损坏，诊断阶段不删除."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt = cache / ".deps-corrupt.json"
    corrupt.write_text('{"wheels": "not-a-list"}', encoding="utf-8")

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.WARN
    assert "1 损坏" in result.detail
    assert corrupt.is_file()


def test_check_cache_integrity_multiple_corrupt_count(tmp_path: Path) -> None:
    """多个损坏文件时详情显示总数（iter-139 改为概要，文件名列表在 fsp cache status）."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(5):
        (cache / f".deps-bad{i}.json").write_text("{bad", encoding="utf-8")

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.WARN
    assert "5 损坏" in result.detail


def test_check_cache_integrity_oserror_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """read_text 抛 OSError 时不计为损坏（可能是瞬时文件系统问题）."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    cache_file = cache / ".deps-key.json"
    cache_file.write_text('{"wheels": ["x.whl"]}', encoding="utf-8")

    def raise_oserror(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", raise_oserror)
    result = _check_cache_integrity(cache)
    # OSError 不计为损坏，0 损坏 -> OK（iter-139：详情用 "deps 缓存" 格式）
    assert result.status is CheckStatus.OK
    assert "扫描 1 个 deps 缓存" in result.detail
    assert "1 有效" in result.detail
    # 文件未被删除
    assert cache_file.is_file()


def test_check_cache_integrity_stale_deps_warns(tmp_path: Path) -> None:
    """deps 引用的 wheel 不存在时返回 WARN（stale deps，iter-139）."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.WARN
    assert "1 stale 引用缺失 wheel" in result.detail
    assert "fsp cache clean" in result.suggestion


def test_check_cache_integrity_orphan_wheel_with_valid_deps(tmp_path: Path) -> None:
    """有有效 deps 但存在未被引用的孤儿 wheel 时返回 WARN（iter-139）."""
    from fspack.doctor import _check_cache_integrity

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["numpy-1.0.whl"]}', encoding="utf-8")
    (cache / "numpy-1.0.whl").write_bytes(b"x")
    (cache / "orphan-1.0.whl").write_bytes(b"yy")

    result = _check_cache_integrity(cache)
    assert result.status is CheckStatus.WARN
    assert "1 孤儿" in result.detail
    assert "fsp cache clean" in result.suggestion


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


# ---- _scan_cache_health（iter-139） ----


def test_scan_cache_health_dir_not_exists(tmp_path: Path) -> None:
    """缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_cache_health

    report = _scan_cache_health(tmp_path / "no-cache")
    assert report.total_deps_files == 0
    assert report.total_wheels == 0
    assert report.corrupt_deps_files == ()
    assert report.stale_deps_files == ()
    assert report.orphan_wheels == ()
    assert not report.has_issues


def test_scan_cache_health_empty_dir(tmp_path: Path) -> None:
    """空缓存目录返回空报告."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    report = _scan_cache_health(cache)
    assert report.total_deps_files == 0
    assert report.total_wheels == 0
    assert not report.has_issues


def test_scan_cache_health_all_valid(tmp_path: Path) -> None:
    """所有 deps 有效且 wheel 都存在时无问题."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key1.json").write_text('{"wheels": ["numpy-1.0.whl"]}', encoding="utf-8")
    (cache / ".deps-key2.json").write_text('{"wheels": ["rich-1.0.whl"]}', encoding="utf-8")
    (cache / "numpy-1.0.whl").write_bytes(b"x")
    (cache / "rich-1.0.whl").write_bytes(b"yy")

    report = _scan_cache_health(cache)
    assert report.total_deps_files == 2
    assert report.total_wheels == 2
    assert report.corrupt_deps_files == ()
    assert report.stale_deps_files == ()
    assert report.orphan_wheels == ()
    assert report.orphan_size_bytes == 0
    assert not report.has_issues


def test_scan_cache_health_corrupt_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时损坏 deps 文件被删除并计入 corrupt_deps_files."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-good.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    corrupt = cache / ".deps-bad.json"
    corrupt.write_text("{bad", encoding="utf-8")

    report = _scan_cache_health(cache, delete_corrupt=True)
    assert report.total_deps_files == 2
    assert report.corrupt_deps_files == (".deps-bad.json",)
    assert not corrupt.is_file()
    assert report.stale_deps_files == ()
    assert report.orphan_wheels == ()
    assert report.has_issues


def test_scan_cache_health_default_keeps_corrupt(tmp_path: Path) -> None:
    """默认（delete_corrupt=False）只报告损坏 deps 不删除（只读路径无副作用）."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt = cache / ".deps-bad.json"
    corrupt.write_text("{bad", encoding="utf-8")

    report = _scan_cache_health(cache)
    assert report.corrupt_deps_files == (".deps-bad.json",)
    assert corrupt.is_file()
    assert report.has_issues


def test_scan_cache_health_stale_deps_detected(tmp_path: Path) -> None:
    """deps 引用缺失 wheel 时计入 stale_deps_files 与 missing_wheels."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")

    report = _scan_cache_health(cache)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert "missing.whl" in report.missing_wheels
    # stale deps 文件未被删除（需 _clean_cache_issues 才删）
    assert (cache / ".deps-stale.json").is_file()
    assert report.has_issues


def test_scan_cache_health_orphan_wheel_detected(tmp_path: Path) -> None:
    """未被任何 deps 引用的 wheel 计入 orphan_wheels 并累加体积."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["numpy-1.0.whl"]}', encoding="utf-8")
    (cache / "numpy-1.0.whl").write_bytes(b"x")
    (cache / "orphan-1.0.whl").write_bytes(b"yyyy")

    report = _scan_cache_health(cache)
    assert report.total_wheels == 2
    assert report.orphan_wheels == ("orphan-1.0.whl",)
    assert report.orphan_size_bytes == 4
    assert report.has_issues


def test_scan_cache_health_shared_wheel_not_orphan(tmp_path: Path) -> None:
    """多个 deps 引用同一 wheel 时该 wheel 不算孤儿."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key1.json").write_text('{"wheels": ["shared.whl"]}', encoding="utf-8")
    (cache / ".deps-key2.json").write_text('{"wheels": ["shared.whl", "other.whl"]}', encoding="utf-8")
    (cache / "shared.whl").write_bytes(b"x")
    (cache / "other.whl").write_bytes(b"y")

    report = _scan_cache_health(cache)
    assert report.orphan_wheels == ()
    assert not report.has_issues


def test_scan_cache_health_non_string_wheels_ignored(tmp_path: Path) -> None:
    """wheels 列表中非字符串元素被忽略（防御性，避免 is_file 报错）."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    # wheels 含非字符串元素（理论上不会出现，但 _scan_cache_health 应防御）
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl", 123, null]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")

    report = _scan_cache_health(cache)
    assert report.stale_deps_files == ()
    assert report.missing_wheels == ()
    assert not report.has_issues


# ---- _clean_cache_issues（iter-139） ----


def test_clean_cache_issues_no_issues(tmp_path: Path) -> None:
    """无问题时清理不删除任何文件."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")

    report = _clean_cache_issues(cache)
    assert not report.has_issues
    assert (cache / ".deps-key.json").is_file()
    assert (cache / "x.whl").is_file()


def test_clean_cache_issues_dry_run_no_delete(tmp_path: Path) -> None:
    """dry_run=True 时仅扫描不删除文件."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    (cache / "orphan.whl").write_bytes(b"x")

    report = _clean_cache_issues(cache, dry_run=True)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ("orphan.whl",)
    # dry_run 不删除
    assert (cache / ".deps-stale.json").is_file()
    assert (cache / "orphan.whl").is_file()


def test_clean_cache_issues_deletes_stale_and_orphan(tmp_path: Path) -> None:
    """清理删除 stale deps 文件与孤儿 wheel 文件."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    (cache / ".deps-good.json").write_text('{"wheels": ["good.whl"]}', encoding="utf-8")
    (cache / "good.whl").write_bytes(b"x")
    (cache / "orphan.whl").write_bytes(b"yy")

    report = _clean_cache_issues(cache)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ("orphan.whl",)
    # stale deps 与 orphan wheel 被删除
    assert not (cache / ".deps-stale.json").is_file()
    assert not (cache / "orphan.whl").is_file()
    # 有效 deps 与被引用的 wheel 保留
    assert (cache / ".deps-good.json").is_file()
    assert (cache / "good.whl").is_file()


def test_clean_cache_issues_keeps_shared_wheel(tmp_path: Path) -> None:
    """清理时多个 deps 共享的 wheel 不被删除（即使某个 deps 是 stale）."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    # stale deps 引用 shared.whl + missing.whl；good deps 引用 shared.whl
    # shared.whl 仍存在（被 good deps 引用），不应被删
    (cache / ".deps-stale.json").write_text('{"wheels": ["shared.whl", "missing.whl"]}', encoding="utf-8")
    (cache / ".deps-good.json").write_text('{"wheels": ["shared.whl"]}', encoding="utf-8")
    (cache / "shared.whl").write_bytes(b"x")

    report = _clean_cache_issues(cache)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ()  # shared.whl 被 good deps 引用，非孤儿
    # stale deps 被删除
    assert not (cache / ".deps-stale.json").is_file()
    # shared.whl 保留（被 good deps 引用）
    assert (cache / "shared.whl").is_file()
    assert (cache / ".deps-good.json").is_file()


def test_clean_cache_issues_unlink_oserror_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """unlink 抛 OSError 时不阻断其他文件清理（best-effort）."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-stale1.json").write_text('{"wheels": ["missing1.whl"]}', encoding="utf-8")
    (cache / ".deps-stale2.json").write_text('{"wheels": ["missing2.whl"]}', encoding="utf-8")
    (cache / "orphan1.whl").write_bytes(b"x")
    (cache / "orphan2.whl").write_bytes(b"yy")

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        # 第一个文件 unlink 失败，后续正常
        if self.name in (".deps-stale1.json", "orphan1.whl"):
            raise OSError("simulated permission denied")
        real_unlink(self)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    report = _clean_cache_issues(cache)
    # 第一组文件 unlink 失败但保留在报告中
    assert ".deps-stale1.json" in report.stale_deps_files
    assert ".deps-stale2.json" in report.stale_deps_files
    assert "orphan1.whl" in report.orphan_wheels
    assert "orphan2.whl" in report.orphan_wheels
    # 第二组文件成功删除
    assert not (cache / ".deps-stale2.json").is_file()
    assert not (cache / "orphan2.whl").is_file()


def test_scan_cache_health_orphan_stat_oserror_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """orphan wheel 的 stat() 抛 OSError 时跳过体积累加但仍视为孤儿."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["good.whl"]}', encoding="utf-8")
    (cache / "good.whl").write_bytes(b"x")
    (cache / "orphan.whl").write_bytes(b"yy")

    real_stat = Path.stat

    def fail_orphan_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "orphan.whl":
            raise OSError("simulated")
        return real_stat(self)

    monkeypatch.setattr(Path, "stat", fail_orphan_stat)

    report = _scan_cache_health(cache)
    # orphan 仍被识别，但体积为 0（stat 失败跳过）
    assert report.orphan_wheels == ("orphan.whl",)
    assert report.orphan_size_bytes == 0


# ---- run_cache_status / run_cache_clean（iter-139） ----


def test_run_cache_status_no_issues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status 渲染健康报告，无问题时返回 has_issues=False."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_status(target="wheels")
    report = reports[0]
    assert not report.has_issues
    assert report.total_deps_files == 1
    assert report.total_wheels == 1


def test_run_cache_status_with_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status 渲染孤儿 wheel 警告并提示 fsp cache clean."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    (cache / "orphan.whl").write_bytes(b"yy")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_status(target="wheels")
    report = reports[0]
    assert report.orphan_wheels == ("orphan.whl",)
    assert report.orphan_size_bytes == 2


def test_run_cache_status_dir_not_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录不存在时 run_cache_status 返回空报告."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "no-cache"
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_status(target="wheels")
    report = reports[0]
    assert report.total_deps_files == 0
    assert report.total_wheels == 0


def test_run_cache_status_empty_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录为空时 run_cache_status 输出"为空"提示."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "wheels"
    cache.mkdir()
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_status(target="wheels")
    report = reports[0]
    assert report.total_deps_files == 0
    assert report.total_wheels == 0
    assert not report.has_issues


def test_run_cache_status_with_corrupt_and_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status 同时检测 corrupt/stale/orphan 三类问题."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "wheels"
    cache.mkdir()
    # 损坏 deps（扫描时删除）
    (cache / ".deps-bad.json").write_text("{bad", encoding="utf-8")
    # stale deps（引用缺失 wheel）
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    # 有效 deps + 引用的 wheel
    (cache / ".deps-good.json").write_text('{"wheels": ["good.whl"]}', encoding="utf-8")
    (cache / "good.whl").write_bytes(b"x")
    # 孤儿 wheel
    (cache / "orphan.whl").write_bytes(b"yy")

    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_status(target="wheels")
    report = reports[0]
    assert report.corrupt_deps_files == (".deps-bad.json",)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert "missing.whl" in report.missing_wheels
    assert report.orphan_wheels == ("orphan.whl",)
    assert report.has_issues


def test_run_cache_status_wheels_only_no_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status 在 wheel 全部被引用时不报孤儿（覆盖 _format_cache_summary 分支）."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["a.whl", "b.whl"]}', encoding="utf-8")
    (cache / "a.whl").write_bytes(b"x")
    (cache / "b.whl").write_bytes(b"yy")

    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_status(target="wheels")
    report = reports[0]
    assert not report.has_issues
    assert report.orphan_wheels == ()
    assert report.total_wheels == 2


def test_run_cache_clean_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean --dry-run 仅预览不删除."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    (cache / "orphan.whl").write_bytes(b"yy")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_clean(dry_run=True, target="wheels")
    report = reports[0]
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ("orphan.whl",)
    # dry_run 不删除
    assert (cache / ".deps-stale.json").is_file()
    assert (cache / "orphan.whl").is_file()


def test_run_cache_clean_actual_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean 实际删除 stale deps 与孤儿 wheel."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    (cache / "orphan.whl").write_bytes(b"yy")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_clean(dry_run=False, target="wheels")
    report = reports[0]
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ("orphan.whl",)
    assert not (cache / ".deps-stale.json").is_file()
    assert not (cache / "orphan.whl").is_file()


def test_run_cache_clean_no_issues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean 在无问题时输出"无需清理"且不删除文件."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_clean(target="wheels")
    report = reports[0]
    assert not report.has_issues
    assert (cache / ".deps-key.json").is_file()
    assert (cache / "x.whl").is_file()


def test_run_cache_clean_dir_not_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录不存在时 run_cache_clean 返回空报告."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "no-cache"
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_clean(target="wheels")
    report = reports[0]
    assert report.total_deps_files == 0
    assert report.total_wheels == 0


def test_run_cache_clean_with_corrupt_and_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean 同时处理 corrupt/stale/orphan 三类问题（覆盖 _print_cache_clean_lists 分支）."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "wheels"
    cache.mkdir()
    # 损坏 deps（扫描时删除，计入 corrupt_deps_files）
    (cache / ".deps-bad.json").write_text("{bad", encoding="utf-8")
    # stale deps（引用缺失 wheel，clean 阶段删除）
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    # 有效 deps + 引用的 wheel
    (cache / ".deps-good.json").write_text('{"wheels": ["good.whl"]}', encoding="utf-8")
    (cache / "good.whl").write_bytes(b"x")
    # 孤儿 wheel（clean 阶段删除）
    (cache / "orphan.whl").write_bytes(b"yy")

    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_clean(target="wheels")
    report = reports[0]
    assert report.corrupt_deps_files == (".deps-bad.json",)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ("orphan.whl",)
    # stale deps 与 orphan wheel 被删除
    assert not (cache / ".deps-stale.json").is_file()
    assert not (cache / "orphan.whl").is_file()
    # 有效 deps 与被引用的 wheel 保留
    assert (cache / ".deps-good.json").is_file()
    assert (cache / "good.whl").is_file()


def test_run_cache_clean_dry_run_with_all_issue_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean --dry-run 同时预览 corrupt/stale/orphan（覆盖 dry_run 分支）."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-bad.json").write_text("{bad", encoding="utf-8")
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    (cache / "orphan.whl").write_bytes(b"yy")

    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    reports = run_cache_clean(dry_run=True, target="wheels")
    report = reports[0]
    assert report.corrupt_deps_files == (".deps-bad.json",)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ("orphan.whl",)
    # dry_run 不删除任何文件（stale deps 与 orphan wheel 保留）
    assert (cache / ".deps-stale.json").is_file()
    assert (cache / "orphan.whl").is_file()


def test_preview_names_truncates_at_limit() -> None:
    """_preview_names 超过 limit 时显示前 N 个 + 总数提示."""
    from fspack.doctor import _preview_names

    names = tuple(f"file{i}.whl" for i in range(10))
    result = _preview_names(names, limit=3)
    assert "file0.whl" in result
    assert "file2.whl" in result
    assert "file3.whl" not in result
    assert "等 10 个" in result


def test_preview_names_empty_returns_empty() -> None:
    """_preview_names 空列表返回空字符串."""
    from fspack.doctor import _preview_names

    assert _preview_names(()) == ""


def test_preview_names_under_limit() -> None:
    """_preview_names 数量不超过 limit 时全部列出."""
    from fspack.doctor import _preview_names

    result = _preview_names(("a.whl", "b.whl"), limit=5)
    assert result == "a.whl, b.whl"


# ---- fsp cache CLI 派发（iter-139） ----


def test_cli_cache_status_dispatches() -> None:
    """``fsp cache status`` 触发 run_cache_status 调用."""
    from fspack.cli import main
    from fspack.doctor.models import CacheHealthReport

    fake_report = CacheHealthReport(cache_dir=Path("/tmp/cache"))
    with patch("fspack.doctor.run_cache_status", return_value=(fake_report,)) as mock_status:
        main(["cache", "status"])
    mock_status.assert_called_once_with(target=None, full_verify=False)


def test_cli_cache_status_with_target_dispatches() -> None:
    """``fsp cache status --target embed`` 透传 target 参数."""
    from fspack.cli import main
    from fspack.doctor.models import CacheHealthReport

    fake_report = CacheHealthReport(cache_dir=Path("/tmp/cache"), cache_type="embed")
    with patch("fspack.doctor.run_cache_status", return_value=(fake_report,)) as mock_status:
        main(["cache", "status", "--target", "embed"])
    mock_status.assert_called_once_with(target="embed", full_verify=False)


def test_cli_cache_status_verify_dispatches() -> None:
    """``fsp cache status --verify`` 透传 full_verify=True 启用全量 CRC 校验."""
    from fspack.cli import main
    from fspack.doctor.models import CacheHealthReport

    fake_report = CacheHealthReport(cache_dir=Path("/tmp/cache"))
    with patch("fspack.doctor.run_cache_status", return_value=(fake_report,)) as mock_status:
        main(["cache", "status", "--verify"])
    mock_status.assert_called_once_with(target=None, full_verify=True)


def test_cli_cache_clean_dispatches() -> None:
    """``fsp cache clean`` 触发 run_cache_clean 调用（dry_run=False）."""
    from fspack.cli import main
    from fspack.doctor.models import CacheHealthReport

    fake_report = CacheHealthReport(cache_dir=Path("/tmp/cache"))
    with patch("fspack.doctor.run_cache_clean", return_value=(fake_report,)) as mock_clean:
        main(["cache", "clean"])
    mock_clean.assert_called_once_with(dry_run=False, include_stale=False, target=None)


def test_cli_cache_clean_dry_run_dispatches() -> None:
    """``fsp cache clean --dry-run`` 触发 run_cache_clean(dry_run=True)."""
    from fspack.cli import main
    from fspack.doctor.models import CacheHealthReport

    fake_report = CacheHealthReport(cache_dir=Path("/tmp/cache"))
    with patch("fspack.doctor.run_cache_clean", return_value=(fake_report,)) as mock_clean:
        main(["cache", "clean", "--dry-run"])
    mock_clean.assert_called_once_with(dry_run=True, include_stale=False, target=None)


def test_cli_cache_clean_stale_dispatches() -> None:
    """``fsp cache clean --stale`` 透传 include_stale=True."""
    from fspack.cli import main
    from fspack.doctor.models import CacheHealthReport

    fake_report = CacheHealthReport(cache_dir=Path("/tmp/cache"))
    with patch("fspack.doctor.run_cache_clean", return_value=(fake_report,)) as mock_clean:
        main(["cache", "clean", "--stale"])
    mock_clean.assert_called_once_with(dry_run=False, include_stale=True, target=None)


def test_cli_cache_clean_target_stale_dispatches() -> None:
    """``fsp cache clean --target embed --stale`` 同时透传 target 与 include_stale."""
    from fspack.cli import main
    from fspack.doctor.models import CacheHealthReport

    fake_report = CacheHealthReport(cache_dir=Path("/tmp/cache"), cache_type="embed")
    with patch("fspack.doctor.run_cache_clean", return_value=(fake_report,)) as mock_clean:
        main(["cache", "clean", "--target", "embed", "--stale"])
    mock_clean.assert_called_once_with(dry_run=False, include_stale=True, target="embed")


# ---- 多 cache 类型扫描器（iter-148） ----
#
# 覆盖 embed/standalone/nuitka/loaders/ccache/tkinter 6 个新扫描器：
# 损坏文件识别（zip/tar/PE 头/空文件）+ 过期文件识别（版本不在 KNOWN_*_VERSIONS）
# + 聚合分发（_scan_cache_by_type / _scan_all_caches / _clean_cache_by_type /
#   _clean_all_caches）+ run_cache_status/clean 的 --target/--stale 派发。


def _make_zip(path: Path, content: bytes = b"hello") -> None:
    """创建有效 zip 文件（含一个 test.txt 条目）."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test.txt", content)


def _make_tar(path: Path, content: bytes = b"hello") -> None:
    """创建有效 tar.gz 文件（含一个 test.txt 条目）."""
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(name="test.txt")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))


def _make_pe(path: Path) -> None:
    """创建合法 PE 文件（MZ 头 + 填充）."""
    path.write_bytes(b"MZ" + b"\x00" * 100)


# ---- 辅助函数：_is_zip_intact / _is_tar_intact / _is_pe_file ----


def test_is_zip_intact_valid(tmp_path: Path) -> None:
    """_is_zip_intact 对有效 zip 返回 True."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "test.zip"
    _make_zip(z)
    assert _is_zip_intact(z) is True


def test_is_zip_intact_corrupt(tmp_path: Path) -> None:
    """_is_zip_intact 对垃圾数据返回 False."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "bad.zip"
    z.write_bytes(b"not a zip file")
    assert _is_zip_intact(z) is False


def test_is_zip_intact_quick_vs_full_data_corrupt(tmp_path: Path) -> None:
    """快检只读中心目录（数据区损坏仍 True），全量 CRC 校验检出数据区损坏."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "bad_data.zip"
    _make_zip(z)
    data = bytearray(z.read_bytes())
    # 翻转 local file header 中文件名之后的压缩数据首字节（不动文件尾的中心目录）
    idx = data.find(b"test.txt")
    assert idx > 0
    data[idx + len(b"test.txt")] ^= 0xFF
    z.write_bytes(bytes(data))
    assert _is_zip_intact(z) is True  # 快检：中心目录完好，数据区损坏不可见
    assert _is_zip_intact(z, full=True) is False  # 全量：CRC 校验失败


def test_is_zip_intact_full_valid_zip(tmp_path: Path) -> None:
    """full=True 对有效 zip 仍返回 True（testzip 通过）."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "test.zip"
    _make_zip(z)
    assert _is_zip_intact(z, full=True) is True


def test_is_tar_intact_valid(tmp_path: Path) -> None:
    """_is_tar_intact 对有效 tar.gz 返回 True."""
    from fspack.doctor import _is_tar_intact

    t = tmp_path / "test.tar.gz"
    _make_tar(t)
    assert _is_tar_intact(t) is True


def test_is_tar_intact_corrupt(tmp_path: Path) -> None:
    """_is_tar_intact 对垃圾数据返回 False."""
    from fspack.doctor import _is_tar_intact

    t = tmp_path / "bad.tar.gz"
    t.write_bytes(b"not a tar file")
    assert _is_tar_intact(t) is False


def test_is_pe_file_valid(tmp_path: Path) -> None:
    """_is_pe_file 对含 MZ 头的文件返回 True."""
    from fspack.doctor import _is_pe_file

    p = tmp_path / "loader.exe"
    _make_pe(p)
    assert _is_pe_file(p) is True


def test_is_pe_file_missing_mz(tmp_path: Path) -> None:
    """_is_pe_file 对缺少 MZ 头的文件返回 False."""
    from fspack.doctor import _is_pe_file

    p = tmp_path / "bad.exe"
    p.write_bytes(b"XX" + b"\x00" * 100)
    assert _is_pe_file(p) is False


def test_is_pe_file_empty(tmp_path: Path) -> None:
    """_is_pe_file 对空文件返回 False."""
    from fspack.doctor import _is_pe_file

    p = tmp_path / "empty.exe"
    p.write_bytes(b"")
    assert _is_pe_file(p) is False


def test_is_zip_intact_oserror_indeterminate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_zip_intact 对 OSError（杀软/文件锁）返回 None（无法判定，不判损坏）."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "locked.zip"
    z.write_bytes(b"x" * 10)

    class _LockedZip:
        def __init__(self, path: object, *args: object, **kwargs: object) -> None:
            raise PermissionError("file locked by antivirus")

    monkeypatch.setattr("fspack.doctor.integrity.zipfile.ZipFile", _LockedZip)
    assert _is_zip_intact(z) is None


def test_is_tar_intact_oserror_indeterminate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_tar_intact 对 OSError（杀软/文件锁）返回 None（无法判定，不判损坏）."""
    from fspack.doctor import _is_tar_intact

    t = tmp_path / "locked.tar.gz"
    t.write_bytes(b"x" * 10)

    def _raise_open(*args: object, **kwargs: object) -> None:
        raise PermissionError("file locked by antivirus")

    monkeypatch.setattr("fspack.doctor.integrity.tarfile.open", _raise_open)
    assert _is_tar_intact(t) is None


def test_is_pe_file_oserror_indeterminate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_pe_file 对 OSError（杀软/文件锁）返回 None（无法判定，不判损坏）."""
    from fspack.doctor import _is_pe_file

    p = tmp_path / "locked.exe"
    p.write_bytes(b"MZ")

    def _raise_open(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("file locked by antivirus")

    monkeypatch.setattr(Path, "open", _raise_open)
    assert _is_pe_file(p) is None


# ---- _scan_embed_health ----


def test_scan_embed_health_dir_not_exists(tmp_path: Path) -> None:
    """embed 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_embed_health

    report = _scan_embed_health(tmp_path / "no-embed")
    assert report.cache_type == "embed"
    assert report.total_files == 0
    assert not report.has_issues


def test_scan_embed_health_empty_dir(tmp_path: Path) -> None:
    """embed 空目录无问题."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    report = _scan_embed_health(cache)
    assert report.total_files == 0
    assert not report.has_issues


def test_scan_embed_health_valid_zip(tmp_path: Path) -> None:
    """已知版本的有效 embed zip 不报问题."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    # 3.11.9 在 KNOWN_EMBED_VERSIONS.values() 中
    z = cache / "python-3.11.9-embed-amd64.zip"
    _make_zip(z)
    report = _scan_embed_health(cache)
    assert report.total_files == 1
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_embed_health_corrupt_zip_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时损坏的 embed zip 在扫描期删除并计入 corrupt_files."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    bad = cache / "python-3.11.9-embed-amd64.zip"
    bad.write_bytes(b"not a zip")
    report = _scan_embed_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ("python-3.11.9-embed-amd64.zip",)
    assert not bad.is_file()
    assert report.has_issues


def test_scan_embed_health_default_keeps_corrupt(tmp_path: Path) -> None:
    """默认（delete_corrupt=False）损坏的 embed zip 只报告不删除（只读路径）."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    bad = cache / "python-3.11.9-embed-amd64.zip"
    bad.write_bytes(b"not a zip")
    report = _scan_embed_health(cache)
    assert report.corrupt_files == ("python-3.11.9-embed-amd64.zip",)
    assert bad.is_file()
    assert report.has_issues


def test_scan_embed_health_indeterminate_zip_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """zip 完整性无法判定（IO 异常返回 None）时不计损坏也不删除."""
    from fspack.doctor import _scan_embed_health, cache_health

    cache = tmp_path / "embed"
    cache.mkdir()
    locked = cache / "python-3.11.9-embed-amd64.zip"
    _make_zip(locked)

    def _locked_zip(path: Path, **kwargs: object) -> bool | None:
        return None  # 模拟杀软/文件锁导致 OSError 无法判定（兼容 full 等透传参数）

    monkeypatch.setattr(cache_health, "_is_zip_intact", _locked_zip)
    report = _scan_embed_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert locked.is_file()
    assert not report.has_issues


def test_scan_embed_health_stale_zip_detected(tmp_path: Path) -> None:
    """未知版本的 embed zip 计入 stale_files 但不删除（需 --stale 清理）."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_EMBED_VERSIONS.values() 中
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    report = _scan_embed_health(cache)
    assert report.stale_files == ("python-3.7.0-embed-amd64.zip",)
    assert stale.is_file()  # 扫描期不删除
    assert report.has_issues


def test_scan_embed_health_non_zip_ignored(tmp_path: Path) -> None:
    """非 embed zip 命名模式的文件被忽略（不视为问题）."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    (cache / "README.txt").write_text("info", encoding="utf-8")
    (cache / "random.zip").write_bytes(b"x")
    report = _scan_embed_health(cache)
    assert report.total_files == 2  # 计入 total 但无问题
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def _make_data_corrupt_zip(path: Path) -> None:
    """创建中心目录完好但数据区损坏的 zip（快检不可见，全量 CRC 可检出）."""
    _make_zip(path)
    data = bytearray(path.read_bytes())
    idx = data.find(b"test.txt")
    assert idx > 0
    data[idx + len(b"test.txt")] ^= 0xFF  # 翻转压缩数据首字节，不动文件尾中心目录
    path.write_bytes(bytes(data))


def test_scan_embed_health_full_verify_detects_data_corrupt(tmp_path: Path) -> None:
    """full_verify=True 检出数据区损坏的 embed zip（默认快检不可见）."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_data_corrupt_zip(cache / "python-3.11.9-embed-amd64.zip")

    quick = _scan_embed_health(cache)
    assert quick.corrupt_files == ()  # 快检：中心目录完好，不报损坏

    full = _scan_embed_health(cache, full_verify=True)
    assert full.corrupt_files == ("python-3.11.9-embed-amd64.zip",)  # 全量：CRC 检出


# ---- _scan_standalone_health ----


def test_scan_standalone_health_dir_not_exists(tmp_path: Path) -> None:
    """standalone 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_standalone_health

    report = _scan_standalone_health(tmp_path / "no-standalone")
    assert report.cache_type == "standalone"
    assert not report.has_issues


def test_scan_standalone_health_valid_tar(tmp_path: Path) -> None:
    """已知版本的有效 standalone tar.gz 不报问题."""
    from fspack.doctor import _scan_standalone_health

    cache = tmp_path / "standalone"
    cache.mkdir()
    # 3.11.15 在 KNOWN_STANDALONE_VERSIONS.values() 中
    t = cache / "cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz"
    _make_tar(t)
    report = _scan_standalone_health(cache)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_standalone_health_corrupt_tar_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时损坏的 standalone tar.gz 在扫描期删除并计入 corrupt_files."""
    from fspack.doctor import _scan_standalone_health

    cache = tmp_path / "standalone"
    cache.mkdir()
    bad = cache / "cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz"
    bad.write_bytes(b"not a tar")
    report = _scan_standalone_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ("cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz",)
    assert not bad.is_file()
    assert report.has_issues


def test_scan_standalone_health_stale_tar_detected(tmp_path: Path) -> None:
    """未知版本的 standalone tar.gz 计入 stale_files 但不删除."""
    from fspack.doctor import _scan_standalone_health

    cache = tmp_path / "standalone"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_STANDALONE_VERSIONS.values() 中
    stale = cache / "cpython-3.7.0+20260718-x86_64-unknown-linux-install_only.tar.gz"
    _make_tar(stale)
    report = _scan_standalone_health(cache)
    assert report.stale_files == ("cpython-3.7.0+20260718-x86_64-unknown-linux-install_only.tar.gz",)
    assert stale.is_file()
    assert report.has_issues


# ---- _scan_nuitka_health ----


def test_scan_nuitka_health_dir_not_exists(tmp_path: Path) -> None:
    """nuitka 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_nuitka_health

    report = _scan_nuitka_health(tmp_path / "no-nuitka")
    assert report.cache_type == "nuitka"
    assert not report.has_issues


def test_scan_nuitka_health_valid_dir(tmp_path: Path) -> None:
    """含 python.exe 的已知版本子目录不报问题."""
    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    # 3.11.15 在 KNOWN_STANDALONE_VERSIONS.values() 中
    py_dir = cache / "3.11.15" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "python.exe").write_bytes(b"MZ")
    report = _scan_nuitka_health(cache)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_nuitka_health_corrupt_dir_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 且目录 mtime 超过宽限期时，缺 python 可执行的子目录删除."""
    import os
    import time as time_mod

    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    # 已知版本但缺 python 可执行；mtime 回拨到 1 小时前（避开解压进行中宽限）
    bad_dir = cache / "3.11.15" / "python"
    bad_dir.mkdir(parents=True)
    old_ts = time_mod.time() - 3600
    os.utime(cache / "3.11.15", (old_ts, old_ts))
    report = _scan_nuitka_health(cache, delete_corrupt=True)
    assert "3.11.15" in report.corrupt_files
    assert not (cache / "3.11.15").is_dir()
    assert report.has_issues


def test_scan_nuitka_health_recent_extract_skipped(tmp_path: Path) -> None:
    """目录 mtime 距今不足宽限期（视为另一进程解压进行中）时跳过判定不删除."""
    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    # 目录刚创建（mtime 距今约 0 秒 < 600 秒宽限），缺 python 可执行
    extracting = cache / "3.11.15" / "python"
    extracting.mkdir(parents=True)
    report = _scan_nuitka_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ()
    assert (cache / "3.11.15").is_dir()
    assert not report.has_issues


def test_scan_nuitka_health_stale_dir_detected(tmp_path: Path) -> None:
    """未知版本的子目录计入 stale_files、累计体积但不删除."""
    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_STANDALONE_VERSIONS.values() 中
    stale_dir = cache / "3.7.0" / "python"
    stale_dir.mkdir(parents=True)
    (stale_dir / "python.exe").write_bytes(b"MZ" + b"\x00" * 998)
    report = _scan_nuitka_health(cache)
    assert "3.7.0" in report.stale_files
    assert (cache / "3.7.0").is_dir()
    # stale 目录体积（递归）累计到 issues_size_bytes
    assert report.issues_size_bytes == 1000
    assert report.has_issues


def test_scan_nuitka_health_residual_tarball_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时残留 tarball（解压后未清理）视为损坏并删除."""
    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    residual = cache / "cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz"
    _make_tar(residual)
    report = _scan_nuitka_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ("cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz",)
    assert not residual.is_file()
    assert report.has_issues


# ---- _scan_loader_health ----


def test_scan_loader_health_dir_not_exists(tmp_path: Path) -> None:
    """loaders 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_loader_health

    report = _scan_loader_health(tmp_path / "no-loaders")
    assert report.cache_type == "loaders"
    assert not report.has_issues


def test_scan_loader_health_valid_pe(tmp_path: Path) -> None:
    """合法 PE 文件不报问题."""
    from fspack.doctor import _scan_loader_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    _make_pe(cache / "abc123def4567890.exe")
    report = _scan_loader_health(cache)
    assert report.corrupt_files == ()
    assert not report.has_issues


def test_scan_loader_health_empty_file_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时 0 字节文件视为损坏并删除."""
    from fspack.doctor import _scan_loader_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    empty = cache / "empty1234567890.exe"
    empty.write_bytes(b"")
    report = _scan_loader_health(cache, delete_corrupt=True)
    assert "empty1234567890.exe" in report.corrupt_files
    assert not empty.is_file()
    assert report.has_issues


def test_scan_loader_health_non_pe_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时非空但缺 MZ 头的 exe 视为损坏并删除."""
    from fspack.doctor import _scan_loader_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    bad = cache / "bad123456789abc.exe"
    bad.write_bytes(b"XX" + b"\x00" * 50)
    report = _scan_loader_health(cache, delete_corrupt=True)
    assert "bad123456789abc.exe" in report.corrupt_files
    assert not bad.is_file()
    assert report.has_issues


def test_scan_loader_health_indeterminate_pe_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PE 头无法判定（IO 异常返回 None）的 exe 不计损坏也不删除."""
    from fspack.doctor import _scan_loader_health, cache_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    locked = cache / "locked1234567890.exe"
    _make_pe(locked)

    def _locked_pe(path: Path) -> bool | None:
        return None  # 模拟杀软/文件锁导致 OSError 无法判定

    monkeypatch.setattr(cache_health, "_is_pe_file", _locked_pe)
    report = _scan_loader_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ()
    assert locked.is_file()
    assert not report.has_issues


def test_scan_loader_health_non_exe_kept(tmp_path: Path) -> None:
    """非 exe loader 文件（Linux/macOS ELF 产物）非空即健康，跨平台均保留."""
    from fspack.doctor import _scan_loader_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    elf = cache / "elf1234567890abcd"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 50)
    report = _scan_loader_health(cache)
    assert report.corrupt_files == ()
    assert not report.has_issues
    assert elf.is_file()


# ---- _scan_ccache_health ----


def test_scan_ccache_health_dir_not_exists(tmp_path: Path) -> None:
    """ccache 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_ccache_health

    report = _scan_ccache_health(tmp_path / "no-ccache")
    assert report.cache_type == "ccache"
    assert not report.has_issues


def test_scan_ccache_health_valid(tmp_path: Path) -> None:
    """ccache 二进制存在且无残留时不报问题."""
    from fspack.doctor import _scan_ccache_health

    cache = tmp_path / "ccache"
    cache.mkdir()
    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    _make_pe(cache / exe_name)
    report = _scan_ccache_health(cache)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_ccache_health_missing_exe(tmp_path: Path) -> None:
    """ccache 二进制缺失时计入 missing_files（与损坏分列，不计入 corrupt）."""
    from fspack.doctor import _scan_ccache_health

    cache = tmp_path / "ccache"
    cache.mkdir()
    report = _scan_ccache_health(cache)
    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    assert exe_name in report.missing_files
    assert report.corrupt_files == ()
    # 缺失无文件可删，不算需要清理的问题，不虚增 issues_count
    assert not report.has_issues
    assert report.issues_count == 0


def test_scan_ccache_health_stale_subdir(tmp_path: Path) -> None:
    """旧版 ccache-* 子目录计入 stale_files 但不删除."""
    from fspack.doctor import _scan_ccache_health

    cache = tmp_path / "ccache"
    cache.mkdir()
    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    _make_pe(cache / exe_name)
    stale_dir = cache / "ccache-4.10-win64"
    stale_dir.mkdir()
    (stale_dir / "ccache.exe").write_bytes(b"MZ")
    report = _scan_ccache_health(cache)
    assert "ccache-4.10-win64" in report.stale_files
    assert stale_dir.is_dir()
    assert report.has_issues


def test_scan_ccache_health_residual_archive_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时残留下载归档（ccache.tar.xz/ccache.zip）视为损坏并删除."""
    from fspack.doctor import _scan_ccache_health

    cache = tmp_path / "ccache"
    cache.mkdir()
    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    _make_pe(cache / exe_name)
    archive = cache / "ccache.zip"
    archive.write_bytes(b"not a real zip")
    report = _scan_ccache_health(cache, delete_corrupt=True)
    assert "ccache.zip" in report.corrupt_files
    assert not archive.is_file()
    assert report.has_issues


# ---- _scan_tkinter_health ----


def test_scan_tkinter_health_dir_not_exists(tmp_path: Path) -> None:
    """tkinter 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_tkinter_health

    report = _scan_tkinter_health(tmp_path / "no-tkinter")
    assert report.cache_type == "tkinter"
    assert not report.has_issues


def test_scan_tkinter_health_valid_zip(tmp_path: Path) -> None:
    """已知版本的有效 tkinter zip 不报问题."""
    from fspack.doctor import _scan_tkinter_health

    cache = tmp_path / "tkinter"
    cache.mkdir()
    # 3.11.15 在 KNOWN_STANDALONE_VERSIONS.values() 中
    z = cache / "tkinter-3.11.15.zip"
    _make_zip(z)
    report = _scan_tkinter_health(cache)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_tkinter_health_corrupt_zip_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时损坏的 tkinter zip 在扫描期删除并计入 corrupt_files."""
    from fspack.doctor import _scan_tkinter_health

    cache = tmp_path / "tkinter"
    cache.mkdir()
    bad = cache / "tkinter-3.11.15.zip"
    bad.write_bytes(b"not a zip")
    report = _scan_tkinter_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ("tkinter-3.11.15.zip",)
    assert not bad.is_file()
    assert report.has_issues


def test_scan_tkinter_health_stale_zip_detected(tmp_path: Path) -> None:
    """未知版本的 tkinter zip 计入 stale_files 但不删除."""
    from fspack.doctor import _scan_tkinter_health

    cache = tmp_path / "tkinter"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_STANDALONE_VERSIONS.values() 中
    stale = cache / "tkinter-3.7.0.zip"
    _make_zip(stale)
    report = _scan_tkinter_health(cache)
    assert report.stale_files == ("tkinter-3.7.0.zip",)
    assert stale.is_file()
    assert report.has_issues


# ---- _scan_cache_by_type / _scan_all_caches 聚合分发 ----


def test_scan_cache_by_type_dispatches_wheels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_scan_cache_by_type('wheels') 分发到 _scan_cache_health."""
    from fspack.doctor import _scan_cache_by_type

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    report = _scan_cache_by_type("wheels")
    assert report.cache_type == "wheels"
    assert report.total_deps_files == 1


def test_scan_cache_by_type_dispatches_embed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_scan_cache_by_type('embed') 分发到 _scan_embed_health."""
    from fspack.doctor import _scan_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_zip(cache / "python-3.11.9-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _scan_cache_by_type("embed")
    assert report.cache_type == "embed"
    assert report.total_files == 1


def test_scan_cache_by_type_embed_full_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_scan_cache_by_type('embed', full_verify=True) 透传全量校验，快检不报全量报."""
    from fspack.doctor import _scan_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_data_corrupt_zip(cache / "python-3.11.9-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    quick = _scan_cache_by_type("embed")
    assert quick.corrupt_files == ()

    full = _scan_cache_by_type("embed", full_verify=True)
    assert full.corrupt_files == ("python-3.11.9-embed-amd64.zip",)


def test_scan_cache_by_type_wheels_ignores_full_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wheels 扫描器不支持 full_verify，分发器不透传该参数（不抛 TypeError）."""
    from fspack.doctor import _scan_cache_by_type

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    report = _scan_cache_by_type("wheels", full_verify=True)
    assert report.cache_type == "wheels"
    assert report.total_deps_files == 1


def test_scan_cache_by_type_unknown_raises() -> None:
    """_scan_cache_by_type 未知类型抛 ValueError."""
    from fspack.doctor import _scan_cache_by_type

    with pytest.raises(ValueError, match="未知 cache 类型"):
        _scan_cache_by_type("unknown")


def test_scan_all_caches_returns_all_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_scan_all_caches 返回全部 7 个 cache 类型的报告."""
    from fspack.doctor import CACHE_TYPES, _scan_all_caches

    # 将所有 cache 目录重定向到 tmp_path 下空子目录，避免受开发机真实缓存影响
    monkeypatch.setattr("fspack.config.cache.cache_root", lambda: tmp_path / "cache")

    reports = _scan_all_caches()
    assert len(reports) == len(CACHE_TYPES)
    assert tuple(r.cache_type for r in reports) == CACHE_TYPES


# ---- _clean_cache_by_type / _clean_all_caches ----


def test_clean_cache_by_type_wheels_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_clean_cache_by_type('wheels') 分发到 _clean_cache_issues."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    report = _clean_cache_by_type("wheels")
    assert report.stale_deps_files == (".deps-stale.json",)
    assert not (cache / ".deps-stale.json").is_file()


def test_clean_cache_by_type_embed_no_stale_keeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 wheels 类型无 --stale 时保留 stale_files（仅清理 corrupt，扫描期已删）."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_EMBED_VERSIONS 中
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _clean_cache_by_type("embed", include_stale=False)
    assert "python-3.7.0-embed-amd64.zip" in report.stale_files
    assert stale.is_file()  # 未启用 --stale，保留


def test_clean_cache_by_type_embed_with_stale_deletes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 wheels 类型 --stale 时删除 stale_files."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _clean_cache_by_type("embed", include_stale=True)
    # include_stale=True 后重新扫描，stale_files 应已清空
    assert report.stale_files == ()
    assert not stale.is_file()


def test_clean_cache_by_type_dry_run_no_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 时仅扫描不删除（含损坏文件，扫描器不带 delete_corrupt）."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    bad = cache / "python-3.11.9-embed-amd64.zip"
    bad.write_bytes(b"not a zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _clean_cache_by_type("embed", dry_run=True)
    # dry_run 下损坏文件只报告不删除（扫描器 delete_corrupt=False）
    assert "python-3.11.9-embed-amd64.zip" in report.corrupt_files
    assert bad.is_file()


def test_clean_cache_by_type_clean_deletes_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 dry_run 清理路径扫描器带 delete_corrupt=True，损坏文件被删除."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    bad = cache / "python-3.11.9-embed-amd64.zip"
    bad.write_bytes(b"not a zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _clean_cache_by_type("embed", dry_run=False)
    assert "python-3.11.9-embed-amd64.zip" in report.corrupt_files
    assert not bad.is_file()
    assert report.has_issues


def test_clean_all_caches_returns_all_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_clean_all_caches 返回全部 7 个 cache 类型的报告."""
    from fspack.doctor import CACHE_TYPES, _clean_all_caches

    monkeypatch.setattr("fspack.config.cache.cache_root", lambda: tmp_path / "cache")

    reports = _clean_all_caches()
    assert len(reports) == len(CACHE_TYPES)
    assert tuple(r.cache_type for r in reports) == CACHE_TYPES


# ---- run_cache_status / run_cache_clean 多 cache 派发 ----


def test_run_cache_status_target_embed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status(target='embed') 仅扫描 embed 并返回 1 元组."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_zip(cache / "python-3.11.9-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_status(target="embed")
    assert len(reports) == 1
    assert reports[0].cache_type == "embed"
    assert reports[0].total_files == 1
    assert not reports[0].has_issues


def test_run_cache_status_all_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status() 无 target 时扫描全部 7 个 cache 类型."""
    from fspack.doctor import CACHE_TYPES, run_cache_status

    monkeypatch.setattr("fspack.config.cache.cache_root", lambda: tmp_path / "cache")

    reports = run_cache_status()
    assert len(reports) == len(CACHE_TYPES)
    assert tuple(r.cache_type for r in reports) == CACHE_TYPES


def test_run_cache_status_invalid_target_raises_systemexit() -> None:
    """run_cache_status 未知 target 抛 SystemExit(2)."""
    from fspack.doctor import run_cache_status

    with pytest.raises(SystemExit) as exc_info:
        run_cache_status(target="unknown")
    assert exc_info.value.code == 2


def test_run_cache_clean_target_embed_with_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean(target='embed', include_stale=True) 删除 stale zip."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "embed"
    cache.mkdir()
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_clean(target="embed", include_stale=True)
    assert len(reports) == 1
    assert not stale.is_file()


def test_run_cache_clean_target_embed_no_stale_keeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean(target='embed') 无 --stale 时保留 stale zip."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "embed"
    cache.mkdir()
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    run_cache_clean(target="embed", include_stale=False)
    assert stale.is_file()


def test_run_cache_clean_invalid_target_raises_systemexit() -> None:
    """run_cache_clean 未知 target 抛 SystemExit(2)."""
    from fspack.doctor import run_cache_clean

    with pytest.raises(SystemExit) as exc_info:
        run_cache_clean(target="unknown")
    assert exc_info.value.code == 2


def test_run_cache_clean_all_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean() 无 target 时清理全部 7 个 cache 类型."""
    from fspack.doctor import CACHE_TYPES, run_cache_clean

    monkeypatch.setattr("fspack.config.cache.cache_root", lambda: tmp_path / "cache")

    reports = run_cache_clean()
    assert len(reports) == len(CACHE_TYPES)
    assert tuple(r.cache_type for r in reports) == CACHE_TYPES


def test_run_cache_status_embed_with_corrupt_and_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status(target='embed') 同时检测损坏+过期 zip，覆盖渲染分支."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "embed"
    cache.mkdir()
    # 损坏 zip（扫描期删除）
    (cache / "python-3.11.9-embed-amd64.zip").write_bytes(b"not a zip")
    # 过期 zip（未知版本）
    _make_zip(cache / "python-3.7.0-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_status(target="embed")
    report = reports[0]
    assert "python-3.11.9-embed-amd64.zip" in report.corrupt_files
    assert "python-3.7.0-embed-amd64.zip" in report.stale_files
    assert report.has_issues


def test_run_cache_clean_embed_with_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean(target='embed') 损坏 zip 渲染清理报告."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "embed"
    cache.mkdir()
    (cache / "python-3.11.9-embed-amd64.zip").write_bytes(b"not a zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_clean(target="embed", dry_run=True)
    report = reports[0]
    assert "python-3.11.9-embed-amd64.zip" in report.corrupt_files


def test_run_cache_clean_embed_with_stale_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean(target='embed') 过期 zip 渲染清理报告."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_zip(cache / "python-3.7.0-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_clean(target="embed", include_stale=False)
    report = reports[0]
    assert "python-3.7.0-embed-amd64.zip" in report.stale_files


def test_build_clean_hint_wheels_empty() -> None:
    """_build_clean_hint 对 wheels 报告返回空字符串（无需 --target/--stale）."""
    from fspack.doctor.cache import _build_clean_hint
    from fspack.doctor.models import CacheHealthReport

    report = CacheHealthReport(cache_dir=Path("/tmp"), cache_type="wheels", stale_deps_files=("a.json",))
    assert _build_clean_hint(report) == ""


def test_build_clean_hint_non_wheels_no_stale() -> None:
    """_build_clean_hint 非 wheels 无 stale_files 仅返回 --target."""
    from fspack.doctor.cache import _build_clean_hint
    from fspack.doctor.models import CacheHealthReport

    report = CacheHealthReport(cache_dir=Path("/tmp"), cache_type="embed", corrupt_files=("a.zip",))
    assert _build_clean_hint(report) == " --target embed"


def test_build_clean_hint_non_wheels_with_stale() -> None:
    """_build_clean_hint 非 wheels 含 stale_files 返回 --target <type> --stale."""
    from fspack.doctor.cache import _build_clean_hint
    from fspack.doctor.models import CacheHealthReport

    report = CacheHealthReport(cache_dir=Path("/tmp"), cache_type="embed", stale_files=("a.zip",))
    assert _build_clean_hint(report) == " --target embed --stale"


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


def test_cli_doctor_test_and_bench_mutex_warning() -> None:
    """``fsp doctor --test --bench`` 同时指定时提示 --test 被忽略，仅执行 bench."""
    from fspack.cli import main

    fake_report = DoctorReport(
        env_info=(CheckResult("Python", CheckStatus.OK, "3.11.9"),),
        tool_checks=(CheckResult("pip", CheckStatus.OK, "24.0"),),
    )
    with patch("fspack.doctor.run_doctor", return_value=fake_report), patch("fspack.doctor.print_doctor_report"), patch(
        "fspack.doctor.run_doctor_bench"
    ) as mock_bench, patch("fspack.doctor.run_doctor_test") as mock_test, patch(
        "fspack.console.console.warn"
    ) as mock_warn:
        main(["doctor", "--test", "--bench"])
    mock_bench.assert_called_once()
    mock_test.assert_not_called()
    mock_warn.assert_called_once()
    # warning 内容提示互斥语义：--bench 已包含 --test 的构建测试
    assert "--bench" in mock_warn.call_args[0][0]
    assert "--test" in mock_warn.call_args[0][0]


def test_cli_doctor_in_help() -> None:
    """``fsp --help`` 输出含 doctor 子命令."""
    from fspack.cli import build_parser

    parser = build_parser()
    help_text = parser.format_help()
    assert "doctor" in help_text
    assert "环境诊断" in help_text


# ---- 平台兼容性过滤（doctor --test/--bench 跳过无兼容 Python 的模板）----


def _tpl(tpl_id: str, requires_python: str) -> Template:
    """构造最小 Template（仅 id 与 requires_python 参与平台过滤）."""
    return Template(id=tpl_id, name=tpl_id, description="", category="cli", files=(), requires_python=requires_python)


def test_platform_skip_reason_incompatible_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 无 3.8/3.9 standalone，``>=3.8,<3.10`` 约束应给出跳过原因."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    reason = _platform_skip_reason(_tpl("pyside2", ">=3.8,<3.10"))
    assert reason is not None
    assert "requires-python" in reason
    assert ">=3.8,<3.10" in reason


def test_platform_skip_reason_compatible_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """宽松约束（``>=3.8``）在 Linux 有 3.10+ 可用，可构建返回 None."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    assert _platform_skip_reason(_tpl("helloworld", ">=3.8")) is None


def test_platform_skip_reason_pyside2_ok_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 ``>=3.8,<3.10`` 约束在 Windows 有 3.8/3.9 embed 可用，不跳过."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    assert _platform_skip_reason(_tpl("pyside2", ">=3.8,<3.10")) is None


def test_platform_skip_reason_unsatisfiable_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何平台版本表都无法满足的约束（``>=3.15``）给出跳过原因."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.WINDOWS)
    reason = _platform_skip_reason(_tpl("future", ">=3.15"))
    assert reason is not None
    assert "requires-python" in reason


def test_filter_platform_supported_splits(monkeypatch: pytest.MonkeyPatch) -> None:
    """混合列表按平台兼容性拆分为 (可构建, 跳过及原因)."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    ok1, ok2 = _tpl("a", ">=3.8"), _tpl("b", ">=3.10,<3.14")
    skip1 = _tpl("pyside2", ">=3.8,<3.10")
    buildable, skipped = _filter_platform_supported([ok1, skip1, ok2])
    assert buildable == [ok1, ok2]
    assert [tpl for tpl, _ in skipped] == [skip1]
    assert all("requires-python" in reason for _, reason in skipped)


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
    monkeypatch.setattr("fspack.doctor.shutil.which", lambda name: "/usr/bin/wine")
    exe = tmp_path / "app.exe"
    assert _build_run_cmd(exe) == ["/usr/bin/wine", str(exe)]


def test_build_run_cmd_exe_on_linux_no_wine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 下 .exe 但 wine 未安装时回退字符串 'wine'（_run_template 捕获 OSError）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("fspack.doctor.shutil.which", lambda name: None)
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

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
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
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
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
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is False
    assert result.timed_out is False
    assert result.exit_code == 1
    assert "ModuleNotFoundError" in result.error


def test_run_template_failure_empty_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程退出码非 0 且 stderr 为空时 error 显示退出码."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(returncode=2, stdout="", stderr="")
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
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
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
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
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
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
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", _raise_popen)
    result = _run_template(["wine", str(tmp_path / "app.exe")], timeout=1.0)
    assert result.success is False
    assert result.timed_out is False
    assert result.exit_code is None
    assert "启动失败" in result.error
    assert "wine" in result.error


def test_run_template_passes_env_to_popen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 模式传入 env 时透传给 Popen，并追加 FSPACK_TIMING=1 激活自终止钩子."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    captured: dict[str, object] = {}
    proc = _FakeProc(returncode=0)

    def _capture_popen(*args: object, **kwargs: object) -> _FakeProc:
        captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr("fspack.doctor.subprocess.Popen", _capture_popen)
    env = {"PYTHONHOME": "/tmp/python", "PYTHONUNBUFFERED": "1"}
    _run_template([str(tmp_path / "app")], env, timeout=1.0)  # type: ignore[arg-type]
    assert captured["env"] == {**env, "FSPACK_TIMING": "1"}


def test_run_template_env_none_injects_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """env=None 继承当前环境，同样追加 FSPACK_TIMING=1（回退直跑 exe 场景）."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    captured: dict[str, object] = {}
    proc = _FakeProc(returncode=0)

    def _capture_popen(*args: object, **kwargs: object) -> _FakeProc:
        captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr("fspack.doctor.subprocess.Popen", _capture_popen)
    _run_template([str(tmp_path / "app")], timeout=1.0)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FSPACK_TIMING"] == "1"
    # 基于当前环境复制（PATH 等原键保留），未误用 env=None 继承语义
    assert env.get("PATH") == os.environ.get("PATH")


def test_run_template_failure_skips_timing_marker_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非零退出时 stderr 的 ``[fspack`` 打点行被跳过，error 取首个真实错误行."""
    monkeypatch.setattr("fspack.platform.detect_platform", lambda: Platform.LINUX)
    proc = _FakeProc(
        returncode=1,
        stdout="",
        stderr=(
            "[fspack timing] env_ready @1.0ms\n[fspack timing] gui_ready @50.0ms\nValueError: boom\nTraceback follows\n"
        ),
    )
    monkeypatch.setattr("fspack.doctor.subprocess.Popen", lambda *a, **kw: proc)
    result = _run_template([str(tmp_path / "app")], timeout=1.0)
    assert result.success is False
    assert "ValueError: boom" in result.error
    assert "fspack timing" not in result.error


# ---- _copy_ignore（模板复制过滤）----


def test_copy_ignore_filters_dev_and_build_residue() -> None:
    """回调过滤本地开发/构建残留：node_modules/dist/deploy/缓存目录与编译产物.

    模板源目录中的这些条目均为本地测试残留（git 已忽略），带入 doctor 临时
    环境会破坏构建语义（node_modules 触发 pnpm 交互式询问挂起、dist/deploy
    非空让前端阶段误判产物就绪跳过构建）。
    """
    from fspack.doctor.templates import _copy_ignore

    names = ["node_modules", "dist", "deploy", "__pycache__", ".venv", ".git", "a.pyc", "b.pyo", "c.pyd"]
    assert _copy_ignore("src", names) == set(names)

    kept = ["main.py", "package.json", "src", "public", "pyproject.toml"]
    assert _copy_ignore("src", kept) == set()


def test_copytree_with_copy_ignore_skips_residue(tmp_path: Path) -> None:
    """copytree 挂载 _copy_ignore 后残留目录不进入目标，正常源文件完整复制."""
    import shutil as _shutil

    from fspack.doctor.templates import _copy_ignore

    src = tmp_path / "tpl"
    (src / "src" / "app").mkdir(parents=True)
    (src / "src" / "app" / "main.py").write_text('print("hi")', encoding="utf-8")
    (src / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (src / "node_modules" / "vue").mkdir(parents=True)
    (src / "node_modules" / "vue" / "package.json").write_text("{}", encoding="utf-8")
    (src / "dist").mkdir()
    (src / "dist" / "app.exe").write_bytes(b"bin")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "m.pyc").write_bytes(b"pyc")

    dst = tmp_path / "dst"
    _shutil.copytree(src, dst, ignore=_copy_ignore)

    assert (dst / "src" / "app" / "main.py").read_text(encoding="utf-8") == 'print("hi")'
    assert (dst / "pyproject.toml").is_file()
    assert not (dst / "node_modules").exists()
    assert not (dst / "dist").exists()
    assert not (dst / "__pycache__").exists()


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


# --- Win7 兼容自检（doctor.win7）---


def _patch_win7_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_dir: Path) -> None:
    """隔离 win7 自检环境：缓存目录与 shim 资产均指向临时路径（故障注入访问私有常量）."""
    import fspack.doctor.win7 as doctor_win7

    monkeypatch.setattr(doctor_win7, "win7_dll_cache_dir", lambda: cache_dir)
    shim = tmp_path / "api-ms-win-core-path-l1-1-0.dll"
    shim.write_bytes(b"shim")
    monkeypatch.setattr(doctor_win7, "WIN7_SHIM_DLL_PATH", shim)


def test_check_win7_compat_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无缓存时 OK：清单对齐、shim 就绪、提示首次打包自动下载."""
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    _patch_win7_env(monkeypatch, tmp_path, cache)

    result = _check_win7_compat()

    assert result.status is CheckStatus.OK
    assert "暂无缓存" in result.detail


def test_check_win7_compat_cached_zip_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存 zip 哈希与清单一致时 OK，detail 报告校验通过数."""
    import hashlib

    import fspack.doctor.win7 as doctor_win7
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    _patch_win7_env(monkeypatch, tmp_path, cache)
    version = next(iter(doctor_win7.WIN7_EMBED_SHA256))
    data = b"win7-embed-zip"
    (cache / doctor_win7.win7_zip_cache_name(version)).write_bytes(data)
    monkeypatch.setitem(doctor_win7.WIN7_EMBED_SHA256, version, hashlib.sha256(data).hexdigest())

    result = _check_win7_compat()

    assert result.status is CheckStatus.OK
    assert "缓存 1 个 zip 校验通过" in result.detail


def test_check_win7_compat_cached_zip_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存 zip 哈希不匹配时 WARN，建议删除后重新构建自动重下."""
    import fspack.doctor.win7 as doctor_win7
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    _patch_win7_env(monkeypatch, tmp_path, cache)
    version = next(iter(doctor_win7.WIN7_EMBED_SHA256))
    (cache / doctor_win7.win7_zip_cache_name(version)).write_bytes(b"corrupted-bytes")

    result = _check_win7_compat()

    assert result.status is CheckStatus.WARN
    assert "哈希不匹配" in result.detail
    assert "删除" in result.suggestion


def test_check_win7_compat_manifest_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """清单缺失 3.12+ 版本时 ERROR（版本升级遗漏）."""
    import fspack.doctor.win7 as doctor_win7
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    _patch_win7_env(monkeypatch, tmp_path, cache)
    # 删掉一个 3.12+ 版本条目模拟升级 KNOWN_EMBED_VERSIONS 后忘同步清单
    # delitem 在 teardown 自动恢复，避免污染全局清单 dict
    monkeypatch.delitem(doctor_win7.WIN7_EMBED_SHA256, "3.12.10")

    result = _check_win7_compat()

    assert result.status is CheckStatus.ERROR
    assert "3.12.10" in result.detail


def test_check_win7_compat_shim_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内置 shim 资产缺失时 ERROR（3.9+ 打包必需）."""
    import fspack.doctor.win7 as doctor_win7
    from fspack.doctor.win7 import _check_win7_compat

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(doctor_win7, "win7_dll_cache_dir", lambda: cache)
    monkeypatch.setattr(doctor_win7, "WIN7_SHIM_DLL_PATH", tmp_path / "not-exist.dll")

    result = _check_win7_compat()

    assert result.status is CheckStatus.ERROR
    assert "shim 缺失" in result.detail


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
