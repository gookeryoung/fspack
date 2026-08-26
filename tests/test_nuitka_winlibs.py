"""``NuitkaWinlibs`` winlibs-mingw 工具链测试：归档识别/解压、MSVC 探测与 force-mingw64 判定."""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.platform import Platform
from tests._stubs import (
    patch_winlibs_hit,
)

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


def test_msvc_available_vswhere_reports_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """vswhere 找到含 C++ 工具集的 VS 实例（stdout 非空）时返回 True."""
    from fspack.packaging.nuitka.winlibs import msvc_available

    class _VswhereOK:
        returncode = 0
        stdout = "C:\\Program Files\\Microsoft Visual Studio\\2022\\Community\n"

    # 只放行 vswhere.exe 的 is_file（探测路径构造的 Path），其余文件不存在
    monkeypatch.setattr(Path, "is_file", lambda self: self.name == "vswhere.exe", raising=False)
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.subprocess.run", lambda cmd, **kw: _VswhereOK())
    # 绕过 lru_cache 直测原函数（缓存结果会跨测试泄漏）
    assert msvc_available.__wrapped__() is True


def test_msvc_available_vswhere_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """vswhere 存在但无含 C++ 工具集的实例（stdout 空）时返回 False."""
    from fspack.packaging.nuitka.winlibs import msvc_available

    class _VswhereEmpty:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(Path, "is_file", lambda self: self.name == "vswhere.exe", raising=False)
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.subprocess.run", lambda cmd, **kw: _VswhereEmpty())
    assert msvc_available.__wrapped__() is False


def test_msvc_available_vswhere_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """vswhere 探测超时/启动失败按无 MSVC 处理（False），不抛异常."""
    import subprocess as _sp

    from fspack.packaging.nuitka.winlibs import msvc_available

    def _timeout(cmd: list[str], **kw: object) -> object:
        raise _sp.TimeoutExpired(cmd, timeout=30)

    monkeypatch.setattr(Path, "is_file", lambda self: self.name == "vswhere.exe", raising=False)
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.subprocess.run", _timeout)
    assert msvc_available.__wrapped__() is False


def test_msvc_available_no_vswhere_no_cl(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 vswhere 且 cl.exe 不在 PATH 时返回 False（无 VS2017+ 的机器）."""
    from fspack.packaging.nuitka.winlibs import msvc_available

    monkeypatch.setattr(Path, "is_file", lambda self: False, raising=False)
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.shutil.which", lambda name: None)
    assert msvc_available.__wrapped__() is False


def test_msvc_available_no_vswhere_cl_in_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 vswhere 但 cl.exe 在 PATH（开发者手动配 VS 环境）时返回 True."""
    from fspack.packaging.nuitka.winlibs import msvc_available

    monkeypatch.setattr(Path, "is_file", lambda self: False, raising=False)
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.shutil.which", lambda name: "C:\\VS\\cl.exe")
    assert msvc_available.__wrapped__() is True


def test_needs_force_mingw64_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """needs_force_mingw64：仅 Windows + py>=3.13 + 无 MSVC 时才需要 force flag."""
    from fspack.packaging.nuitka.winlibs import needs_force_mingw64

    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: False)
    # 无 MSVC：Windows py>=3.13 需要（防 zig），py<3.13 默认 winlibs 不需要，
    # Linux 不需要，空版本保持旧行为不加
    assert needs_force_mingw64(Platform.WINDOWS, "3.13.1") is True
    assert needs_force_mingw64(Platform.WINDOWS, "3.14.0t") is True
    assert needs_force_mingw64(Platform.WINDOWS, "3.12.10") is False
    assert needs_force_mingw64(Platform.LINUX, "3.13.1") is False
    assert needs_force_mingw64(Platform.WINDOWS, "") is False

    # 有 MSVC：scons 优先 MSVC，flag 反而把 MSVC 顶掉，一律不需要
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: True)
    assert needs_force_mingw64(Platform.WINDOWS, "3.13.1") is False
    assert needs_force_mingw64(Platform.WINDOWS, "3.14.0t") is False


def test_needs_force_mingw64_mingw_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """compiler=mingw：强制 winlibs 无视 MSVC——有 MSVC 须 flag 顶掉，无 MSVC 仅 py>=3.13 需要."""
    from fspack.packaging.nuitka.winlibs import needs_force_mingw64

    # 有 MSVC：任何版本都须 flag 顶掉 MSVC（scons 默认优先 MSVC）
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: True)
    assert needs_force_mingw64(Platform.WINDOWS, "3.13.1", "mingw") is True
    assert needs_force_mingw64(Platform.WINDOWS, "3.11.9", "mingw") is True
    assert needs_force_mingw64(Platform.WINDOWS, "", "mingw") is True
    assert needs_force_mingw64(Platform.LINUX, "3.13.1", "mingw") is False

    # 无 MSVC：py>=3.13 需要（zig 防护），py<3.13 scons 默认即 winlibs 不加
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: False)
    assert needs_force_mingw64(Platform.WINDOWS, "3.13.1", "mingw") is True
    assert needs_force_mingw64(Platform.WINDOWS, "3.11.9", "mingw") is False
    assert needs_force_mingw64(Platform.WINDOWS, "", "mingw") is False


def test_needs_force_mingw64_msvc_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """compiler=msvc：恒不需要 force flag（force 与 MSVC 选择互斥，缺失由入口 fail-fast）."""
    from fspack.packaging.nuitka.winlibs import needs_force_mingw64

    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.msvc_available", lambda: False)
    assert needs_force_mingw64(Platform.WINDOWS, "3.13.1", "msvc") is False
    assert needs_force_mingw64(Platform.WINDOWS, "3.11.9", "msvc") is False


def test_ensure_winlibs_mingw_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存命中（gcc.exe 已存在）时返回缓存根并回写 hit_cache，不触发下载."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    winlibs_root = patch_winlibs_hit(tmp_path, monkeypatch, nuitka_ver="4.1.3")

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
    """离线模式缓存未命中（无 gcc.exe 且无本地归档）时 fail-fast raise NuitkaError."""
    from fspack.exceptions import NuitkaError
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FSPACK_OFFLINE", "1")

    st = StageRecorder("Nuitka 编译")
    with pytest.raises(NuitkaError, match="离线模式下 winlibs-mingw 缓存未命中"):
        NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)


def test_ensure_winlibs_mingw_extracts_local_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录存在用户手动放置的 winlibs zip 时解压替代下载（离线同样适用，zip 保留）."""
    import zipfile

    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(cache_root))
    # 离线模式也应能用本地 zip（纯本地解压不联网）
    monkeypatch.setenv("FSPACK_OFFLINE", "1")

    # 缓存根放置正确版本命名的 zip（顶层 mingw64/bin/gcc.exe）
    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    local_zip = cache_root / "nuitka-winlibs-mingw" / zip_name
    local_zip.parent.mkdir(parents=True)
    staging = tmp_path / "staging" / "mingw64" / "bin"
    staging.mkdir(parents=True)
    (staging / "gcc.exe").write_bytes(b"fake-gcc")
    with zipfile.ZipFile(local_zip, "w") as zf:
        zf.write(staging / "gcc.exe", "mingw64/bin/gcc.exe")

    # 误走下载路径时立即失败（本地 zip 应被识别，双保险）
    class _NoDownload:
        def __init__(self, timeout: float = 0.0) -> None:
            pass

        def download(self, url: str, dest: Path, label: str = "") -> None:
            raise AssertionError("不应触发下载（本地 zip 应被识别）")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _NoDownload)

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)

    assert result == cache_root / "nuitka-winlibs-mingw"
    gcc_exe = NuitkaCompiler._winlibs_gcc_dir("4.1.3") / "mingw64" / "bin" / "gcc.exe"
    assert gcc_exe.is_file()
    # 用户手动放置的 zip 保留（资产不删除）
    assert local_zip.is_file()
    assert "本地归档" in st._detail


def test_ensure_winlibs_mingw_local_zip_wrong_name_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """版本不匹配的 winlibs zip 不被识别（精确匹配文件名，避免 ABI 不兼容误用）."""
    from fspack.exceptions import NuitkaError
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FSPACK_OFFLINE", "1")

    # 文件名版本不匹配（对应 2.5.1 的归档名，当前查询 4.1.3）
    wrong_zip = (
        tmp_path
        / "cache"
        / "nuitka-winlibs-mingw"
        / ("winlibs-x86_64-posix-seh-gcc-14.2.0-llvm-19.1.1-mingw-w64msvcrt-12.0.0-r2.zip")
    )
    wrong_zip.parent.mkdir(parents=True)
    wrong_zip.write_bytes(b"whatever")

    st = StageRecorder("Nuitka 编译")
    with pytest.raises(NuitkaError, match="离线模式下 winlibs-mingw 缓存未命中"):
        NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)


def test_ensure_winlibs_mingw_corrupt_local_zip_falls_back_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地 zip 损坏（下载中断残留）时删除后回退下载，下载产物正常解压."""
    import zipfile

    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(cache_root))

    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    corrupt_zip = cache_root / "nuitka-winlibs-mingw" / zip_name
    corrupt_zip.parent.mkdir(parents=True)
    corrupt_zip.write_bytes(b"not a zip")

    class _FakeDownloader:
        def __init__(self, timeout: float = 0.0) -> None:
            pass

        def download(self, url: str, dest: Path, label: str = "") -> None:
            staging = tmp_path / "staging" / "mingw64" / "bin"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "gcc.exe").write_bytes(b"fake-gcc")
            with zipfile.ZipFile(dest, "w") as zf:
                zf.write(staging / "gcc.exe", "mingw64/bin/gcc.exe")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _FakeDownloader)
    # 未装 7-Zip：走 .zip 下载路径（本机装有 7-Zip，显式排除环境差异）
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs._find_7z", lambda: None)

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)

    gcc_exe = NuitkaCompiler._winlibs_gcc_dir("4.1.3") / "mingw64" / "bin" / "gcc.exe"
    assert gcc_exe.is_file()
    # 损坏的本地 zip 已删除；下载的临时 zip 解压后同样删除
    assert not corrupt_zip.exists()
    downloaded_zip = NuitkaCompiler._winlibs_gcc_dir("4.1.3") / zip_name
    assert not downloaded_zip.exists()
    assert "下载完成" in st._detail


def test_ensure_winlibs_mingw_corrupt_local_zip_offline_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线模式本地 zip 损坏时删除 zip 并重抛解压失败（无法下载回退）."""
    from fspack.exceptions import NuitkaError
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FSPACK_OFFLINE", "1")

    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    corrupt_zip = tmp_path / "cache" / "nuitka-winlibs-mingw" / zip_name
    corrupt_zip.parent.mkdir(parents=True)
    corrupt_zip.write_bytes(b"not a zip")

    st = StageRecorder("Nuitka 编译")
    with pytest.raises(NuitkaError, match="解压失败"):
        NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)
    # 损坏归档已删除（下次构建不再误识别）
    assert not corrupt_zip.exists()


def _patch_7z_extract(monkeypatch: pytest.MonkeyPatch, gcc_dir: Path, returncode: int = 0) -> list[list[str]]:
    """mock winlibs 侧 7z 解压子进程：创建 mingw64/bin/gcc.exe 并记录命令.

    :param gcc_dir: gcc.exe 落位目录（specificity 目录）
    :param returncode: 模拟的 7z 退出码（非 0 模拟归档损坏）
    :return: 捕获的 7z 命令参数列表
    """
    commands: list[list[str]] = []

    class _Result:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = "ERROR: archive corrupted" if returncode else ""

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        commands.append(cmd)
        if returncode == 0:
            bin_dir = gcc_dir / "mingw64" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcc.exe").write_bytes(b"fake-gcc")
        return _Result()

    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.subprocess.run", _fake_run)
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs._find_7z", lambda: "C:\\fake\\7z.exe")
    return commands


def test_ensure_winlibs_mingw_extracts_local_7z(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存目录存在匹配版本的 .7z 归档时经系统 7z 解压（离线同样适用，归档保留）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(cache_root))
    # 离线模式也应能用本地 .7z（纯本地解压不联网）
    monkeypatch.setenv("FSPACK_OFFLINE", "1")

    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    seven_name = zip_name[: -len(".zip")] + ".7z"
    local_7z = cache_root / "nuitka-winlibs-mingw" / seven_name
    local_7z.parent.mkdir(parents=True)
    local_7z.write_bytes(b"fake-7z")

    gcc_dir = NuitkaCompiler._winlibs_gcc_dir("4.1.3")
    commands = _patch_7z_extract(monkeypatch, gcc_dir)

    st = StageRecorder("Nuitka 编译")
    result = NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)

    assert result == cache_root / "nuitka-winlibs-mingw"
    assert (gcc_dir / "mingw64" / "bin" / "gcc.exe").is_file()
    # 用户手动放置的 .7z 保留（资产不删除）
    assert local_7z.is_file()
    assert "本地归档" in st._detail
    # 7z 命令形态：解压到 -o<gcc_dir>、-y 免交互
    assert len(commands) == 1
    assert commands[0][1:] == ["x", "-y", f"-o{gcc_dir}", str(local_7z)]


def test_ensure_winlibs_mingw_local_7z_without_7z_offline_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """离线 + 本地 .7z + 未装 7-Zip：raise 并提示安装 7-Zip（无下载回退路径）."""
    from fspack.exceptions import NuitkaError
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs._find_7z", lambda: None)

    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    local_7z = tmp_path / "cache" / "nuitka-winlibs-mingw" / (zip_name[: -len(".zip")] + ".7z")
    local_7z.parent.mkdir(parents=True)
    local_7z.write_bytes(b"fake-7z")

    st = StageRecorder("Nuitka 编译")
    with pytest.raises(NuitkaError, match="7-Zip"):
        NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)
    # 归档不删除（无法解压非归档损坏，用户装 7-Zip 后仍可用）
    assert local_7z.is_file()


def test_ensure_winlibs_mingw_local_7z_without_7z_online_downloads_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在线 + 本地 .7z + 未装 7-Zip：跳过本地归档回退下载 .zip（归档保留）."""
    import zipfile

    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(cache_root))
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs._find_7z", lambda: None)

    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    local_7z = cache_root / "nuitka-winlibs-mingw" / (zip_name[: -len(".zip")] + ".7z")
    local_7z.parent.mkdir(parents=True)
    local_7z.write_bytes(b"fake-7z")

    downloaded_urls: list[str] = []

    class _FakeDownloader:
        def __init__(self, timeout: float = 0.0) -> None:
            pass

        def download(self, url: str, dest: Path, label: str = "") -> None:
            downloaded_urls.append(url)
            staging = tmp_path / "staging" / "mingw64" / "bin"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "gcc.exe").write_bytes(b"fake-gcc")
            with zipfile.ZipFile(dest, "w") as zf:
                zf.write(staging / "gcc.exe", "mingw64/bin/gcc.exe")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _FakeDownloader)

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)

    # 未装 7-Zip 时下载 .zip（非 .7z）
    assert downloaded_urls == [WINLIBS_URLS["4.1.3"]]
    gcc_exe = NuitkaCompiler._winlibs_gcc_dir("4.1.3") / "mingw64" / "bin" / "gcc.exe"
    assert gcc_exe.is_file()
    # 本地 .7z 未被删除（装 7-Zip 后下次可直接用）
    assert local_7z.is_file()
    assert "下载完成" in st._detail


def test_ensure_winlibs_mingw_download_prefers_7z(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存未命中且装有 7-Zip 时优先下载 .7z（体积约为 zip 一半），解压后归档删除."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path / "cache"))

    downloaded_urls: list[str] = []

    class _FakeDownloader:
        def __init__(self, timeout: float = 0.0) -> None:
            pass

        def download(self, url: str, dest: Path, label: str = "") -> None:
            downloaded_urls.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake-7z")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _FakeDownloader)

    gcc_dir = NuitkaCompiler._winlibs_gcc_dir("4.1.3")
    commands = _patch_7z_extract(monkeypatch, gcc_dir)

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)

    # 下载与解压均走 .7z
    expected_7z_url = WINLIBS_URLS["4.1.3"][: -len(".zip")] + ".7z"
    assert downloaded_urls == [expected_7z_url]
    assert (gcc_dir / "mingw64" / "bin" / "gcc.exe").is_file()
    assert len(commands) == 1
    assert commands[0][-1].endswith(".7z")
    # 下载的 .7z 解压完成后删除
    assert not (gcc_dir / expected_7z_url.rsplit("/", 1)[1]).exists()
    assert "下载完成" in st._detail


def test_ensure_winlibs_mingw_corrupt_local_7z_falls_back_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地 .7z 损坏（7z 非零退出码）时删除后回退下载（下载产物正常解压）."""
    from fspack.packaging.nuitka import NuitkaCompiler
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS
    from fspack.progress import StageRecorder

    cache_root = tmp_path / "cache"
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(cache_root))

    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    seven_name = zip_name[: -len(".zip")] + ".7z"
    corrupt_7z = cache_root / "nuitka-winlibs-mingw" / seven_name
    corrupt_7z.parent.mkdir(parents=True)
    corrupt_7z.write_bytes(b"corrupt-7z")

    downloaded_urls: list[str] = []

    class _FakeDownloader:
        def __init__(self, timeout: float = 0.0) -> None:
            pass

        def download(self, url: str, dest: Path, label: str = "") -> None:
            downloaded_urls.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake-7z")

    monkeypatch.setattr("fspack.packaging.net.Downloader", _FakeDownloader)

    # 首次解压（本地损坏归档）非零退出码；回退下载后第二次解压成功
    gcc_dir = NuitkaCompiler._winlibs_gcc_dir("4.1.3")
    return_codes = [2, 0]
    commands: list[list[str]] = []

    class _Result:
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stdout = ""
            self.stderr = "ERROR: archive corrupted" if code else ""

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        commands.append(cmd)
        code = return_codes.pop(0)
        if code == 0:
            bin_dir = gcc_dir / "mingw64" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "gcc.exe").write_bytes(b"fake-gcc")
        return _Result(code)

    monkeypatch.setattr("fspack.packaging.nuitka.winlibs.subprocess.run", _fake_run)
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs._find_7z", lambda: "C:\\fake\\7z.exe")

    st = StageRecorder("Nuitka 编译")
    NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)

    # 损坏的本地 .7z 已删除；下载回退同样取 .7z（装有 7-Zip）
    assert not corrupt_7z.exists()
    assert downloaded_urls and downloaded_urls[0].endswith(".7z")
    assert (gcc_dir / "mingw64" / "bin" / "gcc.exe").is_file()
    assert len(commands) == 2
    assert "下载完成" in st._detail


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
    # 未装 7-Zip：走 .zip 下载路径（本机装有 7-Zip，显式排除环境差异）
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs._find_7z", lambda: None)

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
    # 未装 7-Zip：走 .zip 下载路径（本机装有 7-Zip，显式排除环境差异）
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs._find_7z", lambda: None)

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
    # 未装 7-Zip：走 .zip 下载路径（本机装有 7-Zip，显式排除环境差异）
    monkeypatch.setattr("fspack.packaging.nuitka.winlibs._find_7z", lambda: None)

    st = StageRecorder("Nuitka 编译")
    with pytest.raises(NuitkaError, match="未找到 gcc"):
        NuitkaCompiler.ensure_winlibs_mingw("3.11.9", st)
