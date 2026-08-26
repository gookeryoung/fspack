"""doctor 缓存健康测试：integrity.py 归档完好性探测与 cache_health.py 各类型扫描/清理/聚合分发."""

from __future__ import annotations

import io
import os
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

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


# ---- _scan_cache_health（iter-139） ----


def test_scan_cache_health_dir_not_exists(tmp_path: Path) -> None:
    """缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_cache_health

    report = _scan_cache_health(tmp_path / "no-cache")
    assert report.total_deps_files == 0
    assert report.total_wheels == 0
    assert report.corrupt_deps_files == ()
    assert report.stale_deps_files == ()
    assert report.orphan_wheels == ()
    assert not report.has_issues


def test_scan_cache_health_empty_dir(tmp_path: Path) -> None:
    """空缓存目录返回空报告."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    report = _scan_cache_health(cache)
    assert report.total_deps_files == 0
    assert report.total_wheels == 0
    assert not report.has_issues


def test_scan_cache_health_all_valid(tmp_path: Path) -> None:
    """所有 deps 有效且 wheel 都存在时无问题."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key1.json").write_text('{"wheels": ["numpy-1.0.whl"]}', encoding="utf-8")
    (cache / ".deps-key2.json").write_text('{"wheels": ["rich-1.0.whl"]}', encoding="utf-8")
    (cache / "numpy-1.0.whl").write_bytes(b"x")
    (cache / "rich-1.0.whl").write_bytes(b"yy")

    report = _scan_cache_health(cache)
    assert report.total_deps_files == 2
    assert report.total_wheels == 2
    assert report.corrupt_deps_files == ()
    assert report.stale_deps_files == ()
    assert report.orphan_wheels == ()
    assert report.orphan_size_bytes == 0
    assert not report.has_issues


def test_scan_cache_health_corrupt_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时损坏 deps 文件被删除并计入 corrupt_deps_files."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-good.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    corrupt = cache / ".deps-bad.json"
    corrupt.write_text("{bad", encoding="utf-8")

    report = _scan_cache_health(cache, delete_corrupt=True)
    assert report.total_deps_files == 2
    assert report.corrupt_deps_files == (".deps-bad.json",)
    assert not corrupt.is_file()
    assert report.stale_deps_files == ()
    assert report.orphan_wheels == ()
    assert report.has_issues


def test_scan_cache_health_default_keeps_corrupt(tmp_path: Path) -> None:
    """默认（delete_corrupt=False）只报告损坏 deps 不删除（只读路径无副作用）."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    corrupt = cache / ".deps-bad.json"
    corrupt.write_text("{bad", encoding="utf-8")

    report = _scan_cache_health(cache)
    assert report.corrupt_deps_files == (".deps-bad.json",)
    assert corrupt.is_file()
    assert report.has_issues


def test_scan_cache_health_stale_deps_detected(tmp_path: Path) -> None:
    """deps 引用缺失 wheel 时计入 stale_deps_files 与 missing_wheels."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")

    report = _scan_cache_health(cache)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert "missing.whl" in report.missing_wheels
    # stale deps 文件未被删除（需 _clean_cache_issues 才删）
    assert (cache / ".deps-stale.json").is_file()
    assert report.has_issues


def test_scan_cache_health_orphan_wheel_detected(tmp_path: Path) -> None:
    """未被任何 deps 引用的 wheel 计入 orphan_wheels 并累加体积."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["numpy-1.0.whl"]}', encoding="utf-8")
    (cache / "numpy-1.0.whl").write_bytes(b"x")
    (cache / "orphan-1.0.whl").write_bytes(b"yyyy")

    report = _scan_cache_health(cache)
    assert report.total_wheels == 2
    assert report.orphan_wheels == ("orphan-1.0.whl",)
    assert report.orphan_size_bytes == 4
    assert report.has_issues


def test_scan_cache_health_shared_wheel_not_orphan(tmp_path: Path) -> None:
    """多个 deps 引用同一 wheel 时该 wheel 不算孤儿."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key1.json").write_text('{"wheels": ["shared.whl"]}', encoding="utf-8")
    (cache / ".deps-key2.json").write_text('{"wheels": ["shared.whl", "other.whl"]}', encoding="utf-8")
    (cache / "shared.whl").write_bytes(b"x")
    (cache / "other.whl").write_bytes(b"y")

    report = _scan_cache_health(cache)
    assert report.orphan_wheels == ()
    assert not report.has_issues


def test_scan_cache_health_non_string_wheels_ignored(tmp_path: Path) -> None:
    """wheels 列表中非字符串元素被忽略（防御性，避免 is_file 报错）."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    # wheels 含非字符串元素（理论上不会出现，但 _scan_cache_health 应防御）
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl", 123, null]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")

    report = _scan_cache_health(cache)
    assert report.stale_deps_files == ()
    assert report.missing_wheels == ()
    assert not report.has_issues


# ---- _clean_cache_issues（iter-139） ----


def test_clean_cache_issues_no_issues(tmp_path: Path) -> None:
    """无问题时清理不删除任何文件."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")

    report = _clean_cache_issues(cache)
    assert not report.has_issues
    assert (cache / ".deps-key.json").is_file()
    assert (cache / "x.whl").is_file()


def test_clean_cache_issues_dry_run_no_delete(tmp_path: Path) -> None:
    """dry_run=True 时仅扫描不删除文件."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    (cache / "orphan.whl").write_bytes(b"x")

    report = _clean_cache_issues(cache, dry_run=True)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ("orphan.whl",)
    # dry_run 不删除
    assert (cache / ".deps-stale.json").is_file()
    assert (cache / "orphan.whl").is_file()


def test_clean_cache_issues_deletes_stale_and_orphan(tmp_path: Path) -> None:
    """清理删除 stale deps 文件与孤儿 wheel 文件."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    (cache / ".deps-good.json").write_text('{"wheels": ["good.whl"]}', encoding="utf-8")
    (cache / "good.whl").write_bytes(b"x")
    (cache / "orphan.whl").write_bytes(b"yy")

    report = _clean_cache_issues(cache)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ("orphan.whl",)
    # stale deps 与 orphan wheel 被删除
    assert not (cache / ".deps-stale.json").is_file()
    assert not (cache / "orphan.whl").is_file()
    # 有效 deps 与被引用的 wheel 保留
    assert (cache / ".deps-good.json").is_file()
    assert (cache / "good.whl").is_file()


def test_clean_cache_issues_keeps_shared_wheel(tmp_path: Path) -> None:
    """清理时多个 deps 共享的 wheel 不被删除（即使某个 deps 是 stale）."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    # stale deps 引用 shared.whl + missing.whl；good deps 引用 shared.whl
    # shared.whl 仍存在（被 good deps 引用），不应被删
    (cache / ".deps-stale.json").write_text('{"wheels": ["shared.whl", "missing.whl"]}', encoding="utf-8")
    (cache / ".deps-good.json").write_text('{"wheels": ["shared.whl"]}', encoding="utf-8")
    (cache / "shared.whl").write_bytes(b"x")

    report = _clean_cache_issues(cache)
    assert report.stale_deps_files == (".deps-stale.json",)
    assert report.orphan_wheels == ()  # shared.whl 被 good deps 引用，非孤儿
    # stale deps 被删除
    assert not (cache / ".deps-stale.json").is_file()
    # shared.whl 保留（被 good deps 引用）
    assert (cache / "shared.whl").is_file()
    assert (cache / ".deps-good.json").is_file()


def test_clean_cache_issues_unlink_oserror_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """unlink 抛 OSError 时不阻断其他文件清理（best-effort）."""
    from fspack.doctor import _clean_cache_issues

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-stale1.json").write_text('{"wheels": ["missing1.whl"]}', encoding="utf-8")
    (cache / ".deps-stale2.json").write_text('{"wheels": ["missing2.whl"]}', encoding="utf-8")
    (cache / "orphan1.whl").write_bytes(b"x")
    (cache / "orphan2.whl").write_bytes(b"yy")

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        # 第一个文件 unlink 失败，后续正常
        if self.name in (".deps-stale1.json", "orphan1.whl"):
            raise OSError("simulated permission denied")
        real_unlink(self)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    report = _clean_cache_issues(cache)
    # 第一组文件 unlink 失败但保留在报告中
    assert ".deps-stale1.json" in report.stale_deps_files
    assert ".deps-stale2.json" in report.stale_deps_files
    assert "orphan1.whl" in report.orphan_wheels
    assert "orphan2.whl" in report.orphan_wheels
    # 第二组文件成功删除
    assert not (cache / ".deps-stale2.json").is_file()
    assert not (cache / "orphan2.whl").is_file()


def test_scan_cache_health_orphan_stat_oserror_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """orphan wheel 的 stat() 抛 OSError 时跳过体积累加但仍视为孤儿."""
    from fspack.doctor import _scan_cache_health

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["good.whl"]}', encoding="utf-8")
    (cache / "good.whl").write_bytes(b"x")
    (cache / "orphan.whl").write_bytes(b"yy")

    real_stat = Path.stat

    def fail_orphan_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "orphan.whl":
            raise OSError("simulated")
        return real_stat(self)

    monkeypatch.setattr(Path, "stat", fail_orphan_stat)

    report = _scan_cache_health(cache)
    # orphan 仍被识别，但体积为 0（stat 失败跳过）
    assert report.orphan_wheels == ("orphan.whl",)
    assert report.orphan_size_bytes == 0


# ---- 多 cache 类型扫描器（iter-148） ----
#
# 覆盖 embed/standalone/nuitka/loaders/ccache/tkinter 6 个新扫描器：
# 损坏文件识别（zip/tar/PE 头/空文件）+ 过期文件识别（版本不在 KNOWN_*_VERSIONS）
# + 聚合分发（_scan_cache_by_type / _scan_all_caches / _clean_cache_by_type /
#   _clean_all_caches）+ run_cache_status/clean 的 --target/--stale 派发。


def _make_zip(path: Path, content: bytes = b"hello") -> None:
    """创建有效 zip 文件（含一个 test.txt 条目）."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("test.txt", content)


def _make_tar(path: Path, content: bytes = b"hello") -> None:
    """创建有效 tar.gz 文件（含一个 test.txt 条目）."""
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(name="test.txt")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))


def _make_pe(path: Path) -> None:
    """创建合法 PE 文件（MZ 头 + 填充）."""
    path.write_bytes(b"MZ" + b"\x00" * 100)


# ---- 辅助函数：_is_zip_intact / _is_tar_intact / _is_pe_file ----


def test_is_zip_intact_valid(tmp_path: Path) -> None:
    """_is_zip_intact 对有效 zip 返回 True."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "test.zip"
    _make_zip(z)
    assert _is_zip_intact(z) is True


def test_is_zip_intact_corrupt(tmp_path: Path) -> None:
    """_is_zip_intact 对垃圾数据返回 False."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "bad.zip"
    z.write_bytes(b"not a zip file")
    assert _is_zip_intact(z) is False


def test_is_zip_intact_quick_vs_full_data_corrupt(tmp_path: Path) -> None:
    """快检只读中心目录（数据区损坏仍 True），全量 CRC 校验检出数据区损坏."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "bad_data.zip"
    _make_zip(z)
    data = bytearray(z.read_bytes())
    # 翻转 local file header 中文件名之后的压缩数据首字节（不动文件尾的中心目录）
    idx = data.find(b"test.txt")
    assert idx > 0
    data[idx + len(b"test.txt")] ^= 0xFF
    z.write_bytes(bytes(data))
    assert _is_zip_intact(z) is True  # 快检：中心目录完好，数据区损坏不可见
    assert _is_zip_intact(z, full=True) is False  # 全量：CRC 校验失败


def test_is_zip_intact_full_valid_zip(tmp_path: Path) -> None:
    """full=True 对有效 zip 仍返回 True（testzip 通过）."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "test.zip"
    _make_zip(z)
    assert _is_zip_intact(z, full=True) is True


def test_is_tar_intact_valid(tmp_path: Path) -> None:
    """_is_tar_intact 对有效 tar.gz 返回 True."""
    from fspack.doctor import _is_tar_intact

    t = tmp_path / "test.tar.gz"
    _make_tar(t)
    assert _is_tar_intact(t) is True


def test_is_tar_intact_corrupt(tmp_path: Path) -> None:
    """_is_tar_intact 对垃圾数据返回 False."""
    from fspack.doctor import _is_tar_intact

    t = tmp_path / "bad.tar.gz"
    t.write_bytes(b"not a tar file")
    assert _is_tar_intact(t) is False


def test_is_pe_file_valid(tmp_path: Path) -> None:
    """_is_pe_file 对含 MZ 头的文件返回 True."""
    from fspack.doctor import _is_pe_file

    p = tmp_path / "loader.exe"
    _make_pe(p)
    assert _is_pe_file(p) is True


def test_is_pe_file_missing_mz(tmp_path: Path) -> None:
    """_is_pe_file 对缺少 MZ 头的文件返回 False."""
    from fspack.doctor import _is_pe_file

    p = tmp_path / "bad.exe"
    p.write_bytes(b"XX" + b"\x00" * 100)
    assert _is_pe_file(p) is False


def test_is_pe_file_empty(tmp_path: Path) -> None:
    """_is_pe_file 对空文件返回 False."""
    from fspack.doctor import _is_pe_file

    p = tmp_path / "empty.exe"
    p.write_bytes(b"")
    assert _is_pe_file(p) is False


def test_is_zip_intact_oserror_indeterminate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_zip_intact 对 OSError（杀软/文件锁）返回 None（无法判定，不判损坏）."""
    from fspack.doctor import _is_zip_intact

    z = tmp_path / "locked.zip"
    z.write_bytes(b"x" * 10)

    class _LockedZip:
        def __init__(self, path: object, *args: object, **kwargs: object) -> None:
            raise PermissionError("file locked by antivirus")

    monkeypatch.setattr("fspack.doctor.integrity.zipfile.ZipFile", _LockedZip)
    assert _is_zip_intact(z) is None


def test_is_tar_intact_oserror_indeterminate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_tar_intact 对 OSError（杀软/文件锁）返回 None（无法判定，不判损坏）."""
    from fspack.doctor import _is_tar_intact

    t = tmp_path / "locked.tar.gz"
    t.write_bytes(b"x" * 10)

    def _raise_open(*args: object, **kwargs: object) -> None:
        raise PermissionError("file locked by antivirus")

    monkeypatch.setattr("fspack.doctor.integrity.tarfile.open", _raise_open)
    assert _is_tar_intact(t) is None


def test_is_pe_file_oserror_indeterminate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_pe_file 对 OSError（杀软/文件锁）返回 None（无法判定，不判损坏）."""
    from fspack.doctor import _is_pe_file

    p = tmp_path / "locked.exe"
    p.write_bytes(b"MZ")

    def _raise_open(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("file locked by antivirus")

    monkeypatch.setattr(Path, "open", _raise_open)
    assert _is_pe_file(p) is None


# ---- _scan_embed_health ----


def test_scan_embed_health_dir_not_exists(tmp_path: Path) -> None:
    """embed 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_embed_health

    report = _scan_embed_health(tmp_path / "no-embed")
    assert report.cache_type == "embed"
    assert report.total_files == 0
    assert not report.has_issues


def test_scan_embed_health_empty_dir(tmp_path: Path) -> None:
    """embed 空目录无问题."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    report = _scan_embed_health(cache)
    assert report.total_files == 0
    assert not report.has_issues


def test_scan_embed_health_valid_zip(tmp_path: Path) -> None:
    """已知版本的有效 embed zip 不报问题."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    # 3.11.9 在 KNOWN_EMBED_VERSIONS.values() 中
    z = cache / "python-3.11.9-embed-amd64.zip"
    _make_zip(z)
    report = _scan_embed_health(cache)
    assert report.total_files == 1
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_embed_health_corrupt_zip_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时损坏的 embed zip 在扫描期删除并计入 corrupt_files."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    bad = cache / "python-3.11.9-embed-amd64.zip"
    bad.write_bytes(b"not a zip")
    report = _scan_embed_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ("python-3.11.9-embed-amd64.zip",)
    assert not bad.is_file()
    assert report.has_issues


def test_scan_embed_health_default_keeps_corrupt(tmp_path: Path) -> None:
    """默认（delete_corrupt=False）损坏的 embed zip 只报告不删除（只读路径）."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    bad = cache / "python-3.11.9-embed-amd64.zip"
    bad.write_bytes(b"not a zip")
    report = _scan_embed_health(cache)
    assert report.corrupt_files == ("python-3.11.9-embed-amd64.zip",)
    assert bad.is_file()
    assert report.has_issues


def test_scan_embed_health_indeterminate_zip_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """zip 完整性无法判定（IO 异常返回 None）时不计损坏也不删除."""
    from fspack.doctor import _scan_embed_health, cache_health

    cache = tmp_path / "embed"
    cache.mkdir()
    locked = cache / "python-3.11.9-embed-amd64.zip"
    _make_zip(locked)

    def _locked_zip(path: Path, **kwargs: object) -> bool | None:
        return None  # 模拟杀软/文件锁导致 OSError 无法判定（兼容 full 等透传参数）

    monkeypatch.setattr(cache_health, "_is_zip_intact", _locked_zip)
    report = _scan_embed_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert locked.is_file()
    assert not report.has_issues


def test_scan_embed_health_stale_zip_detected(tmp_path: Path) -> None:
    """未知版本的 embed zip 计入 stale_files 但不删除（需 --stale 清理）."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_EMBED_VERSIONS.values() 中
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    report = _scan_embed_health(cache)
    assert report.stale_files == ("python-3.7.0-embed-amd64.zip",)
    assert stale.is_file()  # 扫描期不删除
    assert report.has_issues


def test_scan_embed_health_non_zip_ignored(tmp_path: Path) -> None:
    """非 embed zip 命名模式的文件被忽略（不视为问题）."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    (cache / "README.txt").write_text("info", encoding="utf-8")
    (cache / "random.zip").write_bytes(b"x")
    report = _scan_embed_health(cache)
    assert report.total_files == 2  # 计入 total 但无问题
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def _make_data_corrupt_zip(path: Path) -> None:
    """创建中心目录完好但数据区损坏的 zip（快检不可见，全量 CRC 可检出）."""
    _make_zip(path)
    data = bytearray(path.read_bytes())
    idx = data.find(b"test.txt")
    assert idx > 0
    data[idx + len(b"test.txt")] ^= 0xFF  # 翻转压缩数据首字节，不动文件尾中心目录
    path.write_bytes(bytes(data))


def test_scan_embed_health_full_verify_detects_data_corrupt(tmp_path: Path) -> None:
    """full_verify=True 检出数据区损坏的 embed zip（默认快检不可见）."""
    from fspack.doctor import _scan_embed_health

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_data_corrupt_zip(cache / "python-3.11.9-embed-amd64.zip")

    quick = _scan_embed_health(cache)
    assert quick.corrupt_files == ()  # 快检：中心目录完好，不报损坏

    full = _scan_embed_health(cache, full_verify=True)
    assert full.corrupt_files == ("python-3.11.9-embed-amd64.zip",)  # 全量：CRC 检出


# ---- _scan_standalone_health ----


def test_scan_standalone_health_dir_not_exists(tmp_path: Path) -> None:
    """standalone 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_standalone_health

    report = _scan_standalone_health(tmp_path / "no-standalone")
    assert report.cache_type == "standalone"
    assert not report.has_issues


def test_scan_standalone_health_valid_tar(tmp_path: Path) -> None:
    """已知版本的有效 standalone tar.gz 不报问题."""
    from fspack.doctor import _scan_standalone_health

    cache = tmp_path / "standalone"
    cache.mkdir()
    # 3.11.15 在 KNOWN_STANDALONE_VERSIONS.values() 中
    t = cache / "cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz"
    _make_tar(t)
    report = _scan_standalone_health(cache)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_standalone_health_corrupt_tar_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时损坏的 standalone tar.gz 在扫描期删除并计入 corrupt_files."""
    from fspack.doctor import _scan_standalone_health

    cache = tmp_path / "standalone"
    cache.mkdir()
    bad = cache / "cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz"
    bad.write_bytes(b"not a tar")
    report = _scan_standalone_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ("cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz",)
    assert not bad.is_file()
    assert report.has_issues


def test_scan_standalone_health_stale_tar_detected(tmp_path: Path) -> None:
    """未知版本的 standalone tar.gz 计入 stale_files 但不删除."""
    from fspack.doctor import _scan_standalone_health

    cache = tmp_path / "standalone"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_STANDALONE_VERSIONS.values() 中
    stale = cache / "cpython-3.7.0+20260718-x86_64-unknown-linux-install_only.tar.gz"
    _make_tar(stale)
    report = _scan_standalone_health(cache)
    assert report.stale_files == ("cpython-3.7.0+20260718-x86_64-unknown-linux-install_only.tar.gz",)
    assert stale.is_file()
    assert report.has_issues


# ---- _scan_nuitka_health ----


def test_scan_nuitka_health_dir_not_exists(tmp_path: Path) -> None:
    """nuitka 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_nuitka_health

    report = _scan_nuitka_health(tmp_path / "no-nuitka")
    assert report.cache_type == "nuitka"
    assert not report.has_issues


def test_scan_nuitka_health_valid_dir(tmp_path: Path) -> None:
    """含 python.exe 的已知版本子目录不报问题."""
    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    # 3.11.15 在 KNOWN_STANDALONE_VERSIONS.values() 中
    py_dir = cache / "3.11.15" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "python.exe").write_bytes(b"MZ")
    report = _scan_nuitka_health(cache)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_nuitka_health_corrupt_dir_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 且目录 mtime 超过宽限期时，缺 python 可执行的子目录删除."""
    import time as time_mod

    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    # 已知版本但缺 python 可执行；mtime 回拨到 1 小时前（避开解压进行中宽限）
    bad_dir = cache / "3.11.15" / "python"
    bad_dir.mkdir(parents=True)
    old_ts = time_mod.time() - 3600
    os.utime(cache / "3.11.15", (old_ts, old_ts))
    report = _scan_nuitka_health(cache, delete_corrupt=True)
    assert "3.11.15" in report.corrupt_files
    assert not (cache / "3.11.15").is_dir()
    assert report.has_issues


def test_scan_nuitka_health_recent_extract_skipped(tmp_path: Path) -> None:
    """目录 mtime 距今不足宽限期（视为另一进程解压进行中）时跳过判定不删除."""
    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    # 目录刚创建（mtime 距今约 0 秒 < 600 秒宽限），缺 python 可执行
    extracting = cache / "3.11.15" / "python"
    extracting.mkdir(parents=True)
    report = _scan_nuitka_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ()
    assert (cache / "3.11.15").is_dir()
    assert not report.has_issues


def test_scan_nuitka_health_stale_dir_detected(tmp_path: Path) -> None:
    """未知版本的子目录计入 stale_files、累计体积但不删除."""
    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_STANDALONE_VERSIONS.values() 中
    stale_dir = cache / "3.7.0" / "python"
    stale_dir.mkdir(parents=True)
    (stale_dir / "python.exe").write_bytes(b"MZ" + b"\x00" * 998)
    report = _scan_nuitka_health(cache)
    assert "3.7.0" in report.stale_files
    assert (cache / "3.7.0").is_dir()
    # stale 目录体积（递归）累计到 issues_size_bytes
    assert report.issues_size_bytes == 1000
    assert report.has_issues


def test_scan_nuitka_health_residual_tarball_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时残留 tarball（解压后未清理）视为损坏并删除."""
    from fspack.doctor import _scan_nuitka_health

    cache = tmp_path / "nuitka"
    cache.mkdir()
    residual = cache / "cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz"
    _make_tar(residual)
    report = _scan_nuitka_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ("cpython-3.11.15+20260718-x86_64-unknown-linux-install_only.tar.gz",)
    assert not residual.is_file()
    assert report.has_issues


# ---- _scan_loader_health ----


def test_scan_loader_health_dir_not_exists(tmp_path: Path) -> None:
    """loaders 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_loader_health

    report = _scan_loader_health(tmp_path / "no-loaders")
    assert report.cache_type == "loaders"
    assert not report.has_issues


def test_scan_loader_health_valid_pe(tmp_path: Path) -> None:
    """合法 PE 文件不报问题."""
    from fspack.doctor import _scan_loader_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    _make_pe(cache / "abc123def4567890.exe")
    report = _scan_loader_health(cache)
    assert report.corrupt_files == ()
    assert not report.has_issues


def test_scan_loader_health_empty_file_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时 0 字节文件视为损坏并删除."""
    from fspack.doctor import _scan_loader_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    empty = cache / "empty1234567890.exe"
    empty.write_bytes(b"")
    report = _scan_loader_health(cache, delete_corrupt=True)
    assert "empty1234567890.exe" in report.corrupt_files
    assert not empty.is_file()
    assert report.has_issues


def test_scan_loader_health_non_pe_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时非空但缺 MZ 头的 exe 视为损坏并删除."""
    from fspack.doctor import _scan_loader_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    bad = cache / "bad123456789abc.exe"
    bad.write_bytes(b"XX" + b"\x00" * 50)
    report = _scan_loader_health(cache, delete_corrupt=True)
    assert "bad123456789abc.exe" in report.corrupt_files
    assert not bad.is_file()
    assert report.has_issues


def test_scan_loader_health_indeterminate_pe_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PE 头无法判定（IO 异常返回 None）的 exe 不计损坏也不删除."""
    from fspack.doctor import _scan_loader_health, cache_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    locked = cache / "locked1234567890.exe"
    _make_pe(locked)

    def _locked_pe(path: Path) -> bool | None:
        return None  # 模拟杀软/文件锁导致 OSError 无法判定

    monkeypatch.setattr(cache_health, "_is_pe_file", _locked_pe)
    report = _scan_loader_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ()
    assert locked.is_file()
    assert not report.has_issues


def test_scan_loader_health_non_exe_kept(tmp_path: Path) -> None:
    """非 exe loader 文件（Linux/macOS ELF 产物）非空即健康，跨平台均保留."""
    from fspack.doctor import _scan_loader_health

    cache = tmp_path / "loaders"
    cache.mkdir()
    elf = cache / "elf1234567890abcd"
    elf.write_bytes(b"\x7fELF" + b"\x00" * 50)
    report = _scan_loader_health(cache)
    assert report.corrupt_files == ()
    assert not report.has_issues
    assert elf.is_file()


# ---- _scan_ccache_health ----


def test_scan_ccache_health_dir_not_exists(tmp_path: Path) -> None:
    """ccache 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_ccache_health

    report = _scan_ccache_health(tmp_path / "no-ccache")
    assert report.cache_type == "ccache"
    assert not report.has_issues


def test_scan_ccache_health_valid(tmp_path: Path) -> None:
    """ccache 二进制存在且无残留时不报问题."""
    from fspack.doctor import _scan_ccache_health

    cache = tmp_path / "ccache"
    cache.mkdir()
    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    _make_pe(cache / exe_name)
    report = _scan_ccache_health(cache)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_ccache_health_missing_exe(tmp_path: Path) -> None:
    """ccache 二进制缺失时计入 missing_files（与损坏分列，不计入 corrupt）."""
    from fspack.doctor import _scan_ccache_health

    cache = tmp_path / "ccache"
    cache.mkdir()
    report = _scan_ccache_health(cache)
    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    assert exe_name in report.missing_files
    assert report.corrupt_files == ()
    # 缺失无文件可删，不算需要清理的问题，不虚增 issues_count
    assert not report.has_issues
    assert report.issues_count == 0


def test_scan_ccache_health_stale_subdir(tmp_path: Path) -> None:
    """旧版 ccache-* 子目录计入 stale_files 但不删除."""
    from fspack.doctor import _scan_ccache_health

    cache = tmp_path / "ccache"
    cache.mkdir()
    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    _make_pe(cache / exe_name)
    stale_dir = cache / "ccache-4.10-win64"
    stale_dir.mkdir()
    (stale_dir / "ccache.exe").write_bytes(b"MZ")
    report = _scan_ccache_health(cache)
    assert "ccache-4.10-win64" in report.stale_files
    assert stale_dir.is_dir()
    assert report.has_issues


def test_scan_ccache_health_residual_archive_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时残留下载归档（ccache.tar.xz/ccache.zip）视为损坏并删除."""
    from fspack.doctor import _scan_ccache_health

    cache = tmp_path / "ccache"
    cache.mkdir()
    exe_name = "ccache.exe" if sys.platform.startswith("win") else "ccache"
    _make_pe(cache / exe_name)
    archive = cache / "ccache.zip"
    archive.write_bytes(b"not a real zip")
    report = _scan_ccache_health(cache, delete_corrupt=True)
    assert "ccache.zip" in report.corrupt_files
    assert not archive.is_file()
    assert report.has_issues


# ---- _scan_tkinter_health ----


def test_scan_tkinter_health_dir_not_exists(tmp_path: Path) -> None:
    """tkinter 缓存目录不存在时返回空报告."""
    from fspack.doctor import _scan_tkinter_health

    report = _scan_tkinter_health(tmp_path / "no-tkinter")
    assert report.cache_type == "tkinter"
    assert not report.has_issues


def test_scan_tkinter_health_valid_zip(tmp_path: Path) -> None:
    """已知版本的有效 tkinter zip 不报问题."""
    from fspack.doctor import _scan_tkinter_health

    cache = tmp_path / "tkinter"
    cache.mkdir()
    # 3.11.15 在 KNOWN_STANDALONE_VERSIONS.values() 中
    z = cache / "tkinter-3.11.15.zip"
    _make_zip(z)
    report = _scan_tkinter_health(cache)
    assert report.corrupt_files == ()
    assert report.stale_files == ()
    assert not report.has_issues


def test_scan_tkinter_health_corrupt_zip_deleted(tmp_path: Path) -> None:
    """delete_corrupt=True 时损坏的 tkinter zip 在扫描期删除并计入 corrupt_files."""
    from fspack.doctor import _scan_tkinter_health

    cache = tmp_path / "tkinter"
    cache.mkdir()
    bad = cache / "tkinter-3.11.15.zip"
    bad.write_bytes(b"not a zip")
    report = _scan_tkinter_health(cache, delete_corrupt=True)
    assert report.corrupt_files == ("tkinter-3.11.15.zip",)
    assert not bad.is_file()
    assert report.has_issues


def test_scan_tkinter_health_stale_zip_detected(tmp_path: Path) -> None:
    """未知版本的 tkinter zip 计入 stale_files 但不删除."""
    from fspack.doctor import _scan_tkinter_health

    cache = tmp_path / "tkinter"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_STANDALONE_VERSIONS.values() 中
    stale = cache / "tkinter-3.7.0.zip"
    _make_zip(stale)
    report = _scan_tkinter_health(cache)
    assert report.stale_files == ("tkinter-3.7.0.zip",)
    assert stale.is_file()
    assert report.has_issues


# ---- _scan_cache_by_type / _scan_all_caches 聚合分发 ----


def test_scan_cache_by_type_dispatches_wheels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_scan_cache_by_type('wheels') 分发到 _scan_cache_health."""
    from fspack.doctor import _scan_cache_by_type

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    report = _scan_cache_by_type("wheels")
    assert report.cache_type == "wheels"
    assert report.total_deps_files == 1


def test_scan_cache_by_type_dispatches_embed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_scan_cache_by_type('embed') 分发到 _scan_embed_health."""
    from fspack.doctor import _scan_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_zip(cache / "python-3.11.9-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _scan_cache_by_type("embed")
    assert report.cache_type == "embed"
    assert report.total_files == 1


def test_scan_cache_by_type_embed_full_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_scan_cache_by_type('embed', full_verify=True) 透传全量校验，快检不报全量报."""
    from fspack.doctor import _scan_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_data_corrupt_zip(cache / "python-3.11.9-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    quick = _scan_cache_by_type("embed")
    assert quick.corrupt_files == ()

    full = _scan_cache_by_type("embed", full_verify=True)
    assert full.corrupt_files == ("python-3.11.9-embed-amd64.zip",)


def test_scan_cache_by_type_wheels_ignores_full_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """wheels 扫描器不支持 full_verify，分发器不透传该参数（不抛 TypeError）."""
    from fspack.doctor import _scan_cache_by_type

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-key.json").write_text('{"wheels": ["x.whl"]}', encoding="utf-8")
    (cache / "x.whl").write_bytes(b"x")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    report = _scan_cache_by_type("wheels", full_verify=True)
    assert report.cache_type == "wheels"
    assert report.total_deps_files == 1


def test_scan_cache_by_type_unknown_raises() -> None:
    """_scan_cache_by_type 未知类型抛 ValueError."""
    from fspack.doctor import _scan_cache_by_type

    with pytest.raises(ValueError, match="未知 cache 类型"):
        _scan_cache_by_type("unknown")


def test_scan_all_caches_returns_all_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_scan_all_caches 返回全部 7 个 cache 类型的报告."""
    from fspack.doctor import CACHE_TYPES, _scan_all_caches

    # 将所有 cache 目录重定向到 tmp_path 下空子目录，避免受开发机真实缓存影响
    monkeypatch.setattr("fspack.config.cache.cache_root", lambda: tmp_path / "cache")

    reports = _scan_all_caches()
    assert len(reports) == len(CACHE_TYPES)
    assert tuple(r.cache_type for r in reports) == CACHE_TYPES


# ---- _clean_cache_by_type / _clean_all_caches ----


def test_clean_cache_by_type_wheels_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_clean_cache_by_type('wheels') 分发到 _clean_cache_issues."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "wheels"
    cache.mkdir()
    (cache / ".deps-stale.json").write_text('{"wheels": ["missing.whl"]}', encoding="utf-8")
    monkeypatch.setattr("fspack.config.cache.wheel_cache_dir", lambda: cache)

    report = _clean_cache_by_type("wheels")
    assert report.stale_deps_files == (".deps-stale.json",)
    assert not (cache / ".deps-stale.json").is_file()


def test_clean_cache_by_type_embed_no_stale_keeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 wheels 类型无 --stale 时保留 stale_files（仅清理 corrupt，扫描期已删）."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    # 3.7.0 不在 KNOWN_EMBED_VERSIONS 中
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _clean_cache_by_type("embed", include_stale=False)
    assert "python-3.7.0-embed-amd64.zip" in report.stale_files
    assert stale.is_file()  # 未启用 --stale，保留


def test_clean_cache_by_type_embed_with_stale_deletes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 wheels 类型 --stale 时删除 stale_files."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _clean_cache_by_type("embed", include_stale=True)
    # include_stale=True 后重新扫描，stale_files 应已清空
    assert report.stale_files == ()
    assert not stale.is_file()


def test_clean_cache_by_type_dry_run_no_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True 时仅扫描不删除（含损坏文件，扫描器不带 delete_corrupt）."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    bad = cache / "python-3.11.9-embed-amd64.zip"
    bad.write_bytes(b"not a zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _clean_cache_by_type("embed", dry_run=True)
    # dry_run 下损坏文件只报告不删除（扫描器 delete_corrupt=False）
    assert "python-3.11.9-embed-amd64.zip" in report.corrupt_files
    assert bad.is_file()


def test_clean_cache_by_type_clean_deletes_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 dry_run 清理路径扫描器带 delete_corrupt=True，损坏文件被删除."""
    from fspack.doctor import _clean_cache_by_type

    cache = tmp_path / "embed"
    cache.mkdir()
    bad = cache / "python-3.11.9-embed-amd64.zip"
    bad.write_bytes(b"not a zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    report = _clean_cache_by_type("embed", dry_run=False)
    assert "python-3.11.9-embed-amd64.zip" in report.corrupt_files
    assert not bad.is_file()
    assert report.has_issues


def test_clean_all_caches_returns_all_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_clean_all_caches 返回全部 7 个 cache 类型的报告."""
    from fspack.doctor import CACHE_TYPES, _clean_all_caches

    monkeypatch.setattr("fspack.config.cache.cache_root", lambda: tmp_path / "cache")

    reports = _clean_all_caches()
    assert len(reports) == len(CACHE_TYPES)
    assert tuple(r.cache_type for r in reports) == CACHE_TYPES


# ---- run_cache_status / run_cache_clean 多 cache 派发 ----


def test_run_cache_status_target_embed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status(target='embed') 仅扫描 embed 并返回 1 元组."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_zip(cache / "python-3.11.9-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_status(target="embed")
    assert len(reports) == 1
    assert reports[0].cache_type == "embed"
    assert reports[0].total_files == 1
    assert not reports[0].has_issues


def test_run_cache_status_all_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status() 无 target 时扫描全部 7 个 cache 类型."""
    from fspack.doctor import CACHE_TYPES, run_cache_status

    monkeypatch.setattr("fspack.config.cache.cache_root", lambda: tmp_path / "cache")

    reports = run_cache_status()
    assert len(reports) == len(CACHE_TYPES)
    assert tuple(r.cache_type for r in reports) == CACHE_TYPES


def test_run_cache_status_invalid_target_raises_systemexit() -> None:
    """run_cache_status 未知 target 抛 SystemExit(2)."""
    from fspack.doctor import run_cache_status

    with pytest.raises(SystemExit) as exc_info:
        run_cache_status(target="unknown")
    assert exc_info.value.code == 2


def test_run_cache_clean_target_embed_with_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean(target='embed', include_stale=True) 删除 stale zip."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "embed"
    cache.mkdir()
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_clean(target="embed", include_stale=True)
    assert len(reports) == 1
    assert not stale.is_file()


def test_run_cache_clean_target_embed_no_stale_keeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean(target='embed') 无 --stale 时保留 stale zip."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "embed"
    cache.mkdir()
    stale = cache / "python-3.7.0-embed-amd64.zip"
    _make_zip(stale)
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    run_cache_clean(target="embed", include_stale=False)
    assert stale.is_file()


def test_run_cache_clean_invalid_target_raises_systemexit() -> None:
    """run_cache_clean 未知 target 抛 SystemExit(2)."""
    from fspack.doctor import run_cache_clean

    with pytest.raises(SystemExit) as exc_info:
        run_cache_clean(target="unknown")
    assert exc_info.value.code == 2


def test_run_cache_clean_all_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean() 无 target 时清理全部 7 个 cache 类型."""
    from fspack.doctor import CACHE_TYPES, run_cache_clean

    monkeypatch.setattr("fspack.config.cache.cache_root", lambda: tmp_path / "cache")

    reports = run_cache_clean()
    assert len(reports) == len(CACHE_TYPES)
    assert tuple(r.cache_type for r in reports) == CACHE_TYPES


def test_run_cache_status_embed_with_corrupt_and_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_status(target='embed') 同时检测损坏+过期 zip，覆盖渲染分支."""
    from fspack.doctor import run_cache_status

    cache = tmp_path / "embed"
    cache.mkdir()
    # 损坏 zip（扫描期删除）
    (cache / "python-3.11.9-embed-amd64.zip").write_bytes(b"not a zip")
    # 过期 zip（未知版本）
    _make_zip(cache / "python-3.7.0-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_status(target="embed")
    report = reports[0]
    assert "python-3.11.9-embed-amd64.zip" in report.corrupt_files
    assert "python-3.7.0-embed-amd64.zip" in report.stale_files
    assert report.has_issues


def test_run_cache_clean_embed_with_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean(target='embed') 损坏 zip 渲染清理报告."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "embed"
    cache.mkdir()
    (cache / "python-3.11.9-embed-amd64.zip").write_bytes(b"not a zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_clean(target="embed", dry_run=True)
    report = reports[0]
    assert "python-3.11.9-embed-amd64.zip" in report.corrupt_files


def test_run_cache_clean_embed_with_stale_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_cache_clean(target='embed') 过期 zip 渲染清理报告."""
    from fspack.doctor import run_cache_clean

    cache = tmp_path / "embed"
    cache.mkdir()
    _make_zip(cache / "python-3.7.0-embed-amd64.zip")
    monkeypatch.setattr("fspack.config.cache.embed_cache_dir", lambda: cache)

    reports = run_cache_clean(target="embed", include_stale=False)
    report = reports[0]
    assert "python-3.7.0-embed-amd64.zip" in report.stale_files
