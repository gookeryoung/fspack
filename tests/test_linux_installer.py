"""linux_installer tar.gz + .deb 生成测试."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from fspack.config import AppType, BuildOptions, ProjectInfo, get_mirror
from fspack.exceptions import InstallerError
from fspack.packaging.installer import ReleaseRequest, SignOptions, build_deb, build_linux_installer, build_tarball
from fspack.packaging.installer.linux import build_deb_release, sign_deb_file
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
    (release / "stale.deb").write_bytes(b"stale")
    return dist


def test_build_tarball_creates_archive(tmp_path: Path) -> None:
    """build_tarball 打包 dist 为 tar.gz，排除 release/ 目录."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = dist / "release"

    out = build_tarball(dist, info, release)
    assert out.is_file()
    assert out.name == "app-1.0-py3.11.10-linux-slim.tar.gz"
    assert out.stat().st_size > 0

    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert "app-1.0-py3.11.10-linux-slim" in names
    assert "app-1.0-py3.11.10-linux-slim/app" in names
    assert "app-1.0-py3.11.10-linux-slim/src/app.py" in names
    assert not any("release" in n for n in names), "release/ 未被排除"

    assert not (release / "app-1.0-py3.11.10-linux-slim").exists(), "staging 未清理"


def test_build_tarball_cleans_existing_staging(tmp_path: Path) -> None:
    """build_tarball 重复打包时清理旧 staging."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = dist / "release"
    stale_staging = release / "app-1.0-py3.11.10-linux-slim"
    stale_staging.mkdir(parents=True)
    (stale_staging / "stale.txt").write_text("stale")

    out = build_tarball(dist, info, release)
    assert out.is_file()

    with tarfile.open(out) as tf:
        names = tf.getnames()
    assert "app-1.0-py3.11.10-linux-slim/stale.txt" not in names, "旧 staging 未清理"


def test_build_deb_creates_deb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_deb 构造 .deb，校验 control/wrapper/exe 内容与 dpkg-deb 调用，清理旧 staging."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"
    stale_staging = release / "app_1.0-py3.11.10-slim_amd64"
    stale_staging.mkdir(parents=True)
    (stale_staging / "stale.txt").write_text("stale")

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
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
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)

    out = build_deb(dist, info, release)
    assert out == release / "app_1.0-py3.11.10-slim_amd64.deb"
    assert out.is_file()
    assert captured["cmd"][0] == "dpkg-deb"
    assert captured["cmd"][1] == "--build"
    assert not (release / "app_1.0-py3.11.10-slim_amd64").exists(), "staging 未清理"


def test_build_deb_dpkg_deb_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dpkg-deb 缺失抛 InstallerError."""
    dist = _make_dist(tmp_path)
    info = _make_info(tmp_path)
    release = tmp_path / "release"

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
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

    monkeypatch.setattr("fspack.packaging.installer.subprocess.run", fake_run)
    with pytest.raises(InstallerError, match="dpkg-deb 构建失败"):
        build_deb(dist, info, release)


def test_build_linux_installer_no_build_missing_dist(tmp_path: Path) -> None:
    with pytest.raises(InstallerError, match="未找到 dist"):
        build_linux_installer(ReleaseRequest(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True))


def test_build_linux_installer_no_build_missing_exe(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    (tmp_path / "dist").mkdir()
    with pytest.raises(InstallerError, match="未找到已构建"):
        build_linux_installer(ReleaseRequest(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True))


def test_build_linux_installer_no_build_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    captured: dict[str, object] = {}

    def fake_build_tarball(dist_dir: Path, info: object, release_dir: Path) -> Path:
        captured["tarball"] = info
        return release_dir / "app-1.0-py3.11.10-linux-slim.tar.gz"

    def fake_build_deb(dist_dir: Path, info: object, release_dir: Path) -> Path:
        captured["deb"] = info
        return release_dir / "app_1.0-py3.11.10-slim_amd64.deb"

    monkeypatch.setattr("fspack.packaging.installer.linux.build_tarball", fake_build_tarball)
    monkeypatch.setattr("fspack.packaging.installer.linux.build_deb", fake_build_deb)

    result = build_linux_installer(ReleaseRequest(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=True))
    assert result == dist / "release" / "app_1.0-py3.11.10-slim_amd64.deb"
    assert captured["tarball"] is not None


def test_build_linux_installer_with_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr("fspack.packaging.installer.linux.build_tarball", lambda *a, **kw: tmp_path / "x.tar.gz")
    monkeypatch.setattr(
        "fspack.packaging.installer.linux.build_deb",
        lambda *a, **kw: dist / "release" / "app_1.0-py3.11.10-slim_amd64.deb",
    )

    result = build_linux_installer(ReleaseRequest(tmp_path, get_mirror("aliyun"), "3.11.10", no_build=False))
    assert result == dist / "release" / "app_1.0-py3.11.10-slim_amd64.deb"
    assert (dist / "app").is_file()


# ---- sign_deb_file 测试 ----


def test_sign_deb_file_calls_gpg_with_correct_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sign_deb_file 调用 ``gpg --detach-sign --armor <deb>``，命令含 .deb 路径."""
    deb_path = tmp_path / "app_1.0_amd64.deb"
    deb_path.write_bytes(b"fake deb")

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        # gpg 签名成功后会产出 <deb>.asc 文件
        deb_path.with_suffix(".deb.asc").write_text("-----BEGIN PGP SIGNATURE-----\n", encoding="utf-8")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.linux.subprocess.run", fake_run)

    asc_path = sign_deb_file(deb_path)

    cmd = captured["cmd"]
    assert cmd[:3] == ["gpg", "--detach-sign", "--armor"]
    assert str(deb_path) in cmd
    # 未指定 key_id 时不含 --local-user
    assert "--local-user" not in cmd
    # 返回 .asc 路径
    assert asc_path.suffix == ".asc"
    assert asc_path.is_file()


def test_sign_deb_file_with_key_id_includes_local_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """传入 key_id 时命令含 ``--local-user <key_id>``."""
    deb_path = tmp_path / "app.deb"
    deb_path.write_bytes(b"deb")

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        captured["cmd"] = cmd
        Path(str(deb_path) + ".asc").write_text("signature", encoding="utf-8")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.linux.subprocess.run", fake_run)

    asc_path = sign_deb_file(deb_path, key_id="0x12345678")

    cmd = captured["cmd"]
    assert "--local-user" in cmd
    local_user_idx = cmd.index("--local-user")
    assert cmd[local_user_idx + 1] == "0x12345678"
    assert asc_path.is_file()


def test_sign_deb_file_gpg_missing_raises_installer_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gpg 不可用（FileNotFoundError）时抛 InstallerError."""
    deb_path = tmp_path / "app.deb"
    deb_path.write_bytes(b"deb")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.installer.linux.subprocess.run", fake_run)

    with pytest.raises(InstallerError, match="未找到 gpg"):
        sign_deb_file(deb_path)


def test_sign_deb_file_gpg_failure_raises_installer_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gpg 签名失败（CalledProcessError）时抛 InstallerError."""
    deb_path = tmp_path / "app.deb"
    deb_path.write_bytes(b"deb")
    err = subprocess.CalledProcessError(2, "gpg", stderr="secret key not found")

    def fake_run(cmd: list[str], **kw: Any) -> object:
        raise err

    monkeypatch.setattr("fspack.packaging.installer.linux.subprocess.run", fake_run)

    with pytest.raises(InstallerError, match="gpg 签名失败"):
        sign_deb_file(deb_path)


# ---- build_deb_release 透传 sign_deb 参数测试 ----


def test_build_deb_release_passes_sign_deb_to_sign_deb_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_deb_release(sign_deb=True, sign_deb_key=...) 透传到 sign_deb_file."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    deb_path = dist / "release" / "app_1.0-py3.11.10-slim_amd64.deb"

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        # dpkg-deb --build <staging> <deb_path>
        if cmd[0] == "dpkg-deb":
            deb_path.parent.mkdir(parents=True, exist_ok=True)
            deb_path.write_bytes(b"deb-content")
        else:
            # gpg 签名
            captured["cmd"] = cmd
            Path(str(deb_path) + ".asc").write_text("sig", encoding="utf-8")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.linux.subprocess.run", fake_run)

    result = build_deb_release(
        ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.10", no_build=True),
        sign=SignOptions(sign_deb=True, sign_deb_key="0xABCD1234"),
    )

    assert result == deb_path
    # 验证 sign_deb_file 被调用（cmd 是 gpg 命令）
    assert "cmd" in captured
    assert captured["cmd"][:3] == ["gpg", "--detach-sign", "--armor"]
    assert "--local-user" in captured["cmd"]
    local_user_idx = captured["cmd"].index("--local-user")
    assert captured["cmd"][local_user_idx + 1] == "0xABCD1234"
    # .asc 签名文件已生成
    assert (dist / "release" / "app_1.0-py3.11.10-slim_amd64.deb.asc").is_file()


def test_build_deb_release_sign_deb_failure_does_not_block_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """签名失败降级为 warning 不阻断构建（仅 .deb 仍生成）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        if cmd[0] == "dpkg-deb":
            deb_path = Path(cmd[-1])
            deb_path.parent.mkdir(parents=True, exist_ok=True)
            deb_path.write_bytes(b"deb-content")
            return CompletedStub()
        # gpg 失败
        raise FileNotFoundError()

    monkeypatch.setattr("fspack.packaging.installer.linux.subprocess.run", fake_run)

    with caplog.at_level("WARNING", logger="fspack.packaging.installer"):
        result = build_deb_release(
            ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.10", no_build=True), sign=SignOptions(sign_deb=True)
        )

    # .deb 仍生成
    assert result.is_file()
    assert result.name == "app_1.0-py3.11.10-slim_amd64.deb"
    # 签名失败仅 warning
    assert any("签名 .deb 失败" in r.message for r in caplog.records)


def test_build_deb_release_without_sign_deb_does_not_call_gpg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未启用 sign_deb 时不调用 gpg（仅 dpkg-deb 一次）."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "1.0"\n')
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "app").write_bytes(b"")
    deb_path = dist / "release" / "app_1.0-py3.11.10-slim_amd64.deb"

    gpg_called: list[bool] = []

    def fake_run(cmd: list[str], **kw: Any) -> CompletedStub:
        if cmd[0] == "gpg":
            gpg_called.append(True)
            Path(str(deb_path) + ".asc").write_text("sig", encoding="utf-8")
        else:
            deb_path.parent.mkdir(parents=True, exist_ok=True)
            deb_path.write_bytes(b"deb-content")
        return CompletedStub()

    monkeypatch.setattr("fspack.packaging.installer.linux.subprocess.run", fake_run)

    build_deb_release(ReleaseRequest(tmp_path, get_mirror("huawei"), "3.11.10", no_build=True))

    assert not gpg_called, "未启用 sign_deb 不应调用 gpg"
