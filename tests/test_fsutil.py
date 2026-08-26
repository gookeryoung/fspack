"""``fspack.fsutil`` 文件系统工具测试：目录大小、原子写、安全删除、长路径删除."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from fspack.fsutil import (
    atomic_write_text,
    dir_size_with_count,
    rmtree_longpath,
    safe_unlink,
    scandir_dir_size,
    scandir_tree,
    walk_dir_size,
)


def _make_tree(root: Path) -> None:
    """构造测试目录树：root/a.txt(3B) + root/sub/b.txt(5B)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_bytes(b"abc")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.txt").write_bytes(b"hello")


# --- 目录大小 --------------------------------------------------------------


def test_walk_dir_size(tmp_path: Path) -> None:
    """walk_dir_size 返回递归总字节数."""
    root = tmp_path / "tree"
    _make_tree(root)
    assert walk_dir_size(root) == 8


def test_scandir_dir_size(tmp_path: Path) -> None:
    """scandir_dir_size 返回递归总字节数（与 walk 一致）."""
    root = tmp_path / "tree"
    _make_tree(root)
    assert scandir_dir_size(root) == 8


def test_dir_size_with_count(tmp_path: Path) -> None:
    """dir_size_with_count 返回 (总字节数, 文件数)."""
    root = tmp_path / "tree"
    _make_tree(root)
    assert dir_size_with_count(root) == (8, 2)


def test_dir_size_with_count_non_dir_returns_zero(tmp_path: Path) -> None:
    """非目录路径返回 (0, 0)."""
    f = tmp_path / "file.txt"
    f.write_bytes(b"x")
    assert dir_size_with_count(f) == (0, 0)


def test_scandir_tree_sorted_and_files_only(tmp_path: Path) -> None:
    """scandir_tree 仅 yield 文件，按名排序深度优先."""
    root = tmp_path / "tree"
    _make_tree(root)
    names = [e.name for e in scandir_tree(root)]
    # a.txt 在 sub/b.txt 之前（a < sub 排序，深度优先在 sub 内展开）
    assert names == ["a.txt", "b.txt"]


def test_scandir_tree_missing_root_yields_nothing(tmp_path: Path) -> None:
    """根目录不存在时不 yield（OSError 静默跳过）."""
    assert list(scandir_tree(tmp_path / "missing")) == []


# --- atomic_write_text -----------------------------------------------------


def test_atomic_write_text_creates_file(tmp_path: Path) -> None:
    """原子写入内容正确，父目录自动创建."""
    target = tmp_path / "nested" / "out.txt"
    atomic_write_text(target, "内容")
    assert target.read_text(encoding="utf-8") == "内容"


def test_atomic_write_text_overwrites(tmp_path: Path) -> None:
    """已存在文件被覆盖."""
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_cleans_temp_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """replace 失败时清理临时文件并重抛 OSError."""
    target = tmp_path / "out.txt"

    def raise_replace(self: Path, target_path: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", raise_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "content")
    # 临时文件已清理：目标目录下无 .tmp_ 残留
    assert not list(tmp_path.glob(".tmp_*"))
    assert not target.exists()


# --- safe_unlink -----------------------------------------------------------


def test_safe_unlink_removes_file(tmp_path: Path) -> None:
    """删除存在的文件."""
    f = tmp_path / "x.txt"
    f.write_bytes(b"x")
    safe_unlink(f)
    assert not f.exists()


def test_safe_unlink_oserror_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """删除失败仅告警不抛，用传入 logger 记录."""
    f = tmp_path / "x.txt"
    f.write_bytes(b"x")
    logger = logging.getLogger("test.safe_unlink")

    def raise_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", raise_unlink)
    with caplog.at_level("WARNING", logger="test.safe_unlink"):
        safe_unlink(f, logger=logger)
    assert any("删除文件失败" in r.message for r in caplog.records)


# --- rmtree_longpath -------------------------------------------------------


def test_rmtree_longpath_removes_tree(tmp_path: Path) -> None:
    """删除普通目录树（含嵌套子目录与文件）."""
    d = tmp_path / "tree"
    (d / "sub" / "deeper").mkdir(parents=True)
    (d / "sub" / "deeper" / "f.txt").write_text("x")
    (d / "top.txt").write_text("x")
    rmtree_longpath(d)
    assert not d.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH 260 长路径场景")
def test_rmtree_longpath_removes_over_maxpath(tmp_path: Path) -> None:
    """删除总长超 MAX_PATH 260 的深层目录树（node_modules/.pnpm 同类场景）.

    创建超长路径同样受 260 限制，须用 ``\\\\?\\`` 前缀 ``os.makedirs``；
    删除走 :func:`rmtree_longpath`（内部同为前缀路径）。
    """
    deep = tmp_path / "dist"
    for i in range(18):
        deep = deep / f"level_{i:02d}_padding_padding_padding"
    assert len(str(deep)) > 260  # 前置：确认已触发长路径场景
    Path("\\\\?\\" + str(deep)).mkdir(parents=True)
    (Path("\\\\?\\" + str(deep)) / "f.js").write_text("x")

    rmtree_longpath(tmp_path / "dist")
    assert not (tmp_path / "dist").exists()
