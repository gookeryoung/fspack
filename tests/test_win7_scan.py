"""win7_scan 模块测试：dist 全量扫描聚合、报告渲染与 loader exe 硬门禁.

合成 PE 复用 test_win7_check 的 ``_build_pe`` 构造器（tests 为包，跨模块导入）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.packaging.win7_scan import (
    Win7ScanError,
    enforce_win7_loaders,
    render_win7_report,
    scan_dist_win7,
    write_win7_report,
)
from tests.test_win7_check import _build_pe

# 内置 shim 实际导出的 PathCch* 函数（test_win7_check 已实测覆盖）
_SHIM_EXPORTED = "PathCchSkipRoot"

_OK_IMPORTS: dict[str, list[str]] = {"KERNEL32.dll": ["GetModuleHandleW", "CreateFileW"]}
_BAD_IMPORTS: dict[str, list[str]] = {"KERNEL32.dll": ["CopyFile2"]}


def _write_pe(path: Path, imports: dict[str, list[str]] | None = None) -> Path:
    """在 path 写入合成 PE（默认 Win7 干净导入表），父目录自动创建."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_build_pe(imports if imports is not None else _OK_IMPORTS))
    return path


def test_scan_empty_dist(tmp_path: Path) -> None:
    """空 dist 扫描结果为零计数且 ok."""
    report = scan_dist_win7(tmp_path)
    assert report.scanned == 0
    assert report.violations == ()
    assert report.skipped == ()
    assert report.ok


def test_scan_aggregates_violations_and_skips(tmp_path: Path) -> None:
    """违规聚合、非 PE 跳过、build/release 子目录排除."""
    _write_pe(tmp_path / "runtime" / "python312.dll")
    _write_pe(tmp_path / "site-packages" / "_rust.pyd", _BAD_IMPORTS)
    _write_pe(tmp_path / "app.exe")
    (tmp_path / "runtime" / "fake.dll").write_bytes(b"not-a-pe")
    # build/release 子目录排除（即使违规也不计入）
    _write_pe(tmp_path / "build" / "tmp.dll", _BAD_IMPORTS)
    _write_pe(tmp_path / "release" / "r.dll", _BAD_IMPORTS)

    report = scan_dist_win7(tmp_path)

    assert report.scanned == 3
    assert report.skipped == ("fake.dll",)
    assert not report.ok
    assert len(report.violations) == 1
    assert report.violations[0].path.name == "_rust.pyd"
    assert [v.target for v in report.violations[0].violations] == ["KERNEL32.dll!CopyFile2"]


def test_scan_counts_shim_and_ucrt_files(tmp_path: Path) -> None:
    """需 shim 与依赖 UCRT 的文件分别计数（运行前提提示，不算违规）."""
    _write_pe(tmp_path / "python311.dll", {"api-ms-win-core-path-l1-1-0.dll": [_SHIM_EXPORTED]})
    _write_pe(tmp_path / "vcruntime.pyd", {"api-ms-win-crt-runtime-l1-1-0.dll": ["memcpy"]})

    report = scan_dist_win7(tmp_path)

    assert report.scanned == 2
    assert report.ok
    assert report.shim_files == 1
    assert report.ucrt_files == 1


def test_scan_shim_none_skips_coverage(tmp_path: Path) -> None:
    """shim=None 跳过覆盖校验（导入任意 PathCch* 也不判 shim_missing）."""
    _write_pe(tmp_path / "python311.dll", {"api-ms-win-core-path-l1-1-0.dll": ["PathCchNotExists"]})

    report = scan_dist_win7(tmp_path, shim=None)

    assert report.ok
    assert report.shim_files == 1


def test_render_report_all_pass(tmp_path: Path) -> None:
    """全部通过时报告含通过计数与结论行."""
    _write_pe(tmp_path / "app.exe")
    report = scan_dist_win7(tmp_path)
    text = render_win7_report(report)
    assert "扫描文件: 1（通过 1，违规 0，跳过 0）" in text
    assert "全部通过" in text


def test_render_report_lists_violations(tmp_path: Path) -> None:
    """违规报告含相对路径、API 明细与不阻断结论."""
    dll = _write_pe(tmp_path / "site-packages" / "_rust.pyd", _BAD_IMPORTS)
    report = scan_dist_win7(tmp_path)
    text = render_win7_report(report)
    assert f"[违规] {dll.relative_to(tmp_path)}" in text
    assert "KERNEL32.dll!CopyFile2 — Win8+ API，Win7 SP1 不存在" in text
    assert "不阻断构建" in text


def test_render_report_tips_lines(tmp_path: Path) -> None:
    """shim/UCRT/跳过文件各有独立提示行."""
    _write_pe(tmp_path / "python311.dll", {"api-ms-win-core-path-l1-1-0.dll": [_SHIM_EXPORTED]})
    _write_pe(tmp_path / "vc.pyd", {"api-ms-win-crt-runtime-l1-1-0.dll": ["memcpy"]})
    (tmp_path / "broken.dll").write_bytes(b"x" * 16)
    report = scan_dist_win7(tmp_path)
    text = render_win7_report(report)
    assert "1 个文件需 api-ms-win-core-path shim" in text
    assert "1 个文件依赖 api-ms-win-crt-*" in text
    assert "跳过非 PE 文件: broken.dll" in text


def test_write_win7_report_to_release(tmp_path: Path) -> None:
    """报告写入 dist/release/win7-compat-report.txt 并返回路径."""
    _write_pe(tmp_path / "app.exe")
    report = scan_dist_win7(tmp_path)
    out = write_win7_report(report, tmp_path)
    assert out == tmp_path / "release" / "win7-compat-report.txt"
    assert out.is_file()
    assert out.read_text(encoding="utf-8").startswith("Win7 兼容性扫描报告")


def test_enforce_win7_loaders_ok(tmp_path: Path) -> None:
    """Win7 干净的 loader exe 全部通过（含缓存命中复检场景）."""
    exes = [_write_pe(tmp_path / f"{name}.exe") for name in ("cli", "gui")]
    enforce_win7_loaders(exes)


def test_enforce_win7_loaders_blocks(tmp_path: Path) -> None:
    """违规 exe 抛 Win7ScanError 且消息含 API 明细."""
    exe = _write_pe(tmp_path / "app.exe", _BAD_IMPORTS)
    with pytest.raises(Win7ScanError, match=r"KERNEL32\.dll!CopyFile2"):
        enforce_win7_loaders([exe])


def test_enforce_win7_loaders_reports_all_failures(tmp_path: Path) -> None:
    """多个违规 exe 的明细全部列在异常消息中."""
    exes = [
        _write_pe(tmp_path / "cli.exe", _BAD_IMPORTS),
        _write_pe(tmp_path / "gui.exe", {"KERNEL32.dll": ["PssCaptureSnapshot"]}),
    ]
    with pytest.raises(Win7ScanError) as exc_info:
        enforce_win7_loaders(exes)
    message = str(exc_info.value)
    assert "cli.exe" in message
    assert "gui.exe" in message
    assert "PssCaptureSnapshot" in message


def test_enforce_win7_loaders_skips_non_pe(tmp_path: Path) -> None:
    """非 PE 的 exe（如测试 stub 产物）告警跳过，不阻断也不影响其余 exe 校验."""
    bad = tmp_path / "stub.exe"
    bad.write_bytes(b"MZ-truncated")
    good = _write_pe(tmp_path / "app.exe")
    enforce_win7_loaders([bad, good])


def test_enforce_win7_loaders_missing_file_skipped(tmp_path: Path) -> None:
    """文件缺失（测试 stub 未生成 exe）同样告警跳过，不抛错."""
    enforce_win7_loaders([tmp_path / "not-exist.exe"])
