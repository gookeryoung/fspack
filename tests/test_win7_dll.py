"""win7_dll 模块测试：合成 zip + monkeypatch 导入表校验，保证测试封闭.

下载经 ``urllib.request.urlopen`` 全局 patch 拦截（FakeResp），导入表校验经
``fspack.packaging.win7_dll.check_win7_imports`` patch 拦截——win7_check 自身
行为已由 test_win7_check.py 覆盖，本文件聚焦清单/下载/提取/缓存逻辑。
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from fspack.exceptions import EmbedError
from fspack.packaging import win7_dll
from fspack.packaging.win7_check import Win7ApiViolation, Win7CheckResult
from fspack.packaging.win7_dll import (
    Win7DllError,
    Win7EmbedRuntime,
    download_win7_embed,
    ensure_win7_dll,
    extract_win7_dll,
    needs_win7_dll,
    win7_dll_name,
    win7_zip_cache_name,
    win7_zip_url,
)
from fspack.progress import StageRecorder
from tests._stubs import FakeResp

# 测试锚定版本：已在 WIN7_EMBED_SHA256 清单中
_VER = "3.12.10"


def _make_zip_bytes(members: dict[str, bytes]) -> bytes:
    """在内存构造 zip，返回字节串（清单哈希按此计算）."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _patch_check_ok(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """patch 导入表校验恒通过，返回被校验的 dll 路径列表."""
    checked: list[Path] = []

    def fake_check(path: Path, *, shim: Path | None = None) -> Win7CheckResult:
        checked.append(Path(path))
        return Win7CheckResult(path=Path(path))

    monkeypatch.setattr(win7_dll, "check_win7_imports", fake_check)
    return checked


def _patch_manifest(monkeypatch: pytest.MonkeyPatch, version: str, zip_bytes: bytes) -> None:
    """把清单中 version 的哈希替换为合成 zip 的真实哈希."""
    sha = hashlib.sha256(zip_bytes).hexdigest()
    monkeypatch.setitem(win7_dll.WIN7_EMBED_SHA256, version, sha)


# --- 纯函数测试 ---


def test_needs_win7_dll_version_boundary() -> None:
    """3.11 及以下 shim 注入即可，3.12 起须替换 dll."""
    assert not needs_win7_dll("3.9.13")
    assert not needs_win7_dll("3.11.9")
    assert needs_win7_dll("3.12.0")
    assert needs_win7_dll("3.14.6")


def test_win7_dll_name() -> None:
    assert win7_dll_name("3.12.10") == "python312.dll"
    assert win7_dll_name("3.14.6") == "python314.dll"


def test_win7_zip_url_and_cache_name() -> None:
    """URL 指向 GitHub releases v{version} tag 路径；缓存名加 -win7 后缀避免与官方 zip 冲突."""
    url = win7_zip_url("3.12.10")
    assert url == ("https://github.com/adang1345/PythonVista/releases/download/v3.12.10/python-3.12.10-embed-amd64.zip")
    assert win7_zip_cache_name("3.12.10") == "python-3.12.10-embed-amd64-win7.zip"


def test_download_unknown_version_rejected(tmp_path: Path) -> None:
    """清单未收录的版本直接拒绝，不发网络请求."""
    with pytest.raises(Win7DllError, match="不在 win7 重编译版清单"):
        download_win7_embed("3.12.99", tmp_path / "cache")
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").iterdir())


# --- 下载与哈希校验测试 ---


def test_download_win7_embed_fetches_and_verifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载后 zip sha256 与清单匹配，缓存文件名带 -win7 后缀."""
    zip_bytes = _make_zip_bytes({"python312.dll": b"dll-bytes"})
    _patch_manifest(monkeypatch, _VER, zip_bytes)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(zip_bytes))
    path = download_win7_embed(_VER, tmp_path / "cache")
    assert path.name == "python-3.12.10-embed-amd64-win7.zip"
    assert path.read_bytes() == zip_bytes


def test_download_win7_embed_hash_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载内容与清单哈希不符时删除文件并抛错（防篡改/防 URL 指错版本）."""
    zip_bytes = _make_zip_bytes({"python312.dll": b"dll-bytes"})
    _patch_manifest(monkeypatch, _VER, zip_bytes)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(b"tampered"))
    with pytest.raises(EmbedError, match="sha256 校验失败"):
        download_win7_embed(_VER, tmp_path / "cache")
    assert not (tmp_path / "cache" / "python-3.12.10-embed-amd64-win7.zip").exists()


def test_download_win7_embed_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存命中且哈希匹配时复用，不发网络请求."""
    zip_bytes = _make_zip_bytes({"python312.dll": b"dll-bytes"})
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / win7_zip_cache_name(_VER)
    cached.write_bytes(zip_bytes)
    _patch_manifest(monkeypatch, _VER, zip_bytes)
    path = download_win7_embed(_VER, cache)
    assert path == cached


# --- 提取测试 ---


def test_extract_win7_dll(tmp_path: Path) -> None:
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(_make_zip_bytes({"python312.dll": b"dll", "python.exe": b"exe"}))
    dll = extract_win7_dll(zip_path, tmp_path / "dest", _VER)
    assert dll == tmp_path / "dest" / "python312.dll"
    assert dll.read_bytes() == b"dll"
    # 仅提取 dll，其余成员不落盘
    assert not (tmp_path / "dest" / "python.exe").exists()


def test_extract_win7_dll_missing_member(tmp_path: Path) -> None:
    zip_path = tmp_path / "w.zip"
    zip_path.write_bytes(_make_zip_bytes({"python.exe": b"exe"}))
    with pytest.raises(Win7DllError, match=r"缺少 python312\.dll"):
        extract_win7_dll(zip_path, tmp_path / "dest", _VER)


def test_extract_win7_dll_bad_zip_deleted(tmp_path: Path) -> None:
    """损坏 zip 抛错并删除缓存，避免下次构建反复命中损坏文件."""
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(Win7DllError, match="损坏"):
        extract_win7_dll(bad, tmp_path / "dest", _VER)
    assert not bad.exists()


# --- ensure 流程测试 ---


def test_ensure_skips_download_when_dll_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dest 已有 dll 且导入表校验通过时复用，不下载."""
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "python312.dll").write_bytes(b"existing")
    checked = _patch_check_ok(monkeypatch)
    called = {"download": False}
    monkeypatch.setattr(win7_dll, "download_win7_embed", lambda *a, **k: called.__setitem__("download", True))
    result = ensure_win7_dll(_VER, tmp_path / "cache", dest)
    assert result == dest / "python312.dll"
    assert not called["download"]
    assert checked == [dest / "python312.dll"]


def test_ensure_existing_dll_with_violations_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dest 已有 dll 但导入表含 Win8+ 依赖时拒绝复用（防误换/篡改）."""
    dest = tmp_path / "dest"
    dest.mkdir()
    dll = dest / "python312.dll"
    dll.write_bytes(b"tampered")

    def fake_check(path: Path, *, shim: Path | None = None) -> Win7CheckResult:
        return Win7CheckResult(
            path=Path(path),
            violations=(Win7ApiViolation("KERNEL32.dll!CopyFile2", "Win8+ API，Win7 SP1 不存在"),),
        )

    monkeypatch.setattr(win7_dll, "check_win7_imports", fake_check)
    with pytest.raises(Win7DllError, match=r"KERNEL32\.dll!CopyFile2"):
        ensure_win7_dll(_VER, tmp_path / "cache", dest)


def test_ensure_replace_invalid_replaces_official_dll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """replace_invalid=True 时官方 dll（校验必然违规）被静默替换为重编译版.

    打包 pipeline 场景：官方 embed 解压出的 python3XX.dll 含 Win8+ 导入，
    应下载重编译版覆盖而非报错。
    """
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "python312.dll").write_bytes(b"official-dll")
    zip_bytes = _make_zip_bytes({"python312.dll": b"win7-dll"})
    _patch_manifest(monkeypatch, _VER, zip_bytes)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(zip_bytes))

    check_calls: list[bytes] = []

    def fake_check(path: Path, *, shim: Path | None = None) -> Win7CheckResult:
        check_calls.append(Path(path).read_bytes())
        # 官方 dll 违规，重编译版通过
        ok = Path(path).read_bytes() == b"win7-dll"
        if ok:
            return Win7CheckResult(path=Path(path))
        return Win7CheckResult(
            path=Path(path),
            violations=(Win7ApiViolation("KERNEL32.dll!CopyFile2", "Win8+ API，Win7 SP1 不存在"),),
        )

    monkeypatch.setattr(win7_dll, "check_win7_imports", fake_check)
    dll = ensure_win7_dll(_VER, tmp_path / "cache", dest, replace_invalid=True)
    assert dll.read_bytes() == b"win7-dll"
    # 官方 dll 先被校验（违规→删除），重编译版提取后再校验（通过）
    assert check_calls == [b"official-dll", b"win7-dll"]


def test_ensure_downloads_extracts_and_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """完整流程：下载 → 哈希校验 → 提取 dll → 导入表校验通过."""
    zip_bytes = _make_zip_bytes({"python312.dll": b"dll-bytes", "python313._pth": b"x"})
    _patch_manifest(monkeypatch, _VER, zip_bytes)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(zip_bytes))
    checked = _patch_check_ok(monkeypatch)
    dest = tmp_path / "dest"
    result = ensure_win7_dll(_VER, tmp_path / "cache", dest)
    assert result == dest / "python312.dll"
    assert result.read_bytes() == b"dll-bytes"
    assert checked == [result]


def test_ensure_blocks_downloaded_violations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载的 dll 导入表校验不通过时抛错（上游发布损坏/污染的兜底门禁）."""
    zip_bytes = _make_zip_bytes({"python312.dll": b"bad-dll"})
    _patch_manifest(monkeypatch, _VER, zip_bytes)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(zip_bytes))

    def fake_check(path: Path, *, shim: Path | None = None) -> Win7CheckResult:
        return Win7CheckResult(
            path=Path(path),
            violations=(
                Win7ApiViolation("KERNEL32.dll!CopyFile2", "Win8+ API，Win7 SP1 不存在"),
                Win7ApiViolation("shim!PathCchCanonicalizeEx", "shim（x.dll）缺少导出"),
            ),
        )

    monkeypatch.setattr(win7_dll, "check_win7_imports", fake_check)
    with pytest.raises(Win7DllError, match=r"导入表含 Win8\+ 依赖"):
        ensure_win7_dll(_VER, tmp_path / "cache", tmp_path / "dest")
    # 已提取的违规 dll 不应残留
    assert not (tmp_path / "dest" / "python312.dll").exists()


def test_ensure_wraps_pe_parse_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dll 非 PE 镜像时包装为 Win7DllError."""
    from fspack.packaging.win7_check import PeParseError

    zip_bytes = _make_zip_bytes({"python312.dll": b"not-a-pe"})
    _patch_manifest(monkeypatch, _VER, zip_bytes)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(zip_bytes))

    def fake_check(path: Path, *, shim: Path | None = None) -> Win7CheckResult:
        raise PeParseError("非 MZ 文件")

    monkeypatch.setattr(win7_dll, "check_win7_imports", fake_check)
    with pytest.raises(Win7DllError, match="不是合法 PE 镜像"):
        ensure_win7_dll(_VER, tmp_path / "cache", tmp_path / "dest")


def test_ensure_records_stage_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dll 已就绪时 stage 记录缓存命中."""
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "python312.dll").write_bytes(b"existing")
    _patch_check_ok(monkeypatch)
    rec = StageRecorder("test")
    ensure_win7_dll(_VER, tmp_path / "cache", dest, stage=rec)
    record = rec._finalize()
    assert record.cache_hit == 1
    assert record.bytes_downloaded == 0


def test_ensure_records_stage_download_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载时 stage 记录字节数且不记缓存命中."""
    zip_bytes = _make_zip_bytes({"python312.dll": b"dll-bytes"})
    _patch_manifest(monkeypatch, _VER, zip_bytes)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout, **kw: FakeResp(zip_bytes))
    _patch_check_ok(monkeypatch)
    rec = StageRecorder("test")
    ensure_win7_dll(_VER, tmp_path / "cache", tmp_path / "dest", stage=rec)
    record = rec._finalize()
    assert record.bytes_downloaded == len(zip_bytes)
    assert record.cache_hit == 0


# --- RuntimeDownloader 钩子测试 ---


def test_win7_embed_runtime_hooks(tmp_path: Path) -> None:
    """基类钩子：缓存名/URL/marker 与模块函数一致，extract_archive 提取 dll."""
    assert Win7EmbedRuntime.archive_name(_VER) == win7_zip_cache_name(_VER)
    assert Win7EmbedRuntime.download_url(_VER) == win7_zip_url(_VER)
    runtime_dir = tmp_path / "runtime"
    assert Win7EmbedRuntime.marker_path(runtime_dir, _VER) == runtime_dir / "python312.dll"
    zip_path = tmp_path / win7_zip_cache_name(_VER)
    zip_path.write_bytes(_make_zip_bytes({"python312.dll": b"dll"}))
    Win7EmbedRuntime.extract_archive(zip_path, runtime_dir)
    assert (runtime_dir / "python312.dll").read_bytes() == b"dll"
