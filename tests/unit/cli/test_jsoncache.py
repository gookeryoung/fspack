"""``fspack.jsoncache`` JSON 缓存读取骨架测试：解析、校验、损坏处理."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fspack.jsoncache import load_json_dict


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
    with caplog.at_level("WARNING", logger="fspack.jsoncache"):
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
    with caplog.at_level("WARNING", logger="fspack.jsoncache"):
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
