"""packaging.runtime 测试：embed python 与 python-build-standalone 下载/解压/ensure."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from urllib.request import Request

import pytest

from fspack.config import MirrorConfig
from fspack.exceptions import EmbedError
from fspack.packaging.runtime import (
    STANDALONE_BASE_URL,
    STANDALONE_RELEASE_TAG,
    download_embed,
    download_standalone,
    embed_dirname,
    embed_zip_name,
    ensure_embed,
    ensure_standalone,
    extract_embed,
    extract_standalone,
    standalone_tarball_name,
    standalone_url,
    write_pth,
)
from fspack.progress import StageRecorder
from tests._stubs import FakeResp


def _make_tar(path: Path, members: list[tuple[str, bytes]]) -> None:
    """生成测试用 tar.gz 文件."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _make_malicious_tar(
    path: Path,
    members: list[tuple[str, str]] | list[tuple[str, str, str]],
) -> None:
    """生成含恶意条目的 tar.gz，members 为 ``(name, type)`` 或 ``(name, type, linkname)``.

    type ∈ {'file', 'symlink', 'hardlink', 'char'}：
    - ``file``：普通文件（用于构造路径穿越/绝对路径测试）
    - ``symlink``：符号链接（``linkname`` 默认 ``/etc/passwd``）
    - ``hardlink``：硬链接（``linkname`` 默认 ``python/bin/python3.11``）
    - ``char``：字符设备文件

    linkname 可选，用于自定义链接目标以测试相对/绝对/穿越路径：
    PEP 706 ``data`` filter 仅拒绝绝对路径或路径穿越的链接，相对路径安全链接
    应放行（python-build-standalone 官方 tarball 含 ``python/bin/2to3`` 等相对链接）。
    """
    with tarfile.open(path, "w:gz") as tf:
        for entry in members:
            name, mtype = entry[0], entry[1]
            linkname = entry[2] if len(entry) > 2 else None
            info = tarfile.TarInfo(name=name)
            if mtype == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = linkname if linkname is not None else "/etc/passwd"
                tf.addfile(info, None)
            elif mtype == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = linkname if linkname is not None else "python/bin/python3.11"
                tf.addfile(info, None)
            elif mtype == "char":
                info.type = tarfile.CHRTYPE
                info.devmajor = 1
                info.devminor = 3
                tf.addfile(info, None)
            else:  # file
                info.type = tarfile.REGTYPE
                data = b"x"
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))


# --- embed python 测试 ---


def test_embed_dirname_and_zipname() -> None:
    assert embed_dirname("3.11.9") == "python311"
    assert embed_zip_name("3.11.9") == "python-3.11.9-embed-amd64.zip"


def test_download_embed_cache_hit(tmp_path: Path, mirror: MirrorConfig) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "python-3.11.9-embed-amd64.zip").write_bytes(b"old")
    path = download_embed("3.11.9", mirror, cache)
    assert path.read_bytes() == b"old"


def test_download_embed_fetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
        captured["url"] = req.full_url
        return FakeResp(b"ZIPDATA")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    path = download_embed("3.11.9", mirror, tmp_path / "cache")
    assert path.read_bytes() == b"ZIPDATA"
    assert captured["url"].endswith("python-3.11.9-embed-amd64.zip")


def test_download_embed_network_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig) -> None:
    def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
        raise OSError("boom")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(EmbedError, match="下载 embed python 失败"):
        download_embed("3.11.9", mirror, tmp_path / "cache")


def test_download_embed_cache_hit_calls_stage(tmp_path: Path, mirror: MirrorConfig) -> None:
    """缓存命中时调 stage.hit_cache()."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "python-3.11.9-embed-amd64.zip").write_bytes(b"old")
    rec = StageRecorder("test")
    download_embed("3.11.9", mirror, cache, stage=rec)
    record = rec._finalize()
    assert record.cache_hit == 1
    assert record.bytes_downloaded == 0


def test_download_embed_fetches_records_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """下载成功时 stage.add_bytes 被调用."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout, **kw: FakeResp(b"ZIPDATA"),
    )
    rec = StageRecorder("test")
    download_embed("3.11.9", mirror, tmp_path / "cache", stage=rec)
    record = rec._finalize()
    assert record.bytes_downloaded == 7  # len("ZIPDATA")
    assert record.cache_hit == 0


def test_extract_embed(tmp_path: Path) -> None:
    zip_path = tmp_path / "e.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("python311.dll", b"dll")
        zf.writestr("python311._pth", "python311.zip\n")
    dest = tmp_path / "runtime"
    extract_embed(zip_path, dest)
    assert (dest / "python311.dll").is_file()
    assert (dest / "python311._pth").read_text().startswith("python311.zip")


def test_extract_embed_bad_zip(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(EmbedError, match="embed zip 损坏"):
        extract_embed(bad, tmp_path / "runtime")
    # iter-130：损坏归档应被删除，避免下次构建缓存命中损坏文件反复解压失败
    assert not bad.exists(), "损坏的 embed zip 应被删除"


def test_extract_embed_bad_zip_unlink_failure_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """删除损坏 embed zip 失败时仅告警不抛（仍抛 EmbedError 让上层处理）."""
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")

    def fail_unlink(self: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with caplog.at_level("WARNING", logger="fspack.packaging.runtime"), pytest.raises(
        EmbedError, match="embed zip 损坏"
    ):
        extract_embed(bad, tmp_path / "runtime")
    assert any("删除损坏的 embed zip 失败" in r.message for r in caplog.records)


def test_extract_embed_rejects_path_traversal(tmp_path: Path) -> None:
    """zip 含 ``..`` 路径穿越条目应被预检拒绝，归档删除避免下次使用."""
    zip_path = tmp_path / "mal.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../../etc/passwd", b"root")
    with pytest.raises(EmbedError, match="路径穿越"):
        extract_embed(zip_path, tmp_path / "runtime")
    assert not zip_path.exists(), "含恶意条目的 embed zip 应被删除"


def test_extract_embed_rejects_absolute_path(tmp_path: Path) -> None:
    """zip 含 Unix 绝对路径条目应被预检拒绝."""
    zip_path = tmp_path / "mal.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("/etc/passwd", b"root")
    with pytest.raises(EmbedError, match="绝对路径"):
        extract_embed(zip_path, tmp_path / "runtime")
    assert not zip_path.exists()


def test_extract_embed_rejects_windows_drive(tmp_path: Path) -> None:
    """zip 含 Windows 盘符路径（``C:foo``）应被预检拒绝."""
    zip_path = tmp_path / "mal.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("C:evil.txt", b"x")
    with pytest.raises(EmbedError, match="盘符"):
        extract_embed(zip_path, tmp_path / "runtime")
    assert not zip_path.exists()


def test_extract_embed_rejects_symlink(tmp_path: Path) -> None:
    """zip 含符号链接条目（Unix 模式 S_IFLNK）应被预检拒绝."""
    zip_path = tmp_path / "mal.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        info = zipfile.ZipInfo("evil_link")
        # 0o120777 = S_IFLNK | 0o777，external_attr 高 16 位存 Unix st_mode
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "")
    with pytest.raises(EmbedError, match="符号链接"):
        extract_embed(zip_path, tmp_path / "runtime")
    assert not zip_path.exists()


def test_write_pth_content(tmp_path: Path) -> None:
    pth = write_pth(tmp_path, "3.11.9")
    assert pth == tmp_path / "runtime" / "python311._pth"
    content = pth.read_text(encoding="utf-8")
    assert "python311.zip" in content
    assert "Lib\\site-packages" in content
    assert "..\\src" in content
    assert "import site" in content


def test_write_pth_extra_paths(tmp_path: Path) -> None:
    pth = write_pth(tmp_path, "3.11.9", extra_paths=("assets",))
    assert "assets" in pth.read_text()


def test_write_pth_disable_site(tmp_path: Path) -> None:
    """enable_site=False 省略 `import site` 行，启动跳过 site.py 加速."""
    pth = write_pth(tmp_path, "3.11.9", enable_site=False)
    content = pth.read_text(encoding="utf-8")
    assert "import site" not in content
    # 标准库与 site-packages 路径仍存在
    assert "python311.zip" in content
    assert "Lib\\site-packages" in content


def test_write_pth_enable_site_default(tmp_path: Path) -> None:
    """默认 enable_site=True 保留 `import site` 行（向后兼容）."""
    pth = write_pth(tmp_path, "3.11.9")
    assert "import site" in pth.read_text(encoding="utf-8")


def test_ensure_embed_skips_when_dll_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python311.dll").write_bytes(b"")
    called = {"download": False}
    monkeypatch.setattr("fspack.packaging.runtime.download_embed", lambda *a, **k: called.__setitem__("download", True))
    ensure_embed("3.11.9", mirror, tmp_path / "cache", runtime)
    assert not called["download"]
    assert (runtime / "Lib" / "site-packages").is_dir()


def test_ensure_embed_downloads_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    runtime = tmp_path / "runtime"
    zip_path = tmp_path / "fake.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("python311.dll", b"")
    monkeypatch.setattr("fspack.packaging.runtime.download_embed", lambda *a, **k: zip_path)
    ensure_embed("3.11.9", mirror, tmp_path / "cache", runtime)
    assert (runtime / "python311.dll").is_file()
    assert (runtime / "Lib" / "site-packages").is_dir()


# --- python-build-standalone 测试 ---


def test_standalone_tarball_name() -> None:
    assert (
        standalone_tarball_name("3.13.14", "20260718")
        == "cpython-3.13.14+20260718-x86_64-unknown-linux-gnu-install_only.tar.gz"
    )


def test_standalone_tarball_name_windows() -> None:
    """windows=True 返回 msvc 平台 tarball."""
    assert (
        standalone_tarball_name("3.13.14", "20260718", windows=True)
        == "cpython-3.13.14+20260718-x86_64-pc-windows-msvc-install_only.tar.gz"
    )


def test_standalone_tarball_name_macos_x86_64() -> None:
    """macos_arch='x86_64' 返回 Intel Mac tarball."""
    assert (
        standalone_tarball_name("3.13.14", "20260718", macos_arch="x86_64")
        == "cpython-3.13.14+20260718-x86_64-apple-darwin-install_only.tar.gz"
    )


def test_standalone_tarball_name_macos_arm64() -> None:
    """macos_arch='arm64' 返回 Apple Silicon tarball."""
    assert (
        standalone_tarball_name("3.13.14", "20260718", macos_arch="arm64")
        == "cpython-3.13.14+20260718-arm64-apple-darwin-install_only.tar.gz"
    )


def test_standalone_tarball_name_macos_ignores_windows_flag() -> None:
    """macos_arch 非 None 时忽略 windows 标志（macOS 与 Linux/Windows 互斥）."""
    name_with_windows = standalone_tarball_name("3.13.14", "20260718", windows=True, macos_arch="arm64")
    name_without_windows = standalone_tarball_name("3.13.14", "20260718", macos_arch="arm64")
    assert name_with_windows == name_without_windows


def test_standalone_url() -> None:
    url = standalone_url("3.13.14", "20260718")
    assert url.startswith(STANDALONE_BASE_URL)
    assert "20260718" in url
    assert "3.13.14" in url


def test_standalone_url_macos() -> None:
    """macOS tarball URL 含 apple-darwin 平台段."""
    url = standalone_url("3.13.14", "20260718", macos_arch="arm64")
    assert url.startswith(STANDALONE_BASE_URL)
    assert "arm64-apple-darwin" in url


def test_download_standalone_cache_hit(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    name = standalone_tarball_name("3.11.9", STANDALONE_RELEASE_TAG)
    (cache / name).write_bytes(b"old")
    path = download_standalone("3.11.9", STANDALONE_RELEASE_TAG, cache)
    assert path.read_bytes() == b"old"


def test_download_standalone_fetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> FakeResp:
        captured["url"] = req.full_url
        return FakeResp(b"TARDATA")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    path = download_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache")
    assert path.read_bytes() == b"TARDATA"
    assert STANDALONE_RELEASE_TAG in captured["url"]


def test_download_standalone_network_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: int, **kwargs: object) -> object:
        raise OSError("boom")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(EmbedError, match="下载 python-build-standalone 失败"):
        download_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache")


def test_download_standalone_cache_hit_calls_stage(tmp_path: Path) -> None:
    """缓存命中时调 stage.hit_cache()."""
    cache = tmp_path / "cache"
    cache.mkdir()
    name = standalone_tarball_name("3.11.9", STANDALONE_RELEASE_TAG)
    (cache / name).write_bytes(b"old")
    rec = StageRecorder("test")
    download_standalone("3.11.9", STANDALONE_RELEASE_TAG, cache, stage=rec)
    record = rec._finalize()
    assert record.cache_hit == 1
    assert record.bytes_downloaded == 0


def test_download_standalone_fetches_records_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载成功时 stage.add_bytes 被调用."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout, **kw: FakeResp(b"TARDATA"),
    )
    rec = StageRecorder("test")
    download_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache", stage=rec)
    record = rec._finalize()
    assert record.bytes_downloaded == 7  # len("TARDATA")
    assert record.cache_hit == 0


def test_extract_standalone(tmp_path: Path) -> None:
    tar = tmp_path / "s.tar.gz"
    _make_tar(tar, [("python/bin/python3.11", b"#!/bin/sh"), ("python/lib/libpython3.11.so", b"so")])
    runtime = tmp_path / "runtime"
    extract_standalone(tar, runtime)
    assert (runtime / "python" / "bin" / "python3.11").is_file()
    assert (runtime / "python" / "lib" / "libpython3.11.so").is_file()


def test_extract_standalone_bad_tar(tmp_path: Path) -> None:
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tar")
    with pytest.raises(EmbedError, match="tarball 损坏"):
        extract_standalone(bad, tmp_path / "runtime")
    # iter-130：损坏归档应被删除，避免下次构建缓存命中损坏文件反复解压失败
    assert not bad.exists(), "损坏的 tarball 应被删除"


def test_extract_standalone_bad_tar_unlink_failure_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """删除损坏 tarball 失败时仅告警不抛（仍抛 EmbedError 让上层处理）."""
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tar")

    def fail_unlink(self: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with caplog.at_level("WARNING", logger="fspack.packaging.runtime"), pytest.raises(EmbedError, match="tarball 损坏"):
        extract_standalone(bad, tmp_path / "runtime")
    assert any("删除损坏的 python-build-standalone tarball 失败" in r.message for r in caplog.records)


def test_extract_standalone_rejects_path_traversal(tmp_path: Path) -> None:
    """tar 含 ``..`` 路径穿越条目应被预检拒绝，归档删除避免下次使用."""
    tar = tmp_path / "mal.tar.gz"
    _make_malicious_tar(tar, [("../../etc/passwd", "file")])
    with pytest.raises(EmbedError, match="路径穿越"):
        extract_standalone(tar, tmp_path / "runtime")
    assert not tar.exists(), "含恶意条目的 tarball 应被删除"


def test_extract_standalone_rejects_absolute_path(tmp_path: Path) -> None:
    """tar 含 Unix 绝对路径条目应被预检拒绝."""
    tar = tmp_path / "mal.tar.gz"
    _make_malicious_tar(tar, [("/etc/passwd", "file")])
    with pytest.raises(EmbedError, match="绝对路径"):
        extract_standalone(tar, tmp_path / "runtime")
    assert not tar.exists()


def test_extract_standalone_rejects_windows_drive(tmp_path: Path) -> None:
    """tar 含 Windows 盘符路径（``C:foo``）应被预检拒绝."""
    tar = tmp_path / "mal.tar.gz"
    _make_malicious_tar(tar, [("C:evil.txt", "file")])
    with pytest.raises(EmbedError, match="盘符"):
        extract_standalone(tar, tmp_path / "runtime")
    assert not tar.exists()


def test_extract_standalone_rejects_symlink(tmp_path: Path) -> None:
    """tar 含绝对路径符号链接（linkname 指向 /etc/passwd）应被预检拒绝.

    PEP 706 ``data`` filter 仅拒绝绝对路径或路径穿越的链接，相对路径安全链接
    应放行；本测试构造绝对路径符号链接验证拦截。
    """
    tar = tmp_path / "mal.tar.gz"
    _make_malicious_tar(tar, [("evil_link", "symlink")])
    with pytest.raises(EmbedError, match="绝对路径链接"):
        extract_standalone(tar, tmp_path / "runtime")
    assert not tar.exists()


def test_extract_standalone_rejects_traversal_symlink(tmp_path: Path) -> None:
    """tar 含路径穿越符号链接（linkname 含 ``..`` 段）应被预检拒绝."""
    tar = tmp_path / "mal.tar.gz"
    _make_malicious_tar(tar, [("evil_link", "symlink", "../../etc/passwd")])
    with pytest.raises(EmbedError, match="路径穿越链接"):
        extract_standalone(tar, tmp_path / "runtime")
    assert not tar.exists()


def test_extract_standalone_rejects_windows_drive_symlink(tmp_path: Path) -> None:
    """tar 含 Windows 盘符符号链接（linkname 如 ``C:evil``）应被预检拒绝."""
    tar = tmp_path / "mal.tar.gz"
    _make_malicious_tar(tar, [("evil_link", "symlink", "C:evil")])
    with pytest.raises(EmbedError, match="盘符链接"):
        extract_standalone(tar, tmp_path / "runtime")
    assert not tar.exists()


def test_extract_standalone_rejects_hardlink(tmp_path: Path) -> None:
    """tar 含路径穿越硬链接应被预检拒绝（防硬链接穿越覆盖 runtime_dir 内文件）."""
    tar = tmp_path / "mal.tar.gz"
    _make_malicious_tar(tar, [("evil_link", "hardlink", "../../etc/passwd")])
    with pytest.raises(EmbedError, match="路径穿越链接"):
        extract_standalone(tar, tmp_path / "runtime")
    assert not tar.exists()


def test_extract_standalone_allows_relative_symlink() -> None:
    """相对路径符号链接应放行：python-build-standalone 官方 tarball 含
    ``python/bin/2to3`` 等指向 ``python3.11`` 的合法相对符号链接.

    PEP 706 ``data`` filter 仅拒绝绝对路径或路径穿越的链接，相对路径安全链接
    通过。直接验证 ``_validate_tar_member`` 不抛异常，避免依赖符号链接解压
    权限（Windows 需开发者模式）。
    """
    from fspack.packaging.runtime import _validate_tar_member

    info = tarfile.TarInfo(name="python/bin/2to3")
    info.type = tarfile.SYMTYPE
    info.linkname = "python3.11"
    _validate_tar_member(info)  # 不应抛异常


def test_extract_standalone_allows_relative_hardlink() -> None:
    """相对路径硬链接应放行：python-build-standalone tarball 内部硬链接
    指向同归档内其他文件（如 ``python/bin/python3`` -> ``python3.11``）.

    PEP 706 ``data`` filter 仅拒绝绝对路径或路径穿越的链接，相对路径安全
    硬链接通过。
    """
    from fspack.packaging.runtime import _validate_tar_member

    info = tarfile.TarInfo(name="python/bin/python3")
    info.type = tarfile.LNKTYPE
    info.linkname = "python3.11"
    _validate_tar_member(info)  # 不应抛异常


def test_extract_standalone_rejects_device_file(tmp_path: Path) -> None:
    """tar 含字符设备文件条目应被预检拒绝（防解压特殊文件触发内核行为）."""
    tar = tmp_path / "mal.tar.gz"
    _make_malicious_tar(tar, [("evil_dev", "char")])
    with pytest.raises(EmbedError, match="设备文件"):
        extract_standalone(tar, tmp_path / "runtime")
    assert not tar.exists()


def test_ensure_standalone_skips_when_python_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_text("")
    called = {"download": False}
    monkeypatch.setattr(
        "fspack.packaging.runtime.download_standalone", lambda *a, **k: called.__setitem__("download", True)
    )
    ensure_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache", runtime)
    assert not called["download"]


def test_ensure_standalone_downloads_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    tar_path = tmp_path / "fake.tar.gz"
    _make_tar(tar_path, [("python/bin/python3.11", b"")])
    monkeypatch.setattr("fspack.packaging.runtime.download_standalone", lambda *a, **k: tar_path)
    ensure_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache", runtime)
    assert (runtime / "python" / "bin" / "python3.11").is_file()


# --- ensure_* with stage 参数测试 ---


def test_ensure_embed_skips_with_stage_records_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """ensure_embed marker 命中且传入 stage 时调 stage.hit_cache."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python311.dll").write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.runtime.download_embed", lambda *a, **k: None)
    rec = StageRecorder("test")
    ensure_embed("3.11.9", mirror, tmp_path / "cache", runtime, stage=rec)
    record = rec._finalize()
    assert record.cache_hit == 1


def test_ensure_standalone_skips_with_stage_records_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_standalone marker 命中且传入 stage 时调 stage.hit_cache."""
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_text("")
    monkeypatch.setattr("fspack.packaging.runtime.download_standalone", lambda *a, **k: None)
    rec = StageRecorder("test")
    ensure_standalone("3.11.9", STANDALONE_RELEASE_TAG, tmp_path / "cache", runtime, stage=rec)
    record = rec._finalize()
    assert record.cache_hit == 1
