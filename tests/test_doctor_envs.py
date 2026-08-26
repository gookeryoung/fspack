"""doctor/envs.py 测试：_format_size、_dir_size、_check_cache_dir 与缓存完整性检查."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from fspack.console import console
from fspack.doctor import (
    CheckStatus,
    _check_cache_dir,
    _dir_size,
    _format_size,
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
