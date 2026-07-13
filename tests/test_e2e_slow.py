"""端到端慢测试：真实下载 embed python + mingw 编译 + wine 运行。

需 mingw-w64 与 wine（Windows 目标）或 gcc（Linux 目标），标 slow，默认门禁不执行。
覆盖 5 类典型项目：无库 CLI、有库 CLI、有库 GUI、有库 pygame、有库 web。
另含 Linux 平台端到端测试（python-build-standalone + gcc 编译 + 原生运行）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).parent / "examples"


def _build_and_run(
    proj_name: str,
    expect_substr: str,
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 240,
) -> None:
    """构建示例并在 wine 下运行，断言输出含预期字符串。

    proj_name: tests/examples 下的示例目录名。
    expect_substr: 运行输出中应包含的子串。
    extra_env: wine 运行时额外环境变量（如 GUI/pygame 的 offscreen 驱动）。
    """
    from fspack.builder import build
    from fspack.loader import mingw_available
    from fspack.mirror import get_mirror
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / proj_name
    shutil.copytree(_EXAMPLES / proj_name, proj)

    build(proj, get_mirror("aliyun"), "3.11.9", target=Platform.WINDOWS)
    exe = proj / "dist" / f"{proj_name}.exe"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python311.dll").is_file(), "未找到 python311.dll"
    assert (proj / "dist" / "runtime" / "python311._pth").is_file(), "未生成 _pth"

    env = {**os.environ, "WINEDEBUG": "-all"}
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(["wine", str(exe)], capture_output=True, text=True, timeout=timeout, env=env, check=False)
    combined = result.stdout + result.stderr
    assert expect_substr in combined, f"未在输出中发现 {expect_substr!r}: {combined!r}"


@pytest.mark.slow
def test_build_and_run_helloworld(tmp_path: Path) -> None:
    """helloworld 示例真实构建并在 wine 下运行。."""
    _build_and_run("helloworld", "hello, world", tmp_path)


@pytest.mark.slow
def test_build_and_run_clitool(tmp_path: Path) -> None:
    """clitool 示例：有库 CLI（requests），验证依赖打包与运行。."""
    _build_and_run("clitool", "requests ", tmp_path)
    # 验证 requests 包确实解包到 site-packages
    proj = tmp_path / "clitool"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "requests").is_dir()


@pytest.mark.slow
def test_build_and_run_guicalc(tmp_path: Path) -> None:
    """guicalc 示例：有库 GUI（PySide6），验证构建与打包。

    PySide6 的 Qt6Core 依赖 icuuc.dll（Windows 10+ 系统 DLL），wine 默认不提供。
    缺 ICU 时仅验证构建（下载/解包/_pth/exe），跳过运行断言。
    """
    from fspack.builder import build
    from fspack.loader import mingw_available
    from fspack.mirror import get_mirror
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "guicalc"
    shutil.copytree(_EXAMPLES / "guicalc", proj)
    build(proj, get_mirror("aliyun"), "3.11.9", target=Platform.WINDOWS)

    exe = proj / "dist" / "guicalc.exe"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python311.dll").is_file(), "未找到 python311.dll"
    assert (proj / "dist" / "runtime" / "python311._pth").is_file(), "未生成 _pth"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "PySide6").is_dir(), "PySide6 未解包"

    env = {**os.environ, "WINEDEBUG": "-all", "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run(["wine", str(exe)], capture_output=True, text=True, timeout=300, env=env, check=False)
    combined = result.stdout + result.stderr
    if "hello from PySide6" not in combined and "DLL load failed" in combined:
        pytest.skip(f"wine 缺少系统 DLL（如 icuuc.dll），PySide6 Qt DLL 无法加载，真实 Windows 可运行: {combined!r}")
    assert "hello from PySide6" in combined, f"未在输出中发现 'hello from PySide6': {combined!r}"


@pytest.mark.slow
def test_build_and_run_pygamedemo(tmp_path: Path) -> None:
    """pygamedemo 示例：有库 pygame，dummy 驱动验证。."""
    _build_and_run(
        "pygamedemo",
        "pygame ",
        tmp_path,
        extra_env={"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"},
    )
    proj = tmp_path / "pygamedemo"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "pygame").is_dir()


@pytest.mark.slow
def test_build_and_run_webapp(tmp_path: Path) -> None:
    """webapp 示例：有库 web（flask），test_client 验证路由。."""
    _build_and_run("webapp", "hello from flask", tmp_path)
    proj = tmp_path / "webapp"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "flask").is_dir()


@pytest.mark.slow
def test_build_and_run_linux_helloworld(tmp_path: Path) -> None:
    """Linux 平台端到端：gcc 编译 + python-build-standalone 运行 helloworld。

    python-build-standalone 的 20241016 release 只提供 3.11.10（非 3.11.9），
    故 Linux 目标使用 3.11.10。
    """
    from fspack.builder import build
    from fspack.loader import gcc_available
    from fspack.mirror import get_mirror
    from fspack.platform import Platform

    if not gcc_available():
        pytest.skip("gcc 未安装")

    proj = tmp_path / "helloworld"
    shutil.copytree(_EXAMPLES / "helloworld", proj)
    build(proj, get_mirror("aliyun"), "3.11.10", target=Platform.LINUX)

    exe = proj / "dist" / "helloworld"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python" / "lib" / "libpython3.11.so").is_file(), "未找到 libpython3.11.so"

    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)
    combined = result.stdout + result.stderr
    assert "hello, world" in combined, f"未在输出中发现 'hello, world': {combined!r}"


@pytest.mark.slow
def test_build_and_run_linux_clitool(tmp_path: Path) -> None:
    """Linux 平台端到端：有库 CLI（requests），验证依赖打包与运行。."""
    from fspack.builder import build
    from fspack.loader import gcc_available
    from fspack.mirror import get_mirror
    from fspack.platform import Platform

    if not gcc_available():
        pytest.skip("gcc 未安装")

    proj = tmp_path / "clitool"
    shutil.copytree(_EXAMPLES / "clitool", proj)
    build(proj, get_mirror("aliyun"), "3.11.10", target=Platform.LINUX)

    exe = proj / "dist" / "clitool"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python" / "lib" / "python3.11" / "site-packages" / "requests").is_dir()

    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)
    combined = result.stdout + result.stderr
    assert "requests " in combined, f"未在输出中发现 'requests ': {combined!r}"


@pytest.mark.slow
def test_build_installer_helloworld_slow(tmp_path: Path) -> None:
    """NSIS 端到端：build helloworld → makensis 编译 → 验证安装包产出。

    需 mingw-w64（Windows loader 编译）与 makensis（NSIS 安装包编译）。
    验证 dist/installer.nsi 生成正确、dist/release/helloworld-setup.exe 产出为合法 PE 文件且非空。
    """
    from fspack.installer import build_installer
    from fspack.loader import mingw_available
    from fspack.mirror import get_mirror

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("makensis"):
        pytest.skip("makensis 未安装（sudo apt install -y nsis）")

    proj = tmp_path / "helloworld"
    shutil.copytree(_EXAMPLES / "helloworld", proj)

    out = build_installer(proj, get_mirror("aliyun"), "3.11.9", no_build=False)
    expected = proj / "dist" / "release" / "helloworld-setup.exe"
    assert out == expected
    assert expected.is_file(), f"未生成安装包: {expected}"
    assert expected.stat().st_size > 1024 * 1024, f"安装包过小: {expected.stat().st_size} bytes"

    with expected.open("rb") as f:
        assert f.read(2) == b"MZ", "安装包非合法 PE 文件"

    nsi = proj / "dist" / "installer.nsi"
    assert nsi.is_file(), "未生成 installer.nsi"
    content = nsi.read_text(encoding="utf-8")
    assert 'Name "helloworld 0.1.0"' in content
    assert 'OutFile "release\\helloworld-setup.exe"' in content


@pytest.mark.slow
def test_build_linux_installer_helloworld_slow(tmp_path: Path) -> None:
    """Linux 安装包端到端：build helloworld → tar.gz + .deb 真实产出。

    需 gcc（Linux loader 编译）与 dpkg-deb（.deb 构建）。
    验证 dist/release/helloworld_0.1.0_amd64.deb 为合法 ar 归档，
    dist/release/helloworld-0.1.0-linux.tar.gz 为合法 gzip。
    """
    from fspack.linux_installer import build_linux_installer
    from fspack.loader import gcc_available
    from fspack.mirror import get_mirror

    if not gcc_available():
        pytest.skip("gcc 未安装")
    if not shutil.which("dpkg-deb"):
        pytest.skip("dpkg-deb 未安装")

    proj = tmp_path / "helloworld"
    shutil.copytree(_EXAMPLES / "helloworld", proj)

    out = build_linux_installer(proj, get_mirror("aliyun"), "3.11.10", no_build=False)
    expected_deb = proj / "dist" / "release" / "helloworld_0.1.0_amd64.deb"
    assert out == expected_deb
    assert expected_deb.is_file(), f"未生成 .deb: {expected_deb}"
    assert expected_deb.stat().st_size > 1024 * 1024, f".deb 过小: {expected_deb.stat().st_size} bytes"

    tarball = proj / "dist" / "release" / "helloworld-0.1.0-linux.tar.gz"
    assert tarball.is_file(), f"未生成 tar.gz: {tarball}"
    assert tarball.stat().st_size > 1024 * 1024, f"tar.gz 过小: {tarball.stat().st_size} bytes"

    with expected_deb.open("rb") as f:
        magic = f.read(8)
    assert magic == b"!<arch>\n", f".deb 非 ar 归档: {magic!r}"

    with tarball.open("rb") as f:
        assert f.read(2) == b"\x1f\x8b", "tar.gz 非 gzip 格式"
