"""离线模式测试：FSPACK_OFFLINE=1 时下载层 fail-fast 行为.

验证离线模式下：
1. 缓存命中 → 正常返回，不尝试网络请求
2. 缓存未命中 → 立即抛出包含 "离线模式" 的异常，不卡死、不重试网络

覆盖下载层：
- :mod:`fspack.packaging.runtime`：embed python / python-build-standalone
- :mod:`fspack.packaging.wheels.downloader`：wheel 依赖下载
- :mod:`fspack.packaging.nuitka.env`：standalone python / nuitka / ccache
- :mod:`fspack.packaging.builtin`：tkinter 补充包
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

from fspack.config import MirrorConfig
from fspack.exceptions import BuiltinError, DependencyError, EmbedError, NuitkaError
from fspack.packaging.builtin import TkinterBundler
from fspack.packaging.nuitka import NuitkaCompiler
from fspack.packaging.runtime import (
    STANDALONE_RELEASE_TAG,
    download_embed,
    download_standalone,
)
from fspack.packaging.wheels import (
    _run_pip_download,
    download_wheels,
)
from fspack.packaging.wheels.resolver import DownloadContext
from fspack.platform import Platform
from fspack.progress import StageRecorder
from tests._stubs import CompletedStub, fail_urlopen

# ---- runtime.py：embed / standalone 离线模式 ----


def test_download_embed_offline_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """离线模式下 embed 缓存命中 → 正常返回，不尝试网络."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    cache = tmp_path / "cache"
    cache.mkdir()
    zip_path = cache / "python-3.11.9-embed-amd64.zip"
    zip_path.write_bytes(b"cached")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    path = download_embed("3.11.9", mirror, cache)
    assert path.read_bytes() == b"cached"


def test_download_embed_offline_cache_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """离线模式下 embed 缓存未命中 → 立即抛 EmbedError，不尝试网络."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    with pytest.raises(EmbedError, match=r"离线模式下.*缓存未命中"):
        download_embed("3.11.9", mirror, tmp_path / "cache")


def test_download_standalone_offline_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 standalone 缓存命中 → 正常返回."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / f"cpython-3.10.20+{STANDALONE_RELEASE_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
    archive.write_bytes(b"cached")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    path = download_standalone("3.10.20", STANDALONE_RELEASE_TAG, cache)
    assert path.read_bytes() == b"cached"


def test_download_standalone_offline_cache_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 standalone 缓存未命中 → 立即抛 EmbedError."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    with pytest.raises(EmbedError, match=r"离线模式下.*缓存未命中"):
        download_standalone("3.10.20", STANDALONE_RELEASE_TAG, tmp_path / "cache")


def test_download_embed_offline_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig) -> None:
    """非离线模式缓存未命中 → 正常走网络下载路径（不抛离线异常）."""
    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)

    class _FakeResp:
        """模拟 urlopen 响应：首次 read 返回数据，后续返回空（终止下载循环）."""

        def __init__(self) -> None:
            self._read = False
            self.headers = {"Content-Length": "4"}

        def read(self, n: int = -1) -> bytes:
            if self._read:
                return b""
            self._read = True
            return b"DATA"

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

    def fake_urlopen(req: Request, timeout: int, **kwargs: object) -> _FakeResp:
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    path = download_embed("3.11.9", mirror, tmp_path / "cache")
    assert path.read_bytes() == b"DATA"


# ---- wheel_pip.py：wheel 依赖下载离线模式 ----


def test_download_wheels_offline_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 wheel 缓存命中（--no-index 成功） → 不查询网络 index."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    cache = tmp_path / "cache"
    cache.mkdir()
    # 模拟 pip download --no-index 成功（返回 stdout 含 Saved 行）
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache)
    cmd = captured["cmd"]
    assert "--no-index" in cmd
    assert "https://idx/simple" not in cmd


def test_download_wheels_offline_cache_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 wheel 缓存未命中（--no-index 失败） → 立即抛 DependencyError."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        raise subprocess.CalledProcessError(1, "pip", stderr="not in cache")

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    with pytest.raises(DependencyError, match=r"离线模式下依赖缓存未命中"):
        download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache")


def test_download_wheels_offline_disabled_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非离线模式缓存未命中 → 回退到在线下载（不抛离线异常）."""
    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, "pip", stderr="not in cache")

    def fake_stream(cmd: list[str]) -> CompletedStub:
        calls.append(cmd)
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._stream_subprocess", fake_stream)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    monkeypatch.setattr("fspack.packaging.wheels.resolver._find_uv", lambda: None)
    # 不抛异常即通过：成功回退到在线下载
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", tmp_path / "cache")
    assert len(calls) == 2
    assert "--no-index" in calls[0]
    assert "https://idx/simple" in calls[1]


def test_run_pip_download_offline_cache_miss_direct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """直接测试 _run_pip_download：离线模式 + --no-index 失败 → 抛 DependencyError."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        raise subprocess.CalledProcessError(1, "pip", stderr="not in cache")

    monkeypatch.setattr("fspack.packaging.wheels.downloader._run_pip", lambda *a, **kw: None)
    with pytest.raises(DependencyError, match=r"离线模式下"):
        _run_pip_download(
            ["numpy"],
            DownloadContext(
                py="py",
                py_version="3.11.9",
                platform_tags=("win_amd64",),
                pypi_index="https://idx/simple",
                cache_dir=tmp_path,
                base_args=["py", "-m", "pip", "download"],
            ),
        )


def test_run_pip_download_offline_includes_user_find_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 _run_pip_download 的 --no-index 调用包含用户提供的 find-links 路径."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    captured: dict[str, list[str]] = {}

    def fake_run_pip(cmd: list[str], *args: object, **kw: object) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.downloader._run_pip", fake_run_pip)
    user_find_links = ["/custom/wheels", "/shared/wheels"]
    _run_pip_download(
        ["numpy"],
        DownloadContext(
            py="py",
            py_version="3.11.9",
            platform_tags=("win_amd64",),
            pypi_index="https://idx/simple",
            cache_dir=tmp_path,
            base_args=["py", "-m", "pip", "download", "--find-links", str(tmp_path)],
            find_links=user_find_links,
        ),
    )
    cmd = captured["cmd"]
    # 用户提供的 find-links 应出现在 --no-index 之前的命令中
    assert "/custom/wheels" in cmd
    assert "/shared/wheels" in cmd
    assert "--no-index" in cmd
    # 命令应包含 3 个 --find-links：base_args 1 个 + 用户 2 个
    assert cmd.count("--find-links") == 3


def test_run_pip_download_offline_error_lists_searched_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式报错信息应列出已搜索的所有路径（cache_dir + 用户 find-links）."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setattr("fspack.packaging.wheels.downloader._run_pip", lambda *a, **kw: None)
    user_find_links = ["/custom/wheels"]
    with pytest.raises(DependencyError) as exc_info:
        _run_pip_download(
            ["numpy"],
            DownloadContext(
                py="py",
                py_version="3.11.9",
                platform_tags=("win_amd64",),
                pypi_index="https://idx/simple",
                cache_dir=tmp_path,
                base_args=["py", "-m", "pip", "download"],
                find_links=user_find_links,
            ),
        )
    msg = str(exc_info.value)
    assert "已搜索路径" in msg
    assert str(tmp_path) in msg
    assert "/custom/wheels" in msg


def test_download_wheels_offline_with_user_find_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 download_wheels 透传用户 find-links 到 --no-index 调用."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    cache = tmp_path / "cache"
    download_wheels(
        ("numpy",),
        "3.11.9",
        "https://idx/simple",
        cache,
        find_links=("/extra/wheels",),
    )
    cmd = captured["cmd"]
    assert "--no-index" in cmd
    assert "/extra/wheels" in cmd
    assert str(cache) in cmd


def test_download_wheels_offline_no_user_find_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下无用户 find-links 时 --no-index 调用仅含 cache_dir."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.wheels.subprocess.run", fake_run)
    monkeypatch.setattr("fspack.packaging.wheels.downloader._find_pip_python", lambda: "/py/python")
    cache = tmp_path / "cache"
    download_wheels(("numpy",), "3.11.9", "https://idx/simple", cache)
    cmd = captured["cmd"]
    assert "--no-index" in cmd
    # 仅含 cache_dir 一个 --find-links（来自 base_args）
    assert cmd.count("--find-links") == 1
    assert str(cache) in cmd


# ---- nuitka_env.py：standalone python / nuitka / ccache 离线模式 ----


def test_nuitka_download_standalone_python_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 standalone python 缓存未命中 → 抛 NuitkaError."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    stage = StageRecorder("test")
    build_dir = tmp_path / "python"

    def fail_download(*a: object, **kw: object) -> None:
        raise AssertionError("离线模式不应触发网络请求")

    monkeypatch.setattr("fspack.packaging.net.Downloader.download", fail_download)
    with pytest.raises(NuitkaError, match=r"离线模式下.*standalone python 缓存未命中"):
        NuitkaCompiler._download_standalone_python(build_dir, "3.10.20", stage)


def test_nuitka_ensure_ccache_offline_skips_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 ccache 无系统级且无缓存 → 跳过下载返回 None（不抛异常）."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    # 模拟无系统 ccache、无本地缓存
    monkeypatch.setattr("fspack.packaging.nuitka.ccache.shutil.which", lambda _: None)
    stage = StageRecorder("test")
    cache_root = tmp_path / "nuitka"

    def fail_download(*a: object, **kw: object) -> None:
        raise AssertionError("离线模式不应触发网络请求")

    monkeypatch.setattr("fspack.packaging.net.Downloader.download", fail_download)
    result = NuitkaCompiler._ensure_ccache(cache_root, Platform.LINUX, stage)
    assert result is None


def test_nuitka_ensure_env_offline_cache_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """离线模式下 nuitka 包缓存未命中 → 抛 NuitkaError，不调 pip install."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    # 隔离 wheel 缓存目录：避免真实缓存里残留的 Nuitka sdist 被
    # _find_local_nuitka_sdist 命中而跳过离线 fail-fast 抛错（走到 _has_pip
    # 的 subprocess.run 触发守卫，误判为代码缺陷）
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    cache_root = tmp_path / "nuitka"
    stage = StageRecorder("test")

    # 让 _check_c_compiler 通过
    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)

    # 守卫：若误触发 pip install，subprocess.run 抛错
    def fail_run(*a: object, **kw: object) -> None:
        raise AssertionError("离线模式不应触发 pip install")

    monkeypatch.setattr("fspack.packaging.nuitka.env.subprocess.run", fail_run)
    with pytest.raises(NuitkaError, match=r"离线模式下.*nuitka 缓存未命中"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.LINUX, mirror, stage=stage)


def test_nuitka_ensure_env_offline_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """离线模式下 nuitka 缓存命中 → 正常返回，不调 pip install."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    cache_root = tmp_path / "nuitka"
    stage = StageRecorder("test")
    # 预填充缓存
    nuitka_cache = cache_root / "3.11.9" / "site-packages" / "nuitka"
    nuitka_cache.mkdir(parents=True)
    (nuitka_cache / "__init__.py").write_text("", encoding="utf-8")

    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)

    def fail_run(*a: object, **kw: object) -> None:
        raise AssertionError("离线模式缓存命中不应触发 pip install")

    monkeypatch.setattr("fspack.packaging.nuitka.env.subprocess.run", fail_run)
    version = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.LINUX, mirror, stage=stage)
    assert version  # 返回非空版本号


def test_nuitka_ensure_env_offline_local_sdist_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """离线模式下 wheels 缓存有锁定版本 sdist 归档时从本地安装（--no-index + --find-links 纯本地）."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    wheels_dir = tmp_path / "cache" / "wheels"
    wheels_dir.mkdir(parents=True)
    sdist = wheels_dir / "Nuitka-4.1.3.tar.gz"
    sdist.write_bytes(b"")
    cache_root = tmp_path / "nuitka"
    stage = StageRecorder("test")
    expected_cache_dir = cache_root / "3.11.9" / "site-packages"

    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)

    captured_cmd: list[list[str]] = []
    calls = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        calls["n"] += 1
        return CompletedStub()  # _has_pip 与 pip install 均成功

    monkeypatch.setattr("fspack.packaging.nuitka.env.subprocess.run", stateful_run)

    def fake_is_cached(cache_dir: Path) -> bool:
        # 第 1 次 _has_pip、第 2 次 pip install 之后即视为已安装
        return calls["n"] >= 2

    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(fake_is_cached))

    version = NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.LINUX, mirror, stage=stage)
    assert version == "4.1.3"

    pip_cmds = [c for c in captured_cmd if "install" in c and "--target" in c]
    assert len(pip_cmds) == 1
    cmd = pip_cmds[0]
    assert cmd[-1] == str(sdist)
    # 离线：禁用索引，构建/运行依赖全部经 --find-links 从 wheels 缓存解析
    assert "--no-index" in cmd
    assert "-i" not in cmd
    assert cmd[cmd.index("--find-links") + 1] == str(wheels_dir)
    assert cmd[cmd.index("--target") + 1] == str(expected_cache_dir)
    # 归档保留不删（用户显式放置的资产）
    assert sdist.is_file()
    assert "本地 sdist" in stage._detail


def test_nuitka_ensure_env_offline_local_sdist_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror: MirrorConfig
) -> None:
    """离线模式本地 sdist 安装失败 → raise NuitkaError（无法回退在线），归档保留."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    wheels_dir = tmp_path / "cache" / "wheels"
    wheels_dir.mkdir(parents=True)
    sdist = wheels_dir / "Nuitka-4.1.3.tar.gz"
    sdist.write_bytes(b"")
    cache_root = tmp_path / "nuitka"
    stage = StageRecorder("test")

    monkeypatch.setattr("fspack.packaging.loader.gcc_available", lambda: True)
    monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)

    captured_cmd: list[list[str]] = []
    state = {"n": 0}

    def stateful_run(cmd: list[str], **kw: Any) -> object:
        captured_cmd.append(cmd)
        state["n"] += 1
        if state["n"] == 2:  # 第 2 次：pip install 本地归档 → 失败
            fail = CompletedStub()
            fail.returncode = 1
            fail.stderr = "corrupt archive"
            return fail
        return CompletedStub()  # 第 1 次 _has_pip → 成功

    monkeypatch.setattr("fspack.packaging.nuitka.env.subprocess.run", stateful_run)
    monkeypatch.setattr(NuitkaCompiler, "_is_nuitka_cached", staticmethod(lambda cache_dir: False))

    with pytest.raises(NuitkaError, match=r"pip install .*失败"):
        NuitkaCompiler.ensure_env(cache_root, "3.11.9", Platform.LINUX, mirror, stage=stage)

    # 离线无法回退在线：仅一次 install，且归档保留不删
    pip_cmds = [c for c in captured_cmd if "install" in c and "--target" in c]
    assert len(pip_cmds) == 1
    assert sdist.is_file()


# ---- builtin.py：tkinter 补充包离线模式 ----


def test_tkinter_offline_tarball_cache_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 tkinter standalone tarball 缓存未命中 → 抛 BuiltinError."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    cache_dir = tmp_path / "cache"

    # 让 tkinter_marker 检查失败（runtime 内无 tkinter）
    # 让 cache_zip 检查失败（cache/tkinter/ 下无 zip）
    # 让 tarball_path 检查失败（cache/standalone-windows/ 下无 tarball）

    def fail_download(*a: object, **kw: object) -> None:
        raise AssertionError("离线模式不应触发网络请求")

    monkeypatch.setattr("fspack.packaging.net.Downloader.download", fail_download)
    stage = StageRecorder("test")
    with pytest.raises(BuiltinError, match=r"离线模式下.*standalone Windows tarball 缓存未命中"):
        TkinterBundler.ensure(runtime_dir, "3.11.9", cache_dir, stage)


def test_tkinter_offline_cache_zip_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式下 tkinter zip 缓存命中 → 正常解压，不触发网络."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    cache_dir = tmp_path / "cache"
    tkinter_cache = cache_dir / "tkinter"
    tkinter_cache.mkdir(parents=True)
    # tkinter zip 缓存按 standalone 版本命名（3.11.9 → 3.11.15，由 KNOWN_STANDALONE_VERSIONS 解析）
    from fspack.config import KNOWN_STANDALONE_VERSIONS

    standalone_ver = KNOWN_STANDALONE_VERSIONS["3.11"]
    # 创建一个最小有效 zip（含 Lib/tkinter/__init__.py）
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Lib/tkinter/__init__.py", "# cached tkinter")
    cache_zip = tkinter_cache / f"tkinter-{standalone_ver}.zip"
    cache_zip.write_bytes(buf.getvalue())

    def fail_download(*a: object, **kw: object) -> None:
        raise AssertionError("离线模式缓存命中不应触发网络请求")

    monkeypatch.setattr("fspack.packaging.net.Downloader.download", fail_download)
    stage = StageRecorder("test")
    TkinterBundler.ensure(runtime_dir, "3.11.9", cache_dir, stage)
    assert (runtime_dir / "Lib" / "tkinter" / "__init__.py").is_file()


# ---- cli.py：--offline/-O 单次约定 ----


def test_build_offline_flag_parsed() -> None:
    """``--offline``/``-O`` 解析为 ns.offline=True，未指定时为 False."""
    from fspack.cli_parser import build_parser

    assert build_parser().parse_args(["b", ".", "--offline"]).offline is True
    assert build_parser().parse_args(["b", ".", "-O"]).offline is True
    assert build_parser().parse_args(["b", "."]).offline is False


def test_dispatch_offline_sets_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp b -O`` 分发时设 FSPACK_OFFLINE=1，构建期间 is_offline() 为 True."""
    import os

    from fspack.cli import main
    from fspack.config import is_offline

    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
    captured: dict[str, bool] = {}

    def fake_run_build(project: object, ns: object) -> None:
        captured["offline"] = is_offline()

    monkeypatch.setattr("fspack.cli._run_build", fake_run_build)
    main(["b", str(tmp_path), "-O"])
    assert captured["offline"] is True
    # 命令进程内环境变量已设置（后续下载层经 is_offline() 读到）
    assert os.environ["FSPACK_OFFLINE"] == "1"
    # dispatch 直接写 os.environ（非 monkeypatch 管理），delenv 会把 "1" 记为
    # 旧值并在 teardown 恢复造成泄漏，须手动 pop 清理
    os.environ.pop("FSPACK_OFFLINE", None)


def test_dispatch_without_offline_flag_env_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定 ``-O`` 时不设置 FSPACK_OFFLINE（不影响已删环境状态）."""
    import os

    from fspack.cli import main
    from fspack.config import is_offline

    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)

    def fake_run_build(project: object, ns: object) -> None:
        assert is_offline() is False

    monkeypatch.setattr("fspack.cli._run_build", fake_run_build)
    main(["b", str(tmp_path)])
    assert "FSPACK_OFFLINE" not in os.environ
