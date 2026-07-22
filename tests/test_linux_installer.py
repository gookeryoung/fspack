"""linux_installer tar.gz + .deb 生成测试."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType, ProjectInfo
from fspack.exceptions import InstallerError
from fspack.linux_installer import build_deb, build_linux_installer, build_tarball
from fspack.mirror import get_mirror


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


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
    (release / "stale.deb").write_bytes(b"stale")
    return dist


def test_build_tarball_creates_archive(tmp_path: Path) -> None:
    """build_tarball 打包 dist 为 tar.gz，排除 release/ 目录."""
    dist = _make_dist(tmp_path)
    release = dist / "release"

    out = build_tarball(dist, "app", "1.0", release)
    assert out.is_file()
    assert out.name == "app-1.0-linux.tar.gz"
    assert out.stat().st_size > 0

    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert "app-1.0-linux" in names
    assert "app-1.0-linux/app" in names
    assert "app-1.0-linux/src/app.py" in names
    assert not any("release" in n for n in names), "release/ 未被排除"

    assert not (release / "app-1.0-linux").exists(), "staging 未清理"


def test_build_tarball_cleans_existing_staging(tmp_path: Path) -> None:
    """build_tarball 重复打包时清理旧 staging."""
    dist = _make_dist(tmp_path)
    release = dist / "release"
    stale_staging = release / "app-1.0-linux"
    stale_staging.mkdir(parents=True)
    (stale_staging / "stale.txt").write_text("stale")

    out = build_tarball(dist, "app", "1.0", release)
    assert out.is_file()

    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert "app-1.0-linux/stale.txt" not in names, "旧 staging 未清理"


def test_build_deb_creates_deb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_deb 构造 .deb，校验 control/wrapper/exe 内容与 dpkg-deb 调用，清理旧 staging."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"
    stale_staging = release / "app_1.0_amd64"
    stale_staging.mkdir(parents=True)
    (stale_staging / "stale.txt").write_text("stale")

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> _Completed:
        captured["cmd"] = cmd
        staging = Path(cmd[-2])
        deb_path = Path(cmd[-1])

        assert not (staging / "stale.txt").exists(), "旧 staging 未清理"

        control = (staging / "DEBIAN" / "control").read_text(encoding="utf-8")
        assert "Package: app" in control
        assert "Version: 1.0" in control
        assert "Architecture: amd64" in control
        assert "Maintainer: fspack" in control

        wrapper = staging / "usr" / "bin" / "app"
        assert wrapper.is_file()
        # Windows 的 chmod 不设置 Unix 可执行位，仅 posix 平台校验
        if os.name == "posix":
            assert wrapper.stat().st_mode & 0o111, "wrapper 无可执行位"
        wrapper_content = wrapper.read_text(encoding="utf-8")
        assert "/usr/lib/app/app" in wrapper_content
        assert '"$@"' in wrapper_content

        assert (staging / "usr" / "lib" / "app" / "app").is_file(), "exe 未复制到 pkg_dir"
        assert not (staging / "usr" / "lib" / "app" / "release").exists(), "release/ 未被排除"

        deb_path.parent.mkdir(parents=True, exist_ok=True)
        deb_path.write_bytes(b"fake deb")
        return _Completed()

    monkeypatch.setattr("fspack.linux_installer.subprocess.run", fake_run)

    out = build_deb(dist, info, release)
    assert out == release / "app_1.0_amd64.deb"
    assert out.is_file()
    assert captured["cmd"][0] == "dpkg-deb"
    assert captured["cmd"][1] == "--build"
    assert not (release / "app_1.0_amd64").exists(), "staging 未清理"


def test_build_deb_dpkg_deb_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dpkg-deb 缺失抛 InstallerError."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.linux_installer.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="未找到 dpkg-deb"):
        build_deb(dist, info, release)


def test_build_deb_dpkg_deb_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dpkg-deb 失败抛 InstallerError."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"

    err = subprocess.CalledProcessError(1, "dpkg-deb", stderr="bad control")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.linux_installer.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="dpkg-deb 构建失败"):
        build_deb(dist, info, release)


def test_build_linux_installer_no_build_missing_dist(tmp_path: Path) -> None:
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_linux_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)


def test_build_linux_installer_no_build_missing_exe(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_linux_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)


def test_build_linux_installer_no_build_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    captured: dict[str, object] = {}

    def fake_build_tarball(dist_dir: Path, name: str, version: str, release_dir: Path) -> Path:
        captured["tarball"] = (name, version)
        return release_dir / f"{name}-{version}-linux.tar.gz"

    def fake_build_deb(dist_dir: Path, info: object, release_dir: Path) -> Path:
        captured["deb"] = info
        return release_dir / "app_1.0_amd64.deb"

    monkeypatch.setattr("fspack.linux_installer.build_tarball", fake_build_tarball)
    monkeypatch.setattr("fspack.linux_installer.build_deb", fake_build_deb)

    result = build_linux_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True)
    assert result == dist / "release" / "app_1.0_amd64.deb"
    assert captured["tarball"] == ("app", "1.0")


def test_build_linux_installer_with_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"

    def fake_build(  # noqa: PLR0913
        project_dir: Path,
        mirror: object,
        py_version: str,
        dist_dir: Path | None = None,
        embed_cache: Path | None = None,
        target: object = None,
    ) -> object:
        d = dist_dir or project_dir / "dist"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app").write_bytes(b"")
        return None

    monkeypatch.setattr("fspack.linux_installer.build", fake_build)
    monkeypatch.setattr("fspack.linux_installer.build_tarball", lambda *a, **kw: tmp_path / "x.tar.gz")
    monkeypatch.setattr("fspack.linux_installer.build_deb", lambda *a, **kw: dist / "release" / "app_1.0_amd64.deb")

    result = build_linux_installer(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=False)
    assert result == dist / "release" / "app_1.0_amd64.deb"
    assert (dist / "app").is_file()
