"""``NuitkaStandalone`` 构建 Python 就绪测试：standalone tarball 下载/缓存与宿主 Python 复用."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from fspack.config import (
    KNOWN_STANDALONE_VERSIONS,
)
from fspack.exceptions import NuitkaError
from fspack.packaging.nuitka import NuitkaCompiler
from fspack.packaging.runtime import STANDALONE_RELEASE_TAG, standalone_tarball_name
from fspack.platform import Platform
from fspack.progress import StageRecorder

# ---- _build_python_cache_dir 与 _build_python_exe 路径解析测试 ----


def test_build_python_cache_dir(tmp_path: Path) -> None:
    """_build_python_cache_dir 返回 cache_root / py_version（按版本隔离避免 ABI 冲突）."""
    cache_root = tmp_path / "python_cache"
    result = NuitkaCompiler._build_python_cache_dir(cache_root, "3.11.15")
    assert result == cache_root / "3.11.15"


def test_build_python_exe_windows(tmp_path: Path) -> None:
    """Windows standalone python 路径为 <dir>/python/python.exe."""
    build_dir = tmp_path / "3.11.15"
    result = NuitkaCompiler._build_python_exe(build_dir, "3.11.15", Platform.WINDOWS)
    assert result == build_dir / "python" / "python.exe"


def test_build_python_exe_linux(tmp_path: Path) -> None:
    """Linux standalone python 路径为 <dir>/python/bin/python{major}.{minor}."""
    build_dir = tmp_path / "3.11.15"
    result = NuitkaCompiler._build_python_exe(build_dir, "3.11.15", Platform.LINUX)
    assert result == build_dir / "python" / "bin" / "python3.11"


def test_build_python_exe_linux_freethreaded(tmp_path: Path) -> None:
    """free-threaded 版本 Linux 路径为 python/bin/python3.13t（与 python.org 一致）.

    标准版二进制名 ``python3.13``，free-threaded build 为 ``python3.13t``，
    两者不互通（ABI 不一致），需用 t 后缀定位正确可执行文件。
    """
    build_dir = tmp_path / "3.13.14t"
    result = NuitkaCompiler._build_python_exe(build_dir, "3.13.14t", Platform.LINUX)
    assert result == build_dir / "python" / "bin" / "python3.13t"
    build_dir_314 = tmp_path / "3.14.6t"
    result_314 = NuitkaCompiler._build_python_exe(build_dir_314, "3.14.6t", Platform.LINUX)
    assert result_314 == build_dir_314 / "python" / "bin" / "python3.14t"


# ---- _ensure_build_python standalone python 就绪测试 ----


@pytest.fixture
def no_host_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁用构建机 python 复用，强制走 standalone 下载/缓存路径.

    测试环境（Windows + 与目标同 minor 的 python）会命中 ``_host_python_exe``
    复用分支提前返回，覆盖不到下载/缓存逻辑，故显式关闭。
    """
    monkeypatch.setattr(NuitkaCompiler, "_host_python_exe", staticmethod(lambda py_version: None))


def _make_standalone_tarball(dest: Path, version: str, tag: str, *, with_python: bool = True) -> None:
    """构造 standalone python tarball，模拟 python-build-standalone 解压结构.

    真实 tarball 结构：``cpython-<base>+<tag>-x86_64-pc-windows-msvc[-freethreaded]-install_only/python/python.exe``。
    ``with_python=False`` 时内层无 ``python/`` 目录，用于模拟结构异常场景。

    free-threaded build（``version`` 末尾 ``t`` 后缀）在平台三元组与 ``install_only``
    之间插入 ``-freethreaded``（**版本号无 t 后缀**），与
    :func:`fspack.packaging.runtime.standalone_tarball_name` 命名一致。
    """
    from fspack.config.versions import _split_t_suffix

    base, is_t = _split_t_suffix(version)
    freethreaded_segment = "-freethreaded" if is_t else ""
    inner_root = f"cpython-{base}+{tag}-x86_64-pc-windows-msvc{freethreaded_segment}-install_only"
    with tarfile.open(dest, "w:gz") as tf:
        if with_python:
            data = b"fake-python-exe"
            info = tarfile.TarInfo(f"{inner_root}/python/python.exe")
        else:
            data = b"readme"
            info = tarfile.TarInfo(f"{inner_root}/README.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


def test_ensure_build_python_linux_returns_placeholder(tmp_path: Path) -> None:
    """Linux runtime 已是完整 standalone，返回空 Path 占位（compile_src 内部回退 runtime python）."""
    cache_root = tmp_path / "cache"
    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.15", Platform.LINUX, stage=st)
    assert result == Path()
    # Linux 分支不触发下载：缓存目录不应被创建
    assert not cache_root.exists()


def test_ensure_build_python_unknown_version_raises(tmp_path: Path) -> None:
    """py_version 的 major.minor 不在 KNOWN_STANDALONE_VERSIONS 时 raise NuitkaError."""
    st = StageRecorder("standalone python")
    with pytest.raises(NuitkaError, match="无对应 python-build-standalone Windows 版本"):
        NuitkaCompiler._ensure_build_python(tmp_path / "cache", "3.15.0", Platform.WINDOWS, stage=st)


def test_ensure_build_python_cache_hit_skips_download(tmp_path: Path, no_host_python: None) -> None:
    """standalone python.exe 已存在时缓存命中，跳过下载并标注 stage."""
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]
    cache_root = tmp_path / "cache"
    build_dir = NuitkaCompiler._build_python_cache_dir(cache_root, ver)
    py_exe = NuitkaCompiler._build_python_exe(build_dir, ver, Platform.WINDOWS)
    py_exe.parent.mkdir(parents=True)
    py_exe.write_bytes(b"fake-python")

    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.9", Platform.WINDOWS, stage=st)

    assert result == py_exe
    assert st._hits == 1
    assert "已就绪" in st._detail


def test_ensure_build_python_download_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_python: None
) -> None:
    """下载 standalone python 失败（OSError）时包装为 NuitkaError."""

    class _FailDownloader:
        """Downloader 桩：模拟网络失败."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            raise OSError("network unreachable")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _FailDownloader)

    st = StageRecorder("standalone python")
    with pytest.raises(NuitkaError, match="下载 standalone python 失败"):
        NuitkaCompiler._ensure_build_python(tmp_path / "cache", "3.11.9", Platform.WINDOWS, stage=st)


def test_ensure_build_python_download_extract_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_python: None
) -> None:
    """下载并解压 standalone python 成功：内层 python/ 提升到缓存根，解压根被清理.

    tarball 保留在共享缓存目录 ``<cache根>/standalone-windows/``（与
    TkinterBundler 共享，供 tkinter 提取复用）。
    """
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]
    cache_root = tmp_path / "cache"

    class _OKDownloader:
        """Downloader 桩：写入真实 tarball 供解压流程测试."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_standalone_tarball(dest, ver, STANDALONE_RELEASE_TAG)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _OKDownloader)

    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.9", Platform.WINDOWS, stage=st)

    expected_exe = cache_root / ver / "python" / "python.exe"
    assert result == expected_exe
    assert expected_exe.is_file()
    # tarball 保留在共享缓存目录（供 tkinter 打包复用，不再删除）
    shared_tarball = (
        cache_root.parent / "standalone-windows" / standalone_tarball_name(ver, STANDALONE_RELEASE_TAG, windows=True)
    )
    assert shared_tarball.is_file()
    # 内层解压根（share/doc 等）已清理
    inner_root = cache_root / ver / f"cpython-{ver}+{STANDALONE_RELEASE_TAG}-x86_64-pc-windows-msvc-install_only"
    assert not inner_root.exists()
    assert "安装完成" in st._detail


def test_ensure_build_python_corrupt_tarball_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_python: None
) -> None:
    """tarball 损坏（无法解压）时 raise NuitkaError."""

    class _CorruptDownloader:
        """Downloader 桩：写入非 gzip 内容模拟损坏 tarball."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            dest.write_bytes(b"not-a-gzip-file")
            return 16

    monkeypatch.setattr("fspack.packaging.net.Downloader", _CorruptDownloader)

    st = StageRecorder("standalone python")
    with pytest.raises(NuitkaError, match="standalone python tarball 损坏"):
        NuitkaCompiler._ensure_build_python(tmp_path / "cache", "3.11.9", Platform.WINDOWS, stage=st)


def test_ensure_build_python_missing_exe_after_extract_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_host_python: None
) -> None:
    """解压后未找到 python.exe（tarball 结构异常）时 raise NuitkaError."""
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]

    class _NoPythonDownloader:
        """Downloader 桩：tarball 内层无 python/ 目录."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_standalone_tarball(dest, ver, STANDALONE_RELEASE_TAG, with_python=False)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _NoPythonDownloader)

    st = StageRecorder("standalone python")
    with pytest.raises(NuitkaError, match="standalone python 解压后未找到"):
        NuitkaCompiler._ensure_build_python(tmp_path / "cache", "3.11.9", Platform.WINDOWS, stage=st)


def test_ensure_build_python_injects_win7_compat_dll_on_extract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_host_python: None,
) -> None:
    """解压 standalone python 后注入 api-ms-win-core-path-l1-1-0.dll 到 python.exe 同目录.

    Python 3.9+ 在 Win7 上启动需此 DLL，standalone python 同样需要（与 embed runtime
    同样需要）。未注入会导致 fspack 自身打包后在 Win7 上调用 standalone python
    运行 nuitka 时启动失败。
    """
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]
    cache_root = tmp_path / "cache"

    class _OKDownloader:
        """Downloader 桩：写入真实 tarball 供解压流程测试."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            _make_standalone_tarball(dest, ver, STANDALONE_RELEASE_TAG)
            return dest.stat().st_size

    monkeypatch.setattr("fspack.packaging.net.Downloader", _OKDownloader)

    st = StageRecorder("standalone python")
    py_exe = NuitkaCompiler._ensure_build_python(cache_root, "3.11.9", Platform.WINDOWS, stage=st)

    # standalone python 目录应有 Win7 兼容 DLL（与 python.exe 同目录）
    dll = py_exe.parent / "api-ms-win-core-path-l1-1-0.dll"
    assert dll.is_file()
    # DLL 应为非空二进制（~114KB x64 构建）
    assert dll.stat().st_size > 10000


def test_ensure_build_python_injects_win7_compat_dll_on_cache_hit(
    tmp_path: Path,
    no_host_python: None,
) -> None:
    """缓存命中时也注入 Win7 兼容 DLL（用户清理过 DLL 但保留 python.exe 时补充）.

    注入逻辑幂等：DLL 已存在则跳过，缺失则补充。覆盖缓存命中场景下 DLL 缺失的修复。
    """
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]
    cache_root = tmp_path / "cache"
    build_dir = NuitkaCompiler._build_python_cache_dir(cache_root, ver)
    py_exe = NuitkaCompiler._build_python_exe(build_dir, ver, Platform.WINDOWS)
    py_exe.parent.mkdir(parents=True)
    py_exe.write_bytes(b"fake-python")
    # 不预置 DLL，模拟用户清理过 DLL 但保留 python.exe

    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.9", Platform.WINDOWS, stage=st)

    assert result == py_exe
    # 缓存命中后仍补充注入 DLL
    dll = py_exe.parent / "api-ms-win-core-path-l1-1-0.dll"
    assert dll.is_file()
    assert dll.stat().st_size > 10000


def test_ensure_build_python_skips_win7_compat_dll_for_linux(tmp_path: Path) -> None:
    """Linux 分支早返回，不下载 standalone python 也不注入 Win7 兼容 DLL."""
    cache_root = tmp_path / "cache"
    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.15", Platform.LINUX, stage=st)
    assert result == Path()
    # Linux 不创建缓存目录，自然无 DLL 注入
    assert not cache_root.exists()


# ---- 构建机 python 复用（_host_python_exe）测试 ----


def test_host_python_exe_matches_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Windows 原生构建 + major.minor 匹配时返回 sys.executable（免下载 standalone）."""
    fake_exe = tmp_path / "python.exe"
    fake_exe.write_bytes(b"fake")
    # SimpleNamespace 模拟 version_info（代码仅访问 .major/.minor）
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.platform", "win32")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.version_info", SimpleNamespace(major=3, minor=11))
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.executable", str(fake_exe))
    assert NuitkaCompiler._host_python_exe("3.11.9") == fake_exe
    # 补丁版本差异不影响（CPython 按 major.minor ABI 兼容）
    assert NuitkaCompiler._host_python_exe("3.11.15") == fake_exe


def test_host_python_exe_version_mismatch_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """构建机与目标 major.minor 不一致时返回 None（Nuitka 必须在目标版本下运行）."""
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.platform", "win32")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.version_info", SimpleNamespace(major=3, minor=12))
    assert NuitkaCompiler._host_python_exe("3.11.9") is None


def test_host_python_exe_non_windows_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 构建机（如 Linux 交叉编译 Windows 目标）返回 None（ELF 无法运行）."""
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.platform", "linux")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.version_info", SimpleNamespace(major=3, minor=11))
    assert NuitkaCompiler._host_python_exe("3.11.9") is None


def test_host_python_exe_missing_executable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """sys.executable 不存在（异常环境）时返回 None，回退下载分支."""
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.platform", "win32")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.version_info", SimpleNamespace(major=3, minor=11))
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.executable", "Z:/nonexistent/python.exe")
    assert NuitkaCompiler._host_python_exe("3.11.9") is None


def test_host_python_exe_freethreaded_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """目标 t 版本 + 构建机是 free-threaded build（GIL disabled）+ Windows + 同 minor → 复用.

    ``sys._is_gil_enabled()`` 在 free-threaded build 返回 False，与目标 t 变体
    一致即可复用 sys.executable 编译（避免下载 ~40MB tarball）。
    """
    fake_exe = tmp_path / "python.exe"
    fake_exe.write_bytes(b"fake")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.platform", "win32")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.version_info", SimpleNamespace(major=3, minor=13))
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.executable", str(fake_exe))
    # free-threaded build：_is_gil_enabled 返回 False
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys._is_gil_enabled", lambda: False, raising=False)
    assert NuitkaCompiler._host_python_exe("3.13.14t") == fake_exe


def test_host_python_exe_freethreaded_target_standard_host_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """目标 t 版本 + 构建机是标准版（GIL enabled）→ 返回 None，避免 ABI 不兼容.

    标准 CPython 的 ``_is_gil_enabled()`` 返回 True，与 t 变体 ABI 不互通
    （python313t.dll vs python313.dll），Nuitka 编译产物需与 runtime 同源 t 变体。
    """
    fake_exe = tmp_path / "python.exe"
    fake_exe.write_bytes(b"fake")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.platform", "win32")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.version_info", SimpleNamespace(major=3, minor=13))
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.executable", str(fake_exe))
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys._is_gil_enabled", lambda: True, raising=False)
    assert NuitkaCompiler._host_python_exe("3.13.14t") is None


def test_host_python_exe_standard_target_freethreaded_host_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """目标标准版 + 构建机是 free-threaded build（GIL disabled）→ 返回 None.

    反向场景：构建机是 t 变体但目标标准版，ABI 同样不互通。
    """
    fake_exe = tmp_path / "python.exe"
    fake_exe.write_bytes(b"fake")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.platform", "win32")
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.version_info", SimpleNamespace(major=3, minor=13))
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys.executable", str(fake_exe))
    monkeypatch.setattr("fspack.packaging.nuitka.standalone.sys._is_gil_enabled", lambda: False, raising=False)
    assert NuitkaCompiler._host_python_exe("3.13.14") is None


def test_ensure_build_python_reuses_host_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构建机 python 版本匹配时直接复用 sys.executable，不创建缓存目录不下载."""
    fake_exe = tmp_path / "python.exe"
    fake_exe.write_bytes(b"fake")
    monkeypatch.setattr(
        NuitkaCompiler,
        "_host_python_exe",
        staticmethod(lambda py_version: fake_exe),
    )
    cache_root = tmp_path / "cache"
    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.9", Platform.WINDOWS, stage=st)
    assert result == fake_exe
    # 复用分支不触碰 standalone 缓存目录
    assert not cache_root.exists()
    assert "复用构建机 python" in st._detail


def test_ensure_build_python_shared_tarball_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_host_python: None,
) -> None:
    """共享缓存目录已有 standalone tarball 时免下载（tkinter 打包阶段已下载过）.

    验证 nuitka 与 TkinterBundler 共享 ``standalone-windows/`` 缓存：
    tarball 已存在则不触发网络请求，直接解压。
    """
    ver = KNOWN_STANDALONE_VERSIONS["3.11"]
    cache_root = tmp_path / "cache"
    shared_dir = tmp_path / "standalone-windows"
    shared_dir.mkdir()
    tarball = shared_dir / standalone_tarball_name(ver, STANDALONE_RELEASE_TAG, windows=True)
    _make_standalone_tarball(tarball, ver, STANDALONE_RELEASE_TAG)

    class _NoNetDownloader:
        """Downloader 桩：共享缓存命中时不应被实例化调用."""

        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            raise AssertionError("共享缓存命中时不应触发下载")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _NoNetDownloader)

    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.11.9", Platform.WINDOWS, stage=st)

    assert result == cache_root / ver / "python" / "python.exe"
    assert st._hits == 1
    # tarball 保留（共享缓存不被解压流程删除）
    assert tarball.is_file()


def test_ensure_build_python_freethreaded_shared_tarball_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_host_python: None,
) -> None:
    """free-threaded 版本（3.13t）共享缓存命中 + 解压根含 -freethreaded 段.

    python-build-standalone 的 free-threaded tarball 名与解压根目录名均含
    ``-freethreaded`` 段（版本号无 t 后缀），与 ``standalone_tarball_name`` 命名一致。
    """
    ver = KNOWN_STANDALONE_VERSIONS["3.13t"]
    assert ver == "3.13.14t"
    cache_root = tmp_path / "cache"
    shared_dir = tmp_path / "standalone-windows"
    shared_dir.mkdir()
    tarball = shared_dir / standalone_tarball_name(ver, STANDALONE_RELEASE_TAG, windows=True)
    # tarball 文件名含 -freethreaded-install_only.tar.gz 后缀
    assert tarball.name.endswith("-freethreaded-install_only.tar.gz")
    _make_standalone_tarball(tarball, ver, STANDALONE_RELEASE_TAG)

    class _NoNetDownloader:
        def __init__(self, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            raise AssertionError("共享缓存命中时不应触发下载")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _NoNetDownloader)

    st = StageRecorder("standalone python")
    result = NuitkaCompiler._ensure_build_python(cache_root, "3.13.14t", Platform.WINDOWS, stage=st)

    assert result == cache_root / ver / "python" / "python.exe"
    assert st._hits == 1
    assert tarball.is_file()
