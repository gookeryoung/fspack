"""``NuitkaCcache`` ccache 测试：本地缓存识别/迁移、下载解压与编译管线接线."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.config import (
    get_mirror,
)
from fspack.platform import Platform


def test_ensure_ccache_finds_system_ccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 中有 ccache 时优先使用系统 ccache."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    fake_ccache = tmp_path / "ccache"
    fake_ccache.write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: str(fake_ccache))
    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(tmp_path / "nuitka", Platform.LINUX, st)
    assert result == fake_ccache


def test_ensure_ccache_finds_local_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 无 ccache 但本地缓存有时使用本地缓存."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "nuitka"
    ccache_dir = tmp_path / "ccache"
    ccache_exe = ccache_dir / "ccache"
    ccache_exe.parent.mkdir(parents=True)
    ccache_exe.write_bytes(b"")

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    st = StageRecorder("Nuitka 编译")
    # cache_root.parent / "ccache" = tmp_path / "ccache"
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.LINUX, st)
    assert result == ccache_exe


def test_ensure_ccache_windows_uses_exe_extension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标查找 ccache.exe."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "nuitka"
    ccache_dir = tmp_path / "ccache"
    ccache_exe = ccache_dir / "ccache.exe"
    ccache_exe.parent.mkdir(parents=True)
    ccache_exe.write_bytes(b"")

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.WINDOWS, st)
    assert result == ccache_exe


def test_ensure_ccache_migrates_nested_local_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地缓存根目录无 ccache 但有 ccache-<ver>-<platform>/ 子目录时自动迁移复用.

    用户已下载旧版 ccache（解压后子目录结构未迁移），_ensure_ccache 应识别并
    迁移到根目录，避免重新下载。覆盖 Windows 反复下载的真实场景。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "nuitka"
    ccache_dir = tmp_path / "ccache"
    nested_dir = ccache_dir / "ccache-4.10.2-windows-x86_64"
    nested_dir.mkdir(parents=True)
    (nested_dir / "ccache.exe").write_bytes(b"fake-exe")
    (nested_dir / "LICENSE.html").write_bytes(b"license")
    # 根目录无 ccache.exe（旧 bug 导致未迁移）

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    # 拦截下载，确保不触发
    download_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "_download_and_extract_ccache",
        staticmethod(lambda *a, **kw: download_called.__setitem__("n", download_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.WINDOWS, st)

    # 返回迁移后的根目录 ccache.exe
    assert result == ccache_dir / "ccache.exe"
    assert (ccache_dir / "ccache.exe").is_file()
    # 子目录已清理
    assert not nested_dir.exists()
    assert not list(ccache_dir.glob("ccache-*/"))
    # 未触发下载
    assert download_called["n"] == 0


def test_ensure_ccache_migrates_nested_local_cache_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 本地缓存子目录结构同样支持自动迁移."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "nuitka"
    ccache_dir = tmp_path / "ccache"
    nested_dir = ccache_dir / "ccache-4.10.2-linux-x86_64"
    nested_dir.mkdir(parents=True)
    (nested_dir / "ccache").write_bytes(b"#!/bin/sh\nexit 0\n")

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    download_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "_download_and_extract_ccache",
        staticmethod(lambda *a, **kw: download_called.__setitem__("n", download_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.LINUX, st)

    assert result == ccache_dir / "ccache"
    assert (ccache_dir / "ccache").is_file()
    assert not list(ccache_dir.glob("ccache-*/"))
    assert download_called["n"] == 0


def test_ensure_ccache_unsupported_platform_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无预编译二进制的平台（理论上所有平台都有，但 mock URL 缺失时）返回 None."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    # 清空 URL 映射模拟不支持的平台
    monkeypatch.setattr("fspack.packaging.nuitka.ccache.CCACHE_URLS", {})
    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)
    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(tmp_path, Platform.LINUX, st)
    assert result is None


def test_ensure_ccache_download_failure_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载失败时回退到 None（warning 不中断）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)

    def _fail_download(url: str, ccache_dir: Path, target: object) -> None:
        raise OSError("network error")

    monkeypatch.setattr(NuitkaCompiler, "_download_and_extract_ccache", staticmethod(_fail_download))
    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(tmp_path, Platform.LINUX, st)
    assert result is None


def test_compile_src_passes_ccache_to_compile_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ccache=True 时 compile_src 调 _ensure_ccache 并传 ccache_exe 到 _compile_files."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("x = 1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = tmp_path / "nuitka"
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("")

    captured_ccache: list[Path | None] = []

    def fake_compile_files(cls: Any, *args: Any, **kwargs: Any) -> tuple[set[Path], list[Path]]:
        captured_ccache.append(kwargs.get("ccache_exe"))
        return set(), []

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))

    fake_ccache = tmp_path / "ccache_bin"
    fake_ccache.write_bytes(b"")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_ccache", classmethod(lambda cls, *a, **kw: fake_ccache))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st, ccache=True, cache_root=cache)
    assert len(captured_ccache) == 1
    assert captured_ccache[0] == fake_ccache


def test_compile_src_ccache_false_skips_ensure_ccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ccache=False 时不调 _ensure_ccache，_compile_files 收到 ccache_exe=None."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("x = 1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = tmp_path / "nuitka"
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("")

    ensure_called: list[bool] = []

    def fake_ensure_ccache(cls: Any, *a: Any, **kw: Any) -> None:
        ensure_called.append(True)

    monkeypatch.setattr(NuitkaCompiler, "_ensure_ccache", classmethod(fake_ensure_ccache))

    captured: list[Path | None] = []

    def fake_compile_files(cls: Any, *args: Any, **kwargs: Any) -> tuple[set[Path], list[Path]]:
        captured.append(kwargs.get("ccache_exe"))
        return set(), []

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))

    st = StageRecorder("Nuitka 编译")
    # 用 Platform.WINDOWS 匹配 runtime/python.exe（Linux 路径为 runtime/python/bin/python3.11）
    NuitkaCompiler.compile_src(
        src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st, ccache=False, cache_root=cache
    )
    assert not ensure_called  # ccache=False 不调 _ensure_ccache
    assert captured[0] is None  # ccache_exe=None


def test_compile_with_stamp_passes_ccache_to_compile_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_with_stamp 透传 ccache=True 到 compile_src."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = tmp_path / "nuitka"

    captured_ccache: list[bool] = []

    def fake_compile_src(cls: Any, *args: Any, **kwargs: Any) -> list[str]:
        captured_ccache.append(kwargs.get("ccache", False))
        return []

    def fake_ensure_env(cls: Any, *args: Any, **kwargs: Any) -> str:
        return "4.1.3"

    def fake_is_nuitka_cached(cache_dir: Any) -> bool:
        return True

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(fake_compile_src))
    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(fake_ensure_env))
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_nuitka_cached))
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: None),
    )
    # 让 stamp 未命中
    monkeypatch.setattr(NuitkaCompiler, "_stamp_path", staticmethod(lambda dist: tmp_path / "nonexistent_stamp"))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, tmp_path, runtime, "3.11.9", Platform.LINUX, get_mirror("huawei"), cache, stage=st, ccache=True
    )
    assert captured_ccache[0] is True


# ---- _download_and_extract_ccache 与 _ensure_ccache 成功路径测试 ----


def _make_ccache_linux_tarball(dest: Path) -> None:
    """构造 ccache Linux tarball：内含 ccache-<ver>-linux-x86_64/ccache."""
    inner_dir = "ccache-4.10.2-linux-x86_64"
    with tarfile.open(dest, "w:xz") as tf:
        data = b"#!/bin/sh\nexit 0\n"
        info = tarfile.TarInfo(f"{inner_dir}/ccache")
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))


def _make_ccache_windows_zip(dest: Path) -> None:
    """构造 ccache Windows zip：内含 ccache-<ver>-windows-x86_64/ccache.exe.

    真实 ccache releases 的 Windows zip 解压后是 ``ccache-<ver>-windows-x86_64/``
    子目录，内含 ``ccache.exe`` 与 LICENSE/MANUAL 等附属文件。
    """
    inner_dir = "ccache-4.10.2-windows-x86_64"
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(f"{inner_dir}/ccache.exe", b"fake-exe")
        zf.writestr(f"{inner_dir}/LICENSE.html", b"license")
        zf.writestr(f"{inner_dir}/README.md", b"readme")


def test_download_and_extract_ccache_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 归档解压后 ccache 从 ccache-<ver>-linux-x86_64/ccache 移到根目录."""
    from fspack.packaging.nuitka import NuitkaCompiler

    ccache_dir = tmp_path / "ccache"

    class _StubDownloader:
        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_ccache_linux_tarball(dest)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _StubDownloader)
    NuitkaCompiler._download_and_extract_ccache("http://fake/ccache.tar.xz", ccache_dir, Platform.LINUX)

    # ccache 已从内层目录移到根
    assert (ccache_dir / "ccache").is_file()
    # 内层目录已清理
    assert not list(ccache_dir.glob("ccache-*/"))
    # tarball 已删除
    assert not (ccache_dir / "ccache.tar.xz").exists()


def test_download_and_extract_ccache_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows zip 解压后 ccache.exe 从 ccache-<ver>-windows-x86_64/ 移到根目录."""
    from fspack.packaging.nuitka import NuitkaCompiler

    ccache_dir = tmp_path / "ccache"

    class _StubDownloader:
        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_ccache_windows_zip(dest)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _StubDownloader)
    NuitkaCompiler._download_and_extract_ccache("http://fake/ccache.zip", ccache_dir, Platform.WINDOWS)

    # ccache.exe 已从内层目录移到根
    assert (ccache_dir / "ccache.exe").is_file()
    # 内层目录（含 LICENSE/README 等）已清理
    assert not list(ccache_dir.glob("ccache-*/"))
    # zip 已删除
    assert not (ccache_dir / "ccache.zip").exists()


def test_ensure_ccache_download_success_returns_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 无 ccache 且本地缓存无时下载预编译二进制成功，返回 ccache 路径并设 stage."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)

    def _fake_download_and_extract(url: str, ccache_dir: Path, target: object) -> None:
        ccache_dir.mkdir(parents=True, exist_ok=True)
        exe_name = "ccache.exe" if target is Platform.WINDOWS else "ccache"
        (ccache_dir / exe_name).write_bytes(b"fake")

    monkeypatch.setattr(NuitkaCompiler, "_download_and_extract_ccache", staticmethod(_fake_download_and_extract))

    st = StageRecorder("Nuitka 编译")
    cache_root = tmp_path / "nuitka"
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.WINDOWS, st)
    assert result is not None
    assert result.name == "ccache.exe"
    assert "已下载" in st._detail


def test_ensure_ccache_download_success_linux_chmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 下载成功后 chmod 0o755（容错 OSError 不中断）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.platform import Platform
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)

    def _fake_download_and_extract(url: str, ccache_dir: Path, target: object) -> None:
        ccache_dir.mkdir(parents=True, exist_ok=True)
        (ccache_dir / "ccache").write_bytes(b"fake")

    monkeypatch.setattr(NuitkaCompiler, "_download_and_extract_ccache", staticmethod(_fake_download_and_extract))

    st = StageRecorder("Nuitka 编译")
    # Linux chmod 在 Windows 上无意义但不抛错（contextlib.suppress 容错）
    result = NuitkaCompiler._ensure_ccache(tmp_path / "nuitka", Platform.LINUX, st)
    assert result is not None
    assert result.name == "ccache"


def test_ensure_ccache_download_succeeds_but_exe_missing_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载"成功"但 ccache_exe 未出现在预期路径时返回 None（warning 不中断）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", lambda name: None)

    def _empty_download(url: str, ccache_dir: Path, target: object) -> None:
        ccache_dir.mkdir(parents=True, exist_ok=True)
        # 不写 ccache.exe，模拟归档内容异常

    monkeypatch.setattr(NuitkaCompiler, "_download_and_extract_ccache", staticmethod(_empty_download))

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler._ensure_ccache(tmp_path / "nuitka", Platform.WINDOWS, st)
    assert result is None
