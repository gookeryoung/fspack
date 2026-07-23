"""内置库打包测试：TkinterBundler 的 URL 生成、需求检测、缓存策略与 zip 提取."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from fspack.exceptions import BuiltinError
from fspack.packaging.builtin import TkinterBundler
from fspack.packaging.runtime import STANDALONE_BASE_URL, STANDALONE_RELEASE_TAG
from fspack.platform import Platform
from fspack.progress import StageRecorder


def test_standalone_windows_tarball_name() -> None:
    """Windows tarball 文件名遵循 cpython-{ver}+{tag}-x86_64-pc-windows-msvc-install_only 模式."""
    name = TkinterBundler.standalone_windows_tarball_name("3.11.9", "20241016")
    assert name == "cpython-3.11.9+20241016-x86_64-pc-windows-msvc-shared-install_only.tar.gz"


def test_standalone_windows_url() -> None:
    """URL 由 BASE_URL/release_tag/tarball_name 拼接."""
    url = TkinterBundler.standalone_windows_url("3.11.9", "20241016")
    assert url.startswith(STANDALONE_BASE_URL)
    assert "20241016" in url
    assert "3.11.9" in url
    assert url.endswith("x86_64-pc-windows-msvc-shared-install_only.tar.gz")


def test_is_needed_windows_with_tkinter() -> None:
    """Windows 目标且 AST 检出 tkinter → True."""
    assert TkinterBundler.is_needed(("tkinter", "os"), Platform.WINDOWS) is True


def test_is_needed_windows_without_tkinter() -> None:
    """Windows 目标但 AST 未检出 tkinter → False."""
    assert TkinterBundler.is_needed(("os", "sys"), Platform.WINDOWS) is False


def test_is_needed_linux_with_tkinter() -> None:
    """Linux 目标（standalone 已含 tkinter）→ False，无需补充."""
    assert TkinterBundler.is_needed(("tkinter",), Platform.LINUX) is False


def test_is_needed_linux_without_tkinter() -> None:
    """Linux 目标且无 tkinter → False."""
    assert TkinterBundler.is_needed(("os",), Platform.LINUX) is False


def test_is_needed_empty_stdlib() -> None:
    """空 stdlib 元组 → False."""
    assert TkinterBundler.is_needed((), Platform.WINDOWS) is False


def _make_tkinter_tarball(path: Path) -> None:
    """构造模拟 python-build-standalone Windows 构建的 tarball.

    结构：
    - python/install/Lib/tkinter/__init__.py + dialog.py
    - python/install/DLLs/_tkinter.pyd
    - python/install/tcl/tcl8.6/init.tcl
    - python/install/tcl/tk8.6/tk.tcl
    """
    members = [
        ("python/install/Lib/tkinter/__init__.py", b"# tkinter package"),
        ("python/install/Lib/tkinter/dialog.py", b"# dialog"),
        ("python/install/DLLs/_tkinter.pyd", b"PYD_BINARY"),
        ("python/install/tcl/tcl8.6/init.tcl", b"# tcl init"),
        ("python/install/tcl/tcl8.6/http1.0/http.tcl", b"# http"),
        ("python/install/tcl/tk8.6/tk.tcl", b"# tk lib"),
        ("python/install/tcl/tk8.6/entry.tcl", b"# entry"),
        # 干扰项：非 tkinter 相关文件，应被忽略
        ("python/install/python311.dll", b"DLL"),
        ("python/install/Lib/os.py", b"# os stdlib"),
    ]
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_build_tkinter_zip_extracts_all_components(tmp_path: Path) -> None:
    """_build_tkinter_zip 从 tarball 提取四类组件并映射到正确目录."""
    tar_path = tmp_path / "fake.tar.gz"
    _make_tkinter_tarball(tar_path)

    zip_data = TkinterBundler._build_tkinter_zip(tar_path)
    with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
        names = set(zf.namelist())

    # tkinter 纯 Python 包 → Lib/tkinter/...
    assert "Lib/tkinter/__init__.py" in names
    assert "Lib/tkinter/dialog.py" in names
    # _tkinter C 扩展 → 根目录
    assert "_tkinter.pyd" in names
    # tcl 运行时 → tcl/tcl8.6/...
    assert "tcl/tcl8.6/init.tcl" in names
    assert "tcl/tcl8.6/http1.0/http.tcl" in names
    # tk 运行时 → tcl/tk8.6/...
    assert "tcl/tk8.6/tk.tcl" in names
    assert "tcl/tk8.6/entry.tcl" in names
    # 干扰项被排除
    assert "python311.dll" not in names
    assert not any(n.startswith("python/install/") for n in names)
    assert "Lib/os.py" not in names


def test_build_tkinter_zip_preserves_content(tmp_path: Path) -> None:
    """提取的文件内容与 tarball 中一致."""
    tar_path = tmp_path / "fake.tar.gz"
    _make_tkinter_tarball(tar_path)

    zip_data = TkinterBundler._build_tkinter_zip(tar_path)
    with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
        assert zf.read("Lib/tkinter/__init__.py") == b"# tkinter package"
        assert zf.read("_tkinter.pyd") == b"PYD_BINARY"
        assert zf.read("tcl/tcl8.6/init.tcl") == b"# tcl init"
        assert zf.read("tcl/tk8.6/tk.tcl") == b"# tk lib"


def test_build_tkinter_zip_no_tkinter_raises(tmp_path: Path) -> None:
    """tarball 中无 tkinter 包时抛 BuiltinError."""
    tar_path = tmp_path / "no-tkinter.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="python/install/Lib/os.py")
        info.size = len(b"# os")
        tf.addfile(info, io.BytesIO(b"# os"))

    with pytest.raises(BuiltinError, match="未找到 tkinter 包"):
        TkinterBundler._build_tkinter_zip(tar_path)


def test_unpack_tkinter_zip_extracts_to_runtime(tmp_path: Path) -> None:
    """_unpack_tkinter_zip 将 zip 内容解压到 runtime 目录."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    zip_path = tmp_path / "tk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Lib/tkinter/__init__.py", b"# tk")
        zf.writestr("_tkinter.pyd", b"PYD")
        zf.writestr("tcl/tcl8.6/init.tcl", b"# tcl")

    TkinterBundler._unpack_tkinter_zip(zip_path, runtime)
    assert (runtime / "Lib" / "tkinter" / "__init__.py").read_bytes() == b"# tk"
    assert (runtime / "_tkinter.pyd").read_bytes() == b"PYD"
    assert (runtime / "tcl" / "tcl8.6" / "init.tcl").read_bytes() == b"# tcl"


def test_ensure_skips_when_runtime_has_tkinter(tmp_path: Path) -> None:
    """runtime 已含 Lib/tkinter/__init__.py → 命中跳过，不下载."""
    runtime = tmp_path / "runtime"
    (runtime / "Lib" / "tkinter").mkdir(parents=True)
    (runtime / "Lib" / "tkinter" / "__init__.py").write_text("")

    rec = StageRecorder("test")
    TkinterBundler.ensure(runtime, "3.11.9", tmp_path / "cache", stage=rec)
    record = rec._finalize()
    assert record.cache_hit == 1
    # 未创建缓存目录（未触发下载分支）
    assert not (tmp_path / "cache" / "tkinter").exists()


def test_ensure_unpacks_from_cache_zip(tmp_path: Path) -> None:
    """缓存 zip 存在 → 直接解压到 runtime，不下载 tarball."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_dir = tmp_path / "cache"
    tkinter_cache = cache_dir / "tkinter"
    tkinter_cache.mkdir(parents=True)
    cache_zip = tkinter_cache / "tkinter-3.11.9.zip"
    with zipfile.ZipFile(cache_zip, "w") as zf:
        zf.writestr("Lib/tkinter/__init__.py", b"# from cache")
        zf.writestr("_tkinter.pyd", b"PYD")

    rec = StageRecorder("test")
    TkinterBundler.ensure(runtime, "3.11.9", cache_dir, stage=rec)

    assert (runtime / "Lib" / "tkinter" / "__init__.py").read_bytes() == b"# from cache"
    assert (runtime / "_tkinter.pyd").read_bytes() == b"PYD"
    # 未下载 standalone tarball
    assert not (cache_dir / "standalone-windows").exists()


def test_ensure_downloads_and_caches_when_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无缓存 → 下载 tarball → 提取 → 生成缓存 zip → 解压到 runtime."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_dir = tmp_path / "cache"

    # 准备 fake tarball，让 Downloader.download 把它写入目标路径
    fake_tar = tmp_path / "fake.tar.gz"
    _make_tkinter_tarball(fake_tar)

    def fake_download(self: object, url: str, dest: Path, **kwargs: object) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(fake_tar.read_bytes())
        return fake_tar.stat().st_size

    monkeypatch.setattr("fspack.packaging.builtin.Downloader.download", fake_download)

    rec = StageRecorder("test")
    TkinterBundler.ensure(runtime, "3.11.9", cache_dir, stage=rec)

    # runtime 已补充 tkinter
    assert (runtime / "Lib" / "tkinter" / "__init__.py").is_file()
    assert (runtime / "_tkinter.pyd").is_file()
    assert (runtime / "tcl" / "tcl8.6" / "init.tcl").is_file()
    assert (runtime / "tcl" / "tk8.6" / "tk.tcl").is_file()

    # 缓存 zip 已生成
    cache_zip = cache_dir / "tkinter" / "tkinter-3.11.9.zip"
    assert cache_zip.is_file()
    with zipfile.ZipFile(cache_zip, "r") as zf:
        assert "Lib/tkinter/__init__.py" in zf.namelist()
        assert "_tkinter.pyd" in zf.namelist()

    # standalone tarball 也被缓存
    tarball_name = TkinterBundler.standalone_windows_tarball_name("3.11.9", STANDALONE_RELEASE_TAG)
    assert (cache_dir / "standalone-windows" / tarball_name).is_file()


def test_ensure_reuses_cached_tarball(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """standalone tarball 已缓存 → 不重新下载，直接提取."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_dir = tmp_path / "cache"
    standalone_cache = cache_dir / "standalone-windows"
    standalone_cache.mkdir(parents=True)
    tarball_name = TkinterBundler.standalone_windows_tarball_name("3.11.9", STANDALONE_RELEASE_TAG)
    tarball_path = standalone_cache / tarball_name
    _make_tkinter_tarball(tarball_path)

    download_called = {"count": 0}

    def fake_download(self: object, url: str, dest: Path, **kwargs: object) -> int:
        download_called["count"] += 1
        return 0

    monkeypatch.setattr("fspack.packaging.builtin.Downloader.download", fake_download)

    rec = StageRecorder("test")
    TkinterBundler.ensure(runtime, "3.11.9", cache_dir, stage=rec)

    assert download_called["count"] == 0
    assert (runtime / "Lib" / "tkinter" / "__init__.py").is_file()
    # tarball 缓存命中时 stage.hit_cache 被调用
    record = rec._finalize()
    assert record.cache_hit == 1
