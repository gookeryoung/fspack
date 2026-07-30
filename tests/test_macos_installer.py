"""macOS installer .pkg + .dmg 生成测试."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType, BuildOptions, ProjectInfo, get_mirror
from fspack.exceptions import InstallerError
from fspack.packaging.installer import (
    MacInstaller,
    build_dmg,
    build_dmg_release,
    build_mac_installer,
    build_pkg,
    build_pkg_release,
)
from fspack.packaging.installer.macos import _bundle_identifier
from fspack.platform import Platform
from tests._stubs import CompletedStub


def _make_info(tmp_path: Path, name: str = "app") -> ProjectInfo:
    return ProjectInfo(
        name=name,
        version="1.0",
        src_dir=tmp_path,
        entry_module=name,
        entry_file=tmp_path / f"{name}.py",
        app_type=AppType.CLI,
        dependencies=(),
        py_version="3.11.10",
    )


def _make_dist(tmp_path: Path, name: str = "app") -> Path:
    """构造最小 dist 目录（含 exe + src/<name>.py + release/ 残留）."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / name).write_bytes(b"#!/bin/sh\nexit 0\n")
    (dist / name).chmod(0o755)
    src = dist / "src"
    src.mkdir()
    (src / f"{name}.py").write_text("def main():\n    pass\n")
    release = dist / "release"
    release.mkdir()
    (release / "stale.pkg").write_bytes(b"stale")
    return dist


# ---- _bundle_identifier ----


def test_bundle_identifier_format() -> None:
    """_bundle_identifier 返回 com.fspack.<name> 反向域名格式."""
    info = _make_info(Path(), name="myapp")
    assert _bundle_identifier(info) == "com.fspack.myapp"


# ---- build_pkg ----


def test_build_pkg_creates_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_pkg 构造 .pkg，校验 pkgbuild 命令与 staging 内容，清理旧 staging."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"
    stale_staging = release / "app-1.0-py3.11.10-macos-slim.pkg-staging"
    stale_staging.mkdir(parents=True)
    (stale_staging / "stale.txt").write_text("stale")

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        # 校验 staging 内容
        staging_root = Path(cmd[cmd.index("--root") + 1])
        assert (staging_root / "app" / "app").is_file(), "exe 未复制到 staging"
        assert not (staging_root / "app" / "release").exists(), "release/ 未被排除"
        assert not (staging_root / "stale.txt").exists(), "旧 staging 未清理"

        # 校验 identifier / version / install-location
        assert cmd[cmd.index("--identifier") + 1] == "com.fspack.app"
        assert cmd[cmd.index("--version") + 1] == "1.0"
        assert cmd[cmd.index("--install-location") + 1] == "/Applications"

        # 模拟 pkgbuild 生成 .pkg
        pkg_path = Path(cmd[-1])
        pkg_path.parent.mkdir(parents=True, exist_ok=True)
        pkg_path.write_bytes(b"fake pkg")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.macos.subprocess.run", fake_run)

    out = build_pkg(dist, info, release)
    assert out == release / "app-1.0-py3.11.10-macos-slim.pkg"
    assert out.is_file()
    assert captured["cmd"][0] == "pkgbuild"
    assert not (release / "app-1.0-py3.11.10-macos-slim.pkg-staging").exists(), "staging 未清理"


def test_build_pkg_pkgbuild_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pkgbuild 缺失抛 InstallerError."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.installer.macos.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="未找到 pkgbuild"):
        build_pkg(dist, info, release)


def test_build_pkg_pkgbuild_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pkgbuild 失败抛 InstallerError."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"

    err = subprocess.CalledProcessError(1, "pkgbuild", stderr="bad root")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.installer.macos.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="pkgbuild 执行失败"):
        build_pkg(dist, info, release)


def test_build_pkg_with_codesign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """codesign=True 时 build_pkg 调 codesign 签名 .pkg."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd)
        if cmd[0] == "pkgbuild":
            pkg_path = Path(cmd[-1])
            pkg_path.parent.mkdir(parents=True, exist_ok=True)
            pkg_path.write_bytes(b"fake pkg")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.macos.subprocess.run", fake_run)

    build_pkg(dist, info, release, codesign=True)
    assert len(calls) == 2
    assert calls[0][0] == "pkgbuild"
    assert calls[1][0] == "codesign"
    assert "--force" in calls[1]
    assert "--sign" in calls[1]
    assert "-" in calls[1]  # ad-hoc 签名


# ---- build_dmg ----


def test_build_dmg_creates_dmg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_dmg 构造 .dmg，校验 hdiutil 命令与 staging 内容，清理旧 staging."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"
    stale_staging = release / "app-1.0-py3.11.10-macos-slim.dmg-staging"
    stale_staging.mkdir(parents=True)
    (stale_staging / "stale.txt").write_text("stale")

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        # 校验 staging 内容
        srcfolder = Path(cmd[cmd.index("-srcfolder") + 1])
        assert (srcfolder / "app" / "app").is_file(), "exe 未复制到 staging"
        assert not (srcfolder / "app" / "release").exists(), "release/ 未被排除"
        assert not (srcfolder / "stale.txt").exists(), "旧 staging 未清理"

        # 校验 volname / format
        assert cmd[cmd.index("-volname") + 1] == "app"
        assert "UDZO" in cmd

        # 模拟 hdiutil 生成 .dmg
        dmg_path = Path(cmd[-1])
        dmg_path.parent.mkdir(parents=True, exist_ok=True)
        dmg_path.write_bytes(b"fake dmg")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.macos.subprocess.run", fake_run)

    out = build_dmg(dist, info, release)
    assert out == release / "app-1.0-py3.11.10-macos-slim.dmg"
    assert out.is_file()
    assert captured["cmd"][0] == "hdiutil"
    assert captured["cmd"][1] == "create"
    assert not (release / "app-1.0-py3.11.10-macos-slim.dmg-staging").exists(), "staging 未清理"


def test_build_dmg_hdiutil_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hdiutil 缺失抛 InstallerError."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.installer.macos.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="未找到 hdiutil"):
        build_dmg(dist, info, release)


def test_build_dmg_with_codesign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """codesign=True 时 build_dmg 调 codesign 签名 .dmg."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        calls.append(cmd)
        if cmd[0] == "hdiutil":
            dmg_path = Path(cmd[-1])
            dmg_path.parent.mkdir(parents=True, exist_ok=True)
            dmg_path.write_bytes(b"fake dmg")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.macos.subprocess.run", fake_run)

    build_dmg(dist, info, release, codesign=True)
    assert len(calls) == 2
    assert calls[0][0] == "hdiutil"
    assert calls[1][0] == "codesign"


# ---- MacInstaller 类 ----


def test_mac_installer_target_platform() -> None:
    """MacInstaller 目标平台为 MACOS。"""
    assert MacInstaller.target_platform() is Platform.MACOS


def test_mac_installer_exe_filename() -> None:
    """MacInstaller exe_filename 返回 <name>（无后缀）。"""
    info = _make_info(Path(), name="myapp")
    assert MacInstaller.exe_filename(info) == "myapp"


def test_build_mac_installer_no_build_missing_dist(tmp_path: Path) -> None:
    """build_mac_installer no_build=True 时 dist 缺失抛 InstallerError."""
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_mac_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)


def test_build_mac_installer_no_build_missing_exe(tmp_path: Path) -> None:
    """build_mac_installer no_build=True 时 exe 缺失抛 InstallerError."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_mac_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)


def test_build_mac_installer_no_build_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_mac_installer no_build=True 成功编排 pkg + dmg，返回 .dmg 路径."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    captured: dict[str, object] = {}

    def fake_build_pkg(dist_dir: Path, info: object, release_dir: Path, *, codesign: bool = False) -> Path:
        captured["pkg"] = info
        return release_dir / "app-1.0-py3.11.10-macos-slim.pkg"

    def fake_build_dmg(dist_dir: Path, info: object, release_dir: Path, *, codesign: bool = False) -> Path:
        captured["dmg"] = info
        return release_dir / "app-1.0-py3.11.10-macos-slim.dmg"

    monkeypatch.setattr("fspack.packaging.installer.macos.build_pkg", fake_build_pkg)
    monkeypatch.setattr("fspack.packaging.installer.macos.build_dmg", fake_build_dmg)

    result = build_mac_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)
    assert result == dist / "release" / "app-1.0-py3.11.10-macos-slim.dmg"
    assert captured["pkg"] is not None
    assert captured["dmg"] is not None


def test_build_mac_installer_with_codesign_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_mac_installer codesign=True 透传到 build_pkg/build_dmg."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    codesign_calls: list[bool] = []

    def fake_build_pkg(dist_dir: Path, info: object, release_dir: Path, *, codesign: bool = False) -> Path:
        codesign_calls.append(codesign)
        return release_dir / "app-1.0-py3.11.10-macos-slim.pkg"

    def fake_build_dmg(dist_dir: Path, info: object, release_dir: Path, *, codesign: bool = False) -> Path:
        codesign_calls.append(codesign)
        return release_dir / "app-1.0-py3.11.10-macos-slim.dmg"

    monkeypatch.setattr("fspack.packaging.installer.macos.build_pkg", fake_build_pkg)
    monkeypatch.setattr("fspack.packaging.installer.macos.build_dmg", fake_build_dmg)

    build_mac_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True, codesign=True)
    assert codesign_calls == [True, True]


# ---- build_pkg_release / build_dmg_release ----


def test_build_pkg_release_no_build_missing_dist(tmp_path: Path) -> None:
    """build_pkg_release no_build=True 时 dist 缺失抛 InstallerError."""
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_pkg_release(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)


def test_build_dmg_release_no_build_missing_exe(tmp_path: Path) -> None:
    """build_dmg_release no_build=True 时 exe 缺失抛 InstallerError."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_dmg_release(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)


def test_build_pkg_release_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_pkg_release 成功生成 .pkg."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    release = dist / "release"

    def fake_build_pkg(dist_dir: Path, info: object, rel: Path, *, codesign: bool = False) -> Path:
        return rel / "app-1.0-py3.11.10-macos-slim.pkg"

    monkeypatch.setattr("fspack.packaging.installer.macos.build_pkg", fake_build_pkg)

    result = build_pkg_release(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)
    assert result == release / "app-1.0-py3.11.10-macos-slim.pkg"


def test_build_dmg_release_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_dmg_release 成功生成 .dmg."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    release = dist / "release"

    def fake_build_dmg(dist_dir: Path, info: object, rel: Path, *, codesign: bool = False) -> Path:
        return rel / "app-1.0-py3.11.10-macos-slim.dmg"

    monkeypatch.setattr("fspack.packaging.installer.macos.build_dmg", fake_build_dmg)

    result = build_dmg_release(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)
    assert result == release / "app-1.0-py3.11.10-macos-slim.dmg"


def test_build_mac_installer_with_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_mac_installer no_build=False 时调用 build() 构建后打包."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"

    def fake_build(  # noqa: PLR0913
        project_dir: Path,
        mirror: object,
        py_version: str,
        *,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
        options: BuildOptions | None = None,
    ) -> ProjectInfo:
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app").write_bytes(b"")
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
    monkeypatch.setattr("fspack.packaging.installer.macos.build_pkg", lambda *a, **kw: tmp_path / "x.pkg")
    monkeypatch.setattr(
        "fspack.packaging.installer.macos.build_dmg",
        lambda *a, **kw: dist / "release" / "app-1.0-py3.11.10-macos-slim.dmg",
    )

    result = build_mac_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=False)
    assert result == dist / "release" / "app-1.0-py3.11.10-macos-slim.dmg"
    assert (dist / "app").is_file()
