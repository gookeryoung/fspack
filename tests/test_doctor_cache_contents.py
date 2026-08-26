"""``fsp doctor`` 压缩包缓存内容盘点测试（:mod:`fspack.doctor.cache_contents`）.

覆盖各归档缓存（embed/standalone/nuitka/tkinter/winlibs）的版本清单盘点：
有内容/空缓存在线/空缓存离线/目录不存在/非预期文件名跳过/版本预览超限，
以及 ``_cache_content_fns`` 的平台分发。缓存目录统一经
``FSPACK_CACHE_DIR`` 环境变量重定向到 tmp_path（``cache_root`` 全链路生效）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.doctor.cache_contents import (
    _cache_content_fns,
    _check_embed_contents,
    _check_nuitka_contents,
    _check_standalone_contents,
    _check_standalone_windows_contents,
    _check_tkinter_contents,
    _check_winlibs_contents,
)
from fspack.doctor.models import CheckStatus
from fspack.platform import Platform


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """缓存根重定向到 tmp_path，并确保在线模式（离线用例单独覆盖）."""
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
    return tmp_path


# ---- embed / standalone / tkinter 归档盘点 ----


def test_check_embed_contents_cached(tmp_path: Path) -> None:
    """embed 缓存有归档：OK，detail 含版本与体积."""
    (tmp_path / "embed").mkdir()
    (tmp_path / "embed" / "python-3.11.9-embed-amd64.zip").write_bytes(b"x" * 1024)
    (tmp_path / "embed" / "python-3.12.4-embed-amd64.zip").write_bytes(b"y" * 2048)
    result = _check_embed_contents()
    assert result.status is CheckStatus.OK
    assert "2 个" in result.detail
    assert "3.11.9" in result.detail
    assert "3.12.4" in result.detail
    assert "3.0 KiB" in result.detail


def test_check_embed_contents_empty_online(tmp_path: Path) -> None:
    """embed 缓存空且在线：OK（首次打包自动下载）."""
    (tmp_path / "embed").mkdir()
    result = _check_embed_contents()
    assert result.status is CheckStatus.OK
    assert "未缓存" in result.detail


def test_check_embed_contents_missing_dir_online(tmp_path: Path) -> None:
    """embed 缓存目录不存在且在线：OK（同空缓存）."""
    result = _check_embed_contents()
    assert result.status is CheckStatus.OK
    assert "未缓存" in result.detail


def test_check_embed_contents_empty_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """embed 缓存空且离线：WARN，建议指向缓存目录路径."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    (tmp_path / "embed").mkdir()
    result = _check_embed_contents()
    assert result.status is CheckStatus.WARN
    assert "离线模式无法下载" in result.suggestion
    assert str(tmp_path / "embed") in result.suggestion


def test_check_embed_contents_ignores_unexpected_names(tmp_path: Path) -> None:
    """非预期文件名（README/普通 zip）不计入版本清单."""
    (tmp_path / "embed").mkdir()
    (tmp_path / "embed" / "README.md").write_text("note")
    (tmp_path / "embed" / "other.zip").write_bytes(b"z")
    result = _check_embed_contents()
    assert result.status is CheckStatus.OK
    assert "未缓存" in result.detail


def test_check_embed_contents_version_preview_over_limit(tmp_path: Path) -> None:
    """版本数超预览上限（5）：detail 追加"等 N 个"且不逐个列出."""
    embed = tmp_path / "embed"
    embed.mkdir()
    for minor in range(7):
        (embed / f"python-3.1{minor}.0-embed-amd64.zip").write_bytes(b"x")
    result = _check_embed_contents()
    assert result.status is CheckStatus.OK
    assert "7 个" in result.detail
    assert "等 7 个" in result.detail
    assert "3.16.0" not in result.detail  # 第 6、7 个不逐个展示


def test_check_standalone_contents_cached(tmp_path: Path) -> None:
    """standalone 缓存有 tarball：OK，detail 含版本."""
    (tmp_path / "standalone").mkdir()
    (tmp_path / "standalone" / "cpython-3.11.9+stable-x86_64-unknown-linux-install_only.tar.gz").write_bytes(b"x")
    result = _check_standalone_contents()
    assert result.status is CheckStatus.OK
    assert "3.11.9" in result.detail


def test_check_standalone_windows_contents_cached(tmp_path: Path) -> None:
    """standalone-windows 共享缓存盘点（cache_root/standalone-windows）."""
    sw = tmp_path / "standalone-windows"
    sw.mkdir()
    (sw / "cpython-3.12.4+stable-x86_64-pc-windows-msvc-install_only.tar.gz").write_bytes(b"x")
    result = _check_standalone_windows_contents()
    assert result.status is CheckStatus.OK
    assert "3.12.4" in result.detail


def test_check_standalone_windows_contents_empty_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """standalone-windows 空缓存离线：WARN."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    (tmp_path / "standalone-windows").mkdir()
    result = _check_standalone_windows_contents()
    assert result.status is CheckStatus.WARN
    assert str(tmp_path / "standalone-windows") in result.suggestion


def test_check_tkinter_contents_cached(tmp_path: Path) -> None:
    """tkinter 缓存有组件 zip：OK，detail 含版本."""
    (tmp_path / "tkinter").mkdir()
    (tmp_path / "tkinter" / "tkinter-3.11.9.zip").write_bytes(b"x" * 512)
    result = _check_tkinter_contents()
    assert result.status is CheckStatus.OK
    assert "3.11.9" in result.detail
    assert "512 B" in result.detail


def test_check_tkinter_contents_empty_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tkinter 缓存空且离线：WARN."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    result = _check_tkinter_contents()
    assert result.status is CheckStatus.WARN
    assert "离线模式无法下载" in result.suggestion


# ---- nuitka 构建用 python 盘点 ----


def test_check_nuitka_contents_versions_and_residual(tmp_path: Path) -> None:
    """nuitka 缓存：已解压版本目录 + 残留 tarball 计数提示."""
    nuitka = tmp_path / "nuitka"
    nuitka.mkdir()
    (nuitka / "3.11.9").mkdir()
    (nuitka / "3.13.1t").mkdir()  # free-threaded 带 t 后缀
    (nuitka / "cpython-3.12.4+stable-x86_64-pc-windows-msvc-install_only.tar.gz").write_bytes(b"x")
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.OK
    assert "2 个版本" in result.detail
    assert "3.11.9" in result.detail
    assert "3.13.1t" in result.detail
    assert "残留 tarball 1 个" in result.detail
    assert "fsp cache clean" in result.detail


def test_check_nuitka_contents_empty_online(tmp_path: Path) -> None:
    """nuitka 缓存空且在线：OK（按需下载）."""
    (tmp_path / "nuitka").mkdir()
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.OK
    assert "未缓存" in result.detail


def test_check_nuitka_contents_ignores_non_version_dirs(tmp_path: Path) -> None:
    """非版本目录名（如 ccache）与无后缀目录不计入版本清单."""
    nuitka = tmp_path / "nuitka"
    nuitka.mkdir()
    (nuitka / "ccache").mkdir()
    (nuitka / "3.11").mkdir()  # 两段式，不符合三段版本正则
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.OK
    assert "未缓存" in result.detail


def test_check_nuitka_contents_only_residual(tmp_path: Path) -> None:
    """仅有残留 tarball（无版本目录）：不算空缓存，单独提示残留."""
    nuitka = tmp_path / "nuitka"
    nuitka.mkdir()
    (nuitka / "cpython-3.11.9+stable-x86_64-pc-windows-msvc-install_only.tar.gz").write_bytes(b"x")
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.OK
    assert "无已解压版本" in result.detail
    assert "残留 tarball 1 个" in result.detail


def test_check_nuitka_contents_sdist_only(tmp_path: Path) -> None:
    """wheels 下存在 nuitka sdist 归档：不算空缓存（构建安装 nuitka 包免下载）.

    复现用户场景：``<cache_root>/wheels/nuitka-4.1.3.tar.gz`` 存在，
    构建侧 ``_find_local_nuitka_sdist`` 识别本地安装，doctor 须可见。
    """
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "nuitka-4.1.3.tar.gz").write_bytes(b"x")
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.OK
    assert "sdist 已缓存 1 个" in result.detail
    assert "4.1.3" in result.detail
    assert "免下载" in result.detail


def test_check_nuitka_contents_sdist_case_insensitive_subdir(tmp_path: Path) -> None:
    """PyPI 官方大写 Nuitka-<ver>.tar.gz 与子目录放置均识别（与构建侧口径一致）."""
    sub = tmp_path / "wheels" / "pypi"
    sub.mkdir(parents=True)
    (sub / "Nuitka-2.5.1.tar.gz").write_bytes(b"x")
    result = _check_nuitka_contents()
    assert "sdist 已缓存 1 个" in result.detail
    assert "2.5.1" in result.detail


def test_check_nuitka_contents_sdist_offline_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线 + 仅 sdist：OK（sdist 本地安装离线可用，不算空缓存 WARN）."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "nuitka-4.1.3.tar.gz").write_bytes(b"x")
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.OK
    assert "未缓存" not in result.detail


def test_check_nuitka_contents_versions_and_sdist(tmp_path: Path) -> None:
    """已解压版本与 sdist 并存：两段清单都展示."""
    (tmp_path / "nuitka" / "3.11.9").mkdir(parents=True)
    (tmp_path / "wheels").mkdir()
    (tmp_path / "wheels" / "nuitka-4.1.3.tar.gz").write_bytes(b"x")
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.OK
    assert "已解压 1 个版本" in result.detail
    assert "sdist 已缓存 1 个" in result.detail


def test_check_nuitka_contents_sdist_wrong_name_ignored(tmp_path: Path) -> None:
    """非 nuitka 命名的 tar.gz（如 cpython standalone 源）不计入 sdist 清单."""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "somepkg-1.0.0.tar.gz").write_bytes(b"x")
    result = _check_nuitka_contents()
    assert "sdist" not in result.detail
    assert "未缓存" in result.detail


# ---- winlibs 工具链盘点 ----


def test_check_winlibs_contents_gcc_ready(tmp_path: Path) -> None:
    """winlibs gcc.exe 就位：OK（gcc 已就绪）."""
    gcc_dir = tmp_path / "nuitka-winlibs-mingw" / "gcc" / "x86_64" / "4.1.3" / "mingw64" / "bin"
    gcc_dir.mkdir(parents=True)
    (gcc_dir / "gcc.exe").write_bytes(b"x")
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.OK
    assert "gcc 已就绪" in result.detail
    assert "4.1.3" in result.detail


def test_check_winlibs_contents_local_zip(tmp_path: Path) -> None:
    """与锁定版本精确匹配的本地 winlibs zip：OK（首次构建自动解压，离线可用）."""
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS

    wl = tmp_path / "nuitka-winlibs-mingw"
    wl.mkdir()
    # 取锁定清单中的真实归档名（构建侧按完整文件名精确匹配）
    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    (wl / zip_name).write_bytes(b"x")
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.OK
    assert "本地归档 1 个待解压" in result.detail


def test_check_winlibs_contents_local_7z_matched(tmp_path: Path) -> None:
    """与锁定版本精确匹配的本地 winlibs .7z（构建侧经系统 7-Zip 解压）：OK 待解压."""
    from fspack.packaging.nuitka.winlibs import WINLIBS_URLS

    wl = tmp_path / "nuitka-winlibs-mingw"
    wl.mkdir()
    zip_name = WINLIBS_URLS["4.1.3"].rsplit("/", 1)[1]
    (wl / (zip_name[: -len(".zip")] + ".7z")).write_bytes(b"x")
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.OK
    assert "本地归档 1 个待解压" in result.detail


def test_check_winlibs_contents_mismatched_zip_not_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """版本不匹配的本地 zip（不在 WINLIBS_URLS 锁定清单）：不算待解压，落入未缓存分支并提示."""
    wl = tmp_path / "nuitka-winlibs-mingw"
    wl.mkdir()
    (wl / "winlibs-x86_64-posix-seh-gcc-13.2.0-mingw-w64ucrt-11.0.1-r2.zip").write_bytes(b"x")
    monkeypatch.setattr("fspack.doctor.cache_contents._msvc_available", lambda: False)
    result = _check_winlibs_contents()
    assert "待解压" not in result.detail
    assert "1 个本地归档不被识别" in result.detail
    assert "不会被构建使用" in result.suggestion
    assert "13.2.0" in result.suggestion


def test_check_winlibs_contents_7z_archive_reported_as_mismatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.7z 格式归档（即使版本号接近）不算可用缓存：未缓存分支提示不被识别.

    复现用户场景：缓存目录放置更新的 winlibs release ``.7z`` 归档，
    构建侧仅识别与锁定版本精确匹配的 ``.zip``，doctor 须明确指出
    归档存在但不会被使用。
    """
    wl = tmp_path / "nuitka-winlibs-mingw"
    wl.mkdir()
    (wl / "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-14.0.0-r7.7z").write_bytes(b"x")
    (wl / "winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64msvcrt-14.0.0-r1.7z").write_bytes(b"x")
    monkeypatch.setattr("fspack.doctor.cache_contents._msvc_available", lambda: True)
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.OK
    assert "未缓存" in result.detail
    assert "2 个本地归档不被识别" in result.detail
    assert ".7z" in result.suggestion or "7z" in result.suggestion
    # suggestion 须给出所需的确切归档名（用户可对照下载正确版本）
    assert "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-13.0.0-r6.zip" in result.suggestion


def test_check_winlibs_contents_offline_mismatched_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线 + 仅不匹配归档：WARN，suggestion 同时含离线提示与所需确切归档名."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setattr("fspack.doctor.cache_contents._msvc_available", lambda: False)
    wl = tmp_path / "nuitka-winlibs-mingw"
    wl.mkdir()
    (wl / "winlibs-x86_64-posix-seh-gcc-15.2.0-mingw-w64msvcrt-14.0.0-r7.7z").write_bytes(b"x")
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.WARN
    assert "1 个本地归档不被识别" in result.detail
    assert "离线模式无法下载" in result.suggestion
    assert "不会被构建使用" in result.suggestion


def test_check_winlibs_contents_gcc_dir_without_exe(tmp_path: Path) -> None:
    """specificity 目录存在但 gcc.exe 缺失：不算就绪，落入未缓存分支."""
    gcc_dir = tmp_path / "nuitka-winlibs-mingw" / "gcc" / "x86_64" / "4.1.3" / "mingw64" / "bin"
    gcc_dir.mkdir(parents=True)
    result = _check_winlibs_contents()
    assert "未缓存" in result.detail


def test_check_winlibs_contents_msvc_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未缓存但检测到 MSVC：OK（编译器优先用 MSVC）."""
    monkeypatch.setattr("fspack.doctor.cache_contents._msvc_available", lambda: True)
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.OK
    assert "MSVC" in result.detail


def test_check_winlibs_contents_empty_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未缓存且离线（无 MSVC）：WARN，建议放本地 zip."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    monkeypatch.setattr("fspack.doctor.cache_contents._msvc_available", lambda: False)
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.WARN
    assert "离线模式无法下载" in result.suggestion
    assert "winlibs zip" in result.suggestion


def test_check_winlibs_contents_empty_online(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未缓存且在线（无 MSVC）：OK（首次构建自动下载）."""
    monkeypatch.setattr("fspack.doctor.cache_contents._msvc_available", lambda: False)
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.OK
    assert "自动下载" in result.detail


# ---- 扫描异常与竞态 ----


def test_check_embed_contents_skips_subdir(tmp_path: Path) -> None:
    """embed 缓存下的子目录条目不计入清单（is_file 过滤）."""
    embed = tmp_path / "embed"
    embed.mkdir()
    (embed / "temp-dir").mkdir()
    (embed / "python-3.11.9-embed-amd64.zip").write_bytes(b"x")
    result = _check_embed_contents()
    assert result.status is CheckStatus.OK
    assert "1 个" in result.detail


def test_check_embed_contents_stat_race_skips_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """枚举后 stat 失败的竞态文件不计入（第二次 stat 抛 OSError）.

    故障注入：``Path.stat`` 按调用次数翻转——``is_file`` 内部首次 stat 正常，
    :func:`_match_files` 的体积统计二次 stat 抛错，模拟枚举后被删除/锁定的竞态。
    """
    embed = tmp_path / "embed"
    embed.mkdir()
    zip_path = embed / "python-3.11.9-embed-amd64.zip"
    zip_path.write_bytes(b"x")
    real_stat = Path.stat
    calls: dict[Path, int] = {}

    def _flaky_stat(self: Path, **kwargs: object) -> object:
        calls[self] = calls.get(self, 0) + 1
        if calls[self] == 2:
            raise PermissionError("simulated race")
        return real_stat(self, **kwargs)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(Path, "stat", _flaky_stat)
    result = _check_embed_contents()
    assert result.status is CheckStatus.OK
    assert "未缓存" in result.detail  # 竞态文件被跳过后清单为空


def _raise_denied(*args: object, **kwargs: object) -> object:
    """通用故障注入桩：任何调用抛 PermissionError."""
    raise PermissionError("denied")


def test_check_archive_inventory_scan_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """归档盘点目录枚举失败：WARN（仅诊断信息缺失）."""
    monkeypatch.setattr("fspack.doctor.cache_contents._match_files", _raise_denied)
    result = _check_embed_contents()
    assert result.status is CheckStatus.WARN
    assert "扫描缓存目录失败" in result.suggestion
    assert str(tmp_path / "embed") in result.detail


def test_check_nuitka_contents_scan_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """nuitka 盘点目录枚举失败：WARN."""
    monkeypatch.setattr("fspack.doctor.cache_contents._nuitka_versions", _raise_denied)
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.WARN
    assert "扫描缓存目录失败" in result.suggestion
    assert str(tmp_path / "nuitka") in result.detail


def test_check_winlibs_contents_scan_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """winlibs 盘点目录枚举失败：WARN."""
    monkeypatch.setattr("fspack.doctor.cache_contents._winlibs_gcc_specificities", _raise_denied)
    result = _check_winlibs_contents()
    assert result.status is CheckStatus.WARN
    assert "扫描缓存目录失败" in result.suggestion
    assert str(tmp_path / "nuitka-winlibs-mingw") in result.detail


def test_check_nuitka_contents_versions_without_residual(tmp_path: Path) -> None:
    """nuitka 缓存仅有版本目录（无残留 tarball）：不追加残留提示."""
    (tmp_path / "nuitka" / "3.11.9").mkdir(parents=True)
    result = _check_nuitka_contents()
    assert result.status is CheckStatus.OK
    assert "1 个版本" in result.detail
    assert "残留" not in result.detail


# ---- 平台分发 ----


def test_cache_content_fns_windows() -> None:
    """Windows 平台分发 5 项：nuitka/embed/standalone-windows/tkinter/winlibs."""
    fns = _cache_content_fns(Platform.WINDOWS)
    results = [fn() for fn in fns]
    names = {r.name for r in results}
    assert names == {"nuitka 缓存", "embed 缓存", "standalone-windows 缓存", "tkinter 缓存", "winlibs 工具链"}


def test_cache_content_fns_linux() -> None:
    """Linux 平台分发 2 项：nuitka/standalone."""
    fns = _cache_content_fns(Platform.LINUX)
    names = {fn().name for fn in fns}
    assert names == {"nuitka 缓存", "standalone 缓存"}


def test_cache_content_fns_macos() -> None:
    """macOS 平台与 Linux 同：nuitka/standalone."""
    fns = _cache_content_fns(Platform.MACOS)
    names = {fn().name for fn in fns}
    assert names == {"nuitka 缓存", "standalone 缓存"}
