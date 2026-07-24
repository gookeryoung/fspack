"""installer NSIS 脚本生成与 makensis 编译测试."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType, ProjectInfo, get_mirror
from fspack.exceptions import InstallerError
from fspack.packaging.installer import (
    _make_zip,
    _resolve_formats,
    build_deb_release,
    build_installer,
    build_release,
    build_tarball_release,
    build_zip,
    compile_installer,
    generate_nsis_script,
)
from fspack.platform import Platform


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def _make_info(tmp_path: Path, app_type: AppType = AppType.CLI, name: str = "app") -> ProjectInfo:
    return ProjectInfo(
        name=name,
        version="1.0",
        src_dir=tmp_path,
        entry_module=name,
        entry_file=tmp_path / f"{name}.py",
        app_type=app_type,
        dependencies=(),
        py_version="3.11.9",
    )


def test_generate_nsis_script_cli(tmp_path: Path) -> None:
    info = _make_info(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    release = dist / "release"
    nsi = generate_nsis_script(info, dist, release)
    content = nsi.read_text(encoding="utf-8")
    assert nsi == dist / "installer.nsi"
    assert release.is_dir()
    assert 'Name "app 1.0"' in content
    assert 'OutFile "release\\app-1.0-py3.11-windows-slim-setup.exe"' in content
    assert 'InstallDir "$PROGRAMFILES64\\app"' in content
    assert "File /r /x installer.nsi /x release /x *.whl /x *.tar.gz *.*" in content
    assert 'WriteUninstaller "$INSTDIR\\uninstall.exe"' in content
    # 所有应用默认生成开始菜单程序快捷方式、卸载快捷方式与桌面快捷方式
    assert 'CreateDirectory "$SMPROGRAMS\\app"' in content
    assert 'CreateShortCut "$SMPROGRAMS\\app\\app.lnk" "$INSTDIR\\app.exe"' in content
    assert 'CreateShortCut "$SMPROGRAMS\\app\\卸载 app.lnk" "$INSTDIR\\uninstall.exe"' in content
    assert 'CreateShortCut "$DESKTOP\\app.lnk" "$INSTDIR\\app.exe"' in content
    # 所有应用都有注册表卸载条目
    assert "WriteRegStr HKLM" in content
    assert "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\app" in content
    assert '"DisplayName" "app"' in content
    assert '"DisplayVersion" "1.0"' in content
    assert '"NoModify" 1' in content
    assert '"NoRepair" 1' in content
    assert "DeleteRegKey HKLM" in content
    assert 'Section "Uninstall"' in content
    assert "MUI_PAGE_WELCOME" in content
    assert 'MUI_LANGUAGE "SimpChinese"' in content


def test_generate_nsis_script_gui(tmp_path: Path) -> None:
    info = _make_info(tmp_path, app_type=AppType.GUI, name="guiapp")
    dist = tmp_path / "dist"
    dist.mkdir()
    nsi = generate_nsis_script(info, dist, dist / "release")
    content = nsi.read_text(encoding="utf-8")
    # GUI 应用快捷方式：与 CLI 一致（开始菜单程序快捷方式 + 桌面快捷方式 + 卸载快捷方式）
    assert 'CreateDirectory "$SMPROGRAMS\\guiapp"' in content
    assert 'CreateShortCut "$SMPROGRAMS\\guiapp\\guiapp.lnk" "$INSTDIR\\guiapp.exe"' in content
    assert 'CreateShortCut "$DESKTOP\\guiapp.lnk"' in content
    assert 'CreateShortCut "$SMPROGRAMS\\guiapp\\卸载 guiapp.lnk" "$INSTDIR\\uninstall.exe"' in content
    # 卸载时清理
    assert 'RMDir /r "$SMPROGRAMS\\guiapp"' in content
    assert 'Delete "$DESKTOP\\guiapp.lnk"' in content
    # 注册表条目
    assert "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\guiapp" in content
    assert '"DisplayIcon" "$INSTDIR\\guiapp.exe"' in content


def test_generate_nsis_script_registry_block(tmp_path: Path) -> None:
    """所有应用都生成完整的添加/删除程序注册表条目."""
    info = _make_info(tmp_path, name="myapp")
    dist = tmp_path / "dist"
    dist.mkdir()
    nsi = generate_nsis_script(info, dist, dist / "release")
    content = nsi.read_text(encoding="utf-8")
    key = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\myapp"
    assert f'WriteRegStr HKLM "{key}" "DisplayName" "myapp"' in content
    assert f'WriteRegStr HKLM "{key}" "DisplayVersion" "1.0"' in content
    # UninstallString 含引号包裹的路径（路径可能含空格）
    assert f'WriteRegStr HKLM "{key}" "UninstallString" \'"$INSTDIR\\uninstall.exe"\'' in content
    assert f'WriteRegStr HKLM "{key}" "QuietUninstallString" \'"$INSTDIR\\uninstall.exe" /S\'' in content
    assert f'WriteRegStr HKLM "{key}" "InstallLocation" "$INSTDIR"' in content
    assert f'WriteRegStr HKLM "{key}" "Publisher" "fspack"' in content
    assert f'WriteRegStr HKLM "{key}" "DisplayIcon" "$INSTDIR\\myapp.exe"' in content
    assert f'WriteRegDWORD HKLM "{key}" "NoModify" 1' in content
    assert f'WriteRegDWORD HKLM "{key}" "NoRepair" 1' in content
    # 卸载时删除注册表键
    assert f'DeleteRegKey HKLM "{key}"' in content


def test_compile_installer_makensis_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="未找到 makensis"):
        compile_installer(tmp_path / "x.nsi", tmp_path / "out.exe")


def test_compile_installer_makensis_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = subprocess.CalledProcessError(1, "makensis", stderr="bad script")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="makensis 编译失败"):
        compile_installer(tmp_path / "x.nsi", tmp_path / "out.exe")


def test_compile_installer_no_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", lambda cmd, **kw: _Completed())
    with pytest.raises(InstallerError, match="未产出安装包"):
        compile_installer(tmp_path / "x.nsi", tmp_path / "out.exe")


def test_compile_installer_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out.exe"

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out.write_bytes(b"")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = compile_installer(tmp_path / "x.nsi", out)
    assert result == out
    assert out.is_file()


def test_build_installer_no_build_missing_dist(tmp_path: Path) -> None:
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_installer(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)


def test_build_installer_no_build_missing_exe(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_installer(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)


def test_build_installer_no_build_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    out_setup = dist / "release" / "app-1.0-py3.11-windows-slim-setup.exe"

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_installer(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)
    assert result == out_setup
    assert (dist / "installer.nsi").is_file()
    assert "app-1.0-py3.11-windows-slim-setup.exe" in (dist / "installer.nsi").read_text(encoding="utf-8")


def test_build_installer_with_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    out_setup = dist / "release" / "app-1.0-py3.11-windows-slim-setup.exe"

    def fake_build(
        project_dir: Path,
        mirror: object,
        py_version: str,
        dist_dir: Path | None = None,
        target: object = None,
    ) -> object:
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.exe").write_bytes(b"")
        return None

    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_installer(tmp_path, get_mirror("huawei"), "3.11.9", no_build=False)
    assert result == out_setup
    assert (dist / "app.exe").is_file()
    assert (dist / "installer.nsi").is_file()


# ---- _resolve_formats 测试 ----


def test_resolve_formats_auto_windows() -> None:
    """auto + Windows → [nsis]（向后兼容默认行为）."""
    assert _resolve_formats("auto", Platform.WINDOWS) == ["nsis"]


def test_resolve_formats_auto_linux() -> None:
    """auto + Linux → [tar.gz, deb]（向后兼容默认行为）."""
    assert _resolve_formats("auto", Platform.LINUX) == ["tar.gz", "deb"]


def test_resolve_formats_all_windows() -> None:
    """all + Windows → [nsis, zip]."""
    assert _resolve_formats("all", Platform.WINDOWS) == ["nsis", "zip"]


def test_resolve_formats_all_linux() -> None:
    """all + Linux → [tar.gz, deb, zip]."""
    assert _resolve_formats("all", Platform.LINUX) == ["tar.gz", "deb", "zip"]


def test_resolve_formats_zip_cross_platform() -> None:
    """zip 跨平台，Windows 与 Linux 均可."""
    assert _resolve_formats("zip", Platform.WINDOWS) == ["zip"]
    assert _resolve_formats("zip", Platform.LINUX) == ["zip"]


def test_resolve_formats_nsis_only_windows() -> None:
    """nsis 仅 Windows，Linux 目标报错."""
    assert _resolve_formats("nsis", Platform.WINDOWS) == ["nsis"]
    with pytest.raises(InstallerError, match="NSIS 安装包仅支持 Windows"):
        _resolve_formats("nsis", Platform.LINUX)


def test_resolve_formats_linux_only_formats() -> None:
    """tar.gz / deb 仅 Linux，Windows 目标报错."""
    assert _resolve_formats("tar.gz", Platform.LINUX) == ["tar.gz"]
    assert _resolve_formats("deb", Platform.LINUX) == ["deb"]
    with pytest.raises(InstallerError, match=r"tar\.gz 格式仅支持 Linux"):
        _resolve_formats("tar.gz", Platform.WINDOWS)
    with pytest.raises(InstallerError, match=r"deb 格式仅支持 Linux"):
        _resolve_formats("deb", Platform.WINDOWS)


def test_resolve_formats_unknown_raises() -> None:
    """未知 fmt 取值报错."""
    with pytest.raises(InstallerError, match="未知 --format 取值"):
        _resolve_formats("rpm", Platform.WINDOWS)


# ---- _make_zip 测试 ----


def test_make_zip_creates_archive_with_top_dir(tmp_path: Path) -> None:
    """_make_zip 生成 zip，内含顶层目录 <name>-<version>-<platform>，排除 release/."""
    info = _make_info(tmp_path, name="myapp")
    dist = tmp_path / "dist"
    dist.mkdir()
    # dist 下放几个文件 + release 子目录（应被排除）
    (dist / "myapp.exe").write_bytes(b"exe")
    (dist / "runtime").mkdir()
    (dist / "runtime" / "python311.dll").write_bytes(b"dll")
    release = dist / "release"
    release.mkdir()
    (release / "should-be-excluded.txt").write_text("x")

    result = _make_zip(dist, info, release, Platform.WINDOWS)
    assert result.is_file()
    assert result.name == "myapp-1.0-py3.11-windows-slim.zip"
    with zipfile.ZipFile(result) as zf:
        names = zf.namelist()
    # 顶层目录为 myapp-1.0-py3.11-windows-slim/
    assert any(n.startswith("myapp-1.0-py3.11-windows-slim/") for n in names)
    # 包含 exe 与 runtime/python311.dll
    assert "myapp-1.0-py3.11-windows-slim/myapp.exe" in names
    assert "myapp-1.0-py3.11-windows-slim/runtime/python311.dll" in names
    # 排除 release/ 子目录
    assert not any("release" in n for n in names)


def test_make_zip_linux_platform_suffix(tmp_path: Path) -> None:
    """Linux 目标 zip 文件名含 -linux 后缀."""
    info = _make_info(tmp_path, name="app")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    release = dist / "release"
    release.mkdir()
    result = _make_zip(dist, info, release, Platform.LINUX)
    assert result.name == "app-1.0-py3.11-linux-slim.zip"


def test_make_zip_overwrites_existing(tmp_path: Path) -> None:
    """重复构建时覆盖已有 zip."""
    info = _make_info(tmp_path, name="app")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"v1")
    release = dist / "release"
    release.mkdir()
    first = _make_zip(dist, info, release, Platform.WINDOWS)
    first.write_bytes(b"stale")
    second = _make_zip(dist, info, release, Platform.WINDOWS)
    assert second.read_bytes() != b"stale"
    with zipfile.ZipFile(second) as zf:
        assert "app-1.0-py3.11-windows-slim/app.exe" in zf.namelist()


# ---- build_zip 编排测试 ----


def test_build_zip_no_build_missing_dist(tmp_path: Path) -> None:
    """no_build=True 且 dist 不存在时报错."""
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_zip(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)


def test_build_zip_no_build_missing_exe(tmp_path: Path) -> None:
    """no_build=True 且 dist 中无 exe 报错."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_zip(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)


def test_build_zip_no_build_success(tmp_path: Path) -> None:
    """no_build=True 且 dist 已就绪时生成 zip."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    result = build_zip(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)
    assert result.is_file()
    assert result.name == "app-1.0-py3.11-windows-slim.zip"


def test_build_zip_with_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """no_build=False 时调用 build() 构建后生成 zip."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")

    def fake_build(  # noqa: PLR0913
        project_dir: Path,
        mirror: object,
        py_version: str,
        dist_dir: Path | None = None,
        target: object = None,
        keep_modules: set[str] | None = None,
        icon: Path | None = None,
    ) -> object:
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.exe").write_bytes(b"")
        return None

    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)
    result = build_zip(tmp_path, get_mirror("huawei"), "3.11.9", no_build=False)
    assert result.is_file()
    assert result.name == "app-1.0-py3.11-windows-slim.zip"


# ---- build_tarball_release 编排测试 ----


def test_build_tarball_release_no_build_success(tmp_path: Path) -> None:
    """build_tarball_release 直接生成 tar.gz（无外部工具依赖）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")  # Linux 可执行文件无后缀
    result = build_tarball_release(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)
    assert result.is_file()
    assert result.name == "app-1.0-py3.11-linux-slim.tar.gz"


def test_build_tarball_release_missing_exe(tmp_path: Path) -> None:
    """build_tarball_release 在 dist 中无 Linux 可执行文件时报错."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_tarball_release(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)


# ---- build_deb_release 编排测试 ----


def test_build_deb_release_no_build_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_deb_release 调用 dpkg-deb 生成 .deb."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        # dpkg-deb --build <staging> <deb_path>，模拟生成 .deb
        deb_path = Path(cmd[-1])
        deb_path.parent.mkdir(parents=True, exist_ok=True)
        deb_path.write_bytes(b"deb-content")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_deb_release(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)
    assert result.is_file()
    assert result.name == "app_1.0-py3.11-slim_amd64.deb"
    assert result.read_bytes() == b"deb-content"


# ---- build_release 调度测试 ----


def test_build_release_auto_windows_dispatches_nsis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fmt=auto + Windows → 仅调用 NsisInstaller.build_installer."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")

    calls: list[str] = []

    def fake_nsis_build_installer(cls, *args, **kw):  # type: ignore[no-untyped-def]
        calls.append("nsis")
        out = dist / "release" / "app-1.0-py3.11-windows-slim-setup.exe"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return out

    monkeypatch.setattr(
        "fspack.packaging.installer.NsisInstaller.build_installer", classmethod(fake_nsis_build_installer)
    )
    outputs = build_release(
        tmp_path, get_mirror("huawei"), "3.11.9", no_build=True, target=Platform.WINDOWS, fmt="auto"
    )
    assert calls == ["nsis"]
    assert len(outputs) == 1


def test_build_release_zip_only(tmp_path: Path) -> None:
    """fmt=zip → 仅生成 zip，不调用 NSIS/dpkg-deb."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    outputs = build_release(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True, target=Platform.WINDOWS, fmt="zip")
    assert len(outputs) == 1
    assert outputs[0].name == "app-1.0-py3.11-windows-slim.zip"


def test_build_release_all_windows_generates_two_formats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fmt=all + Windows → 生成 nsis + zip 两种格式，复用同一 dist."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")

    build_calls: list[str] = []

    def fake_nsis_build_installer(cls, *args, **kw):  # type: ignore[no-untyped-def]
        build_calls.append("nsis-build")
        out = dist / "release" / "app-1.0-py3.11-windows-slim-setup.exe"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return out

    # 监控 build() 是否被多次调用（all 模式下应仅首次调用 build）
    def fake_build(*args, **kw):  # type: ignore[no-untyped-def]
        build_calls.append("build")

    monkeypatch.setattr(
        "fspack.packaging.installer.NsisInstaller.build_installer", classmethod(fake_nsis_build_installer)
    )
    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)
    outputs = build_release(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True, target=Platform.WINDOWS, fmt="all")
    assert len(outputs) == 2
    assert outputs[0].name == "app-1.0-py3.11-windows-slim-setup.exe"
    assert outputs[1].name == "app-1.0-py3.11-windows-slim.zip"
    # no_build=True 时不应触发 build()
    assert "build" not in build_calls


def test_build_release_invalid_fmt_raises(tmp_path: Path) -> None:
    """fmt 取值非法时报错."""
    with pytest.raises(InstallerError, match="未知 --format 取值"):
        build_release(tmp_path, get_mirror("huawei"), "3.11.9", target=Platform.WINDOWS, fmt="rpm")


def test_build_release_platform_mismatch_raises(tmp_path: Path) -> None:
    """fmt=nsis + Linux 目标报错."""
    with pytest.raises(InstallerError, match="NSIS 安装包仅支持 Windows"):
        build_release(tmp_path, get_mirror("huawei"), "3.11.9", target=Platform.LINUX, fmt="nsis")
