"""installer NSIS 脚本生成与 makensis 编译测试."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType, BuildOptions, EntryPoint, ProjectInfo, get_mirror
from fspack.exceptions import InstallerError
from fspack.packaging.installer import (
    ReleaseRequest,
    SignOptions,
    _find_7z,
    _make_7z,
    _make_zip,
    _resolve_formats,
    build_deb_release,
    build_installer,
    build_release,
    build_sevenzip,
    build_tarball_release,
    build_zip,
    compile_installer,
    generate_nsis_script,
    nsis_tool,
)

# 注意：installer.nsis 必须在 installer 之后导入（installer 导入会触发子模块加载）
from fspack.packaging.installer.nsis import dist_needs_ucrt, sign_exe_file, sign_exe_files
from fspack.platform import Platform
from fspack.progress import BuildTracker
from tests._stubs import CompletedStub


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
    assert 'OutFile "release\\app-1.0-py3.11.9-windows-slim-setup.exe"' in content
    assert 'InstallDir "$PROGRAMFILES64\\app"' in content
    # 从注册表读取上次安装路径作为默认目录
    assert (
        'InstallDirRegKey HKLM "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\app" "InstallLocation"'
        in content
    )
    # 包含版本比较所需的头文件
    assert '!include "LogicLib.nsh"' in content
    assert '!include "WordFunc.nsh"' in content
    # NSIS 排除构建中间文件（.dep_cache.json/.nuitka_compile_stamp/.pyc_stamp/*.build）
    assert "File /r /x installer.nsi /x release /x *.whl /x *.tar.gz" in content
    assert "/x .dep_cache.json" in content
    assert "/x .nuitka_compile_stamp" in content
    assert "/x .pyc_stamp" in content
    assert "/x *.build" in content
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


def test_generate_nsis_script_multi_entry_shortcut_uses_default_entry(tmp_path: Path) -> None:
    """多入口项目快捷方式/注册表引用默认入口 exe（gui.exe 而非项目名 app.exe）."""
    info = _make_multi_entry_info(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    nsi = generate_nsis_script(info, dist, dist / "release")
    content = nsi.read_text(encoding="utf-8")
    # 快捷方式名沿用项目名（安装展示），目标指向默认入口 exe
    assert 'CreateShortCut "$SMPROGRAMS\\app\\app.lnk" "$INSTDIR\\gui.exe"' in content
    assert 'CreateShortCut "$DESKTOP\\app.lnk" "$INSTDIR\\gui.exe"' in content
    assert '"DisplayIcon" "$INSTDIR\\gui.exe"' in content


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


def test_generate_nsis_script_uninstall_old_version(tmp_path: Path) -> None:
    """安装时检测已安装版本，不同版本询问并静默卸载旧版，相同版本直接覆盖."""
    info = _make_info(tmp_path, name="app")
    dist = tmp_path / "dist"
    dist.mkdir()
    nsi = generate_nsis_script(info, dist, dist / "release")
    content = nsi.read_text(encoding="utf-8")
    key = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\app"
    # .onInit 函数：读取已安装版本与安装路径
    assert "Function .onInit" in content
    assert f'ReadRegStr $R0 HKLM "{key}" "DisplayVersion"' in content
    assert f'ReadRegStr $R2 HKLM "{key}" "InstallLocation"' in content
    # InstallLocation 存在且 uninstall.exe 存在时进入卸载分支
    assert '${If} $R2 != ""' in content
    assert '${AndIf} ${FileExists} "$R2\\uninstall.exe"' in content
    # 相同版本直接 Return 不打扰
    assert '${If} $R0 == "1.0"' in content
    assert "  Return" in content
    # 不同版本询问是否卸载
    assert "MessageBox MB_YESNO|MB_ICONQUESTION" in content
    assert "检测到已安装 $R0 版本，是否先卸载再安装 1.0？" in content
    # _?= 参数让卸载器不自我复制到 temp，ExecWait 才能等待真正完成
    assert "ExecWait '\"$R2\\uninstall.exe\" /S _?=$R2' $R3" in content
    # 用户拒绝卸载时直接覆盖安装
    assert "skip_uninstall:" in content


def test_dist_needs_ucrt(tmp_path: Path) -> None:
    """dist 内 PE 导入 api-ms-win-crt-* 时判定需 UCRT（release/build 不参与）."""
    dist = tmp_path / "dist"
    runtime = dist / "runtime"
    runtime.mkdir(parents=True)
    # 导入表 dll 名为 ASCII 明文：含前缀即命中（build/release 排除在外）
    (runtime / "python311.dll").write_bytes(b"MZ...api-ms-win-crt-runtime-l1-1-0.dll...")
    (dist / "release").mkdir()
    (dist / "release" / "fake.dll").write_bytes(b"api-ms-win-crt-")
    assert dist_needs_ucrt(dist) is True

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "app.exe").write_bytes(b"MZ clean no ucrt marker")
    assert dist_needs_ucrt(clean) is False


def test_generate_nsis_script_ucrt_check_block(tmp_path: Path) -> None:
    """产物依赖 UCRT 时 .onInit 含 ucrtbase.dll 检测段（缺失提示 KB 编号）."""
    info = _make_info(tmp_path, name="app")
    dist = tmp_path / "dist"
    (dist / "runtime").mkdir(parents=True)
    (dist / "runtime" / "python311.dll").write_bytes(b"MZ...api-ms-win-crt-runtime-l1-1-0.dll...")
    nsi = generate_nsis_script(info, dist, dist / "release")
    content = nsi.read_text(encoding="utf-8")
    assert '${IfNot} ${FileExists} "$SYSDIR\\ucrtbase.dll"' in content
    assert "KB2999226" in content
    # 拒绝继续时中止安装
    assert "Abort" in content
    assert "ucrt_ok:" in content


def test_generate_nsis_script_no_ucrt_block_when_not_needed(tmp_path: Path) -> None:
    """产物无 UCRT 依赖时不生成检测段（Win10+/自带场景不弹窗）."""
    info = _make_info(tmp_path, name="app")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"MZ no ucrt")
    nsi = generate_nsis_script(info, dist, dist / "release")
    content = nsi.read_text(encoding="utf-8")
    assert "ucrtbase.dll" not in content
    assert "Function .onInit" in content


def test_compile_installer_makensis_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.packaging.installer.nsis.ensure_nsis", lambda: "makensis")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="未找到 makensis"):
        compile_installer(tmp_path / "x.nsi", tmp_path / "out.exe")


def test_compile_installer_makensis_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.packaging.installer.nsis.ensure_nsis", lambda: "makensis")
    err = subprocess.CalledProcessError(1, "makensis", stderr="bad script")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="makensis 编译失败"):
        compile_installer(tmp_path / "x.nsi", tmp_path / "out.exe")


def test_compile_installer_no_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fspack.packaging.installer.nsis.ensure_nsis", lambda: "makensis")
    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", lambda cmd, **kw: CompletedStub())
    with pytest.raises(InstallerError, match="未产出安装包"):
        compile_installer(tmp_path / "x.nsi", tmp_path / "out.exe")


def test_compile_installer_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out.exe"

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out.write_bytes(b"")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = compile_installer(tmp_path / "x.nsi", out)
    assert result == out
    assert out.is_file()


def test_compile_installer_uses_cached_makensis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_nsis 返回缓存绝对路径时作为命令首参数（缓存优先于 PATH）."""
    cached = tmp_path / "nsis-3.11" / "Bin" / "makensis.exe"
    monkeypatch.setattr("fspack.packaging.installer.nsis.ensure_nsis", lambda: str(cached))
    out = tmp_path / "out.exe"
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        seen.append(cmd)
        out.write_bytes(b"")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    compile_installer(tmp_path / "x.nsi", out)
    assert seen[0][0] == str(cached)


def test_build_installer_no_build_missing_dist(tmp_path: Path) -> None:
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_installer(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))


def test_build_installer_no_build_missing_exe(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_installer(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))


def test_build_installer_no_build_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    out_setup = dist / "release" / "app-1.0-py3.11.9-windows-slim-setup.exe"

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_installer(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))
    assert result == out_setup
    assert (dist / "installer.nsi").is_file()
    assert "app-1.0-py3.11.9-windows-slim-setup.exe" in (dist / "installer.nsi").read_text(encoding="utf-8")


def test_build_installer_with_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    out_setup = dist / "release" / "app-1.0-py3.11.9-windows-slim-setup.exe"

    def fake_build(  # noqa: PLR0913
        project_dir: Path,
        mirror: object,
        py_version: str,
        *,
        dist_dir: Path | None = None,
        target: object = None,
        options: object = None,
    ) -> ProjectInfo:
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.exe").write_bytes(b"")
        return ProjectInfo(
            name="app",
            version="1.0",
            src_dir=project_dir,
            entry_module="app",
            entry_file=project_dir / "app.py",
            app_type=AppType.CLI,
            dependencies=(),
            py_version=py_version,
        )

    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_installer(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=False))
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


def test_resolve_formats_auto_macos() -> None:
    """auto + macOS → [pkg, dmg]."""
    assert _resolve_formats("auto", Platform.MACOS) == ["pkg", "dmg"]


def test_resolve_formats_all_windows() -> None:
    """all + Windows → [nsis, zip, 7z]."""
    assert _resolve_formats("all", Platform.WINDOWS) == ["nsis", "zip", "7z"]


def test_resolve_formats_all_linux() -> None:
    """all + Linux → [tar.gz, deb, zip, 7z]."""
    assert _resolve_formats("all", Platform.LINUX) == ["tar.gz", "deb", "zip", "7z"]


def test_resolve_formats_all_macos() -> None:
    """all + macOS → [pkg, dmg, zip, 7z]."""
    assert _resolve_formats("all", Platform.MACOS) == ["pkg", "dmg", "zip", "7z"]


def test_resolve_formats_zip_cross_platform() -> None:
    """zip 跨平台，Windows / Linux / macOS 均可."""
    assert _resolve_formats("zip", Platform.WINDOWS) == ["zip"]
    assert _resolve_formats("zip", Platform.LINUX) == ["zip"]
    assert _resolve_formats("zip", Platform.MACOS) == ["zip"]


def test_resolve_formats_7z_cross_platform() -> None:
    """7z 跨平台，Windows / Linux / macOS 均可."""
    assert _resolve_formats("7z", Platform.WINDOWS) == ["7z"]
    assert _resolve_formats("7z", Platform.LINUX) == ["7z"]
    assert _resolve_formats("7z", Platform.MACOS) == ["7z"]


def test_resolve_formats_nsis_only_windows() -> None:
    """nsis 仅 Windows，Linux / macOS 目标报错."""
    assert _resolve_formats("nsis", Platform.WINDOWS) == ["nsis"]
    with pytest.raises(InstallerError, match="NSIS 安装包仅支持 Windows"):
        _resolve_formats("nsis", Platform.LINUX)
    with pytest.raises(InstallerError, match="NSIS 安装包仅支持 Windows"):
        _resolve_formats("nsis", Platform.MACOS)


def test_resolve_formats_linux_only_formats() -> None:
    """tar.gz / deb 仅 Linux，Windows / macOS 目标报错."""
    assert _resolve_formats("tar.gz", Platform.LINUX) == ["tar.gz"]
    assert _resolve_formats("deb", Platform.LINUX) == ["deb"]
    with pytest.raises(InstallerError, match=r"tar\.gz 格式仅支持 Linux"):
        _resolve_formats("tar.gz", Platform.WINDOWS)
    with pytest.raises(InstallerError, match=r"deb 格式仅支持 Linux"):
        _resolve_formats("deb", Platform.WINDOWS)
    with pytest.raises(InstallerError, match=r"tar\.gz 格式仅支持 Linux"):
        _resolve_formats("tar.gz", Platform.MACOS)
    with pytest.raises(InstallerError, match=r"deb 格式仅支持 Linux"):
        _resolve_formats("deb", Platform.MACOS)


def test_resolve_formats_macos_only_formats() -> None:
    """pkg / dmg 仅 macOS，Windows / Linux 目标报错."""
    assert _resolve_formats("pkg", Platform.MACOS) == ["pkg"]
    assert _resolve_formats("dmg", Platform.MACOS) == ["dmg"]
    with pytest.raises(InstallerError, match=r"pkg 格式仅支持 macOS"):
        _resolve_formats("pkg", Platform.WINDOWS)
    with pytest.raises(InstallerError, match=r"dmg 格式仅支持 macOS"):
        _resolve_formats("dmg", Platform.WINDOWS)
    with pytest.raises(InstallerError, match=r"pkg 格式仅支持 macOS"):
        _resolve_formats("pkg", Platform.LINUX)
    with pytest.raises(InstallerError, match=r"dmg 格式仅支持 macOS"):
        _resolve_formats("dmg", Platform.LINUX)


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
    assert result.name == "myapp-1.0-py3.11.9-windows-slim.zip"
    with zipfile.ZipFile(result) as zf:
        names = zf.namelist()
    # 顶层目录为 myapp-1.0-py3.11.9-windows-slim/
    assert any(n.startswith("myapp-1.0-py3.11.9-windows-slim/") for n in names)
    # 包含 exe 与 runtime/python311.dll
    assert "myapp-1.0-py3.11.9-windows-slim/myapp.exe" in names
    assert "myapp-1.0-py3.11.9-windows-slim/runtime/python311.dll" in names
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
    assert result.name == "app-1.0-py3.11.9-linux-slim.zip"


def test_make_zip_macos_platform_suffix(tmp_path: Path) -> None:
    """macOS 目标 zip 文件名含 -macos 后缀（修复前误用 linux）."""
    info = _make_info(tmp_path, name="app")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    release = dist / "release"
    release.mkdir()
    result = _make_zip(dist, info, release, Platform.MACOS)
    assert result.name == "app-1.0-py3.11.9-macos-slim.zip"


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
        assert "app-1.0-py3.11.9-windows-slim/app.exe" in zf.namelist()


# ---- build_zip 编排测试 ----


def test_build_zip_no_build_missing_dist(tmp_path: Path) -> None:
    """no_build=True 且 dist 不存在时报错."""
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_zip(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))


def test_build_zip_no_build_missing_exe(tmp_path: Path) -> None:
    """no_build=True 且 dist 中无 exe 报错."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_zip(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))


def test_build_zip_no_build_success(tmp_path: Path) -> None:
    """no_build=True 且 dist 已就绪时生成 zip."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    result = build_zip(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))
    assert result.is_file()
    assert result.name == "app-1.0-py3.11.9-windows-slim.zip"


def test_build_zip_with_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """no_build=False 时调用 build() 构建后生成 zip."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")

    def fake_build(  # noqa: PLR0913
        project_dir: Path,
        mirror: object,
        py_version: str,
        *,
        dist_dir: Path | None = None,
        target: object = None,
        options: object = None,
    ) -> ProjectInfo:
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.exe").write_bytes(b"")
        return ProjectInfo(
            name="app",
            version="1.0",
            src_dir=project_dir,
            entry_module="app",
            entry_file=project_dir / "app.py",
            app_type=AppType.CLI,
            dependencies=(),
            py_version=py_version,
        )

    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)
    result = build_zip(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=False))
    assert result.is_file()
    assert result.name == "app-1.0-py3.11.9-windows-slim.zip"


# ---- 7z 便携包（_find_7z / _make_7z / build_sevenzip）测试 ----


def test_find_7z_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 命中 7z → 返回其路径."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "7z" else None)
    assert _find_7z() == "/usr/bin/7z"


def test_find_7z_windows_default_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 未命中 + Windows → 探测 %ProgramFiles%\\7-Zip\\7z.exe（安装器默认不写 PATH）."""
    program_files = tmp_path / "ProgramFiles"
    exe = program_files / "7-Zip" / "7z.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    assert _find_7z() == str(exe)


def test_find_7z_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 与默认安装目录均未命中 → 返回 None."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("sys.platform", "linux")
    assert _find_7z() is None


def test_make_7z_invokes_system_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_make_7z 调用系统 7z：超高压缩 + 多线程参数齐备，顶层目录为 <base>."""
    info = _make_info(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    release = dist / "release"
    monkeypatch.setattr("fspack.packaging.installer.sevenzip._find_7z", lambda: "7z")

    cmds: list[list[str]] = []
    cwds: list[object] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        cmds.append(cmd)
        cwds.append(kw.get("cwd"))
        archive = Path(cmd[-2])
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"fake 7z")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = _make_7z(dist, info, release, Platform.WINDOWS)

    assert result.name == "app-1.0-py3.11.9-windows-slim.7z"
    assert result.read_bytes() == b"fake 7z"
    cmd = cmds[0]
    assert cmd[0] == "7z"
    # a=add -t7z 格式 -mx=9 超高压缩 -mmt=on 多线程 -y 免交互
    assert cmd[1:6] == ["a", "-t7z", "-mx=9", "-mmt=on", "-y"]
    # 顶层目录为 <base>（相对路径），cwd 为 release 目录（相对路径的基准）
    assert cmd[-1] == "app-1.0-py3.11.9-windows-slim"
    assert cwds[0] == release
    # 打包后 staging 已清理
    assert not (release / "app-1.0-py3.11.9-windows-slim").exists()


def test_make_7z_missing_tool_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """系统未安装 7-Zip → InstallerError 含各平台安装建议."""
    info = _make_info(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.installer.sevenzip._find_7z", lambda: None)
    with pytest.raises(InstallerError, match="7-Zip"):
        _make_7z(dist, info, dist / "release", Platform.WINDOWS)


def test_build_sevenzip_no_build_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_sevenzip 编排：no_build=True 且 dist 就绪 → 产出 .7z."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.installer.sevenzip._find_7z", lambda: "7z")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        archive = Path(cmd[-2])
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"fake 7z")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_sevenzip(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))
    assert result.is_file()
    assert result.name == "app-1.0-py3.11.9-windows-slim.7z"


def test_build_sevenzip_linux_platform_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_sevenzip + Linux 目标 → 文件名用 linux 平台后缀."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.installer.sevenzip._find_7z", lambda: "7z")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        archive = Path(cmd[-2])
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"fake 7z")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_sevenzip(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True), target=Platform.LINUX
    )
    assert result.name == "app-1.0-py3.11.9-linux-slim.7z"


def test_build_release_7z_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fmt=7z → 仅生成 7z，不调用 NSIS 等其他格式."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")
    monkeypatch.setattr("fspack.packaging.installer.sevenzip._find_7z", lambda: "7z")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        archive = Path(cmd[-2])
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"fake 7z")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    outputs = build_release(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True), target=Platform.WINDOWS, fmt="7z"
    )
    assert [p.name for p in outputs] == ["app-1.0-py3.11.9-windows-slim.7z"]


def test_build_tarball_release_no_build_success(tmp_path: Path) -> None:
    """build_tarball_release 直接生成 tar.gz（无外部工具依赖），排除构建中间文件."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")  # Linux 可执行文件无后缀
    # 预创建构建中间文件，验证打包时被排除
    (dist / ".dep_cache.json").write_text("{}")
    (dist / ".nuitka_compile_stamp").write_text("stamp")
    (dist / ".pyc_stamp").write_text("stamp")
    (dist / "src" / "pkg.build").mkdir(parents=True)
    (dist / "src" / "pkg.build" / "artifact.o").write_bytes(b"")
    result = build_tarball_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))
    assert result.is_file()
    assert result.name == "app-1.0-py3.11.9-linux-slim.tar.gz"
    # 验证 tar.gz 中不包含构建中间文件
    import tarfile

    with tarfile.open(result, "r:gz") as tf:
        names = tf.getnames()
    assert not any(".dep_cache.json" in n for n in names), ".dep_cache.json 不应打包"
    assert not any(".nuitka_compile_stamp" in n for n in names), ".nuitka_compile_stamp 不应打包"
    assert not any(".pyc_stamp" in n for n in names), ".pyc_stamp 不应打包"
    assert not any(n.endswith(".build") or "/.build/" in n for n in names), "*.build 目录不应打包"


def test_build_tarball_release_missing_exe(tmp_path: Path) -> None:
    """build_tarball_release 在 dist 中无 Linux 可执行文件时报错."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_tarball_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))


# ---- build_deb_release 编排测试 ----


def test_build_deb_release_no_build_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_deb_release 调用 dpkg-deb 生成 .deb."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        # dpkg-deb --build <staging> <deb_path>，模拟生成 .deb
        deb_path = Path(cmd[-1])
        deb_path.parent.mkdir(parents=True, exist_ok=True)
        deb_path.write_bytes(b"deb-content")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    result = build_deb_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))
    assert result.is_file()
    assert result.name == "app_1.0-py3.11.9-slim_amd64.deb"
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

    def fake_nsis_build_installer(cls: Any, *args: Any, **kw: Any) -> Path:
        calls.append("nsis")
        out = dist / "release" / "app-1.0-py3.11.9-windows-slim-setup.exe"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return out

    monkeypatch.setattr(
        "fspack.packaging.installer.NsisInstaller.build_installer", classmethod(fake_nsis_build_installer)
    )
    outputs = build_release(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True), target=Platform.WINDOWS, fmt="auto"
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
    outputs = build_release(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True), target=Platform.WINDOWS, fmt="zip"
    )
    assert len(outputs) == 1
    assert outputs[0].name == "app-1.0-py3.11.9-windows-slim.zip"


def test_build_release_all_windows_generates_three_formats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fmt=all + Windows → 生成 nsis + zip + 7z 三种格式，复用同一 dist."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")

    build_calls: list[str] = []

    def fake_nsis_build_installer(cls: Any, *args: Any, **kw: Any) -> Path:
        build_calls.append("nsis-build")
        out = dist / "release" / "app-1.0-py3.11.9-windows-slim-setup.exe"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return out

    # 监控 build() 是否被多次调用（all 模式下应仅首次调用 build）
    def fake_build(*args: Any, **kw: Any) -> None:
        build_calls.append("build")

    monkeypatch.setattr(
        "fspack.packaging.installer.NsisInstaller.build_installer", classmethod(fake_nsis_build_installer)
    )
    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)
    monkeypatch.setattr("fspack.packaging.installer.sevenzip._find_7z", lambda: "7z")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        # 7z a ... <archive> <base>：模拟产出 .7z（倒数第二个参数为归档路径）
        archive = Path(cmd[-2])
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"fake 7z")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    outputs = build_release(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True), target=Platform.WINDOWS, fmt="all"
    )
    assert len(outputs) == 3
    assert outputs[0].name == "app-1.0-py3.11.9-windows-slim-setup.exe"
    assert outputs[1].name == "app-1.0-py3.11.9-windows-slim.zip"
    assert outputs[2].name == "app-1.0-py3.11.9-windows-slim.7z"
    # no_build=True 时不应触发 build()
    assert "build" not in build_calls
    # zip 与 7z 共享 staging，全部格式完成后已清理
    assert not (dist / "release" / "app-1.0-py3.11.9-windows-slim").exists()


def test_build_release_all_linux_shares_tar_zip_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fmt=all + Linux → tar.gz/zip/7z 共享同一 staging（copytree 仅 tar/deb 各一次）."""
    import shutil as _shutil

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"#!/bin/sh\nexit 0\n")

    monkeypatch.setattr("fspack.packaging.installer.sevenzip._find_7z", lambda: "7z")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        if cmd[0] == "dpkg-deb":
            # dpkg-deb --build <staging> <deb_path>：模拟产出 .deb
            deb_path = Path(cmd[-1])
            deb_path.parent.mkdir(parents=True, exist_ok=True)
            deb_path.write_bytes(b"fake deb")
        else:
            # 7z a ... <archive> <base>：模拟产出 .7z（倒数第二个参数为归档路径）
            archive = Path(cmd[-2])
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"fake 7z")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.linux.subprocess.run", fake_run)

    # 统计 base 模块内 copytree 调用次数（tar.gz 与 deb 各 1 次，zip/7z 复用 staging 0 次）
    copy_calls: list[str] = []
    real_copytree = _shutil.copytree

    def counting_copytree(src: Path, dst: Path, **kw: Any) -> Path:
        copy_calls.append(str(src))
        return Path(real_copytree(src, dst, **kw))

    # 拆分后 copytree 分布于 dist_prep（tar.gz staging）与 linux（deb staging）两处
    monkeypatch.setattr("fspack.packaging.installer.dist_prep.shutil.copytree", counting_copytree)
    monkeypatch.setattr("fspack.packaging.installer.linux.shutil.copytree", counting_copytree)

    outputs = build_release(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.10", no_build=True), target=Platform.LINUX, fmt="all"
    )

    assert [p.name for p in outputs] == [
        "app-1.0-py3.11.10-linux-slim.tar.gz",
        "app_1.0-py3.11.10-slim_amd64.deb",
        "app-1.0-py3.11.10-linux-slim.zip",
        "app-1.0-py3.11.10-linux-slim.7z",
    ]
    assert len(copy_calls) == 2, f"期望 copytree 仅 2 次（tar/deb 各一次），实际 {len(copy_calls)} 次"
    # 全部格式完成后共享 staging 已清理
    assert not (dist / "release" / "app-1.0-py3.11.10-linux-slim").exists(), "共享 staging 未清理"


def test_build_release_invalid_fmt_raises(tmp_path: Path) -> None:
    """fmt 取值非法时报错."""
    with pytest.raises(InstallerError, match="未知 --format 取值"):
        build_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9"), target=Platform.WINDOWS, fmt="rpm")


def test_build_release_platform_mismatch_raises(tmp_path: Path) -> None:
    """fmt=nsis + Linux 目标报错."""
    with pytest.raises(InstallerError, match="NSIS 安装包仅支持 Windows"):
        build_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9"), target=Platform.LINUX, fmt="nsis")


def test_build_release_auto_macos_dispatches_pkg_dmg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fmt=auto + macOS → 调用 build_pkg_release + build_dmg_release，codesign 透传."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    calls: list[tuple[str, bool]] = []

    def fake_build_pkg_release(*args: Any, **kw: Any) -> Path:
        calls.append(("pkg", kw.get("codesign", False)))
        out = dist / "release" / "app-1.0-py3.11.9-macos-slim.pkg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return out

    def fake_build_dmg_release(*args: Any, **kw: Any) -> Path:
        calls.append(("dmg", kw.get("codesign", False)))
        out = dist / "release" / "app-1.0-py3.11.9-macos-slim.dmg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return out

    monkeypatch.setattr("fspack.packaging.installer.build_pkg_release", fake_build_pkg_release)
    monkeypatch.setattr("fspack.packaging.installer.build_dmg_release", fake_build_dmg_release)

    outputs = build_release(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True),
        target=Platform.MACOS,
        fmt="auto",
        sign=SignOptions(codesign=True),
    )
    assert len(outputs) == 2
    assert calls == [("pkg", True), ("dmg", True)]


def test_build_release_pkg_only_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fmt=pkg + macOS → 仅调用 build_pkg_release."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    calls: list[str] = []

    def fake_build_pkg_release(*args: Any, **kw: Any) -> Path:
        calls.append("pkg")
        out = dist / "release" / "app-1.0-py3.11.9-macos-slim.pkg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")
        return out

    monkeypatch.setattr("fspack.packaging.installer.build_pkg_release", fake_build_pkg_release)

    outputs = build_release(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True), target=Platform.MACOS, fmt="pkg"
    )
    assert calls == ["pkg"]
    assert len(outputs) == 1


def test_build_release_macos_platform_mismatch_raises(tmp_path: Path) -> None:
    """fmt=pkg + Windows 目标报错（pkg 仅 macOS）."""
    with pytest.raises(InstallerError, match=r"pkg 格式仅支持 macOS"):
        build_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9"), target=Platform.WINDOWS, fmt="pkg")
    with pytest.raises(InstallerError, match=r"dmg 格式仅支持 macOS"):
        build_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9"), target=Platform.LINUX, fmt="dmg")


def test_prepare_dist_passes_build_defaults_to_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp p`` 内部调用 ``build()`` 时透传 ``[tool.fspack]`` 构建默认值.

    验证 ``[tool.fspack] nuitka = true`` 等配置在 ``build_release``/``build_installer``
    路径上生效（修复 ``fsp p`` 不应用 nuitka 配置的 bug）。
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "1.0"\n'
        "[tool.fspack]\nnuitka = true\npyc_strip = true\nno_site = true\npyc_optimize = 1\n"
    )
    (tmp_path / "app.py").write_text("def main():\n    pass\n")

    captured_options: list[BuildOptions | None] = []

    def fake_build(  # noqa: PLR0913
        project_dir: Path,
        mirror: object,
        py_version: str,
        *,
        dist_dir: Path | None = None,
        target: object = None,
        options: BuildOptions | None = None,
    ) -> ProjectInfo:
        captured_options.append(options)
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.exe").write_bytes(b"")
        return ProjectInfo(
            name="app",
            version="1.0",
            src_dir=project_dir,
            entry_module="app",
            entry_file=project_dir / "app.py",
            app_type=AppType.CLI,
            dependencies=(),
            py_version=py_version,
        )

    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)
    # mock makensis 编译，避免 Linux CI 无 NSIS 导致测试失败
    out_setup = tmp_path / "dist" / "release" / "app-1.0-py3.11.9-windows-slim-setup.exe"

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)

    # build_release → _prepare_dist → build()，options 应反映 [tool.fspack] 配置
    build_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9"), target=Platform.WINDOWS, fmt="nsis")
    assert len(captured_options) == 1
    opts = captured_options[0]
    assert opts is not None
    assert opts.nuitka is True
    assert opts.pyc_strip is True
    assert opts.no_site is True
    assert opts.pyc_optimize == 1
    # 未在配置中声明的字段保留 BuildOptions 默认值
    assert opts.no_stdlib_trim is False
    assert opts.no_pyc is False


def test_prepare_dist_no_config_uses_default_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 ``[tool.fspack]`` 配置时 ``fsp p`` 使用 :class:`BuildOptions` 默认值."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")

    captured_options: list[BuildOptions | None] = []

    def fake_build(  # noqa: PLR0913
        project_dir: Path,
        mirror: object,
        py_version: str,
        *,
        dist_dir: Path | None = None,
        target: object = None,
        options: BuildOptions | None = None,
    ) -> ProjectInfo:
        captured_options.append(options)
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.exe").write_bytes(b"")
        return ProjectInfo(
            name="app",
            version="1.0",
            src_dir=project_dir,
            entry_module="app",
            entry_file=project_dir / "app.py",
            app_type=AppType.CLI,
            dependencies=(),
            py_version=py_version,
        )

    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)
    # mock makensis 编译，避免 Linux CI 无 NSIS 导致测试失败
    out_setup = tmp_path / "dist" / "release" / "app-1.0-py3.11.9-windows-slim-setup.exe"

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    build_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9"), target=Platform.WINDOWS, fmt="nsis")
    assert len(captured_options) == 1
    opts = captured_options[0]
    assert opts is not None
    # 无配置时全部使用默认值（nuitka=False 等）
    assert opts.nuitka is False
    assert opts.pyc_strip is False
    assert opts.pyc_optimize == 2


def test_build_options_from_defaults_translation() -> None:
    """``build_options_from_defaults`` 将 ``BuildDefaults`` 转为 ``BuildOptions``."""
    from fspack.config import BuildDefaults, BuildOptions, build_options_from_defaults

    # 全 None：使用 BuildOptions 默认值
    opts = build_options_from_defaults(BuildDefaults())
    assert opts == BuildOptions()

    # 部分指定：非 None 字段覆盖默认值
    opts = build_options_from_defaults(BuildDefaults(nuitka=True, pyc_optimize=1, no_site=True))
    assert opts.nuitka is True
    assert opts.pyc_optimize == 1
    assert opts.no_site is True
    # 未指定的字段保留默认值
    assert opts.pyc_strip is False
    assert opts.no_stdlib_trim is False


def test_prepare_dist_skips_build_when_dist_and_exe_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``fsp p`` 默认（``no_build=False``）且 dist+exe 已就绪时跳过 build，避免重复构建.

    验证 ``fsp b`` 后 ``fsp p`` 不再重新跑 build（尤其 Nuitka 启用场景耗时较长）。
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"")  # 模拟 fsp b 已产出的可执行文件

    build_calls = 0

    def fake_build(*args: object, **kwargs: object) -> ProjectInfo:
        nonlocal build_calls
        build_calls += 1
        raise AssertionError("dist+exe 已就绪时不应调用 build()")

    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)
    # mock makensis 编译，避免 Linux CI 无 NSIS 导致测试失败
    out_setup = dist / "release" / "app-1.0-py3.11.9-windows-slim-setup.exe"

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    # 走 build_installer 路径（NsisInstaller），no_build=False
    build_installer(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=False))
    assert build_calls == 0


def test_prepare_dist_rebuilds_when_dist_exists_but_exe_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dist 存在但可执行文件缺失时（默认 ``no_build=False``）自动重建修复.

    避免用户手动 ``fsp c`` 清理，dist 部分损坏时 ``fsp p`` 自动重建。
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    # dist 存在但 app.exe 缺失（部分损坏）

    build_calls = 0

    def fake_build(  # noqa: PLR0913
        project_dir: Path,
        mirror: object,
        py_version: str,
        *,
        dist_dir: Path | None = None,
        target: object = None,
        options: BuildOptions | None = None,
    ) -> ProjectInfo:
        nonlocal build_calls
        build_calls += 1
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.exe").write_bytes(b"")
        return ProjectInfo(
            name="app",
            version="1.0",
            src_dir=project_dir,
            entry_module="app",
            entry_file=project_dir / "app.py",
            app_type=AppType.CLI,
            dependencies=(),
            py_version=py_version,
        )

    monkeypatch.setattr("fspack.packaging.installer.build", fake_build)
    # mock makensis 编译，避免 Linux CI 无 NSIS 导致测试失败
    out_setup = dist / "release" / "app-1.0-py3.11.9-windows-slim-setup.exe"

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        out_setup.parent.mkdir(parents=True, exist_ok=True)
        out_setup.write_bytes(b"")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    build_installer(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=False))
    assert build_calls == 1


def test_prepare_dist_no_build_true_still_errors_on_missing_dist(tmp_path: Path) -> None:
    """``--no-build`` 显式声明且 dist 不存在时仍报错（保持原语义）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_installer(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.9", no_build=True))


def test_exe_exists_and_exe_path_helpers(tmp_path: Path) -> None:
    """``_exe_exists``/``_exe_path`` 按 target 返回正确可执行文件名."""
    from fspack.packaging.installer import _exe_exists, _exe_path

    info = _make_info(tmp_path, name="myapp")
    assert _exe_path(info, Platform.WINDOWS) == "myapp.exe"
    assert _exe_path(info, Platform.LINUX) == "myapp"

    # dist 目录初始无可执行文件
    assert _exe_exists(tmp_path, info, Platform.WINDOWS) is False
    assert _exe_exists(tmp_path, info, Platform.LINUX) is False

    # 创建 .exe 后 Windows 命中
    (tmp_path / "myapp.exe").write_bytes(b"")
    assert _exe_exists(tmp_path, info, Platform.WINDOWS) is True
    assert _exe_exists(tmp_path, info, Platform.LINUX) is False


def test_exe_path_multi_entry_uses_default_entry(tmp_path: Path) -> None:
    """多入口项目 _exe_path/exe_filename 取默认入口名（与构建侧 exe 命名一致）.

    构建侧（compile_stage._loader_exe_path）按入口名命名 exe：项目名 app、
    入口 cli/gui 时产物为 cli.exe/gui.exe 而非 app.exe；校验侧须同样取
    默认入口名（GUI 优先），否则 fsp p 报"未找到已构建的可执行文件"。
    """
    from fspack.packaging.installer import _check_exe, _exe_path
    from fspack.packaging.installer.linux import LinuxInstaller
    from fspack.packaging.installer.macos import MacInstaller
    from fspack.packaging.installer.nsis import NsisInstaller

    info = _make_multi_entry_info(tmp_path)
    # default_entry GUI 优先：gui
    assert info.default_entry.name == "gui"
    assert _exe_path(info, Platform.WINDOWS) == "gui.exe"
    assert _exe_path(info, Platform.LINUX) == "gui"
    assert NsisInstaller.exe_filename(info) == "gui.exe"
    assert LinuxInstaller.exe_filename(info) == "gui"
    assert MacInstaller.exe_filename(info) == "gui"

    # dist 内只有入口 exe（cli.exe/gui.exe）时校验通过
    (tmp_path / "gui.exe").write_bytes(b"")
    _check_exe(tmp_path, info, Platform.WINDOWS)
    # 项目名 exe（app.exe）不存在，不能因它误判就绪
    assert not (tmp_path / "app.exe").exists()


# ---- sign_exe_file 测试 ----


def test_sign_exe_file_calls_signtool_with_correct_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sign_exe_file 调用 ``signtool sign /f <pfx> /t <timestamp> <exe>``，命令含 exe 路径."""
    exe_path = tmp_path / "app.exe"
    exe_path.write_bytes(b"fake exe")
    cert_path = tmp_path / "cert.pfx"

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.nsis.subprocess.run", fake_run)

    sign_exe_file(exe_path, cert_path, None)

    cmd = captured["cmd"]
    assert cmd[:3] == ["signtool", "sign", "/f"]
    assert str(cert_path) in cmd
    assert str(exe_path) in cmd
    # 时间戳参数默认 DigiCert
    assert "/t" in cmd
    timestamp_idx = cmd.index("/t")
    assert cmd[timestamp_idx + 1] == "http://timestamp.digicert.com"
    # 未指定 password 时不含 /p
    assert "/p" not in cmd


def test_sign_exe_file_with_password_includes_p_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """传入 password 时命令含 ``/p <password>``."""
    exe_path = tmp_path / "app.exe"
    exe_path.write_bytes(b"exe")
    cert_path = tmp_path / "cert.pfx"

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.nsis.subprocess.run", fake_run)

    sign_exe_file(exe_path, cert_path, "secret-pwd")

    cmd = captured["cmd"]
    assert "/p" in cmd
    password_idx = cmd.index("/p")
    assert cmd[password_idx + 1] == "secret-pwd"


def test_sign_exe_file_signtool_missing_raises_installer_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """signtool 不可用（FileNotFoundError）时抛 InstallerError."""
    exe_path = tmp_path / "app.exe"
    exe_path.write_bytes(b"exe")
    cert_path = tmp_path / "cert.pfx"

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.installer.nsis.subprocess.run", fake_run)

    with pytest.raises(InstallerError, match="未找到 signtool"):
        sign_exe_file(exe_path, cert_path, None)


def test_sign_exe_file_signtool_failure_raises_installer_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """signtool 签名失败（CalledProcessError）时抛 InstallerError."""
    exe_path = tmp_path / "app.exe"
    exe_path.write_bytes(b"exe")
    cert_path = tmp_path / "cert.pfx"
    err = subprocess.CalledProcessError(1, "signtool", stderr="bad cert")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.installer.nsis.subprocess.run", fake_run)

    with pytest.raises(InstallerError, match="signtool 签名失败"):
        sign_exe_file(exe_path, cert_path, None)


# ---- sign_exe_files 测试 ----


def _make_multi_entry_info(tmp_path: Path) -> ProjectInfo:
    """构造多入口 ProjectInfo（2 个入口：cli 与 gui）."""
    return ProjectInfo(
        name="app",
        version="1.0",
        src_dir=tmp_path,
        entry_module="app",
        entry_file=tmp_path / "app.py",
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
        entries=(
            EntryPoint(name="cli", module="cli", file=tmp_path / "cli.py", app_type=AppType.CLI),
            EntryPoint(name="gui", module="gui", file=tmp_path / "gui.py", app_type=AppType.GUI),
        ),
    )


def test_sign_exe_files_signs_all_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sign_exe_files 签名 dist 下所有入口 exe（多入口项目 2 个 exe）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "cli.exe").write_bytes(b"cli exe")
    (dist / "gui.exe").write_bytes(b"gui exe")
    cert_path = tmp_path / "cert.pfx"
    info = _make_multi_entry_info(tmp_path)

    signed_exes: list[Path] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        # 命令最后一个是 exe 路径
        signed_exes.append(Path(cmd[-1]))
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.nsis.subprocess.run", fake_run)

    signed_count = sign_exe_files(dist, info, cert_path, None, tracker=BuildTracker())

    assert signed_count == 2
    signed_names = {p.name for p in signed_exes}
    assert signed_names == {"cli.exe", "gui.exe"}


def test_sign_exe_files_skips_missing_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """dist 中 exe 不存在时跳过该入口（warning 不报错）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "cli.exe").write_bytes(b"cli exe")
    # gui.exe 不创建
    cert_path = tmp_path / "cert.pfx"
    info = _make_multi_entry_info(tmp_path)

    monkeypatch.setattr("fspack.packaging.installer.nsis.subprocess.run", lambda cmd, **kw: CompletedStub())

    with caplog.at_level("WARNING", logger="fspack.packaging.installer"):
        signed_count = sign_exe_files(dist, info, cert_path, None, tracker=BuildTracker())

    assert signed_count == 1
    assert any("签名跳过" in r.message for r in caplog.records)


def test_sign_exe_files_sign_failure_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """单个 exe 签名失败不阻断（warning 后继续签名下一个）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "cli.exe").write_bytes(b"cli exe")
    (dist / "gui.exe").write_bytes(b"gui exe")
    cert_path = tmp_path / "cert.pfx"
    info = _make_multi_entry_info(tmp_path)

    call_count = {"n": 0}

    def fake_run(cmd: list[str], **kw: Any) -> object:
        call_count["n"] += 1
        # 第一个 exe 签名失败
        if call_count["n"] == 1:
            raise subprocess.CalledProcessError(1, "signtool", stderr="fail")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.nsis.subprocess.run", fake_run)

    with caplog.at_level("WARNING", logger="fspack.packaging.installer"):
        signed_count = sign_exe_files(dist, info, cert_path, None, tracker=BuildTracker())

    # 第一个失败，第二个成功
    assert signed_count == 1
    assert call_count["n"] == 2, "应继续签名第二个 exe"
    assert any("签名 cli.exe 失败" in r.message for r in caplog.records)


def test_sign_exe_files_single_entry_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单入口项目（无 entries）签名单个 exe（用 ProjectInfo.all_entries 回退构造）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app.exe").write_bytes(b"app exe")
    cert_path = tmp_path / "cert.pfx"
    info = ProjectInfo(
        name="app",
        version="1.0",
        src_dir=tmp_path,
        entry_module="app",
        entry_file=tmp_path / "app.py",
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.9",
    )

    signed_exes: list[Path] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        signed_exes.append(Path(cmd[-1]))
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.nsis.subprocess.run", fake_run)

    signed_count = sign_exe_files(dist, info, cert_path, "pwd123", tracker=BuildTracker())

    assert signed_count == 1
    assert signed_exes[0].name == "app.exe"


# ---- NSIS 工具链管理（nsis_tool：缓存识别/下载/解压）测试 ----


def _make_nsis_zip(archive: Path, top_dir: str) -> None:
    """构造含 ``<top_dir>/Bin/makensis.exe`` 的真实 zip 归档（顶层目录 + 启动器）."""
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{top_dir}/Bin/makensis.exe", b"fake makensis")
        zf.writestr(f"{top_dir}/makensis.exe", b"launcher")


def _make_cached_makensis(cache_root: Path, dir_name: str) -> Path:
    """在 NSIS 缓存下创建已解压的 makensis.exe，返回其路径."""
    exe = cache_root / "nsis" / dir_name / "Bin" / "makensis.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    return exe


@pytest.fixture()
def _nsis_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """NSIS 工具链测试缓存根：重定向缓存、屏蔽 PATH makensis、锁定 win32 分支."""
    monkeypatch.setenv("FSPACK_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("FSPACK_OFFLINE", raising=False)
    monkeypatch.setattr(nsis_tool.shutil, "which", lambda name: None)
    monkeypatch.setattr(nsis_tool.sys, "platform", "win32")
    return tmp_path


def test_ensure_nsis_cache_hit(_nsis_cache: Path) -> None:
    """缓存命中：Bin/makensis.exe 已存在 → 返回其绝对路径."""
    exe = _make_cached_makensis(_nsis_cache, "nsis-3.11")
    assert nsis_tool.ensure_nsis() == str(exe)


def test_find_cached_makensis_portable_dir(_nsis_cache: Path) -> None:
    """portable 变体目录（nsis-3.11-portable）同样命中缓存."""
    exe = _make_cached_makensis(_nsis_cache, "nsis-3.11-portable")
    assert nsis_tool.find_cached_makensis() == exe


def test_ensure_nsis_local_zip_extract(_nsis_cache: Path) -> None:
    """本地官方 zip：解压后返回 makensis 路径，用户归档保留不删."""
    nsis_dir = _nsis_cache / "nsis"
    nsis_dir.mkdir()
    archive = nsis_dir / "nsis-3.11.zip"
    _make_nsis_zip(archive, "nsis-3.11")

    result = nsis_tool.ensure_nsis()

    assert Path(result) == nsis_dir / "nsis-3.11" / "Bin" / "makensis.exe"
    assert archive.is_file(), "用户归档解压后须保留"


def test_ensure_nsis_local_portable_7z_extract(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地 portable .7z：经系统 7-Zip 解压后命中（patch _extract_7z 模拟）."""
    nsis_dir = _nsis_cache / "nsis"
    nsis_dir.mkdir()
    archive = nsis_dir / "nsis-3.11-portable.7z"
    archive.write_bytes(b"fake 7z")

    def fake_extract_7z(arch: Path, dest: Path) -> None:
        assert arch == archive
        _make_cached_makensis(_nsis_cache, "nsis-3.11-portable")

    monkeypatch.setattr(nsis_tool, "_extract_7z", fake_extract_7z)
    result = nsis_tool.ensure_nsis()
    assert Path(result) == nsis_dir / "nsis-3.11-portable" / "Bin" / "makensis.exe"
    assert archive.is_file(), "用户归档解压后须保留"


def test_ensure_nsis_7z_without_sevenzip_falls_back_to_path(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地 .7z 但未装 7-Zip：跳过归档回退 PATH makensis（不 raise）."""
    nsis_dir = _nsis_cache / "nsis"
    nsis_dir.mkdir()
    (nsis_dir / "nsis-3.11.7z").write_bytes(b"fake 7z")
    monkeypatch.setattr(nsis_tool, "_find_7z", lambda: None)
    monkeypatch.setattr(nsis_tool.shutil, "which", lambda name: "C:/Tools/makensis.exe" if name == "makensis" else None)

    assert nsis_tool.ensure_nsis() == "makensis"


def test_ensure_nsis_corrupt_archive_falls_back_to_download(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地归档损坏（非 zip 内容）：删除后回退在线下载."""
    nsis_dir = _nsis_cache / "nsis"
    nsis_dir.mkdir()
    archive = nsis_dir / "nsis-3.11.zip"
    archive.write_bytes(b"not a zip")

    def fake_download() -> Path:
        return _make_cached_makensis(_nsis_cache, "nsis-3.11")

    monkeypatch.setattr(nsis_tool, "_download_and_extract_nsis", fake_download)
    result = nsis_tool.ensure_nsis()

    assert result == str(_nsis_cache / "nsis" / "nsis-3.11" / "Bin" / "makensis.exe")
    assert not archive.exists(), "损坏归档须删除"


def test_ensure_nsis_uses_path_makensis(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存/归档均无但 PATH 已装 makensis：直接用系统安装，不触发下载."""
    monkeypatch.setattr(nsis_tool.shutil, "which", lambda name: "C:/Tools/makensis.exe" if name == "makensis" else None)
    monkeypatch.setattr(
        nsis_tool, "_download_and_extract_nsis", lambda: (_ for _ in ()).throw(AssertionError("不应下载"))
    )
    assert nsis_tool.ensure_nsis() == "makensis"


def test_ensure_nsis_offline_raises(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线且无任何来源：fail-fast，提示归档放置路径与 PATH 安装."""
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    with pytest.raises(InstallerError, match="离线模式下 NSIS 未就绪"):
        nsis_tool.ensure_nsis()


def test_ensure_nsis_offline_7z_without_sevenzip_hint(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线 + 本地 .7z 未装 7-Zip：错误信息补充 7-Zip 安装建议."""
    nsis_dir = _nsis_cache / "nsis"
    nsis_dir.mkdir()
    (nsis_dir / "nsis-3.11-portable.7z").write_bytes(b"fake 7z")
    monkeypatch.setattr(nsis_tool, "_find_7z", lambda: None)
    monkeypatch.setenv("FSPACK_OFFLINE", "1")
    with pytest.raises(InstallerError, match="7-Zip"):
        nsis_tool.ensure_nsis()


def test_ensure_nsis_downloads_when_missing(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在线且缓存/归档/PATH 均无：下载填充缓存并返回 makensis 路径."""
    calls: list[int] = []

    def fake_download() -> Path:
        calls.append(1)
        return _make_cached_makensis(_nsis_cache, "nsis-3.11")

    monkeypatch.setattr(nsis_tool, "_download_and_extract_nsis", fake_download)
    assert nsis_tool.ensure_nsis() == str(_nsis_cache / "nsis" / "nsis-3.11" / "Bin" / "makensis.exe")
    assert calls == [1]


def test_ensure_nsis_non_windows_returns_path_command(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 平台（Linux 交叉打包）：不做缓存管理，直接返回 "makensis"."""
    monkeypatch.setattr(nsis_tool.sys, "platform", "linux")
    assert nsis_tool.ensure_nsis() == "makensis"


def test_find_local_nsis_archive_prefers_zip_and_ignores_mismatch(_nsis_cache: Path) -> None:
    """本地归档识别：.zip 优先于 .7z；版本不匹配（如 3.10）不识别."""
    nsis_dir = _nsis_cache / "nsis"
    nsis_dir.mkdir()
    (nsis_dir / "nsis-3.11.7z").write_bytes(b"a")
    (nsis_dir / "nsis-3.10.zip").write_bytes(b"b")

    assert nsis_tool._find_local_nsis_archive() == nsis_dir / "nsis-3.11.7z"

    (nsis_dir / "nsis-3.11-portable.zip").write_bytes(b"c")
    assert nsis_tool._find_local_nsis_archive() == nsis_dir / "nsis-3.11-portable.zip"


def test_extract_7z_missing_sevenzip_raises(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_extract_7z 未装 7-Zip：InstallerError 含安装建议."""
    monkeypatch.setattr(nsis_tool, "_find_7z", lambda: None)
    with pytest.raises(InstallerError, match="7-Zip"):
        nsis_tool._extract_7z(_nsis_cache / "x.7z", _nsis_cache)


def test_extract_7z_nonzero_exit_raises(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_extract_7z 退出码非零（归档损坏）：InstallerError 含退出码与尾部输出."""

    class _FailResult:
        returncode = 2
        stdout = ""
        stderr = "archive corrupt"

    def fake_run(cmd: list[str], **kw: Any) -> object:
        return _FailResult()

    monkeypatch.setattr(nsis_tool, "_find_7z", lambda: "7z")
    monkeypatch.setattr(nsis_tool.subprocess, "run", fake_run)
    with pytest.raises(InstallerError, match="退出码 2"):
        nsis_tool._extract_7z(_nsis_cache / "x.7z", _nsis_cache)


def test_download_and_extract_nsis_falls_back_to_second_source(
    _nsis_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载源逐一回退：首源失败（OSError）后第二源成功，归档解压后清理."""
    urls_seen: list[str] = []

    class FakeDownloader:
        def __init__(self, *, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            urls_seen.append(url)
            if len(urls_seen) == 1:
                raise OSError("boom")
            _make_nsis_zip(dest, "nsis-3.11")
            return 0

    monkeypatch.setattr("fspack.packaging.net.Downloader", FakeDownloader)
    result = nsis_tool._download_and_extract_nsis()

    assert Path(result) == _nsis_cache / "nsis" / "nsis-3.11" / "Bin" / "makensis.exe"
    assert len(urls_seen) == 2, "首源失败后应回退第二源"
    assert urls_seen[0] != urls_seen[1]
    assert not (_nsis_cache / "nsis" / "nsis-3.11.zip").exists(), "下载归档解压后须清理"


def test_download_and_extract_nsis_all_sources_fail(_nsis_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全部下载源失败：InstallerError 报已尝试源数，归档半成品清理."""

    class FakeDownloader:
        def __init__(self, *, timeout: int = 0) -> None:
            pass

        def download(self, url: str, dest: Path, *, stage: object = None, label: str = "") -> int:
            dest.write_bytes(b"partial")
            raise OSError("boom")

    monkeypatch.setattr("fspack.packaging.net.Downloader", FakeDownloader)
    with pytest.raises(InstallerError, match="已尝试 2 个源"):
        nsis_tool._download_and_extract_nsis()
    assert not (_nsis_cache / "nsis" / "nsis-3.11.zip").exists(), "失败路径同样清理归档"
