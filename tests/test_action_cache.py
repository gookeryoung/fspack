"""通用增量 action 缓存（``packaging/action_cache.py``）单元测试."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fspack.packaging import action_cache


def test_fingerprint_stable_and_order_insensitive() -> None:
    """dict 字段顺序不影响指纹，输入变化则指纹变化."""
    a = action_cache.fingerprint("x", {"b": 2, "a": 1}, [1, 2])
    b = action_cache.fingerprint("x", {"a": 1, "b": 2}, [1, 2])
    assert a == b
    assert action_cache.fingerprint("x", {"a": 1, "b": 3}, [1, 2]) != a


def test_fingerprint_accepts_path() -> None:
    """Path 等不可 JSON 化对象按 str 处理（同一路径稳定得到同一指纹）."""
    p = Path("a") / "b"
    assert action_cache.fingerprint(p) == action_cache.fingerprint(str(p))


def test_marker_path() -> None:
    assert action_cache.marker_path(Path("dist/build"), "icon") == Path("dist/build/action-icon.json")


def test_save_load_roundtrip_with_payload(tmp_path: Path) -> None:
    """payload 原样往返，键与版本写入记录."""
    assert action_cache.save(tmp_path / "build", "demo", "k1", payload={"files": {"a": {"k": "1"}}})
    rec = action_cache.load(tmp_path / "build", "demo")
    assert rec is not None
    assert rec["version"] == action_cache.ACTION_CACHE_VERSION
    assert rec["key"] == "k1"
    assert rec["payload"] == {"files": {"a": {"k": "1"}}}


def test_save_creates_missing_dir(tmp_path: Path) -> None:
    """缓存目录不存在时自动创建."""
    target = tmp_path / "deep" / "build"
    assert action_cache.save(target, "demo", "k")
    assert (target / "action-demo.json").is_file()


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert action_cache.load(tmp_path, "nope") is None


def test_load_corrupt_discards(tmp_path: Path) -> None:
    """损坏标记按未命中处理并删除，避免每次构建重复解析坏文件."""
    path = action_cache.marker_path(tmp_path, "demo")
    path.write_text("{bad", encoding="utf-8")
    assert action_cache.load(tmp_path, "demo") is None
    assert not path.exists()


def test_load_version_mismatch_discards(tmp_path: Path) -> None:
    """版本不符视为未命中（缓存语义已变化）."""
    path = action_cache.marker_path(tmp_path, "demo")
    path.write_text(json.dumps({"version": 0, "key": "k"}), encoding="utf-8")
    assert action_cache.load(tmp_path, "demo") is None
    assert not path.exists()


def test_load_record_without_key_discards(tmp_path: Path) -> None:
    path = action_cache.marker_path(tmp_path, "demo")
    path.write_text(json.dumps({"version": action_cache.ACTION_CACHE_VERSION}), encoding="utf-8")
    assert action_cache.load(tmp_path, "demo") is None


def test_load_non_dict_discards(tmp_path: Path) -> None:
    path = action_cache.marker_path(tmp_path, "demo")
    path.write_text("[1, 2]", encoding="utf-8")
    assert action_cache.load(tmp_path, "demo") is None


def test_save_failure_returns_false(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """写入失败（目标路径被文件占用）只告警不抛异常."""
    blocker = tmp_path / "build"
    blocker.write_text("x", encoding="utf-8")
    assert action_cache.save(blocker, "demo", "k") is False
    assert "action" in caplog.text or caplog.records
