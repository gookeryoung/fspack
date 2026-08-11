"""``fspack._util`` 公用工具子包测试：格式化、目录大小、原子写、JSON 缓存骨架."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fspack._util.format import format_bytes_dec, format_size_bin
from fspack._util.fsutil import (
    atomic_write_text,
    dir_size_with_count,
    safe_unlink,
    scandir_dir_size,
    scandir_tree,
    walk_dir_size,
)
from fspack._util.jsoncache import load_json_dict

# --- format.py -----------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (1024 * 1024 * 1024, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1.0 PiB"),
    ],
)
def test_format_size_bin(size: int, expected: str) -> None:
    """二进制单位带空格格式化（doctor 契约）."""
    assert format_size_bin(size) == expected


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (1024 * 1024, "1.0MB"),
        (1024 * 1024 * 1024, "1.00GB"),
    ],
)
def test_format_bytes_dec(n: int, expected: str) -> None:
    """十进制风格无空格格式化（progress/size_report 契约）."""
    assert format_bytes_dec(n) == expected


# --- fsutil: 目录大小 -----------------------------------------------------


def _make_tree(root: Path) -> None:
    """构造测试目录树：root/a.txt(3B) + root/sub/b.txt(5B)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_bytes(b"abc")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.txt").write_bytes(b"hello")


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


# --- fsutil: atomic_write_text -------------------------------------------


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


# --- fsutil: safe_unlink -------------------------------------------------


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


# --- jsoncache: load_json_dict -------------------------------------------


def test_load_json_dict_reads_valid_dict(tmp_path: Path) -> None:
    """合法 JSON dict 正确解析返回."""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"k": "v", "n": 1}), encoding="utf-8")
    assert load_json_dict(path) == {"k": "v", "n": 1}


def test_load_json_dict_missing_returns_none(tmp_path: Path) -> None:
    """文件不存在返回 None（不告警）."""
    assert load_json_dict(tmp_path / "missing.json") is None


def test_load_json_dict_corrupt_deletes_by_default(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """内容损坏默认删除文件并返回 None."""
    path = tmp_path / "c.json"
    path.write_text("{bad json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack._util.jsoncache"):
        assert load_json_dict(path) is None
    assert not path.exists()
    assert any("JSON 缓存损坏" in r.message for r in caplog.records)


def test_load_json_dict_corrupt_keeps_when_delete_false(tmp_path: Path) -> None:
    """delete_on_corrupt=False 时损坏不删除."""
    path = tmp_path / "c.json"
    path.write_text("{bad json", encoding="utf-8")
    assert load_json_dict(path, delete_on_corrupt=False) is None
    assert path.exists()


def test_load_json_dict_non_dict_root_treated_corrupt(tmp_path: Path) -> None:
    """根对象非 dict（如 list）视为损坏，删除并返回 None."""
    path = tmp_path / "c.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_json_dict(path) is None
    assert not path.exists()


def test_load_json_dict_invalid_utf8_treated_corrupt(tmp_path: Path) -> None:
    """非法 UTF-8 字节（UnicodeDecodeError 是 ValueError 子类）视为损坏删除."""
    path = tmp_path / "c.json"
    path.write_bytes(b"\xff\xfe{bad}")
    assert load_json_dict(path) is None
    assert not path.exists()


def test_load_json_dict_oserror_keeps_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """读取抛非 FileNotFoundError 的 OSError 时不删除文件."""
    path = tmp_path / "c.json"
    path.write_text('{"k": "v"}', encoding="utf-8")

    def raise_oserror(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("io error")

    monkeypatch.setattr(Path, "read_text", raise_oserror)
    assert load_json_dict(path) is None
    assert path.exists()


def test_load_json_dict_delete_failure_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """损坏文件删除失败仅告警不抛."""
    path = tmp_path / "c.json"
    path.write_text("{bad", encoding="utf-8")

    def raise_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("cannot delete")

    monkeypatch.setattr(Path, "unlink", raise_unlink)
    with caplog.at_level("WARNING", logger="fspack._util.jsoncache"):
        assert load_json_dict(path) is None
    assert any("删除损坏缓存文件失败" in r.message for r in caplog.records)


def test_load_json_dict_custom_logger(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """传入自定义 logger 时用该 logger 记录告警."""
    path = tmp_path / "c.json"
    path.write_text("{bad", encoding="utf-8")
    logger = logging.getLogger("test.jsoncache")
    with caplog.at_level("WARNING", logger="test.jsoncache"):
        load_json_dict(path, logger=logger)
    assert any(r.name == "test.jsoncache" for r in caplog.records)
