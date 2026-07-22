"""installer NSIS 脚本生成与 makensis 编译测试."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType, ProjectInfo
from fspack.exceptions import InstallerError
from fspack.mirror import get_mirror
from fspack.packaging.installer import build_installer, compile_installer, generate_nsis_script


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
    assert 'OutFile "release\\app-1.0-setup.exe"' in content
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
    out_setup = dist / "release" / "app-1.0-setup.exe"

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return _Completed()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_installer(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True)
    assert result == out_setup
    assert (dist / "installer.nsi").is_file()
    assert "app-1.0-setup.exe" in (dist / "installer.nsi").read_text(encoding="utf-8")


def test_build_installer_with_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    out_setup = dist / "release" / "app-1.0-setup.exe"

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
