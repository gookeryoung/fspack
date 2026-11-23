"""``pipeline/dist_helpers.py`` 测试：clean_dist、半成品检测、构建失败标记与 .build_ok 完成标记."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fspack.builder import (
    build,
    clean_dist,
)
from fspack.config import get_mirror
from fspack.packaging.pipeline import (
    _BUILD_FAILED,
    _BUILD_OK,
    _clean_dist_dir,
    _handle_dist_incomplete,
    _has_build_stamps,
    _load_build_failure,
    _remove_build_failure,
    _remove_build_ok,
    _save_build_failure,
    _save_build_ok,
)
from fspack.platform import Platform
from tests._stubs import CompletedStub, setup_embed_mocks

# --- clean_dist 测试（原 tests/test_commands.py 的 clean 测试） ---


def test_clean_dist_removes_dist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "x.txt").write_text("x")
    clean_dist(tmp_path)
    # 无保留文件时 dist 整体移除（不重建空目录），清理彻底
    assert not dist.exists()


def test_clean_dist_preserves_nsi(tmp_path: Path) -> None:
    """clean 保留 installer.nsi 便于改代码后重新打包分发."""
    dist = tmp_path / "dist"
    dist.mkdir()
    nsi = dist / "installer.nsi"
    nsi.write_text('Name "app"', encoding="utf-8")
    (dist / "x.txt").write_text("x")
    clean_dist(tmp_path)
    assert dist.is_dir()
    assert nsi.is_file()
    assert nsi.read_text(encoding="utf-8") == 'Name "app"'
    assert not (dist / "x.txt").exists()


def test_clean_dist_no_dist(tmp_path: Path) -> None:
    clean_dist(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH 260 长路径场景")
def test_clean_dist_removes_over_maxpath(tmp_path: Path) -> None:
    """dist 内含超 MAX_PATH 260 的深层路径时 clean 仍能整体删除.

    场景来源：模板 frontend/node_modules/.pnpm 下路径超 260，普通
    ``shutil.rmtree`` 抛 ``WinError 3`` 中途残留（fsp c 清理失败的根因）。
    """
    deep = tmp_path / "dist"
    for i in range(18):
        deep = deep / f"level_{i:02d}_padding_padding_padding"
    assert len(str(deep)) > 260  # 前置：确认已触发长路径场景
    Path("\\\\?\\" + str(deep)).mkdir(parents=True)
    (Path("\\\\?\\" + str(deep)) / "f.js").write_text("x")

    clean_dist(tmp_path)
    assert not (tmp_path / "dist").exists()


# --- _handle_dist_incomplete 测试（iter-140 扩展 iter-130 dist 半成品检测） ---


def test_handle_dist_incomplete_no_dist(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 目录不存在时不告警."""
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(tmp_path / "nonexistent", auto_clean=False)
    assert not caplog.records


def test_handle_dist_incomplete_empty_dist(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 目录为空时不告警（无构建产物）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert not caplog.records


def test_handle_dist_incomplete_only_nsi(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 仅含 installer.nsi（clean_dist 保留）时不告警."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert not caplog.records


def test_handle_dist_incomplete_artifacts_no_stamp_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含构建产物但无 stamp 文件时告警（中断/失败的上次构建）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "src").mkdir()
    (dist / "app.exe").write_bytes(b"")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert any("残留" in r.message and "fsp c" in r.message for r in caplog.records)


def test_handle_dist_incomplete_with_pyc_stamp_no_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含产物且有 .pyc_stamp 时不告警（上次构建至少完成到编译阶段）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "src").mkdir()
    (dist / ".pyc_stamp").write_text("fingerprint", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert not caplog.records


def test_handle_dist_incomplete_with_nuitka_stamp_no_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含产物且有 .nuitka_compile_stamp 时不告警."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / ".nuitka_compile_stamp").write_text("fingerprint", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)
    assert not caplog.records


# --- _handle_dist_incomplete auto_clean 与 .build_failed 测试（iter-140） ---


def test_handle_dist_incomplete_auto_clean_removes_artifacts(tmp_path: Path) -> None:
    """auto_clean=True 时清空 dist 残留产物（不保留 .build_failed）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "src").mkdir()
    (dist / "app.exe").write_bytes(b"")
    (dist / _BUILD_FAILED).write_text('{"stage":"x"}', encoding="utf-8")

    _handle_dist_incomplete(dist, auto_clean=True)

    # 无保留文件（无 installer.nsi）时 dist 整体移除
    assert not dist.exists()


def test_handle_dist_incomplete_auto_clean_preserves_nsi(tmp_path: Path) -> None:
    """auto_clean=True 仍保留 installer.nsi（便于重新打包）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")

    _handle_dist_incomplete(dist, auto_clean=True)

    assert (dist / "installer.nsi").read_text(encoding="utf-8") == 'Name "app"'
    assert not (dist / "runtime").exists()


def test_handle_dist_incomplete_build_failed_shows_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """dist 含 .build_failed 时输出失败阶段与错误信息."""

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text(
        json.dumps({"stage": "编译源码", "error": "NuitkaError: compile failed", "timestamp": "2026-08-04T21:00:00"}),
        encoding="utf-8",
    )

    _handle_dist_incomplete(dist, auto_clean=False)

    assert any("残留" in r.message for r in caplog.records)


def test_handle_dist_incomplete_build_failed_auto_clean_removes_it(tmp_path: Path) -> None:
    """auto_clean=True 时 .build_failed 也被清除（全新开始）."""

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text(
        json.dumps({"stage": "编译源码", "error": "failed"}),
        encoding="utf-8",
    )

    _handle_dist_incomplete(dist, auto_clean=True)

    assert not (dist / _BUILD_FAILED).exists()


def test_handle_dist_incomplete_no_artifacts_with_build_failed_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """dist 无产物但有 .build_failed 时仍视为半成品并告警."""

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text(
        json.dumps({"stage": "下载依赖", "error": "NetworkError"}),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        _handle_dist_incomplete(dist, auto_clean=False)

    assert any("残留" in r.message for r in caplog.records)


# --- _save/_load/_remove_build_failure 测试（iter-140） ---


def test_save_build_failure_writes_json(tmp_path: Path) -> None:
    """_save_build_failure 写入 JSON 含 stage/error/timestamp."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    dist = tmp_path / "dist"
    dist.mkdir()

    tracker = MagicMock()
    # SimpleNamespace 而非 MagicMock(name=...)：MagicMock 的 name 参数设置 repr
    # 名称而非属性，records[-1].name 返回 MagicMock 无法 JSON 序列化
    tracker.records = [SimpleNamespace(name="解析项目"), SimpleNamespace(name="下载依赖")]

    exc = RuntimeError("test error")
    _save_build_failure(dist, tracker, exc)

    data = json.loads((dist / _BUILD_FAILED).read_text(encoding="utf-8"))
    assert data["stage"] == "下载依赖"
    assert "RuntimeError" in data["error"]
    assert "test error" in data["error"]
    assert "timestamp" in data


def test_save_build_failure_no_records_uses_unknown(tmp_path: Path) -> None:
    """tracker.records 为空时 stage 记为'未知'."""
    from unittest.mock import MagicMock

    dist = tmp_path / "dist"
    dist.mkdir()

    tracker = MagicMock()
    tracker.records = []  # type: ignore[list-item]

    _save_build_failure(dist, tracker, ValueError("err"))

    data = json.loads((dist / _BUILD_FAILED).read_text(encoding="utf-8"))
    assert data["stage"] == "未知"


def test_save_build_failure_truncates_long_error(tmp_path: Path) -> None:
    """错误信息超 500 字符时截断."""
    from unittest.mock import MagicMock

    dist = tmp_path / "dist"
    dist.mkdir()

    tracker = MagicMock()
    tracker.records = []  # type: ignore[list-item]
    long_msg = "x" * 600
    _save_build_failure(dist, tracker, RuntimeError(long_msg))

    data = json.loads((dist / _BUILD_FAILED).read_text(encoding="utf-8"))
    assert len(data["error"]) <= 500
    assert data["error"].endswith("...")


def test_save_build_failure_dist_not_exists_skips(tmp_path: Path) -> None:
    """dist 目录不存在时跳过写入（构建可能在创建 dist 前失败）."""
    from unittest.mock import MagicMock

    tracker = MagicMock()
    tracker.records = []  # type: ignore[list-item]

    _save_build_failure(tmp_path / "nonexistent", tracker, RuntimeError("err"))

    assert not (tmp_path / "nonexistent").exists()


def test_load_build_failure_returns_dict(tmp_path: Path) -> None:
    """_load_build_failure 读取 JSON 返回 dict."""

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text(
        json.dumps({"stage": "编译", "error": "err", "timestamp": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )

    result = _load_build_failure(dist)
    assert result is not None
    assert result["stage"] == "编译"
    assert result["error"] == "err"


def test_load_build_failure_no_file_returns_none(tmp_path: Path) -> None:
    """文件不存在时返回 None."""
    dist = tmp_path / "dist"
    dist.mkdir()

    assert _load_build_failure(dist) is None


def test_load_build_failure_invalid_json_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """JSON 解析失败返回 None 并告警."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text("not json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="fspack.packaging.pipeline"):
        result = _load_build_failure(dist)

    assert result is None


def test_remove_build_failure_deletes_file(tmp_path: Path) -> None:
    """_remove_build_failure 删除 .build_failed 文件."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _BUILD_FAILED).write_text("{}", encoding="utf-8")

    _remove_build_failure(dist)

    assert not (dist / _BUILD_FAILED).exists()


def test_remove_build_failure_no_file_noop(tmp_path: Path) -> None:
    """文件不存在时 _remove_build_failure 无操作."""
    dist = tmp_path / "dist"
    dist.mkdir()

    _remove_build_failure(dist)  # 不抛异常


# --- _clean_dist_dir 与 clean_dist 保留诊断测试（iter-140） ---


def test_clean_dist_dir_keeps_diagnostics_preserves_build_failed(tmp_path: Path) -> None:
    """keep_diagnostics=True 时保留 .build_failed 与 installer.nsi."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / _BUILD_FAILED).write_text('{"stage":"x"}', encoding="utf-8")
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")

    _clean_dist_dir(dist, keep_diagnostics=True)

    assert (dist / _BUILD_FAILED).read_text(encoding="utf-8") == '{"stage":"x"}'
    assert (dist / "installer.nsi").read_text(encoding="utf-8") == 'Name "app"'
    assert not (dist / "runtime").exists()


def test_clean_dist_dir_no_diagnostics_removes_build_failed(tmp_path: Path) -> None:
    """keep_diagnostics=False 时删除 .build_failed（全新开始）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    (dist / _BUILD_FAILED).write_text('{"stage":"x"}', encoding="utf-8")

    _clean_dist_dir(dist, keep_diagnostics=False)

    assert not (dist / _BUILD_FAILED).exists()
    assert not (dist / "runtime").exists()


def test_clean_dist_preserves_build_failed(tmp_path: Path) -> None:
    """fsp c (clean_dist) 保留 .build_failed 便于用户排查."""
    project = tmp_path / "proj"
    dist = project / "dist"
    dist.mkdir(parents=True)
    (dist / "runtime").mkdir()
    (dist / _BUILD_FAILED).write_text('{"stage":"编译"}', encoding="utf-8")
    (dist / "installer.nsi").write_text('Name "app"', encoding="utf-8")

    clean_dist(project)

    assert (dist / _BUILD_FAILED).read_text(encoding="utf-8") == '{"stage":"编译"}'
    assert (dist / "installer.nsi").read_text(encoding="utf-8") == 'Name "app"'
    assert not (dist / "runtime").exists()


# --- .build_ok 完成标记测试（no_pyc/交叉构建二次构建误判修复） ---


def test_has_build_stamps_recognizes_build_ok(tmp_path: Path) -> None:
    """仅存在 .build_ok（无编译 stamp）时也视为已完成构建（no_pyc/交叉构建场景）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    assert not _has_build_stamps(dist)

    _save_build_ok(dist)
    assert (dist / _BUILD_OK).is_file()
    assert _has_build_stamps(dist)


def test_remove_build_ok_deletes_marker(tmp_path: Path) -> None:
    """_remove_build_ok 删除标记文件（构建开始时清旧标记），不存在时无操作."""
    dist = tmp_path / "dist"
    dist.mkdir()
    _save_build_ok(dist)
    _remove_build_ok(dist)
    assert not (dist / _BUILD_OK).is_file()
    _remove_build_ok(dist)  # 不抛异常


def test_clean_dist_dir_keeps_diagnostics_removes_build_ok(tmp_path: Path) -> None:
    """.build_ok 是完成标记而非诊断信息，随清理删除（避免空 dist 被误判有效）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    _save_build_ok(dist)

    _clean_dist_dir(dist, keep_diagnostics=True)

    # 无保留文件时 dist 整体移除，.build_ok 一并消失
    assert not dist.exists()


def test_build_success_writes_build_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build() 成功完成后写入 dist/.build_ok 并清除 .build_failed."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")

    setup_embed_mocks(tmp_path, monkeypatch, "3.11.9")
    runtime = proj / "dist" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: CompletedStub())
    monkeypatch.setattr("fspack.packaging.pipeline.stages.detect_platform", lambda: Platform.WINDOWS)

    build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    assert (proj / "dist" / _BUILD_OK).is_file()
    assert not (proj / "dist" / _BUILD_FAILED).exists()


def test_build_keyboard_interrupt_writes_build_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C（KeyboardInterrupt，非 Exception 子类）也写入 .build_failed 标记."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1"\n')
    (proj / "app.py").write_text("def main():\n    pass\n")
    # 预置 dist 目录：_save_build_failure 要求 dist 存在才写入
    (proj / "dist").mkdir()

    def raise_interrupt(ctx: object) -> Path:
        raise KeyboardInterrupt()

    monkeypatch.setattr("fspack.packaging.pipeline.executor._prepare_runtime", raise_interrupt)

    with pytest.raises(KeyboardInterrupt):
        build(proj, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS)

    failed = _load_build_failure(proj / "dist")
    assert failed is not None, "KeyboardInterrupt 未写入 .build_failed"
    assert "KeyboardInterrupt" in failed["error"]
    assert not (proj / "dist" / _BUILD_OK).exists()
