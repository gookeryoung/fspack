"""doctor/tools.py 测试：_check_tool_version 通用检查与 _check_pillow/_check_pip 具体工具检查."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from fspack.console import console
from fspack.doctor import (
    CheckStatus,
    _check_pillow,
    _check_pip,
    _check_tool_version,
)


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
