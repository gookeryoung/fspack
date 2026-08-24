"""NuitkaCompiler 单元测试：用户源码编译为本机 .pyd/.so.

nuitka 装到本地缓存 ``~/.fspack/cache/nuitka/<py_version>/site-packages``，
不污染 ``dist/runtime``。编译时用 ``runtime/python.exe <bootstrap.py>`` 注入
sys.path 调用 nuitka，绕过 ``python3X._pth`` 对 ``PYTHONPATH`` 的限制。
用临时脚本文件而非 ``-c``：Nuitka 的 ``reExecuteNuitka`` 无条件访问
``sys.modules["__main__"].__file__``，``-c`` 模式下该属性不存在会
``AttributeError``。
"""

from __future__ import annotations

import inspect
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fspack.config import (
    DEFAULT_NUITKA_VERSION,
    KNOWN_STANDALONE_VERSIONS,
    NUITKA_VERSIONS,
    get_mirror,
    nuitka_version_for,
)
from fspack.exceptions import NuitkaError
from fspack.packaging.nuitka import NuitkaCompiler
from fspack.packaging.nuitka.compile import (
    _HASH_INDEX_MAX,
    _MAX_COMPILE_WORKERS,
    _hash_index_path,
    _load_hash_index,
    _update_hash_index,
)
from fspack.packaging.runtime import STANDALONE_RELEASE_TAG, standalone_tarball_name
from fspack.platform import Platform
from fspack.progress import StageRecorder


class _CompileOK:
    """subprocess.run 成功返回值桩."""

    returncode = 0
    stdout = ""
    stderr = ""


class _CompileFail:
    """subprocess.run 失败返回值桩."""

    returncode = 1
    stdout = ""
    stderr = "syntax error in foo.py"


class _ImportAbsent:
    """subprocess.run 失败返回值桩（模拟 import 失败）."""

    returncode = 1
    stdout = ""
    stderr = "ModuleNotFoundError: No module named 'pip'"


def _make_nuitka_cache(cache_dir: Path) -> Path:
    """在 cache_dir 下创建 nuitka/__init__.py 模拟已装 nuitka，返回 cache_dir."""
    nuitka_pkg = cache_dir / "nuitka"
    nuitka_pkg.mkdir(parents=True, exist_ok=True)
    (nuitka_pkg / "__init__.py").write_text("", encoding="utf-8")
    return cache_dir


def _patch_winlibs_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nuitka_ver: str = "4.1.3") -> Path:
    """FSPACK_CACHE_DIR 指向 tmp 并预置 winlibs gcc.exe（模拟缓存命中）.

    返回 winlibs 缓存根目录（``<tmp>/cache/nuitka-winlibs-mingw``）。
    预置的 gcc.exe 路径与 Nuitka ``getCachedDownload`` 约定一致，
    使 :meth:`NuitkaCompiler.ensure_winlibs_mingw` 缓存命中不触发下载。
    """
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    winlibs_root = tmp_path / "cache" / "nuitka-winlibs-mingw"
    gcc_exe = NuitkaCompiler._winlibs_gcc_dir(nuitka_ver) / "mingw64" / "bin" / "gcc.exe"
    gcc_exe.parent.mkdir(parents=True, exist_ok=True)
    gcc_exe.write_bytes(b"")
    return winlibs_root


# ---- _nuitka_cache_dir 与 _is_nuitka_cached 测试 ----


def test_nuitka_cache_dir_path(tmp_path: Path) -> None:
    """_nuitka_cache_dir 返回 cache_root / py_version / site-packages."""
    cache_root = tmp_path / "nuitka_cache"
    cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")
    assert cache_dir == cache_root / "3.11.9" / "site-packages"


def test_is_nuitka_cached_true_when_init_exists(tmp_path: Path) -> None:
    """缓存目录有 nuitka/__init__.py 时 _is_nuitka_cached 返回 True."""
    cache_dir = _make_nuitka_cache(tmp_path / "cache")
    assert NuitkaCompiler._is_nuitka_cached(cache_dir) is True


def test_is_nuitka_cached_false_when_missing(tmp_path: Path) -> None:
    """缓存目录无 nuitka 包时 _is_nuitka_cached 返回 False."""
    assert NuitkaCompiler._is_nuitka_cached(tmp_path / "empty") is False


def test_is_nuitka_cached_false_when_init_missing(tmp_path: Path) -> None:
    """缓存目录有 nuitka/ 但无 __init__.py 时返回 False（PEP 420 命名空间包不算）."""
    (tmp_path / "nuitka").mkdir()
    assert NuitkaCompiler._is_nuitka_cached(tmp_path) is False


# ---- _runtime_python 路径解析测试 ----


def test_runtime_python_windows(tmp_path: Path) -> None:
    """Windows 平台 runtime python 路径为 runtime/python.exe."""
    runtime = tmp_path / "runtime"
    py = NuitkaCompiler._runtime_python(runtime, "3.11.9", Platform.WINDOWS)
    assert py == runtime / "python.exe"


def test_runtime_python_linux(tmp_path: Path) -> None:
    """Linux 平台 runtime python 路径为 runtime/python/bin/python{major}.{minor}."""
    runtime = tmp_path / "runtime"
    py = NuitkaCompiler._runtime_python(runtime, "3.11.9", Platform.LINUX)
    assert py == runtime / "python" / "bin" / "python3.11"


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


# ---- compile_src 测试 ----


def test_compile_src_skips_when_python_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """runtime python 未就绪时告警并跳过编译."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    cache = _make_nuitka_cache(tmp_path / "cache")
    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("runtime python 未就绪" in r.message for r in caplog.records)
    assert "未就绪" in st._detail


def test_compile_src_skips_when_nuitka_not_cached(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """缓存目录无 nuitka 时告警并跳过（回退到 .pyc 模式）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = tmp_path / "empty_cache"  # 无 nuitka 包

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("缓存目录无 nuitka" in r.message for r in caplog.records)
    assert "未安装" in st._detail


def test_compile_src_no_py_files(tmp_path: Path) -> None:
    """src 目录无 .py 文件时直接返回，detail 标注无文件."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    cache = _make_nuitka_cache(tmp_path / "cache")

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert "无 .py 文件" in st._detail


def test_compile_src_invokes_bootstrap_script_with_sys_path_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_src 用临时脚本文件注入 sys.path 调用 nuitka（非 -c，因 reExecute 需要 __file__）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "util.py").write_text("x = 1")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured: list[list[str]] = []
    script_contents: list[str] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        # 在 finally 清理前捕获脚本内容
        script_path = Path(cmd[1])
        if script_path.is_file():
            script_contents.append(script_path.read_text(encoding="utf-8"))
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 每个 .py 一次编译调用（无 is_available subprocess 调用，_is_nuitka_cached 是文件系统检查）
    assert len(captured) == 2
    bootstrap_scripts: set[str] = set()
    for cmd in captured:
        assert str(runtime / "python.exe") in cmd[0]
        # cmd[1] 是临时 bootstrap 脚本路径（非 -c，因 reExecute 需要 __main__.__file__）
        assert "-c" not in cmd
        bootstrap_script = cmd[1]
        bootstrap_scripts.add(bootstrap_script)
        # 所有调用复用同一 bootstrap 脚本
        assert bootstrap_script.endswith("_nuitka_bootstrap.py")
        # nuitka 编译参数（--show-progress 在 4.x 已 obsolete，不加；不用 --quiet 抑制 INFO）
        # --mode=module：4.x 中旧 --module 已废弃，模块模式专属选项检查只认 --mode=module，
        # 否则 --no-pyi-file 触发 "has no effect" WARNING
        assert "--mode=module" in cmd
        # --nofollow-imports：显式不跟随导入（模块模式默认行为），消除
        # "did not specify to follow or include anything" 警告
        assert "--nofollow-imports" in cmd
        assert "--module" not in cmd
        assert "--no-pyi-file" in cmd
        assert "--remove-output" in cmd
        # --assume-yes-for-downloads：Nuitka 4.x 内置 zig 作为可选 C 编译器，自动接受下载
        # 避免交互式询问阻塞构建（实际已通过 CC 环境变量指定 gcc/mingw 避免 zig，此为兜底）
        assert "--assume-yes-for-downloads" in cmd
        assert "--show-progress" not in cmd
        assert "--quiet" not in cmd
        # 不再使用 --python-for-scons：改用 standalone python（完整 CPython）运行 nuitka，
        # scons 自动继承 sys.executable，无需另指定。embed runtime python 不完整会触发
        # Nuitka reExecute fork bomb（详见 compile_with_stamp 文档）。
        assert "--python-for-scons" not in cmd
        # --jobs=N 控制 C 编译并行度（N=os.cpu_count()），单进程内并行无膨胀风险。
        # 必须用 = 形式：Nuitka 4.x 的 argparse 配置要求 --jobs=N 格式，
        # 空格分隔（"--jobs", "N"）会报 "requires an argument with '--jobs='" 错误。
        jobs_args = [arg for arg in cmd if arg.startswith("--jobs=")]
        assert len(jobs_args) == 1, f"应仅一个 --jobs=N 参数，实际 {jobs_args}"
        assert jobs_args[0].split("=", 1)[1].isdigit(), f"--jobs=N 的 N 应为数字，实际 {jobs_args[0]}"
        # 不应出现独立的 --jobs（避免空格分隔形式）
        assert "--jobs" not in [arg for arg in cmd if not arg.startswith("--jobs=")]
    # 复用同一脚本文件
    assert len(bootstrap_scripts) == 1
    # 脚本内容含 sys.path 注入与 nuitka main 调用
    assert len(script_contents) == 2
    for content in script_contents:
        assert "sys.path.insert" in content
        assert str(cache) in content
        assert "from nuitka.__main__ import main" in content


def test_compile_src_skips_init_py_not_compiled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """__init__.py 不编译不删除：跳过编译（无收益），保留 .py 维持包标识."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('hi')")
    (src / "sub").mkdir()
    (src / "sub" / "__init__.py").write_text("")
    (src / "sub" / "mod.py").write_text("x = 1")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # __init__.py 保留（不编译不删除）
    assert (src / "__init__.py").is_file()
    assert (src / "sub" / "__init__.py").is_file()
    # 非 __init__.py 被删（.pyd 已生成）
    assert not (src / "app.py").exists()
    assert not (src / "sub" / "mod.py").exists()
    # 仅编译非 __init__.py 文件（app.py + sub/mod.py = 2 次，__init__.py 跳过）
    assert len(captured) == 2
    compiled_names = [Path(cmd[-1]).name for cmd in captured]
    assert "__init__.py" not in compiled_names
    assert "app.py" in compiled_names
    assert "mod.py" in compiled_names


def test_compile_src_prefers_standalone_python_over_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_src 收到真实存在的 build_python_exe 时优先用它而非 runtime python.

    验证 standalone python 接入生效：之前 compile_with_stamp 没传 build_python_exe，
    导致 _ensure_build_python 成死代码，编译回退到 embed runtime python 触发
    Nuitka reExecute fork bomb（Windows 反复衍生 python.exe 进程导致 CPU 卡死）。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")  # embed runtime python（不应被使用）
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = _make_nuitka_cache(tmp_path / "cache")

    # standalone python：真实存在的文件，compile_src 据此选用
    standalone_py = tmp_path / "standalone" / "python.exe"
    standalone_py.parent.mkdir(parents=True)
    standalone_py.write_bytes(b"")

    captured: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(
        src,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        cache,
        stage=st,
        build_python_exe=standalone_py,
    )

    # 编译命令首参（python 可执行文件）必须是 standalone python 而非 runtime python
    assert len(captured) == 1
    assert captured[0][0] == str(standalone_py)
    assert str(runtime / "python.exe") not in captured[0][0]


def test_compile_src_failure_warns_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """单文件编译失败仅告警不中断，后续文件继续编译.

    失败的 .py 必须保留（运行时回退到 .pyc 加载），仅删除成功编译的 .py。
    避免编译失败导致 dist/src 无可用代码。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.py").write_text("x = 1")
    (src / "bad.py").write_text("invalid syntax !!!")
    cache = _make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # cmd 最后一个元素是 py_file 路径
        if "bad.py" in cmd[-1]:
            return (1, "", "syntax error")
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # bad.py 编译失败告警
    assert any("Nuitka 编译失败" in r.message and "bad.py" in r.message for r in caplog.records)
    # detail 含失败计数
    assert "失败 1" in st._detail
    assert "编译 1" in st._detail
    # 成功编译的 ok.py 被删除（.pyd 已生成替代）
    assert not (src / "ok.py").exists()
    # 失败的 bad.py 必须保留：运行时回退到 .pyc 加载，避免 dist/src 无可用代码
    assert (src / "bad.py").is_file(), "编译失败的 .py 不应被删除，需保留供 .pyc 回退加载"


def test_compile_src_failure_cleans_build_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单文件编译失败时 _cleanup_build_dirs 仍清理 .build 残留（iter-130）.

    Nuitka --remove-output 仅在编译成功时清理 .build/，失败时残留。
    compile_src 在 finally 块调 _cleanup_build_dirs 确保失败时也清理。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    cache = _make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # 模拟编译失败：Nuitka 残留 .build 目录（--remove-output 仅成功时清理）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.build").mkdir(exist_ok=True)
        (py_file.parent / f"{py_file.stem}.build" / "module.c").write_text("// c")
        return (1, "", "compile error")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 编译失败但 .build 残留应被清理
    assert not (src / "app.build").exists(), "编译失败的 .build 残留应被 _cleanup_build_dirs 清理"
    # 失败的 .py 保留（运行时回退 .pyc）
    assert (src / "app.py").is_file()


def test_compile_src_compile_files_exception_cleans_build_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker OSError 按失败处理后，compile_src 仍清理 .build 残留（iter-130）.

    _stream_compile 抛 FileNotFoundError（py_exe 不存在）时 _compile_one 捕获
    OSError 按失败文件处理（不中断构建），compile_src 的 finally 调
    _cleanup_build_dirs 确保编译结束（含失败）后清理 .build 残留。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1")
    # 预存在的 .build 目录（模拟上次编译残留）
    build_dir = src / "app.build"
    build_dir.mkdir()
    (build_dir / "module.c").write_text("// c")
    cache = _make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        raise FileNotFoundError("python exe not found")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    # OSError 按失败处理不抛异常，返回失败文件列表
    failed = NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert failed == ["app.py"]

    # .build 残留也应被清理（finally 块）
    assert not build_dir.exists(), ".build 残留也应被 finally 块清理"


def test_compile_src_linux_uses_python3_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 平台用 runtime/python/bin/python{ver} 调 nuitka."""
    runtime = tmp_path / "runtime"
    (runtime / "python" / "bin").mkdir(parents=True)
    (runtime / "python" / "bin" / "python3.11").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured: list[list[str]] = []
    monkeypatch.setattr(
        NuitkaCompiler,
        "_stream_compile",
        staticmethod(lambda cmd, **kw: captured.append(cmd) or (0, "", "")),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.LINUX, cache, stage=st)

    assert len(captured) == 1
    assert "python3.11" in captured[0][0]


def test_compile_src_records_stage_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_src 调用 stage.processed 与 stage.skip 记录编译与剥离计数."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "app.py").write_text("print('hi')")
    (src / "util.py").write_text("x = 1")
    cache = _make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 2 个非 __init__.py 被剥离（__init__.py 保留维持包标识）
    assert st._skipped == 2
    # 2 个非 __init__.py 编译成功（app.py + util.py，__init__.py 收集时跳过不编译）
    assert st._items == 2


def test_compile_src_excludes_nuitka_build_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_src 排除 Nuitka 残留的 <name>.build/ 目录下的 .py 文件.

    --remove-output 只在编译成功时清理 .build/，失败时残留。下次构建若不排除会扫到
    scons-debug.py 等产物并尝试编译（无意义且可能失败）。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "snake.py").write_text("print('hi')")
    # 模拟上次失败留下的 Nuitka 构建目录
    build_dir = src / "snake.build"
    build_dir.mkdir()
    (build_dir / "scons-debug.py").write_text("# scons artifact")
    (build_dir / "scons_input.txt").write_text("ignored")
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured_files: list[str] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # cmd 最后一个元素是 py_file 路径
        captured_files.append(cmd[-1])
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 只编译用户的 snake.py，不编译 .build/ 下的 scons-debug.py
    assert len(captured_files) == 1
    assert captured_files[0].endswith("snake.py")
    assert not any("scons-debug" in f for f in captured_files)
    # 编译 1 个，剥离 1 个
    assert st._items == 1
    assert st._skipped == 1


def test_compile_src_skips_entry_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """entry_rels 指定的入口文件不编译不删除，保留 .py 供 runpy.run_path() 调用.

    入口包装器用 runpy.run_path(os.path.join(_SRC_DIR, _ENTRY_REL)) 显式指定 .py 路径，
    若入口 .py 被 Nuitka 编译后删除，run_path 会 FileNotFoundError。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "snake.py").write_text("print('entry')")  # 入口文件
    (src / "game_logic.py").write_text("x = 1")  # 普通模块
    (src / "utils.py").write_text("y = 2")  # 普通模块
    cache = _make_nuitka_cache(tmp_path / "cache")

    captured_files: list[str] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured_files.append(cmd[-1])
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才删 .py）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(
        src,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        cache,
        stage=st,
        entry_rels=frozenset({"snake.py"}),
    )

    # 只编译非入口文件：game_logic.py 和 utils.py
    assert len(captured_files) == 2
    compiled_names = {Path(f).name for f in captured_files}
    assert compiled_names == {"game_logic.py", "utils.py"}
    assert "snake.py" not in compiled_names, "入口文件不应被编译"
    # 入口 .py 必须保留（runpy.run_path 调用需要）
    assert (src / "snake.py").is_file(), "入口 .py 必须保留供 run_path 调用"
    # 非入口 .py 被剥离
    assert not (src / "game_logic.py").exists()
    assert not (src / "utils.py").exists()
    # 编译 2 个，剥离 2 个
    assert st._items == 2
    assert st._skipped == 2


def test_compile_src_unlink_failure_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """删除 .py 失败时仅告警不中断（OSError 容错）."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    cache = _make_nuitka_cache(tmp_path / "cache")

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        # 模拟 Nuitka 生成 .pyd 产物（_strip_compiled_sources 验证 .pyd 存在才尝试 unlink）
        py_file = Path(cmd[-1])
        (py_file.parent / f"{py_file.stem}.cp311-win_amd64.pyd").write_bytes(b"")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))

    # 让 Path.unlink 抛 OSError
    def fake_unlink(self: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_src(src, runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # unlink 失败告警
    assert any("删除 .py 失败" in r.message for r in caplog.records)
    # stripped 仍为 0（unlink 失败不计入）
    assert st._skipped == 0
    # 编译仍计入
    assert st._items == 1


def test_strip_compiled_sources_preserves_py_when_pyd_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_strip_compiled_sources 验证 .pyd/.so 存在才删 .py：产物缺失时保留源码.

    Nuitka 可能 returncode==0 但未生成 .pyd（如文件名含 ``-`` 触发静默失败），
    此时删除 .py 会导致运行时 ImportError/访问违例。验证产物缺失时保留 .py 并告警。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    # 模拟 Nuitka returncode==0 但未生成 .pyd 的场景（rich._unicode_data.unicode10-0-0.py）
    py_file = tmp_path / "unicode10-0-0.py"
    py_file.write_text("x = 1")
    # 不创建 .pyd 产物

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        stripped = NuitkaCompiler._strip_compiled_sources({py_file}, st)

    # 未删除任何 .py（.pyd 缺失）
    assert stripped == 0
    assert py_file.is_file(), "产物缺失时应保留 .py 避免运行时 ImportError"
    # 告警提示未找到产物
    assert any("未找到 .pyd/.so 产物" in r.message for r in caplog.records)
    # stage 不计剥离
    assert st._skipped == 0


def test_strip_compiled_sources_deletes_py_when_pyd_exists(tmp_path: Path) -> None:
    """_strip_compiled_sources 在 .pyd 存在时删除 .py（正常路径）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    py_file = tmp_path / "app.py"
    py_file.write_text("x = 1")
    # 模拟 Nuitka 生成 .pyd 产物
    (tmp_path / "app.cp311-win_amd64.pyd").write_bytes(b"fake-pyd")

    st = StageRecorder("Nuitka 编译")
    stripped = NuitkaCompiler._strip_compiled_sources({py_file}, st)

    assert stripped == 1
    assert not py_file.exists()
    assert (tmp_path / "app.cp311-win_amd64.pyd").is_file()
    assert st._skipped == 1


def test_strip_compiled_sources_deletes_py_when_so_exists(tmp_path: Path) -> None:
    """Linux 平台 .so 产物同样支持验证."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    py_file = tmp_path / "mod.py"
    py_file.write_text("x = 1")
    (tmp_path / "mod.cpython-311-x86_64-linux-gnu.so").write_bytes(b"fake-so")

    st = StageRecorder("Nuitka 编译")
    stripped = NuitkaCompiler._strip_compiled_sources({py_file}, st)

    assert stripped == 1
    assert not py_file.exists()


# ---- _strip_compiled_sources 可选 import 验证测试 ----


class _VerifyResult:
    """subprocess.run 桩：模拟批量验证成功（returncode=0，输出 JSON 结果）.

    ``module_status`` 为 {模块名: 是否可加载} 字典，控制每个模块的验证结果。
    """

    def __init__(self, module_status: dict[str, bool]) -> None:
        import json

        self.returncode = 0
        results_json = json.dumps(module_status)
        self.stdout = f"FSPACK_VERIFY_RESULT:{results_json}\n"
        self.stderr = ""


class _CrashResult:
    """subprocess.run 桩：模拟批量验证崩溃（returncode=-1073741819 访问违例）."""

    def __init__(self) -> None:
        self.returncode = -1073741819  # 0xC0000005
        self.stdout = ""
        self.stderr = ""


class _SubprocessResult:
    """subprocess.run 返回值桩."""

    def __init__(self, returncode: int = 0, stdout: str | bytes = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _IndividualRunner:
    """subprocess.run 可调用桩：模拟逐个验证，按调用解析模块名返回结果.

    ``ok_modules`` 为二进制有效模块集合：输出 FSPACK_ONE_RESULT:1 标记并 exit 0；
    其余模块模拟硬崩溃（访问违例 returncode），与真实损坏 .pyd 行为一致。
    每次调用返回新的 :class:`_SubprocessResult`，避免 returncode 在调用间被覆盖。
    """

    def __init__(self, ok_modules: set[str]) -> None:
        self._ok_modules = ok_modules

    def __call__(self, cmd: list[str], **kwargs: Any) -> _SubprocessResult:
        # 从 -c 参数中提取模块名（最后一行 importlib.import_module({mod!r})）
        script = cmd[cmd.index("-c") + 1]
        mod = ""
        for line in reversed(script.split("\n")):
            if "importlib.import_module(" in line:
                # 提取引号中的模块名（支持单引号和双引号）
                start = line.find("'") + 1
                if start > 0:
                    end = line.find("'", start)
                    mod = line[start:end]
                else:
                    start = line.find('"') + 1
                    end = line.find('"', start)
                    mod = line[start:end]
                break
        if mod in self._ok_modules:
            # individual 测试 subprocess 未指定 encoding，stdout 为 bytes
            return _SubprocessResult(returncode=0, stdout=b"FSPACK_ONE_RESULT:1\n")
        return _SubprocessResult(returncode=-1073741819)


def test_strip_compiled_sources_verify_preserves_py_when_pyd_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_strip_compiled_sources 验证模式：.pyd 损坏时保留 .py 并删除损坏 .pyd.

    Nuitka 4.x 在 Python 3.13+ Windows 上用 zig 编译可能生成损坏 .pyd，
    验证发现损坏时删除 .pyd（避免运行时优先加载损坏产物）并保留 .py 回退到 .pyc。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    # 模拟 site-packages/rich/errors.py + 损坏的 errors.cp313-win_amd64.pyd
    site_packages = tmp_path / "site-packages"
    rich_dir = site_packages / "rich"
    rich_dir.mkdir(parents=True)
    # __init__.py 让 _find_package_root 推导包根为 site-packages/，模块名 rich.errors
    (rich_dir / "__init__.py").write_text("")
    py_file = rich_dir / "errors.py"
    py_file.write_text("class ConsoleError(Exception): pass")
    pyd_file = rich_dir / "errors.cp313-win_amd64.pyd"
    pyd_file.write_bytes(b"corrupt-pyd")

    # 批量验证返回 errors 模块不可加载
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _VerifyResult({"rich.errors": False}),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        stripped = NuitkaCompiler._strip_compiled_sources(
            {py_file},
            st,
            verify_py_exe=tmp_path / "python.exe",
            verify_search_root=site_packages,
        )

    assert stripped == 0, "损坏 .pyd 对应的 .py 不应删除"
    assert py_file.is_file(), "损坏 .pyd 时应保留 .py 回退到 .pyc"
    assert not pyd_file.exists(), "损坏 .pyd 应删除避免运行时优先加载"
    assert any("损坏" in r.message for r in caplog.records)


def test_strip_compiled_sources_verify_deletes_py_when_pyd_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_strip_compiled_sources 验证模式：.pyd 可加载时正常删除 .py."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    site_packages = tmp_path / "site-packages"
    rich_dir = site_packages / "rich"
    rich_dir.mkdir(parents=True)
    # __init__.py 让 _find_package_root 推导包根为 site-packages/，模块名 rich.errors
    (rich_dir / "__init__.py").write_text("")
    py_file = rich_dir / "errors.py"
    py_file.write_text("class ConsoleError(Exception): pass")
    pyd_file = rich_dir / "errors.cp313-win_amd64.pyd"
    pyd_file.write_bytes(b"valid-pyd")

    # 批量验证返回 errors 模块可加载
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _VerifyResult({"rich.errors": True}),
    )

    st = StageRecorder("Nuitka 编译")
    stripped = NuitkaCompiler._strip_compiled_sources(
        {py_file},
        st,
        verify_py_exe=tmp_path / "python.exe",
        verify_search_root=site_packages,
    )

    assert stripped == 1
    assert not py_file.exists(), "可加载 .pyd 时应删除 .py"
    assert pyd_file.is_file(), "可加载 .pyd 应保留"


def test_strip_compiled_sources_verify_fallback_to_individual_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """批量验证崩溃时回退到逐个验证，定位损坏的 .pyd.

    批量 subprocess 因损坏 .pyd 触发访问违例（returncode != 0），
    回退到逐个模块测试，仅损坏模块保留 .py，可加载模块正常删除 .py。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    site_packages = tmp_path / "site-packages"
    rich_dir = site_packages / "rich"
    rich_dir.mkdir(parents=True)
    # __init__.py 让 _find_package_root 推导包根为 site-packages/，模块名 rich.errors/rich.console
    (rich_dir / "__init__.py").write_text("")
    # 两个模块：errors 可加载，console 损坏
    errors_py = rich_dir / "errors.py"
    errors_py.write_text("class ConsoleError(Exception): pass")
    (rich_dir / "errors.cp313-win_amd64.pyd").write_bytes(b"valid-pyd")
    console_py = rich_dir / "console.py"
    console_py.write_text("print('hello')")
    (rich_dir / "console.cp313-win_amd64.pyd").write_bytes(b"corrupt-pyd")

    # 第一次批量测试崩溃，后续逐个测试只有 rich.errors 成功
    call_count = [0]
    individual_runner = _IndividualRunner({"rich.errors"})

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        call_count[0] += 1
        if call_count[0] == 1:
            # 批量测试崩溃
            return _CrashResult()
        # 逐个测试：rich.errors 成功，rich.console 崩溃
        return individual_runner(cmd, **kwargs)

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", _fake_run)

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        stripped = NuitkaCompiler._strip_compiled_sources(
            {errors_py, console_py},
            st,
            verify_py_exe=tmp_path / "python.exe",
            verify_search_root=site_packages,
        )

    assert stripped == 1, "仅可加载的 errors.py 应删除"
    assert not errors_py.exists(), "可加载 .pyd 对应的 .py 应删除"
    assert console_py.is_file(), "损坏 .pyd 对应的 .py 应保留"
    assert not (rich_dir / "console.cp313-win_amd64.pyd").exists(), "损坏 .pyd 应删除"
    assert any("批量验证 .pyd 崩溃" in r.message for r in caplog.records)


def test_verify_compiled_modules_empty_input() -> None:
    """_verify_compiled_modules 空输入返回空集合."""
    from fspack.packaging.nuitka import NuitkaCompiler

    verified, artifacts = NuitkaCompiler._verify_compiled_modules(Path("python.exe"), set())
    assert verified == set()
    assert artifacts == []


def test_find_package_root_derives_package_root(tmp_path: Path) -> None:
    """_find_package_root 自动推导包根，兼容 flat/src layout.

    - site-packages/rich/errors.py → site-packages/（rich/ 有 __init__.py）
    - dist/src/src/fspack/builder.py → dist/src/src/（fspack/ 有 __init__.py，src/ 无）
    - dist/src/main.py → dist/src/（main.py 父目录无 __init__.py）
    """
    from fspack.packaging.nuitka import NuitkaCompiler

    # flat layout: site-packages/rich/errors.py
    sp = tmp_path / "site-packages"
    rich_dir = sp / "rich"
    rich_dir.mkdir(parents=True)
    (rich_dir / "__init__.py").write_text("")
    errors_py = rich_dir / "errors.py"
    errors_py.write_text("")
    assert NuitkaCompiler._find_package_root(errors_py) == sp

    # src layout: dist/src/src/fspack/builder.py
    src_root = tmp_path / "dist" / "src" / "src"
    fspack_dir = src_root / "fspack"
    fspack_dir.mkdir(parents=True)
    (fspack_dir / "__init__.py").write_text("")
    builder_py = fspack_dir / "builder.py"
    builder_py.write_text("")
    assert NuitkaCompiler._find_package_root(builder_py) == src_root

    # 顶层模块: dist/src/main.py
    dist_src = tmp_path / "dist2" / "src"
    dist_src.mkdir(parents=True)
    main_py = dist_src / "main.py"
    main_py.write_text("")
    assert NuitkaCompiler._find_package_root(main_py) == dist_src


def test_batch_import_test_returns_none_on_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 在 subprocess 崩溃时返回 None（调用方回退到逐个测试）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _CrashResult(),
    )

    result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])
    assert result is None


def test_batch_import_test_returns_importable_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 成功时返回可加载模块集合."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _VerifyResult({"rich.errors": True, "rich.console": False}),
    )

    result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors", "rich.console"])
    assert result == {"rich.errors"}


def test_individual_import_test_locates_corrupt_pyd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_individual_import_test 逐个测试定位损坏 .pyd，仅返回可加载模块."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # rich.errors 可加载，rich.console 崩溃
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        _IndividualRunner({"rich.errors"}),
    )

    result = NuitkaCompiler._individual_import_test(
        tmp_path / "python.exe", [tmp_path], ["rich.errors", "rich.console"]
    )
    assert result == {"rich.errors"}


def test_binary_load_failure_snippet_classification() -> None:
    """分类规则：依赖缺失/模块代码层异常有效，DLL 加载失败与模块自身缺失判损坏."""
    from fspack.packaging.nuitka.verify import _BINARY_LOAD_FAILURE_SNIPPET

    ns: dict[str, Any] = {}
    exec(_BINARY_LOAD_FAILURE_SNIPPET, ns)
    fn = ns["_fspack_binary_load_failure"]

    # 依赖缺失：ModuleNotFoundError.name 指向第三方依赖（如模板模块 import PySide2）
    assert fn("main", ModuleNotFoundError("No module named 'PySide2'", name="PySide2")) is False
    assert fn("modules.module_b", ModuleNotFoundError("No module named 'ordered_set'", name="ordered_set")) is False
    # 模块自身缺失（模块名推导错误或产物不存在）
    assert fn("main", ModuleNotFoundError("No module named 'main'", name="main")) is True
    # DLL 加载失败（.pyd 二进制损坏）：ImportError 无 name
    assert fn("main", ImportError("DLL load failed while importing 'main'")) is True
    # 模块顶层代码运行时异常：.pyd 已成功加载执行
    assert fn("main", ValueError("boom")) is False
    assert fn("main", ZeroDivisionError("division by zero")) is False


def _make_verify_fixture(tmp_path: Path) -> Path:
    """构造真实验证场景：依赖缺失模块 + 损坏扩展产物，返回包根.

    - ``dep_missing.py``：顶层 import 不存在的第三方依赖（模拟模板模块 import PySide2）
    - ``junk.py`` + 垃圾字节扩展产物：import 时二进制加载失败（模拟损坏 .pyd）。
      扩展后缀用 :data:`importlib.machinery.EXTENSION_SUFFIXES[0]` 保证跨平台命名正确
      （Windows 为 ``.cp311-win_amd64.pyd``，Linux 为 ``.cpython-311-...so``），
      且扩展模块优先级高于 .py，import 必走损坏产物。
    """
    import importlib.machinery

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "dep_missing.py").write_text("import fspack_nonexistent_dep\n")
    (pkg / "junk.py").write_text("x = 1\n")
    (pkg / f"junk{importlib.machinery.EXTENSION_SUFFIXES[0]}").write_bytes(b"garbage-not-a-valid-binary")
    return pkg


def test_batch_import_test_real_subclassifies_dependency_missing(tmp_path: Path) -> None:
    """真实 subprocess 集成：依赖缺失判有效、损坏扩展产物判损坏.

    回归：模板模块顶层 import PySide2/pygame 等非本项目依赖时抛 ModuleNotFoundError，
    旧实现误判为 .pyd 损坏并删除产物；修复后仅二进制自身加载失败才判损坏。
    """
    from fspack.packaging.nuitka import NuitkaCompiler

    pkg = _make_verify_fixture(tmp_path)
    result = NuitkaCompiler._batch_import_test(Path(sys.executable), [pkg], ["dep_missing", "junk"])
    assert result is not None
    assert "dep_missing" in result, "依赖缺失应视为二进制有效"
    assert "junk" not in result, "损坏扩展产物应判损坏"


def test_individual_import_test_real_subclassifies_dependency_missing(tmp_path: Path) -> None:
    """真实 subprocess 集成（逐个测试）：依赖缺失判有效、损坏扩展产物判损坏."""
    from fspack.packaging.nuitka import NuitkaCompiler

    pkg = _make_verify_fixture(tmp_path)
    result = NuitkaCompiler._individual_import_test(Path(sys.executable), [pkg], ["dep_missing", "junk"])
    assert result == {"dep_missing"}


def test_batch_import_test_skips_non_prefix_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 跳过非 FSPACK_VERIFY_RESULT 前缀的输出行."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # reversed 迭代：结果行在前 → 非前缀行在后，确保非前缀行也被遍历到
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _SubprocessResult(
            returncode=0,
            stdout=_VerifyResult({"rich.errors": True}).stdout + "trailing line\nanother trailing\n",
        ),
    )
    result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])
    assert result == {"rich.errors"}


def test_batch_import_test_returns_none_on_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 遇到前缀行但 JSON 损坏时返回 None（回退到逐个测试）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _SubprocessResult(
            returncode=0,
            stdout="FSPACK_VERIFY_RESULT:not-valid-json\n",
        ),
    )
    result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])
    assert result is None


def test_batch_import_test_timeout_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_batch_import_test 超时（模块顶层 input()/死循环/GUI）按验证失败处理返回 None.

    无超时会使构建永久挂起；超时返回 None 让调用方回退逐个测试定位阻塞模块。
    """

    def raise_timeout(cmd: list[str], **kwargs: Any) -> Any:
        # 模块顶层含 input()/死循环/GUI 启动代码，subprocess 永不退出
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30.0)

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", raise_timeout)

    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka"):
        result = NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])

    assert result is None
    assert any("超时" in r.message for r in caplog.records)


def test_batch_import_test_timeout_passes_timeout_kwarg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_batch_import_test 调 subprocess.run 时传入模块级超时常量."""
    from fspack.packaging.nuitka.verify import _IMPORT_TEST_TIMEOUT

    captured_kwargs: list[float] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured_kwargs.append(float(kwargs["timeout"]))
        return _CrashResult()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)
    NuitkaCompiler._batch_import_test(tmp_path / "python.exe", [tmp_path], ["rich.errors"])

    assert captured_kwargs == [_IMPORT_TEST_TIMEOUT]


def test_individual_import_test_timeout_treated_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_individual_import_test 单模块超时按该模块验证失败处理（不进结果集合）.

    超时模块判损坏保留 .py 回退 .pyc；其余模块测试不受影响，构建不挂起。
    """

    class _MixedRunner:
        """hanging 模块抛 TimeoutExpired，其余模块正常返回标记."""

        def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
            script = cmd[cmd.index("-c") + 1]
            if "hanging" in script:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30.0)
            return _SubprocessResult(returncode=0, stdout=b"FSPACK_ONE_RESULT:1\n")

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", _MixedRunner())

    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka"):
        result = NuitkaCompiler._individual_import_test(tmp_path / "python.exe", [tmp_path], ["hanging", "normal"])

    assert result == {"normal"}
    assert any("超时" in r.message for r in caplog.records)


def test_import_test_timeout_constant_value() -> None:
    """``_IMPORT_TEST_TIMEOUT`` 默认 30s：覆盖常规模块导入耗时并防永久挂起."""
    from fspack.packaging.nuitka.verify import _IMPORT_TEST_TIMEOUT

    assert _IMPORT_TEST_TIMEOUT == 30.0


def test_verify_compiled_modules_strips_init_module_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_verify_compiled_modules 对 __init__.py 推导的模块名剥离 .__init__ 后缀."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # 构造 site-packages/rich/__init__.py + __init__.cp311-win_amd64.pyd
    site_packages = tmp_path / "site-packages"
    rich_dir = site_packages / "rich"
    rich_dir.mkdir(parents=True)
    init_py = rich_dir / "__init__.py"
    init_py.write_text("")
    (rich_dir / "__init__.cp311-win_amd64.pyd").write_bytes(b"fake")

    captured: dict[str, list[str]] = {}

    def fake_batch(py_exe: Path, roots: list[Path], mods: list[str]) -> set[str] | None:
        captured["mods"] = mods
        return set(mods)

    monkeypatch.setattr(NuitkaCompiler, "_batch_import_test", fake_batch)

    verified, _unverified = NuitkaCompiler._verify_compiled_modules(tmp_path / "python.exe", {init_py})
    assert init_py in verified
    # 模块名应为 "rich" 而非 "rich.__init__"
    assert captured["mods"] == ["rich"]


def test_strip_compiled_sources_no_verify_preserves_original_behavior(tmp_path: Path) -> None:
    """_strip_compiled_sources 不传验证参数时保持原有行为（仅检查 .pyd 存在）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    py_file = tmp_path / "app.py"
    py_file.write_text("x = 1")
    (tmp_path / "app.cp311-win_amd64.pyd").write_bytes(b"fake-pyd")

    st = StageRecorder("Nuitka 编译")
    # 不传 verify_py_exe 和 verify_search_root
    stripped = NuitkaCompiler._strip_compiled_sources({py_file}, st)

    assert stripped == 1
    assert not py_file.exists()


def test_cleanup_build_dirs_removes_residual(tmp_path: Path) -> None:
    """_cleanup_build_dirs 清理 Nuitka 编译失败的 .build 残留目录."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # 模拟编译失败残留的 .build 目录
    build1 = tmp_path / "rich" / "_unicode_data" / "unicode10-0-0.build"
    build1.mkdir(parents=True)
    (build1 / "module.unicode10-0-0.c").write_text("// c source")
    (build1 / "__constants.o").write_bytes(b"object")

    build2 = tmp_path / "app.build"
    build2.mkdir()
    (build2 / "scons-debug.py").write_text("# scons")

    # 非 .build 目录不清理
    keep_dir = tmp_path / "rich" / "_unicode_data"
    (keep_dir / "__init__.py").write_text("")

    cleaned = NuitkaCompiler._cleanup_build_dirs(tmp_path)

    assert cleaned == 2
    assert not build1.exists()
    assert not build2.exists()
    # 非 .build 目录与文件保留
    assert keep_dir.is_dir()
    assert (keep_dir / "__init__.py").is_file()


def test_cleanup_build_dirs_no_match(tmp_path: Path) -> None:
    """_cleanup_build_dirs 无 .build 目录时返回 0."""
    from fspack.packaging.nuitka import NuitkaCompiler

    (tmp_path / "app.py").write_text("x = 1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "mod.py").write_text("y = 2")

    cleaned = NuitkaCompiler._cleanup_build_dirs(tmp_path)
    assert cleaned == 0


def test_cleanup_build_dirs_skips_files_named_build(tmp_path: Path) -> None:
    """_cleanup_build_dirs 跳过名为 *.build 的文件（仅清理目录）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # app.build 是文件而非目录，应跳过
    build_file = tmp_path / "app.build"
    build_file.write_text("not a directory")
    # real.build 是目录，应清理
    build_dir = tmp_path / "real.build"
    build_dir.mkdir()
    (build_dir / "scons.py").write_text("# scons")

    cleaned = NuitkaCompiler._cleanup_build_dirs(tmp_path)
    assert cleaned == 1
    assert build_file.is_file(), "名为 .build 的文件应保留"
    assert not build_dir.exists(), ".build 目录应清理"


def test_cleanup_build_dirs_handles_rmtree_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_cleanup_build_dirs 遇到 rmtree OSError 时 warning 不中断."""
    from fspack.packaging.nuitka import NuitkaCompiler

    build_dir = tmp_path / "fail.build"
    build_dir.mkdir()
    (build_dir / "module.c").write_text("// c")

    def fail_rmtree(path: Path, **kwargs: Any) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("fspack.packaging.nuitka.compile.shutil.rmtree", fail_rmtree)

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        cleaned = NuitkaCompiler._cleanup_build_dirs(tmp_path)
    assert cleaned == 0
    assert any("清理 .build 目录失败" in r.message for r in caplog.records)


# ---- compile_packages 边缘场景测试 ----


def test_compile_packages_skips_when_py_exe_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_compile_python 返回 None 时 compile_packages 直接返回."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    sp.mkdir()
    (sp / "rich").mkdir()
    (sp / "rich" / "__init__.py").write_text("")

    monkeypatch.setattr(NuitkaCompiler, "_resolve_compile_python", lambda *a, **kw: None)

    st = StageRecorder("Nuitka 包编译")
    # 不应抛异常，不应调用 _compile_files
    NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)


def test_compile_packages_skips_when_nuitka_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_nuitka_cached 返回 False 时 compile_packages 跳过编译."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    # 缓存目录无 nuitka 包
    cache = tmp_path / "cache"
    cache.mkdir()
    sp = tmp_path / "site-packages"
    sp.mkdir()
    (sp / "rich").mkdir()
    (sp / "rich" / "__init__.py").write_text("")

    st = StageRecorder("Nuitka 包编译")
    # 不应抛异常，不应调用 _compile_files
    NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)


def test_compile_packages_warns_when_failed_gt_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compile_packages 编译有失败时 warning 记录失败数."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    pkg = sp / "rich"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("x = 1")

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], list[Path]]:
        # 返回 1 个失败
        return (set(), [Path("fake.py")])

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))

    st = StageRecorder("Nuitka 包编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("失败 1 个" in r.message for r in caplog.records)


def test_compile_packages_mixed_existing_and_missing_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compile_packages 同时传入存在与不存在的包：不存在包跳过，存在包正常编译."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    pkg = sp / "rich"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("x = 1")

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], int]:
        for py in py_files:
            (py.parent / f"{py.stem}.cp311-win_amd64.pyd").write_bytes(b"fake")
        return (set(py_files), 0)

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _VerifyResult({"rich.mod": True}),
    )

    st = StageRecorder("Nuitka 包编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # rich 存在，nonexistent 不存在
        NuitkaCompiler.compile_packages(
            sp, ("rich", "nonexistent"), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st
        )
    # nonexistent 包警告
    assert any("未找到包目录" in r.message for r in caplog.records)
    # rich 包正常编译
    assert not (pkg / "mod.py").exists()
    assert (pkg / "mod.cp311-win_amd64.pyd").is_file()


def test_compile_packages_with_ccache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_packages 传 ccache=True + cache_root 时调用 _ensure_ccache."""
    from fspack.packaging.nuitka import NuitkaCompiler

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    pkg = sp / "rich"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("x = 1")

    ccache_called: dict[str, bool] = {}

    def fake_ensure_ccache(cache_root: Path, target: Platform, stage: Any) -> Path:
        ccache_called["yes"] = True
        return Path("/usr/bin/ccache")

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], int]:
        for py in py_files:
            (py.parent / f"{py.stem}.cp311-win_amd64.pyd").write_bytes(b"fake")
        return (set(py_files), 0)

    monkeypatch.setattr(
        NuitkaCompiler, "_ensure_ccache", classmethod(lambda cls, *a, **kw: fake_ensure_ccache(*a, **kw))
    )
    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _VerifyResult({"rich.mod": True}),
    )

    st = StageRecorder("Nuitka 包编译")
    NuitkaCompiler.compile_packages(
        sp,
        ("rich",),
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        cache,
        stage=st,
        ccache=True,
        cache_root=tmp_path / "ccache_root",
    )
    assert ccache_called.get("yes") is True


# ---- nuitka_version_for 字典查询测试 ----


def test_nuitka_version_for_311_returns_413() -> None:
    """Python 3.11.x 锁定 nuitka 4.1.3."""
    assert nuitka_version_for("3.11.9") == "4.1.3"
    assert nuitka_version_for("3.11.15") == "4.1.3"


def test_nuitka_version_for_38_returns_251() -> None:
    """Python 3.8.x 锁定 nuitka 2.5.1（4.x 不再维护 EOL 3.8）."""
    assert nuitka_version_for("3.8.10") == "2.5.1"
    assert nuitka_version_for("3.9.18") == "2.5.1"


def test_nuitka_version_for_unknown_returns_default() -> None:
    """未知 Python 版本（如 3.15）回退 DEFAULT_NUITKA_VERSION."""
    assert nuitka_version_for("3.15.0") == DEFAULT_NUITKA_VERSION


def test_nuitka_version_for_uses_major_minor_only() -> None:
    """版本查询按 major.minor 匹配，补丁版本不影响结果."""
    # 所有 3.10.x 都映射到同一个 nuitka 版本
    ver_a = nuitka_version_for("3.10.0")
    ver_b = nuitka_version_for("3.10.14")
    assert ver_a == ver_b == NUITKA_VERSIONS["3.10"]


# ---- _check_c_compiler C 编译器检查测试 ----


def test_check_c_compiler_windows_no_mingw_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标缺 mingw 交叉编译器时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: False)
    with pytest.raises(NuitkaError, match="mingw-w64"):
        NuitkaCompiler._check_c_compiler(Platform.WINDOWS)


def test_check_c_compiler_windows_with_mingw_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 目标有 mingw 时不 raise."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    # 不抛异常即通过
    NuitkaCompiler._check_c_compiler(Platform.WINDOWS)


def test_check_c_compiler_linux_no_gcc_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标缺 gcc 时 raise NuitkaError（用户要求显式报错）."""
    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: False)
    with pytest.raises(NuitkaError, match="gcc"):
        NuitkaCompiler._check_c_compiler(Platform.LINUX)


def test_check_c_compiler_linux_with_gcc_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标有 gcc 时不 raise."""
    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    NuitkaCompiler._check_c_compiler(Platform.LINUX)


# ---- winlibs-mingw 工具链测试 ----


def test_winlibs_gcc_dir_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_winlibs_gcc_dir 布局与 Nuitka getCachedDownload 约定一致：gcc/x86_64/<specificity>."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    for nuitka_ver, url in WINLIBS_URLS.items():
        specificity = url.rsplit("/", 2)[1]
        gcc_dir = NuitkaCompiler._winlibs_gcc_dir(nuitka_ver)
        assert gcc_dir == tmp_path / "cache" / "nuitka-winlibs-mingw" / "gcc" / "x86_64" / specificity


def test_winlibs_gcc_dir_unknown_version_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nuitka 版本未收录 WINLIBS_URLS 时 raise NuitkaError（提示更新映射）."""
    from fspack.exceptions import NuitkaError
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    with pytest.raises(NuitkaError, match="winlibs-mingw 下载地址未收录"):
        NuitkaCompiler._winlibs_gcc_dir("9.9.9")


def test_uses_winlibs_version_split() -> None:
    """uses_winlibs：py<3.13 走 winlibs，py>=3.13 走 zig（含 free-threaded t 后缀）."""
    from fspack.packaging.nuitka.winlibs import uses_winlibs

    assert uses_winlibs("3.8.10") is True
    assert uses_winlibs("3.9.21") is True
    assert uses_winlibs("3.11.9") is True
    assert uses_winlibs("3.12.10") is True
    assert uses_winlibs("3.13.1") is False
    assert uses_winlibs("3.14.0") is False
    assert uses_winlibs("3.13.1t") is False
    assert uses_winlibs("3.14.0t") is False


def test_ensure_winlibs_mingw_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存命中（gcc.exe 已存在）时返回缓存根并回写 hit_cache，不触发下载."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    winlibs_root = _patch_winlibs_hit(tmp_path, monkeypatch, nuitka_ver="4.1.3")

    called: list[str] = []
    monkeypatch.setattr(
        NuitkaCompiler,
        "_download_and_extract_winlibs",
        staticmethod(lambda *a, **kw: called.append("download")),
    )

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)

    assert result == winlibs_root
    assert called == []
    assert st._hits == 1
    assert "已就绪" in st._detail


def test_ensure_winlibs_mingw_offline_miss_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式缓存未命中时 fail-fast raise NuitkaError（与其他下载层一致）."""
    from fspack.exceptions import NuitkaError
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FSPACK_OFFLINE", "1")

    st = StageRecorder("Nuitka 编译")
    with pytest.raises(NuitkaError, match="离线模式下 winlibs-mingw 缓存未命中"):
        NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)


def test_ensure_winlibs_mingw_downloads_and_extracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存未命中时下载 winlibs zip 解压到 Nuitka 约定目录，解压后删除 zip."""
    import zipfile

    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    nuitka_ver = "4.1.3"
    gcc_dir = NuitkaCompiler._winlibs_gcc_dir(nuitka_ver)
    gcc_exe = gcc_dir / "mingw64" / "bin" / "gcc.exe"
    zip_name = WINLIBS_URLS[nuitka_ver].rsplit("/", 1)[1]

    class _FakeDownloader:
        def __init__(self, timeout: float = 0.0) -> None:
            pass

        def download(self, url: str, dest: Path, label: str = "") -> None:
            # 写真实 zip：顶层 mingw64/bin/gcc.exe（winlibs 归档布局）
            staging = tmp_path / "staging" / "mingw64" / "bin"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "gcc.exe").write_bytes(b"fake-gcc")
            with zipfile.ZipFile(dest, "w") as zf:
                zf.write(staging / "gcc.exe", "mingw64/bin/gcc.exe")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _FakeDownloader)

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)

    assert result == tmp_path / "cache" / "nuitka-winlibs-mingw"
    assert gcc_exe.is_file()
    # zip 解压完成后删除（缓存命中以 gcc.exe 存在为准）
    assert not (gcc_dir / zip_name).exists()
    assert "下载完成" in st._detail


def test_ensure_winlibs_mingw_download_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """下载失败（网络错误）时 raise NuitkaError，半成品 zip 被清理."""
    from fspack.exceptions import NuitkaError
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))

    class _FailDownloader:
        def __init__(self, timeout: float = 0.0) -> None:
            pass

        def download(self, url: str, dest: Path, label: str = "") -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"partial")
            raise OSError("network error")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _FailDownloader)

    st = StageRecorder("Nuitka 编译")
    with pytest.raises(NuitkaError, match="winlibs-mingw 下载或解压失败"):
        NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)
    # 失败路径同样清理半成品 zip
    zips = list((tmp_path / "cache" / "nuitka-winlibs-mingw").rglob("*.zip"))
    assert zips == []


def test_ensure_winlibs_mingw_extract_missing_gcc_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """zip 解压后 gcc.exe 不在预期路径时 raise NuitkaError（归档布局异常）."""
    import zipfile

    from fspack.exceptions import NuitkaError
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))

    class _EmptyZipDownloader:
        def __init__(self, timeout: float = 0.0) -> None:
            pass

        def download(self, url: str, dest: Path, label: str = "") -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("readme.txt", "empty archive")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _EmptyZipDownloader)

    st = StageRecorder("Nuitka 编译")
    with pytest.raises(NuitkaError, match="未找到 gcc"):
        NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)


def test_ensure_env_windows_prefills_winlibs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_env 在 Windows 且 py<3.13 时预填充 winlibs（scons fallback 到 winlibs）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"
    _make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9"))

    called: list[str] = []
    real = NuitkaCompiler.ensure_winlibs_mingw.__func__

    def _spy(cls: object, py_version: str, stage: StageRecorder) -> Path:
        called.append(py_version)
        return real(cls, py_version, stage)  # type: ignore[arg-type]

    monkeypatch.setattr(NuitkaCompiler, "ensure_winlibs_mingw", classmethod(_spy))

    st = StageRecorder("Nuitka 环境")
    NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert called == ["3.11.9"]


def test_ensure_env_windows_py313_skips_winlibs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_env 在 Windows 且 py>=3.13 时不预填充 winlibs（Nuitka 走 zig 自动下载）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    cache_root = tmp_path / "nuitka_cache"
    _make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.13.1"))

    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_winlibs_mingw",
        classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(AssertionError("不应预填充"))),
    )

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.13.1", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_ensure_env_linux_skips_winlibs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_env 在 Linux 时不预填充 winlibs（Linux 用系统 gcc）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    cache_root = tmp_path / "nuitka_cache"
    _make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9"))

    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_winlibs_mingw",
        classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(AssertionError("不应预填充"))),
    )

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.LINUX, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


# ---- ensure_env 环境就绪测试 ----


def test_ensure_env_cache_hit_skips_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录已有 nuitka 时跳过 pip install，stage 标注缓存命中."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"
    # 预装 nuitka 到缓存
    _make_nuitka_cache(NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9"))

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    # winlibs 与 nuitka 两层缓存均命中
    assert st._hits == 2
    assert "4.1.3" in st._detail


def test_ensure_env_pip_install_target_to_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存未命中时用构建机 pip install --target 装 nuitka 到缓存目录（非 dist/runtime）."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"
    expected_cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    captured_cmd: list[list[str]] = []

    # _ensure_pip_available 检查有 pip → 成功；pip install → 成功
    def stateful_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    # pip install 成功后需要缓存目录有 nuitka 包，模拟文件系统写入
    def fake_is_cached(cache_dir: Path) -> bool:
        return cache_dir == expected_cache_dir and bool(captured_cmd)  # pip install 调用后返回 True

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)

    assert nuitka_ver == "4.1.3"
    # 找到 pip install 命令
    pip_cmds = [c for c in captured_cmd if "install" in c and "--target" in c]
    assert len(pip_cmds) == 1, f"应仅一次 pip install，实际 {len(pip_cmds)}"
    cmd = pip_cmds[0]
    # 用构建机 python
    assert cmd[0] == fake_build_python
    assert cmd[1:4] == ["-m", "pip", "install"]
    # --target 指向缓存目录（非 dist/runtime）
    target_idx = cmd.index("--target")
    assert cmd[target_idx + 1] == str(expected_cache_dir)
    assert "--no-compile" in cmd
    assert "--no-cache-dir" in cmd
    assert "-i" in cmd
    assert "nuitka==4.1.3" in cmd
    assert "安装完成" in st._detail


def test_ensure_env_no_pip_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构建机缺 pip 且 ensurepip 与 uv 两轮自救均失败时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 缓存未命中（_is_nuitka_cached 返回 False）
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    # 调用顺序：
    # 1. _has_pip (import pip) → 失败（缺 pip）
    # 2. _try_ensurepip (python -m ensurepip) → 失败
    # 3. _try_uv_install_pip (uv pip install pip) → 失败
    # → raise NuitkaError
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        return _ImportAbsent()  # 所有调用均失败

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match="缺 pip 模块且两轮自助安装失败"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_ensurepip_self_heal_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 pip 时 ensurepip 自救成功，继续 pip install nuitka."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # _is_nuitka_cached：首次 False，pip install 后 True
    is_cached_state = {"first": True}

    def fake_is_cached(cache_dir: Path) -> bool:
        if is_cached_state["first"]:
            is_cached_state["first"] = False
            return False
        return True

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    # 调用顺序：
    # 1. _has_pip (import pip) → 失败（缺 pip）
    # 2. _try_ensurepip (python -m ensurepip) → 成功
    # 3. _has_pip (再次检查) → 成功（ensurepip 装好了）
    # 4. pip install --target nuitka → 成功
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        if state["n"] == 1:
            return _ImportAbsent()  # _has_pip 失败（缺 pip）
        return _CompileOK()  # ensurepip + has_pip + pip install

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_ensure_env_uv_self_heal_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensurepip 失败但 uv pip install pip 自救成功."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    is_cached_state = {"first": True}

    def fake_is_cached(cache_dir: Path) -> bool:
        if is_cached_state["first"]:
            is_cached_state["first"] = False
            return False
        return True

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    # 调用顺序（注意短路求值：_try_ensurepip 返回 False 时不调用 _has_pip）：
    # 1. _has_pip (import pip) → 失败（缺 pip）
    # 2. _try_ensurepip → 失败（uv venv 无 ensurepip 模块，短路不调用 _has_pip）
    # 3. _try_uv_install_pip → 成功
    # 4. _has_pip (再次检查) → 成功
    # 5. pip install --target nuitka → 成功
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        if state["n"] == 1:
            return _ImportAbsent()  # _has_pip 失败
        if state["n"] == 2:
            return _CompileFail()  # _try_ensurepip 失败
        return _CompileOK()  # uv pip install pip 与后续

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    nuitka_ver = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)
    assert nuitka_ver == "4.1.3"


def test_has_pip_returns_bool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_pip 按 import pip 返回值返回 bool."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")

    # 成功
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())
    assert NuitkaCompiler._has_pip(str(py)) is True

    # 失败
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _ImportAbsent())
    assert NuitkaCompiler._has_pip(str(py)) is False


def test_has_pip_timeout_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_pip 探测超时按无 pip 处理，不抛异常不永久挂起."""

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 60))

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)
    assert NuitkaCompiler._has_pip("C:/fake/python.exe") is False


def test_try_ensurepip_timeout_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_ensurepip 超时按失败处理，返回 False 交由调用方进入第二轮自救."""

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 300))

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)
    assert NuitkaCompiler._try_ensurepip("C:/fake/python.exe") is False


def test_try_uv_install_pip_timeout_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_uv_install_pip 超时按失败处理，返回 False 交由调用方报错."""

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 300))

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)
    assert NuitkaCompiler._try_uv_install_pip() is False


def test_try_ensurepip_invokes_python_m_ensurepip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_ensurepip 调用 `python -m ensurepip --default-pip`."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> object:
        captured.append(cmd)
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    assert NuitkaCompiler._try_ensurepip(str(py)) is True
    assert captured[0] == [str(py), "-m", "ensurepip", "--default-pip"]


def test_try_uv_install_pip_invokes_uv_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_try_uv_install_pip 调用 `uv pip install pip`."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> object:
        captured.append(cmd)
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", fake_run)

    assert NuitkaCompiler._try_uv_install_pip() is True
    assert captured[0] == ["uv", "pip", "install", "pip"]


def test_ensure_env_pip_install_fails_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install 返回非零退出码时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 缓存未命中
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        # 第 1 次：_has_pip → 成功
        # 第 2 次：pip install → 失败
        if state["n"] == 2:
            return _CompileFail()
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match=r"pip install nuitka==4\.1\.3 失败"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_pip_install_timeout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install nuitka 超时（网络半开挂起）时 raise NuitkaError，不永久阻塞."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # 缓存未命中
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        state["n"] += 1
        # 第 1 次：_has_pip → 成功
        # 第 2 次：pip install → 超时挂起
        if state["n"] == 2:
            raise subprocess.TimeoutExpired(cmd, timeout=kw.get("timeout", 1800))
        return _CompileOK()

    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", stateful_run)

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match=r"pip install nuitka==4\.1\.3 超时"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


def test_ensure_env_install_fails_cache_still_empty_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pip install 成功但缓存目录仍无 nuitka 包时 raise NuitkaError."""
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
    _patch_winlibs_hit(tmp_path, monkeypatch)
    cache_root = tmp_path / "nuitka_cache"

    fake_build_python = "C:/fake/python.exe"
    monkeypatch.setattr("fspack.packaging.nuitka.sys.executable", fake_build_python)

    # _is_nuitka_cached 始终返回 False（pip install 成功但缓存仍空）
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    # _has_pip 成功，pip install 成功
    monkeypatch.setattr("fspack.packaging.nuitka.subprocess.run", lambda cmd, **kw: _CompileOK())

    st = StageRecorder("Nuitka 环境")
    with pytest.raises(NuitkaError, match="安装后缓存目录仍无 nuitka 包"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), stage=st)


# ---- compile_with_stamp stamp 缓存测试 ----


def test_compile_with_stamp_cache_hit_skips_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 命中时跳过 ensure_env 与 compile_src."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写匹配的 stamp
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    NuitkaCompiler._stamp_path(dist).write_text(expected_key, encoding="utf-8")

    ensure_called = {"n": 0}
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: ensure_called.__setitem__("n", ensure_called["n"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    assert ensure_called["n"] == 0
    assert compile_called["n"] == 0
    assert st._hits == 1
    assert "stamp 命中" in st._detail


def test_compile_with_stamp_writes_stamp_after_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 未命中时调用 ensure_env + ensure_build_python + compile_src 并写入 stamp."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    # mock standalone python 下载：返回占位路径（compile_src 也被 mock 不会真用到）
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: fake_py),
    )
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # stamp 文件已写入，内容匹配 _stamp_key
    stamp = NuitkaCompiler._stamp_path(dist)
    assert stamp.is_file()
    nuitka_ver = nuitka_version_for("3.11.9")
    expected = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    assert stamp.read_text(encoding="utf-8") == expected


def test_compile_with_stamp_invalidates_on_src_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """源码变化使 stamp 失效，重新调用 ensure_env + ensure_build_python + compile_src."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写基于旧源码内容的 stamp
    nuitka_ver = nuitka_version_for("3.11.9")
    old_key = f"{nuitka_ver}|3.11.9|old_fingerprint"
    NuitkaCompiler._stamp_path(dist).write_text(old_key, encoding="utf-8")

    calls = {"ensure": 0, "build_python": 0, "compile": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("ensure", calls["ensure"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("build_python", calls["build_python"] + 1) or Path()),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("compile", calls["compile"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # stamp 不匹配，调用 ensure_env、_ensure_build_python 与 compile_src
    assert calls["ensure"] == 1
    assert calls["build_python"] == 1
    assert calls["compile"] == 1


def test_compile_with_stamp_passes_build_python_to_compile_src(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_with_stamp 将 _ensure_build_python 返回的路径传给 compile_src.

    验证 standalone python 接入闭环：之前该步骤被遗漏导致 _ensure_build_python
    成死代码，编译回退到 embed runtime python 触发 Nuitka reExecute fork bomb
    （Windows 下反复衍生 python.exe 进程导致 CPU 卡死）。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    # standalone python 路径：mock 返回真实存在的文件路径
    fake_py = tmp_path / "fake_standalone_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(
        NuitkaCompiler,
        "_ensure_build_python",
        classmethod(lambda cls, *a, **kw: fake_py),
    )

    captured: dict[str, object] = {}

    def _capture_compile(cls: Any, *a: Any, **kw: Any) -> None:
        captured["build_python_exe"] = kw.get("build_python_exe")

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(_capture_compile))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # 关键断言：compile_src 收到的 build_python_exe 正是 _ensure_build_python 的返回值
    assert captured["build_python_exe"] == fake_py


def test_stamp_key_includes_nuitka_version_py_version_src_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 键含 nuitka_version + py_version + src_fingerprint + entry_rels."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    key = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    assert "4.1.3" in key
    assert "3.11.9" in key
    # 五段式：version|py_version|src_fp|entry_part|pkg_part（entry_rels=None 时 entry_part 为空）
    assert key.count("|") == 4
    # 末尾两段为空（entry_rels=None + nuitka_packages=()）
    assert key.endswith("||")


def test_stamp_key_includes_entry_rels(tmp_path: Path) -> None:
    """entry_rels 纳入 stamp key：入口集合变化时 stamp 失效，强制重编.

    避免上次编译删除了 .py、本次新增入口跳过但 .py 已不在导致 run_path 失败。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "snake.py").write_text("print('entry')")
    (src / "util.py").write_text("x = 1")

    key_no_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9")
    key_with_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"snake.py"}))
    key_different_entry = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"util.py"}))

    # entry_rels 不同则 stamp key 不同
    assert key_no_entry != key_with_entry
    assert key_with_entry != key_different_entry
    # entry_rels 出现在 key 中（排序后拼接）
    assert "snake.py" in key_with_entry
    assert "util.py" in key_different_entry


def test_stamp_key_entry_rels_order_independent(tmp_path: Path) -> None:
    """entry_rels 集合迭代顺序不影响 stamp key（排序后拼接）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("")
    key1 = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"snake.py", "util.py"}))
    key2 = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", frozenset({"util.py", "snake.py"}))
    assert key1 == key2


def test_stamp_path_under_dist(tmp_path: Path) -> None:
    """stamp 文件位于 dist/.nuitka_compile_stamp."""
    dist = tmp_path / "dist"
    assert NuitkaCompiler._stamp_path(dist) == dist / ".nuitka_compile_stamp"


def test_compile_with_stamp_read_oserror_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 读取 OSError（如磁盘错误）时容错继续编译流程，不崩溃."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写 stamp 文件使 is_file() 为 True，随后 read_text 抛 OSError
    stamp = NuitkaCompiler._stamp_path(dist)
    stamp.write_text("stale", encoding="utf-8")

    orig_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == stamp:
            raise OSError("disk error")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    calls = {"compile": 0}
    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: Path()))
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: calls.__setitem__("compile", calls["compile"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # OSError 被容错：继续执行编译流程
    assert calls["compile"] == 1


def test_compile_with_stamp_write_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """stamp 原子写入失败（os.replace OSError）时仅告警不中断.

    iter-128 改用 ``_atomic_write_text``（tempfile + os.replace）写 stamp，
    patch ``_atomic_write_text`` 抛 OSError 模拟只读文件系统/跨设备 rename 失败。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    def raise_oserror(*a: Any, **kw: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("fspack.packaging.nuitka.compile._atomic_write_text", raise_oserror)

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: Path()))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # 不抛异常即通过（写入失败仅告警）
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("写入 Nuitka stamp 失败" in r.message for r in caplog.records)
    # stamp 未写入（原子化保证：要么完整写入要么不存在）
    assert not NuitkaCompiler._stamp_path(dist).is_file()


# ---- compile_with_stamp hash 索引回退测试（iter-129） ----


def test_hash_index_path_under_dist(tmp_path: Path) -> None:
    """hash 索引文件位于 dist/.nuitka_hash_index.json，与 stamp 同目录."""
    dist = tmp_path / "dist"
    assert _hash_index_path(dist) == dist / ".nuitka_hash_index.json"


def test_load_hash_index_missing_file_returns_empty(tmp_path: Path) -> None:
    """索引文件不存在时返回空 dict，不抛异常."""
    dist = tmp_path / "dist"
    dist.mkdir()
    assert _load_hash_index(dist) == {}


def test_load_hash_index_corrupt_json_deletes_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """索引文件 JSON 非法时删除并返回空 dict（与 _load_deps_cache 策略一致）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_hash_index(dist)

    assert result == {}
    assert not index_file.is_file()
    assert any("hash 索引损坏" in r.message for r in caplog.records)


def test_load_hash_index_non_dict_deletes_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """索引文件顶层非 dict 时删除并返回空 dict."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text('["not", "a", "dict"]', encoding="utf-8")

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_hash_index(dist)

    assert result == {}
    assert not index_file.is_file()
    assert any("非 dict" in r.message for r in caplog.records)


def test_load_hash_index_strips_non_str_entries(tmp_path: Path) -> None:
    """索引含非 str 键/值时剔除异常条目，保留有效条目并回写."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    # 混合有效与无效条目：123 是 int 键，"valid" 是有效条目，None 是无效值
    raw = json.dumps({"valid": "2026-01-01T00:00:00", "123": "2026-01-01", "bad_val": None})
    index_file.write_text(raw, encoding="utf-8")

    result = _load_hash_index(dist)

    # 仅保留 valid 条目（int 键 JSON 转为 str，但值 None 被剔除）
    # 注意：json.loads 把数字键转为 str，所以 "123" 实际是 str 键 + str 值，会被保留
    # 真正被剔除的是 "bad_val": None（值非 str）
    assert result["valid"] == "2026-01-01T00:00:00"
    assert "bad_val" not in result
    # 索引文件被回写（剔除后）
    rewritten = json.loads(index_file.read_text(encoding="utf-8"))
    assert "bad_val" not in rewritten


def test_load_hash_index_read_oserror_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """索引读取 OSError（如权限错误）时返回空 dict，不删除文件（瞬时错误）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text('{"k": "v"}', encoding="utf-8")

    orig_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == index_file:
            raise OSError("permission denied")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_hash_index(dist)

    assert result == {}
    # OSError 不删除文件（瞬时错误，下次重试）
    assert index_file.is_file()
    assert any("读取 hash 索引失败" in r.message for r in caplog.records)


def test_load_hash_index_corrupt_json_unlink_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """索引损坏但删除文件失败时仅告警，仍返回空 dict（不因删除失败中断）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text("{corrupt", encoding="utf-8")

    def raise_oserror(self: Path, *args: Any, **kwargs: Any) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", raise_oserror)

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_hash_index(dist)

    assert result == {}
    # 删除失败告警
    assert any("删除文件失败" in r.message for r in caplog.records)
    # 文件仍在（删除失败）
    assert index_file.is_file()


def test_update_hash_index_writes_new_entry(tmp_path: Path) -> None:
    """更新索引：新条目写入，含当前 ISO 时间戳."""
    dist = tmp_path / "dist"
    dist.mkdir()
    stamp_key = "4.1.3|3.11.9|fingerprint||"

    _update_hash_index(dist, stamp_key)

    index = json.loads(_hash_index_path(dist).read_text(encoding="utf-8"))
    assert stamp_key in index
    assert isinstance(index[stamp_key], str)
    assert len(index[stamp_key]) > 0


def test_update_hash_index_merges_existing(tmp_path: Path) -> None:
    """更新索引：保留已有条目，合并新条目."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    index_file.write_text('{"old_key": "2026-01-01T00:00:00"}', encoding="utf-8")

    _update_hash_index(dist, "new_key")

    index = json.loads(index_file.read_text(encoding="utf-8"))
    assert "old_key" in index
    assert "new_key" in index


def test_update_hash_index_lru_eviction(tmp_path: Path) -> None:
    """索引超过 _HASH_INDEX_MAX 时按时间戳淘汰最旧条目."""
    dist = tmp_path / "dist"
    dist.mkdir()
    index_file = _hash_index_path(dist)
    # 预写 _HASH_INDEX_MAX 条旧条目（同一天内秒数递增，字符串排序与数值一致）
    old_index = {f"old_{i:02d}": f"2026-01-01T00:00:{i:02d}" for i in range(_HASH_INDEX_MAX)}
    index_file.write_text(json.dumps(old_index), encoding="utf-8")

    # 更新一条新条目（now_iso 比所有旧条目都新）
    _update_hash_index(dist, "new_key")

    index = json.loads(index_file.read_text(encoding="utf-8"))
    # 总数不超过 _HASH_INDEX_MAX
    assert len(index) == _HASH_INDEX_MAX
    # 新条目保留
    assert "new_key" in index
    # 最旧条目被淘汰（old_00 时间戳最早）
    assert "old_00" not in index
    # 次新条目保留
    assert f"old_{_HASH_INDEX_MAX - 1:02d}" in index


def test_update_hash_index_write_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """索引原子写入失败时仅告警不中断（索引是回退优化，不影响主流程）."""
    dist = tmp_path / "dist"
    dist.mkdir()

    def raise_oserror(*a: Any, **kw: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("fspack.packaging.nuitka.compile._atomic_write_text", raise_oserror)

    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # 不抛异常即通过
        _update_hash_index(dist, "some_key")

    assert any("写入 hash 索引失败" in r.message for r in caplog.records)


def test_compile_with_stamp_hash_index_hit_skips_compile_and_restamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 未命中但 hash 索引命中时跳过编译，重建 stamp（iter-129 核心场景）.

    场景：dist 完整保留（.pyd 产物在）但 stamp 文件单独丢失/损坏。
    索引与 stamp 同在 dist/，删除 dist 时一并清理，故索引命中安全。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写 hash 索引含当前 stamp_key，但不写 stamp 文件
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    _hash_index_path(dist).write_text(json.dumps({expected_key: "2026-01-01T00:00:00"}), encoding="utf-8")

    ensure_called = {"n": 0}
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: ensure_called.__setitem__("n", ensure_called["n"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # 索引命中：跳过 ensure_env 与 compile_src
    assert ensure_called["n"] == 0
    assert compile_called["n"] == 0
    assert st._hits == 1
    assert "hash 索引命中" in st._detail
    # stamp 被重建
    stamp = NuitkaCompiler._stamp_path(dist)
    assert stamp.is_file()
    assert stamp.read_text(encoding="utf-8") == expected_key


def test_compile_with_stamp_hash_index_miss_proceeds_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 未命中且 hash 索引未命中时走完整编译，编译后更新索引."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 不写 stamp，不写索引（索引文件不存在）

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # 编译后 stamp 与索引均写入
    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    assert NuitkaCompiler._stamp_path(dist).read_text(encoding="utf-8") == expected_key
    index = json.loads(_hash_index_path(dist).read_text(encoding="utf-8"))
    assert expected_key in index


def test_compile_with_stamp_hash_index_corrupt_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """hash 索引文件损坏时删除并走完整编译（不因损坏中断）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预写损坏的索引文件
    _hash_index_path(dist).write_text("{corrupt json", encoding="utf-8")

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    # 损坏索引被删除，走完整编译
    assert compile_called["n"] == 1
    assert any("hash 索引损坏" in r.message for r in caplog.records)
    # 编译后索引重建
    assert _hash_index_path(dist).is_file()


def test_compile_with_stamp_hash_index_hit_restamp_oserror_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """hash 索引命中但重建 stamp 失败时仅告警，仍跳过编译（索引命中即视为已编译）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    nuitka_ver = nuitka_version_for("3.11.9")
    expected_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    _hash_index_path(dist).write_text(json.dumps({expected_key: "2026-01-01T00:00:00"}), encoding="utf-8")

    # patch _atomic_write_text 抛 OSError（仅影响 stamp 重建）
    def raise_oserror(*a: Any, **kw: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("fspack.packaging.nuitka.compile._atomic_write_text", raise_oserror)

    ensure_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "ensure_env",
        classmethod(lambda cls, *a, **kw: ensure_called.__setitem__("n", ensure_called["n"] + 1) or "4.1.3"),
    )
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    # 索引命中仍跳过编译（ensure_env 未调用）
    assert ensure_called["n"] == 0
    assert st._hits == 1
    # 重建 stamp 失败告警
    assert any("重建 Nuitka stamp 失败" in r.message for r in caplog.records)
    # stamp 未写入（_atomic_write_text 抛 OSError）
    assert not NuitkaCompiler._stamp_path(dist).is_file()


# ---- compile_with_stamp 环境就绪失败回退测试 ----


def test_compile_with_stamp_ensure_env_failure_falls_back_to_pyc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ensure_env 抛 NuitkaError（如 pip install 失败、C 编译器缺失）时回退到 .pyc 模式.

    Nuitka 是可选优化，环境就绪失败不应中断构建。回退后不写 stamp（下次构建仍会尝试）。
    """

    def _fail_ensure_env(cls: Any, *a: Any, **kw: Any) -> str:
        raise NuitkaError("pip install nuitka==4.1.3 失败（退出码 1）")

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(_fail_ensure_env))
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        # 不抛异常即通过（回退到 .pyc 模式）
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("回退到 .pyc 模式" in r.message for r in caplog.records)
    assert "回退到 .pyc 模式" in st._detail
    # 回退后不调用 compile_src
    assert compile_called["n"] == 0
    # 回退后不写 stamp（下次构建仍会尝试，避免永久跳过 Nuitka）
    assert not NuitkaCompiler._stamp_path(dist).is_file()


def test_compile_with_stamp_ensure_build_python_failure_falls_back_to_pyc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """_ensure_build_python 抛 NuitkaError（如 standalone python 下载失败）时回退到 .pyc 模式."""

    def _fail_build_python(cls: Any, *a: Any, **kw: Any) -> Path:
        raise NuitkaError("下载 standalone python 失败: network unreachable")

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(_fail_build_python))
    compile_called = {"n": 0}
    monkeypatch.setattr(
        NuitkaCompiler,
        "compile_src",
        classmethod(lambda cls, *a, **kw: compile_called.__setitem__("n", compile_called["n"] + 1)),
    )

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_with_stamp(
            src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
        )

    assert any("回退到 .pyc 模式" in r.message for r in caplog.records)
    assert compile_called["n"] == 0
    assert not NuitkaCompiler._stamp_path(dist).is_file()


def test_compile_with_stamp_compile_src_failure_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_src 内部单文件编译失败已有 warning 继续，不触发回退机制.

    回退仅捕获环境就绪阶段（ensure_env + _ensure_build_python）的 NuitkaError，
    不捕获 compile_src 的编译失败（那是用户代码问题，非环境问题）。
    此处验证 compile_src 被 mock 为正常返回时，stamp 正常写入（不回退）。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    # compile_src 正常调用 → stamp 写入（非回退路径）
    assert NuitkaCompiler._stamp_path(dist).is_file()
    assert "回退" not in st._detail


# ---- compile_packages 测试 ----


def test_compile_packages_empty_packages_noop(tmp_path: Path) -> None:
    """packages 为空时 compile_packages 直接返回，不调用任何编译逻辑."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    sp.mkdir()
    st = StageRecorder("Nuitka 包编译")
    # 不应抛异常，不应调用 _resolve_compile_python
    NuitkaCompiler.compile_packages(sp, (), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert st._hits == 0


def test_compile_packages_missing_package_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """指定包在 site-packages 不存在时 warning 并跳过."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    sp.mkdir()
    # nonexistent_pkg 不存在
    st = StageRecorder("Nuitka 包编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_packages(sp, ("nonexistent_pkg",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)
    assert any("未找到包目录" in r.message for r in caplog.records)


def test_compile_packages_compiles_specified_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_packages 编译指定包下的 .py 文件（跳过 __init__.py）.

    fake_compile_files 同时创建 .pyd 产物，验证 _strip_compiled_sources 删除 .py 前检查 .pyd 存在。
    新增 import 验证：compile_packages 用 runtime python 批量验证 .pyd 可加载才删除 .py，
    mock subprocess.run 返回所有模块可加载（fake_pyd 是占位字节，无法真实 import）。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache = _make_nuitka_cache(tmp_path / "cache")
    sp = tmp_path / "site-packages"
    pkg = sp / "rich"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "_extension.py").write_text("x = 1")
    (pkg / "console.py").write_text("y = 2")

    captured: list[list[Path]] = []

    def fake_compile_files(
        cls: Any, py_exe: Path, bootstrap: Path, py_files: list[Path], stage: Any, **kw: Any
    ) -> tuple[set[Path], int]:
        captured.append(py_files)
        # 模拟 Nuitka --module 生成 .pyd 产物（{stem}.cp{ver}-{platform}.pyd）
        for py in py_files:
            (py.parent / f"{py.stem}.cp311-win_amd64.pyd").write_bytes(b"fake-pyd")
        return (set(py_files), 0)

    monkeypatch.setattr(NuitkaCompiler, "_compile_files", classmethod(fake_compile_files))

    # mock 批量验证：返回所有模块可加载（fake_pyd 是占位字节，无法真实 import）
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        lambda cmd, **kwargs: _VerifyResult({"rich._extension": True, "rich.console": True}),
    )

    st = StageRecorder("Nuitka 包编译")
    NuitkaCompiler.compile_packages(sp, ("rich",), runtime, "3.11.9", Platform.WINDOWS, cache, stage=st)

    # 收集了 _extension.py 与 console.py（跳过 __init__.py）
    assert len(captured) == 1
    names = {p.name for p in captured[0]}
    assert names == {"_extension.py", "console.py"}
    # 编译成功的 .py 被删除（.pyd 已生成且验证可加载）
    assert not (pkg / "_extension.py").exists()
    assert not (pkg / "console.py").exists()
    # .pyd 产物保留
    assert (pkg / "_extension.cp311-win_amd64.pyd").is_file()
    assert (pkg / "console.cp311-win_amd64.pyd").is_file()
    # __init__.py 保留
    assert (pkg / "__init__.py").is_file()


def test_compile_with_stamp_passes_nuitka_packages_to_compile_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compile_with_stamp 透传 nuitka_packages 到 compile_packages."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    # 创建 site-packages 目录使 compile_with_stamp 进入 compile_packages 分支
    (dist / "site-packages").mkdir(parents=True)

    captured_pkgs: list[tuple[str, ...]] = []

    def fake_compile_packages(cls: Any, *args: Any, **kwargs: Any) -> None:
        captured_pkgs.append(args[1] if len(args) > 1 else kwargs.get("packages", ()))

    monkeypatch.setattr(NuitkaCompiler, "compile_packages", classmethod(fake_compile_packages))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src,
        dist,
        runtime,
        "3.11.9",
        Platform.WINDOWS,
        get_mirror("aliyun"),
        cache_root,
        stage=st,
        nuitka_packages=("rich", "click"),
    )

    assert captured_pkgs == [("rich", "click")]
    # stamp 写入（含 pkg_part）
    assert NuitkaCompiler._stamp_path(dist).is_file()


def test_compile_with_stamp_warns_when_site_packages_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """compile_with_stamp 在 site-packages 不存在时 warning 跳过包编译."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_bytes(b"")
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: []))

    # 不创建 site-packages 目录

    compile_packages_called: list[bool] = []

    def fake_compile_packages(cls: Any, *args: Any, **kwargs: Any) -> None:
        compile_packages_called.append(True)

    monkeypatch.setattr(NuitkaCompiler, "compile_packages", classmethod(fake_compile_packages))

    st = StageRecorder("Nuitka 编译")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        NuitkaCompiler.compile_with_stamp(
            src,
            dist,
            runtime,
            "3.11.9",
            Platform.WINDOWS,
            get_mirror("aliyun"),
            cache_root,
            stage=st,
            nuitka_packages=("rich",),
        )

    # compile_packages 未被调用（site-packages 不存在）
    assert not compile_packages_called
    assert any("site-packages 不存在" in r.message for r in caplog.records)


def test_stamp_key_includes_nuitka_packages(tmp_path: Path) -> None:
    """nuitka_packages 纳入 stamp key：包列表变化时 stamp 失效."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x")
    key_empty = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, ())
    key_with_pkgs = NuitkaCompiler._stamp_key(src, "4.1.3", "3.11.9", None, ("rich", "click"))
    assert key_empty != key_with_pkgs
    assert "rich,click" in key_with_pkgs


# ---- _stream_compile 流式输出测试 ----


def test_stream_compile_captures_stdout_and_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 捕获子进程 stdout/stderr 并实时写入终端 fd."""
    cmd = [sys.executable, "-c", "import sys; print('out-msg'); sys.stderr.write('err-msg\\n')"]
    returncode, stdout, stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "out-msg" in stdout
    assert "err-msg" in stderr
    # 验证输出被实时写入终端 fd（capfd 捕获 fd 级输出）
    captured = capfd.readouterr()
    assert "out-msg" in captured.out
    assert "err-msg" in captured.err


def test_stream_compile_captures_delayed_output(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 能捕获子进程延迟输出（模拟 nuitka 编译耗时的多段输出）."""
    cmd = [
        sys.executable,
        "-c",
        "import sys, time; print('step1'); time.sleep(0.3); print('step2'); sys.stderr.write('warn\\n')",
    ]
    returncode, stdout, stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "step1" in stdout
    assert "step2" in stdout
    assert "warn" in stderr
    captured = capfd.readouterr()
    assert "step1" in captured.out
    assert "step2" in captured.out
    assert "warn" in captured.err


def test_stream_compile_returns_nonzero_on_failure(capfd: pytest.CaptureFixture[str]) -> None:
    """子进程退出码非零时 _stream_compile 正确返回 returncode."""
    cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    returncode, _stdout, _stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 3


def test_stream_compile_captures_multiline_output(capfd: pytest.CaptureFixture[str]) -> None:
    """_stream_compile 能捕获多行输出（模拟 nuitka --show-progress 的多步骤输出）."""
    script = (
        "print('Nuitka:INFO:Started Python compilation'); "
        "print('Nuitka:INFO:Completed Python level compilation'); "
        "print('Nuitka:INFO:Generating C source code'); "
        "print('Nuitka:INFO:Running C compilation')"
    )
    cmd = [sys.executable, "-c", script]
    returncode, stdout, _stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode == 0
    assert "Started Python compilation" in stdout
    assert "Completed Python level compilation" in stdout
    assert "Generating C source code" in stdout
    assert "Running C compilation" in stdout
    captured = capfd.readouterr()
    assert "Running C compilation" in captured.out


# ---- 心跳线程测试 ----


def test_compile_src_heartbeat_logs_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """compile_src 在编译期间通过全局心跳线程输出进度日志（iter-131 并行化）.

    nuitka 的 reExecute 机制导致子进程输出不可靠（Windows close_fds=True 不继承 PIPE），
    全局心跳线程是唯一的进度反馈。mock _stream_compile 模拟耗时编译，验证心跳日志输出
    "Nuitka 并行编译中: 已完成 X/Y, 已耗时 Zs" 格式。
    """
    import time as _time

    from fspack.progress import StageRecorder

    # 缩短心跳间隔到 0.05 秒，避免测试等待 10 秒
    monkeypatch.setattr("fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL", 0.05)

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "app.py").write_text("print('hello')", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_text("", encoding="utf-8")
    cache = tmp_path / "cache"
    # 创建 nuitka 包假文件，让 _is_nuitka_cached 检查通过
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("", encoding="utf-8")

    # mock _stream_compile 模拟耗时 0.2 秒的编译（触发至少 1 次心跳）
    def slow_stream(_cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        _time.sleep(0.2)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(slow_stream))

    with caplog.at_level(logging.INFO, logger="fspack.packaging.nuitka"):
        st = StageRecorder("Nuitka 编译")
        NuitkaCompiler.compile_src(src, runtime, "3.10.11", Platform.WINDOWS, cache, stage=st)

    # 验证全局心跳日志输出（至少 1 次 "Nuitka 并行编译中"）
    heartbeat_logs = [r for r in caplog.records if "并行编译中" in r.message]
    assert len(heartbeat_logs) >= 1, f"期望至少 1 次心跳日志，实际 {len(heartbeat_logs)} 次"
    # 验证心跳消息格式：含 "已完成" 与 "已耗时"
    assert "已完成" in heartbeat_logs[0].message
    assert "已耗时" in heartbeat_logs[0].message


def test_compile_src_heartbeat_stops_after_compile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """编译完成后心跳线程立即停止，不输出多余日志."""
    from fspack.progress import StageRecorder

    # 心跳间隔设为较长值，确保编译期间不触发心跳
    monkeypatch.setattr("fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL", 10.0)

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "app.py").write_text("print('hello')", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "python.exe").write_text("", encoding="utf-8")
    cache = tmp_path / "cache"
    # 创建 nuitka 包假文件，让 _is_nuitka_cached 检查通过
    (cache / "nuitka").mkdir(parents=True)
    (cache / "nuitka" / "__init__.py").write_text("", encoding="utf-8")

    # mock _stream_compile 立即返回
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))

    # 验证不会因为心跳线程阻塞
    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_src(src, runtime, "3.10.11", Platform.WINDOWS, cache, stage=st)
    # 编译成功，无异常即通过


# ---- 并行编译测试（iter-131）----


def test_max_compile_workers_constant() -> None:
    """``_MAX_COMPILE_WORKERS`` 常量为 4，平衡并行收益与资源限制."""
    assert _MAX_COMPILE_WORKERS == 4


def test_compile_files_parallel_max_workers_capped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_compile_files`` 的 ``max_workers = min(cpu_count, _MAX_COMPILE_WORKERS)``.

    mock ThreadPoolExecutor 捕获 max_workers 参数，验证：
    - cpu_count >= 4 时 max_workers = 4（上限）
    - cpu_count < 4 时 max_workers = cpu_count
    """
    import concurrent.futures as cf

    captured_max_workers: list[int] = []
    real_tpe = cf.ThreadPoolExecutor

    class CapturingTPE(real_tpe):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            mw = kwargs.get("max_workers") or (args[0] if args else None)
            if mw is not None:
                captured_max_workers.append(mw)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("fspack.packaging.nuitka.compile.ThreadPoolExecutor", CapturingTPE)
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    for i in range(3):
        (src / f"f{i}.py").write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        sorted(src.glob("*.py")),
        st,
        target=Platform.WINDOWS,
    )

    assert len(captured_max_workers) == 1
    expected = min(os.cpu_count() or 1, _MAX_COMPILE_WORKERS)
    assert captured_max_workers[0] == expected


def test_compile_files_parallel_completes_all_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行编译完成所有文件，成功/失败计数正确."""

    # 文件 0/1 成功，文件 2 失败
    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        py_file = cmd[-1]
        if "f2" in py_file:
            return (1, "", "error")
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    files = []
    for i in range(3):
        f = src / f"f{i}.py"
        f.write_text("x = 1", encoding="utf-8")
        files.append(f)

    st = StageRecorder("编译")
    compiled, failed = NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        files,
        st,
        target=Platform.WINDOWS,
    )

    assert len(failed) == 1
    assert len(compiled) == 2
    # 成功的是 f0 和 f1
    compiled_names = {p.name for p in compiled}
    assert compiled_names == {"f0.py", "f1.py"}
    # stage.processed 被调用 2 次（2 个成功）
    assert st._items == 2


def test_compile_files_parallel_oserror_treated_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """worker 内 _stream_compile 抛 OSError（Popen 失败）时按失败文件处理，不中断构建.

    与"单文件失败仅告警"承诺一致：OSError（如 py_exe 不存在的 FileNotFoundError）
    等价于退出码非零，文件进 failed_files，其余文件继续编译。
    """

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        raise FileNotFoundError("python exe not found")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1", encoding="utf-8")
    (src / "ok.py").write_text("y = 2", encoding="utf-8")

    st = StageRecorder("编译")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka"):
        compiled, failed = NuitkaCompiler._compile_files(
            tmp_path / "python.exe",
            tmp_path / "bootstrap.py",
            [src / "app.py", src / "ok.py"],
            st,
            target=Platform.WINDOWS,
        )

    # 两个文件均触发 OSError，全部按失败处理，不抛异常中断构建
    assert compiled == set()
    assert set(failed) == {src / "app.py", src / "ok.py"}
    assert any("启动失败" in r.message for r in caplog.records)


def test_compile_files_parallel_oserror_mixed_with_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError 文件按失败处理的同时，正常文件仍成功编译（互不干扰）."""

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        if "bad.py" in cmd[-1]:
            raise FileNotFoundError("python exe not found")
        return 0, "", ""

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "bad.py").write_text("x = 1", encoding="utf-8")
    (src / "good.py").write_text("y = 2", encoding="utf-8")

    st = StageRecorder("编译")
    compiled, failed = NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [src / "bad.py", src / "good.py"],
        st,
        target=Platform.WINDOWS,
    )

    assert compiled == {src / "good.py"}
    assert failed == [src / "bad.py"]


def test_compile_files_parallel_heartbeat_stops_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 内 OSError 全部按失败处理后，finally 块停止心跳线程，不泄漏."""
    import threading as _threading

    monkeypatch.setattr("fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL", 0.05)

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        raise FileNotFoundError("python exe not found")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1", encoding="utf-8")

    active_before = _threading.active_count()
    st = StageRecorder("编译")
    compiled, failed = NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [src / "app.py"],
        st,
        target=Platform.WINDOWS,
    )
    # OSError 按失败处理，不抛异常
    assert compiled == set()
    assert failed == [src / "app.py"]
    # 心跳线程已停止（daemon=True 会在主线程退出时清理，但这里验证 finally 已 join）
    # 等待短暂时间让 daemon 线程完全退出
    import time as _time

    _time.sleep(0.1)
    active_after = _threading.active_count()
    # 心跳线程不应残留（active_count 不应增加）
    assert active_after <= active_before + 1  # 允许少量波动（其他 daemon 线程）


def test_compile_files_parallel_jobs_adjusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行模式 --jobs = max(1, cpu_count // max_workers)，避免过度超订."""
    captured_cmds: list[list[str]] = []

    def fake_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured_cmds.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(fake_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [src / "app.py"],
        st,
        target=Platform.WINDOWS,
    )

    assert len(captured_cmds) == 1
    # 找到 --jobs=N 参数
    jobs_args = [arg for arg in captured_cmds[0] if arg.startswith("--jobs=")]
    assert len(jobs_args) == 1
    jobs_value = int(jobs_args[0].split("=")[1])
    cpu = os.cpu_count() or 1
    max_workers = min(cpu, _MAX_COMPILE_WORKERS)
    expected_jobs = max(1, cpu // max_workers)
    assert jobs_value == expected_jobs


def test_compile_files_parallel_empty_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空文件列表时 _compile_files 返回空集合，无异常."""
    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(lambda cmd, **kw: (0, "", "")))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    st = StageRecorder("编译")
    compiled, failed = NuitkaCompiler._compile_files(
        tmp_path / "python.exe",
        tmp_path / "bootstrap.py",
        [],
        st,
        target=Platform.WINDOWS,
    )
    assert compiled == set()
    assert len(failed) == 0


def test_compile_files_parallel_global_heartbeat_format(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """全局心跳输出 "Nuitka 并行编译中: 已完成 X/Y, 已耗时 Zs" 格式."""
    import time as _time

    monkeypatch.setattr("fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL", 0.05)

    def slow_stream(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        _time.sleep(0.2)
        return (0, "", "")

    monkeypatch.setattr(NuitkaCompiler, "_stream_compile", staticmethod(slow_stream))
    monkeypatch.setattr(NuitkaCompiler, "_build_compile_env", classmethod(lambda cls, *a, **kw: {}))

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1", encoding="utf-8")

    st = StageRecorder("编译")
    with caplog.at_level(logging.INFO, logger="fspack.packaging.nuitka"):
        NuitkaCompiler._compile_files(
            tmp_path / "python.exe",
            tmp_path / "bootstrap.py",
            [src / "app.py"],
            st,
            target=Platform.WINDOWS,
        )

    heartbeat_logs = [r for r in caplog.records if "并行编译中" in r.message]
    assert len(heartbeat_logs) >= 1
    # 验证格式含 "已完成" 和 "/"
    msg = heartbeat_logs[0].message
    assert "已完成" in msg
    assert "/" in msg
    assert "已耗时" in msg


# ---- ccache 相关测试 ----


def test_build_compile_env_without_ccache_sets_cc_compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 设 CC=gcc 避免 zig；Windows 不设 CC 只重定向下载缓存（scons 拒绝外部 gcc）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CC", raising=False)
    monkeypatch.delenv("CFLAGS", raising=False)

    # Linux：CC=gcc
    env_linux = NuitkaCompiler._build_compile_env(Platform.LINUX, None)
    assert env_linux is not None
    assert env_linux["CC"] == "gcc"
    assert "CCACHE_DIR" not in env_linux

    # Windows：CC 被 scons 无条件拒绝，不设避免 "Non downloaded winlibs-gcc
    # ... ignored" 噪音提示；NUITKA_CACHE_DIR_DOWNLOADS 重定向到 fspack 缓存目录
    env_win = NuitkaCompiler._build_compile_env(Platform.WINDOWS, None)
    assert env_win is not None
    assert "CC" not in env_win
    assert "CCACHE_DIR" not in env_win
    assert env_win["NUITKA_CACHE_DIR_DOWNLOADS"] == str(tmp_path / "cache" / "nuitka-winlibs-mingw")


def test_build_compile_env_with_ccache_linux(tmp_path: Path) -> None:
    """Linux ccache 环境设置 CC='"ccache 路径" gcc'（路径引号包裹防空格截断）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    ccache_exe = tmp_path / "ccache"
    ccache_exe.write_bytes(b"")
    env = NuitkaCompiler._build_compile_env(Platform.LINUX, ccache_exe)
    assert env is not None
    assert env["CC"] == f'"{ccache_exe}" gcc'
    assert "CCACHE_DIR" in env


def test_build_compile_env_with_ccache_windows_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 忽略 ccache_exe：CC 被 scons 拒绝，ccache 前缀无意义不设置."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CC", raising=False)
    ccache_exe = tmp_path / "ccache.exe"
    ccache_exe.write_bytes(b"")
    env = NuitkaCompiler._build_compile_env(Platform.WINDOWS, ccache_exe)
    assert env is not None
    assert "CC" not in env
    assert "CCACHE_DIR" not in env


def test_build_compile_env_windows_clears_host_cc_cflags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 清除宿主残留的 CC/CFLAGS（scons 拒绝外部 gcc，残留仅产生噪音提示）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CC", "x86_64-w64-mingw32-gcc")
    monkeypatch.setenv("CFLAGS", "-D_WIN32_WINNT=0x0601")
    env = NuitkaCompiler._build_compile_env(Platform.WINDOWS, None)
    assert "CC" not in env
    assert "CFLAGS" not in env
    assert "NUITKA_CACHE_DIR_DOWNLOADS" in env


def test_build_compile_env_windows_no_cflags_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 不注入 CFLAGS（Nuitka scons 自设 _WIN32_WINNT，注入触发 Inherited CFLAGS 提示）.

    Nuitka 4.1.3 无条件 ``_WIN32_WINNT=0x0601``（Win7），2.5.1 mingw 分支
    ``0x0501``（更保守），fspack 注入同宏纯冗余且触发 "Inherited CFLAGS"
    噪音提示，已删除。
    """
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("CFLAGS", raising=False)
    env = NuitkaCompiler._build_compile_env(Platform.WINDOWS, None)
    assert "CFLAGS" not in env


def test_build_compile_env_skips_win32_winnt_for_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 目标不设置 _WIN32_WINNT（Linux 无此兼容性问题）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    monkeypatch.delenv("CFLAGS", raising=False)
    env = NuitkaCompiler._build_compile_env(Platform.LINUX, None)
    # Linux 不应添加 _WIN32_WINNT
    cflags = env.get("CFLAGS", "")
    assert "_WIN32_WINNT" not in cflags


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


def test_build_options_from_defaults_translates_ccache() -> None:
    """build_options_from_defaults 透传 ccache 字段."""
    from fspack.config import BuildDefaults, build_options_from_defaults

    # ccache=True
    defaults = BuildDefaults(ccache=True)
    opts = build_options_from_defaults(defaults)
    assert opts.ccache is True

    # ccache=False
    defaults = BuildDefaults(ccache=False)
    opts = build_options_from_defaults(defaults)
    assert opts.ccache is False

    # ccache=None → 默认值 False
    defaults = BuildDefaults()
    opts = build_options_from_defaults(defaults)
    assert opts.ccache is False


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


# ---- _stream_compile 超时防护测试（iter-127） ----


def test_stream_compile_timeout_default_value() -> None:
    """``_stream_compile`` timeout 默认 None（运行时 dispatch ``_COMPILE_TIMEOUT``），可被参数覆盖.

    定义期绑定常量会绕过 compile 层 monkeypatch（dispatch 失效），故默认参数
    必须为 None 哨兵，函数体内经 ``_C`` 解析。
    """
    from fspack.packaging.nuitka.compile import _COMPILE_TIMEOUT

    assert _COMPILE_TIMEOUT == 600.0
    # 检查 timeout 参数默认值（通过 __defaults__ 或签名）
    sig = inspect.signature(NuitkaCompiler._stream_compile)
    timeout_param = sig.parameters["timeout"]
    assert timeout_param.default is None


def test_stream_compile_timeout_none_dispatches_compile_constant(
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout=None 时运行时 dispatch compile 层 ``_COMPILE_TIMEOUT``，monkeypatch 生效."""
    monkeypatch.setattr("fspack.packaging.nuitka.compile._COMPILE_TIMEOUT", 0.5)
    # 子进程 sleep 30s，dispatch 后的 0.5s 超时必然触发
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    returncode, _stdout, _stderr = NuitkaCompiler._stream_compile(cmd)
    assert returncode != 0


def test_stream_compile_timeout_kills_long_process(
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``timeout`` 超时后 kill 子进程，返回非零退出码并记录 warning."""
    # 子进程 sleep 30s，timeout=0.5s 必然超时
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.nuitka"):
        returncode, _stdout, _stderr = NuitkaCompiler._stream_compile(cmd, timeout=0.5)
    # kill 后 returncode 非 0（POSIX -9 / Windows 1）
    assert returncode != 0
    # warning 日志记录超时
    timeout_logs = [r for r in caplog.records if "超时" in r.message]
    assert len(timeout_logs) == 1
    assert "0s" in timeout_logs[0].message or "终止子进程" in timeout_logs[0].message


def test_stream_compile_timeout_not_triggered_for_fast_process(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """快速子进程不触发超时，正常返回退出码."""
    cmd = [sys.executable, "-c", "print('fast')"]
    returncode, stdout, _stderr = NuitkaCompiler._stream_compile(cmd, timeout=10.0)
    assert returncode == 0
    assert "fast" in stdout


def test_stream_compile_timeout_preserves_drained_output(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """超时 kill 前已 drain 的输出仍保留在返回值中供诊断."""
    # 子进程先输出再 sleep，超时 kill 后已输出的内容应保留
    cmd = [
        sys.executable,
        "-c",
        "print('partial-output'); import sys; sys.stdout.flush(); import time; time.sleep(30)",
    ]
    returncode, stdout, _stderr = NuitkaCompiler._stream_compile(cmd, timeout=0.5)
    assert returncode != 0
    # partial-output 在 kill 前已 drain 到 chunks（drain 线程 join 后）
    assert "partial-output" in stdout


def test_stream_compile_drain_join_timeout_constant() -> None:
    """``_DRAIN_JOIN_TIMEOUT`` 常量存在且为合理值（5s 覆盖 fd 关闭与调度延迟）."""
    from fspack.packaging.nuitka.compile import _DRAIN_JOIN_TIMEOUT

    assert _DRAIN_JOIN_TIMEOUT == 5.0


# ---- _parse_parallel 超时防护测试（iter-127） ----


def test_parse_parallel_timeout_warns_on_slow_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_parse_parallel`` 整体超时后 warning 提示，未完成 future 被 cancel（iter-138 改 submit+as_completed）.

    用 fake ``as_completed`` 抛 ``TimeoutError`` 模拟超时。验证 warning 日志输出
    与未完成 future 的 cancel 调用（fake future 的 ``done()`` 返回 False 触发 cancel）。
    """
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _parse_parallel

    # 构造 5 个 .py 文件
    for i in range(5):
        (tmp_path / f"mod_{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    cancel_calls: list[bool] = []
    shutdown_calls: list[bool] = []

    class _FakeFuture:
        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            cancel_calls.append(True)
            return True

        def result(self) -> tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]:
            return [], [], {}, []

    class _FakePool:
        def __init__(self, *args: object, **kw: object) -> None:
            pass

        def __enter__(self) -> _FakePool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> _FakeFuture:
            return _FakeFuture()

        def shutdown(self, wait: bool = True) -> None:
            shutdown_calls.append(wait)

    monkeypatch.setattr(analysis, "ProcessPoolExecutor", _FakePool)

    def fake_as_completed(futures: object, timeout: float | None = None) -> object:
        raise FuturesTimeoutError("simulated timeout")

    monkeypatch.setattr(analysis, "as_completed", fake_as_completed)

    all_imports_ord: dict[str, None] = {}
    all_stdlib_ord: dict[str, None] = {}
    all_submodules: dict[str, list[str]] = {}
    all_errors: list[tuple[str, str]] = []

    with caplog.at_level(logging.WARNING, logger="fspack.analyzer"):
        _parse_parallel(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    # 超时 warning
    timeout_logs = [r for r in caplog.records if "超时" in r.message]
    assert len(timeout_logs) == 1
    assert "AST 并行解析" in timeout_logs[0].message
    # 5 个 future 都被 cancel（done() 返回 False）
    assert len(cancel_calls) == 5
    # 超时后 imports/submodules/errors 为空（fake as_completed 抛异常未返回结果）
    assert all_imports_ord == {}
    assert all_stdlib_ord == {}
    assert all_submodules == {}
    assert all_errors == []


def test_parse_parallel_normal_completes_without_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正常完成的并行解析不触发超时，结果完整."""
    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _parse_parallel

    for i in range(5):
        (tmp_path / f"mod_{i}.py").write_text(f"import os\nx = {i}\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    # 设较长 timeout 确保正常完成
    monkeypatch.setattr(analysis, "_PARSE_TOTAL_TIMEOUT", 60.0)

    all_imports_ord: dict[str, None] = {}
    all_stdlib_ord: dict[str, None] = {}
    all_submodules: dict[str, list[str]] = {}
    all_errors: list[tuple[str, str]] = []

    _parse_parallel(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    # 5 个文件都 import os（dict 去重保序，"os" 只出现一次）
    assert "os" in all_stdlib_ord
    assert all_imports_ord == {}
    assert all_errors == []


def test_parse_parallel_timeout_constant_default() -> None:
    """``_PARSE_TOTAL_TIMEOUT`` 默认 300s."""
    from fspack.analyzer import _PARSE_TOTAL_TIMEOUT

    assert _PARSE_TOTAL_TIMEOUT == 300.0


def test_parse_parallel_uses_initializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_parse_parallel`` 用 ``initializer`` 预加载 ``_STDLIB`` 传给 worker（iter-134）."""
    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _init_parse_worker, _parse_parallel
    from fspack.analyzer.ast_scan import _STDLIB

    for i in range(5):
        (tmp_path / f"mod_{i}.py").write_text("x = 0\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    captured: dict[str, object] = {}

    class _FakeFuture:
        def done(self) -> bool:
            return True

        def cancel(self) -> bool:
            return False

        def result(self) -> tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]:
            return [], [], {}, []

    class _Pool:
        def __init__(self, *args: object, **kw: object) -> None:
            captured.update(kw)

        def __enter__(self) -> _Pool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> _FakeFuture:
            return _FakeFuture()

        def shutdown(self, wait: bool = True) -> None:
            pass

    monkeypatch.setattr(analysis, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(analysis, "as_completed", lambda futures, timeout=None: iter(futures))
    _parse_parallel(py_files, {}, {}, {}, [])

    assert captured.get("initializer") is _init_parse_worker
    assert captured.get("initargs") == (_STDLIB,)


def test_parse_parallel_interleave_and_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_parse_parallel`` 对每个文件 submit 一个 future（iter-138 改 submit 逐文件提交）.

    旧 ``_interleave_by_size`` 为 ``map(chunksize=)`` 连续分块设计，submit 模式下
    进程池 FIFO 队列天然负载均衡，已删除——本测试验证 submit 调用次数等于文件数。
    """
    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _parse_parallel

    for i in range(20):
        (tmp_path / f"mod_{i}.py").write_text("x = 0\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    submit_calls: list[str] = []

    class _FakeFuture:
        def done(self) -> bool:
            return True

        def cancel(self) -> bool:
            return False

        def result(self) -> tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]:
            return [], [], {}, []

    class _Pool:
        def __init__(self, *args: object, **kw: object) -> None:
            pass

        def __enter__(self) -> _Pool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> _FakeFuture:
            # args[0] 是文件路径 str
            submit_calls.append(str(args[0]) if args else "")
            return _FakeFuture()

        def shutdown(self, wait: bool = True) -> None:
            pass

    monkeypatch.setattr(analysis, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(analysis, "as_completed", lambda futures, timeout=None: iter(futures))
    _parse_parallel(py_files, {}, {}, {}, [])

    # 20 个文件每个 submit 一次（submit 替代 map+chunksize，无需 interleave 重排）
    assert len(submit_calls) == 20


def test_parse_parallel_partial_timeout_aggregates_completed_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_parse_parallel`` 部分 worker 超时时，已完成 worker 的结果仍被聚合（iter-138）.

    ``map(timeout=)`` 在首个 future 卡死时丢弃后续已完成结果；``submit`` + ``as_completed``
    按完成顺序 yield，超时前已完成的 future 结果被聚合，未完成的被 cancel。
    """
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from fspack.analyzer import analysis
    from fspack.analyzer.analysis import _parse_parallel

    for i in range(5):
        (tmp_path / f"mod_{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    py_files = sorted(tmp_path.glob("*.py"))

    completed_results: list[tuple[list[str], list[str], dict[str, frozenset[str]], list[tuple[str, str]]]] = [
        (["numpy"], ["os"], {}, []),
        (["requests"], ["sys"], {}, []),
        (["flask"], [], {}, []),
    ]

    class _DoneFuture:
        def __init__(self, result: object) -> None:
            self._result = result

        def done(self) -> bool:
            return True

        def cancel(self) -> bool:
            return False

        def result(self) -> object:
            return self._result

    class _PendingFuture:
        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            return True

    futures_chain: list[object] = [
        _DoneFuture(completed_results[0]),
        _DoneFuture(completed_results[1]),
        _DoneFuture(completed_results[2]),
        _PendingFuture(),
        _PendingFuture(),
    ]

    class _FakePool:
        def __init__(self, *args: object, **kw: object) -> None:
            pass

        def __enter__(self) -> _FakePool:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def submit(self, fn: object, *args: object) -> object:
            return futures_chain.pop(0)

        def shutdown(self, wait: bool = True) -> None:
            pass

    monkeypatch.setattr(analysis, "ProcessPoolExecutor", _FakePool)

    def fake_as_completed(futures: object, timeout: float | None = None) -> Iterator[object]:
        # 前 3 个已完成的 yield，然后抛 TimeoutError 模拟后 2 个超时
        futures_list = list(futures)  # type: ignore[arg-type]
        yield from futures_list[:3]
        raise FuturesTimeoutError("partial timeout")

    monkeypatch.setattr(analysis, "as_completed", fake_as_completed)

    all_imports_ord: dict[str, None] = {}
    all_stdlib_ord: dict[str, None] = {}
    all_submodules: dict[str, list[str]] = {}
    all_errors: list[tuple[str, str]] = []

    _parse_parallel(py_files, all_imports_ord, all_stdlib_ord, all_submodules, all_errors)

    # 已完成的 3 个 future 结果被聚合（关键改进：map(timeout=) 会丢失这些结果）
    assert "numpy" in all_imports_ord
    assert "requests" in all_imports_ord
    assert "flask" in all_imports_ord
    assert "os" in all_stdlib_ord
    assert "sys" in all_stdlib_ord


def test_protocol_methods_match_compiler_surface() -> None:
    """Protocol 契约声明的全部方法在 NuitkaCompiler facade 上存在（防签名漂移）.

    :mod:`fspack.packaging.nuitka.protocol` 为纯类型契约（运行时仅类型检查期
    使用），各 mixin 用 ``cls: type[NuitkaCompilerProtocol]`` 注解跨类调用。
    本测试守护契约与 facade 同步：Protocol 声明的方法必须在 NuitkaCompiler
    的 MRO 上有真实实现，防止 mixin 重命名后 Protocol 漂移失真。
    """
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

    # 排除 typing.Protocol/ABCMeta 注入的机制属性（_is_protocol 为 bool、
    # _abc_impl 为 abc 缓存），只收集契约方法
    _TYPING_INTERNAL = {"_is_protocol", "_is_runtime_protocol", "_abc_impl"}
    declared = {
        name for name in vars(NuitkaCompilerProtocol) if not name.startswith("__") and name not in _TYPING_INTERNAL
    }
    assert declared, "Protocol 应声明方法集合"
    for name in declared:
        assert hasattr(NuitkaCompiler, name), f"Protocol 声明的 {name} 未由 NuitkaCompiler 提供"


# ---- _precompile_pyc compileall 超时防护测试（iter-127） ----


def test_precompile_pyc_timeout_skips_stamp_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """compileall 超时不写 stamp（下次重试），记录 warning 并 set_detail."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')", encoding="utf-8")

    # patch subprocess.run 抛 TimeoutExpired
    def raise_timeout(*args: Any, **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=args[0] if args else [], timeout=0.5)

    monkeypatch.setattr("subprocess.run", raise_timeout)

    from fspack.packaging.pyc import _COMPILEALL_TIMEOUT, _precompile_pyc

    st = StageRecorder("预编译字节码")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.pyc"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 不写 stamp（下次重试）
    stamp = dist / ".pyc_stamp"
    assert not stamp.is_file()
    # warning 日志
    timeout_logs = [r for r in caplog.records if "超时" in r.message]
    assert len(timeout_logs) == 1
    assert "compileall" in timeout_logs[0].message
    assert str(int(_COMPILEALL_TIMEOUT)) in timeout_logs[0].message


def test_precompile_pyc_timeout_constant_default() -> None:
    """``_COMPILEALL_TIMEOUT`` 默认 300s."""
    from fspack.packaging.pyc import _COMPILEALL_TIMEOUT

    assert _COMPILEALL_TIMEOUT == 300.0


# ---- iter-137: 并发验证 + 失败文件列表测试 ----


def test_individual_import_test_concurrent_handles_multiple_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_individual_import_test 并发处理多个模块，仍正确返回可加载集合（iter-137 并发化）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    # mod0/mod1 可加载，mod2/mod3 崩溃
    monkeypatch.setattr(
        "fspack.packaging.nuitka.subprocess.run",
        _IndividualRunner({"mod0", "mod1"}),
    )

    result = NuitkaCompiler._individual_import_test(
        tmp_path / "python.exe",
        [tmp_path],
        ["mod0", "mod1", "mod2", "mod3"],
    )
    assert result == {"mod0", "mod1"}


def test_individual_import_test_empty_modules_returns_empty(tmp_path: Path) -> None:
    """_individual_import_test 空模块列表直接返回空集合，不启动线程池."""
    from fspack.packaging.nuitka import NuitkaCompiler

    result = NuitkaCompiler._individual_import_test(tmp_path / "python.exe", [tmp_path], [])
    assert result == set()


def test_collect_py_files_skips_failed_files(tmp_path: Path) -> None:
    """_collect_py_files 带 skip_files 跳过上次失败的文件（iter-137）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    src = tmp_path / "src"
    src.mkdir()
    (src / "good.py").write_text("x = 1")
    (src / "bad.py").write_text("y = 2")
    (src / "sub").mkdir()
    (src / "sub" / "nested.py").write_text("z = 3")

    # skip_files 用相对 src_dir 的 POSIX 路径
    skip = frozenset({"bad.py", "sub/nested.py"})
    collected = NuitkaCompiler._collect_py_files(src, entry_rels=None, skip_files=skip)
    collected_names = {p.relative_to(src).as_posix() for p in collected}
    assert collected_names == {"good.py"}


def test_collect_py_files_skip_files_none_preserves_all(tmp_path: Path) -> None:
    """skip_files=None 时不跳过任何文件（向后兼容）."""
    from fspack.packaging.nuitka import NuitkaCompiler

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("")
    (src / "b.py").write_text("")

    collected = NuitkaCompiler._collect_py_files(src, entry_rels=None, skip_files=None)
    assert len(collected) == 2


def test_load_failed_files_missing_returns_empty(tmp_path: Path) -> None:
    """_load_failed_files 文件不存在返回空 frozenset."""
    from fspack.packaging.nuitka.compile import _load_failed_files

    assert _load_failed_files(tmp_path) == frozenset()


def test_load_failed_files_valid_list(tmp_path: Path) -> None:
    """_load_failed_files 读取合法 JSON 列表."""
    from fspack.packaging.nuitka.compile import _load_failed_files

    (tmp_path / ".nuitka_failed_files.json").write_text('["a.py", "sub/b.py"]', encoding="utf-8")
    result = _load_failed_files(tmp_path)
    assert result == frozenset({"a.py", "sub/b.py"})


def test_load_failed_files_corrupt_json_deletes_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_load_failed_files 内容损坏（非法 JSON）删除文件并返回空（与 _load_hash_index 策略一致）."""
    from fspack.packaging.nuitka.compile import _failed_files_path, _load_failed_files

    path = _failed_files_path(tmp_path)
    path.write_text("not a json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="fspack.packaging.nuitka"):
        result = _load_failed_files(tmp_path)
    assert result == frozenset()
    assert not path.exists(), "损坏的失败文件列表应被删除"
    assert any("失败文件列表损坏" in r.message for r in caplog.records)


def test_load_failed_files_non_list_deletes_file(tmp_path: Path) -> None:
    """_load_failed_files 顶层非 list 删除文件并返回空."""
    from fspack.packaging.nuitka.compile import _load_failed_files

    (tmp_path / ".nuitka_failed_files.json").write_text('{"key": "val"}', encoding="utf-8")
    result = _load_failed_files(tmp_path)
    assert result == frozenset()


def test_load_failed_files_strips_non_str_entries(tmp_path: Path) -> None:
    """_load_failed_files 剔除非 str 条目（保留 str 条目）."""
    from fspack.packaging.nuitka.compile import _load_failed_files

    (tmp_path / ".nuitka_failed_files.json").write_text('["a.py", 123, null, "b.py"]', encoding="utf-8")
    result = _load_failed_files(tmp_path)
    assert result == frozenset({"a.py", "b.py"})


def test_save_failed_files_writes_json(tmp_path: Path) -> None:
    """_save_failed_files 写入 JSON 列表."""
    from fspack.packaging.nuitka.compile import _failed_files_path, _load_failed_files, _save_failed_files

    _save_failed_files(tmp_path, ["a.py", "sub/b.py"])
    path = _failed_files_path(tmp_path)
    assert path.is_file()
    # 回读校验
    assert _load_failed_files(tmp_path) == frozenset({"a.py", "sub/b.py"})


def test_save_failed_files_empty_list_overwrites(tmp_path: Path) -> None:
    """_save_failed_files 空列表也写入，覆盖上次失败记录."""
    from fspack.packaging.nuitka.compile import _failed_files_path, _save_failed_files

    path = _failed_files_path(tmp_path)
    path.write_text('["old.py"]', encoding="utf-8")
    _save_failed_files(tmp_path, [])
    assert path.read_text(encoding="utf-8") == "[]"


def test_compile_with_stamp_writes_failed_files_after_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile_with_stamp 编译后将失败文件列表写入 .nuitka_failed_files.json（iter-137）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "broken.py").write_text("syntax error")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))
    # compile_src 返回失败文件列表
    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(lambda cls, *a, **kw: ["broken.py"]))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    failed_file = dist / ".nuitka_failed_files.json"
    assert failed_file.is_file(), "失败文件列表应被写入"
    assert "broken.py" in failed_file.read_text(encoding="utf-8")


def test_compile_with_stamp_stamp_miss_retries_failed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stamp 未命中（源码已变化）时全量重试：不读取失败文件列表，skip_files 恒 None.

    旧 BUG：stamp 未命中仍传 skip_files 跳过上次失败文件，且编译后用不含该文件的
    新列表覆盖写入 + 照写 stamp，用户修复后的文件永远不被编译。新语义：编译路径
    恒全量重试，失败列表仅作诊断记录写入。
    """
    from fspack.packaging.nuitka.compile import _failed_files_path, _load_failed_files

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    (src / "broken.py").write_text("fixed now")
    dist = tmp_path / "dist"
    dist.mkdir()
    # 预置上次失败文件列表（含本次已修复的 broken.py）
    _failed_files_path(dist).write_text('["broken.py"]', encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    monkeypatch.setattr(NuitkaCompiler, "ensure_env", classmethod(lambda cls, *a, **kw: "4.1.3"))
    fake_py = tmp_path / "fake_python.exe"
    fake_py.write_text("")
    monkeypatch.setattr(NuitkaCompiler, "_ensure_build_python", classmethod(lambda cls, *a, **kw: fake_py))

    captured_skip: list[frozenset[str] | None] = []

    def fake_compile_src(cls: Any, *a: Any, **kw: Any) -> list[str]:
        captured_skip.append(kw.get("skip_files"))
        return []

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(fake_compile_src))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    assert captured_skip, "compile_src 应被调用"
    # skip_files 恒 None：上次失败的 broken.py（已修复）参与全量重试
    assert captured_skip[0] is None, "stamp 未命中时应全量重试（skip_files=None）"
    # 失败文件列表仍被写入（诊断记录），内容为本次 compile_src 返回值
    assert _load_failed_files(dist) == frozenset()


def test_compile_with_stamp_cache_hit_does_not_read_failed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stamp 命中时跳过整个 Nuitka，不读取失败文件列表（无意义）."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')")
    dist = tmp_path / "dist"
    dist.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "nuitka_cache"

    # 预置 stamp 命中
    nuitka_ver = nuitka_version_for("3.11.9")
    stamp_key = NuitkaCompiler._stamp_key(src, nuitka_ver, "3.11.9")
    NuitkaCompiler._stamp_path(dist).write_text(stamp_key, encoding="utf-8")

    compile_called = {"yes": False}

    def fake_compile_src(cls: Any, *a: Any, **kw: Any) -> list[str]:
        compile_called["yes"] = True
        return []

    monkeypatch.setattr(NuitkaCompiler, "compile_src", classmethod(fake_compile_src))

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.compile_with_stamp(
        src, dist, runtime, "3.11.9", Platform.WINDOWS, get_mirror("aliyun"), cache_root, stage=st
    )

    assert not compile_called["yes"], "stamp 命中时不应调用 compile_src"


def test_precompile_pyc_normal_no_timeout_writes_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常完成（无超时）的 compileall 仍写 stamp，验证超时分支不影响正常路径."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')", encoding="utf-8")

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileOK())

    from fspack.packaging.pyc import _precompile_pyc

    st = StageRecorder("预编译字节码")
    _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    # 正常路径写 stamp
    stamp = dist / ".pyc_stamp"
    assert stamp.is_file()


# ---- _atomic_write_text 原子化写入测试（iter-128） ----


def test_atomic_write_text_creates_file_with_content(tmp_path: Path) -> None:
    """``_atomic_write_text`` 成功写入创建目标文件且内容正确."""
    from fspack.packaging.nuitka.compile import _atomic_write_text

    target = tmp_path / "stamp.txt"
    _atomic_write_text(target, "hello-stamp\n")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "hello-stamp\n"


def test_atomic_write_text_overwrites_existing(tmp_path: Path) -> None:
    """``_atomic_write_text`` 覆盖已有文件且内容完整替换（无残留旧内容）."""
    from fspack.packaging.nuitka.compile import _atomic_write_text

    target = tmp_path / "stamp.txt"
    target.write_text("old-content", encoding="utf-8")
    _atomic_write_text(target, "new-content-longer")
    assert target.read_text(encoding="utf-8") == "new-content-longer"


def test_atomic_write_text_no_tmp_residue(tmp_path: Path) -> None:
    """``_atomic_write_text`` 成功后不残留 ``.tmp_`` 临时文件."""
    from fspack.packaging.nuitka.compile import _atomic_write_text

    target = tmp_path / "stamp.txt"
    _atomic_write_text(target, "x")
    residues = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp_")]
    assert residues == []


def test_atomic_write_text_replace_failure_cleans_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``Path.replace`` 失败时清理临时文件并重抛 OSError，目标文件保持原样."""
    from fspack.packaging.nuitka import compile as nuitka_compile

    target = tmp_path / "stamp.txt"
    target.write_text("original", encoding="utf-8")

    orig_replace = Path.replace

    def fail_replace(self: Path, dst: Path, *args: Any, **kwargs: Any) -> Path:
        raise OSError("cross-device link not permitted")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="cross-device"):
        nuitka_compile._atomic_write_text(target, "new-content")

    # 临时文件被清理
    residues = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp_")]
    assert residues == []
    # 目标文件保持原内容（未被替换）
    assert target.read_text(encoding="utf-8") == "original"
    # 确认 Path.replace 被调用过（restore 后可正常使用）
    monkeypatch.setattr(Path, "replace", orig_replace)


def test_atomic_write_text_creates_parent_dir(tmp_path: Path) -> None:
    """``_atomic_write_text`` 自动创建父目录（与原 ``stamp.parent.mkdir`` 行为一致）."""
    from fspack.packaging.nuitka.compile import _atomic_write_text

    target = tmp_path / "nested" / "deep" / "stamp.txt"
    _atomic_write_text(target, "key")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "key"


# ---- _precompile_pyc returncode != 0 不写 stamp 测试（iter-128） ----


def test_precompile_pyc_returncode_nonzero_skips_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """compileall 非零退出码时不写 stamp（与超时分支一致的"失败不缓存"策略）.

    iter-128 扩展 iter-127 的超时不写 stamp 策略到 returncode != 0 场景，
    避免失败的编译被 stamp 跳过导致用户长期运行未编译的 .py。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "python.exe").write_bytes(b"")
    dist = tmp_path / "dist"
    src = dist / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print('hi')", encoding="utf-8")

    class _CompileFail:
        returncode = 2
        stderr = "SyntaxError: invalid syntax"
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: _CompileFail())

    from fspack.packaging.pyc import _precompile_pyc

    st = StageRecorder("预编译字节码")
    with caplog.at_level(logging.WARNING, logger="fspack.packaging.pyc"):
        _precompile_pyc(dist, runtime, "3.11.9", Platform.WINDOWS, strip_py=False, stage=st)

    stamp = dist / ".pyc_stamp"
    assert not stamp.is_file()
    fail_logs = [r for r in caplog.records if "compileall 失败" in r.message]
    assert len(fail_logs) == 1
    assert "SyntaxError" in fail_logs[0].message


# ---- NuitkaCompilerProtocol 类型契约模块（纯类型，运行时仅需可导入）----


def test_nuitka_protocol_module_importable() -> None:
    """类型契约模块可导入且定义 NuitkaCompilerProtocol.

    Protocol 仅在类型检查期被 ``if TYPE_CHECKING`` 引用，运行时无人导入；
    本测试保证其 import 依赖（config/platform/progress）始终完整，
    避免 TYPE_CHECKING 引用掩盖模块级 import 断裂。
    """
    from fspack.packaging.nuitka.protocol import NuitkaCompilerProtocol

    assert NuitkaCompilerProtocol is not None
