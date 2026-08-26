"""doctor/cache.py 测试：run_cache_status/run_cache_clean 命令派发、清理提示与 CLI 派发."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from fspack.console import console


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
