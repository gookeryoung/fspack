"""端到端慢测试：真实下载 embed python + mingw 编译 + wine 运行。

需 mingw-w64 与 wine（Windows 目标）或 gcc（Linux 目标），标 slow，默认门禁不执行。
覆盖 9 类典型项目：无库 CLI、有库 CLI、有库 GUI（PySide6/PySide2/PyQt5）、有库 pygame、
有库 web、多入口混合（cli+gui+web 共享 runtime/依赖）。
另含 Linux 平台端到端测试（python-build-standalone + gcc 编译 + 原生运行）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).parent.parent / "examples"


def _build_and_run(
    proj_name: str,
    expect_substr: str,
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 240,
) -> None:
    """构建示例并在 wine 下运行，断言输出含预期字符串。

    proj_name: examples/ 下的示例目录名。
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
    """cli_helloworld 示例真实构建并在 wine 下运行。."""
    _build_and_run("cli_helloworld", "hello, world", tmp_path)


@pytest.mark.slow
def test_build_and_run_clitool(tmp_path: Path) -> None:
    """cli_tool 示例：有库 CLI（requests），验证依赖打包与运行。."""
    _build_and_run("cli_tool", "requests ", tmp_path)
    # 验证 requests 包确实解包到 site-packages
    proj = tmp_path / "cli_tool"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "requests").is_dir()


@pytest.mark.slow
def test_build_and_run_guicalc(tmp_path: Path) -> None:
    """gui_calc 示例：有库 GUI（PySide6），验证构建与打包。

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

    proj = tmp_path / "gui_calc"
    shutil.copytree(_EXAMPLES / "gui_calc", proj)
    build(
        proj, get_mirror("aliyun"), "3.11.9", target=Platform.WINDOWS, keep_modules={"PySide6.QtCore", "PySide6.QtGui"}
    )

    exe = proj / "dist" / "gui_calc.exe"
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
def test_build_and_run_pygame_cli(tmp_path: Path) -> None:
    """pygame_cli 示例：有库 pygame，dummy 驱动验证。."""
    _build_and_run(
        "pygame_cli",
        "pygame ",
        tmp_path,
        extra_env={"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"},
    )
    proj = tmp_path / "pygame_cli"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "pygame").is_dir()


@pytest.mark.slow
def test_build_and_run_webapp(tmp_path: Path) -> None:
    """web_app 示例：有库 web（flask），test_client 验证路由。."""
    _build_and_run("web_app", "hello from flask", tmp_path)
    proj = tmp_path / "web_app"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "flask").is_dir()


@pytest.mark.slow
def test_build_and_run_pyside2app(tmp_path: Path) -> None:
    """pyside2_app 示例：版本自动解析 + PySide2，验证 requires-python 约束。

    .python-version=3.9 + requires-python=">=3.8,<3.11" 应解析到 3.9.13。
    PySide2 的 Qt DLL 在 wine 上可能缺系统 DLL，缺时跳过运行断言。
    """
    from fspack.builder import build
    from fspack.loader import mingw_available
    from fspack.mirror import get_mirror
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "pyside2_app"
    shutil.copytree(_EXAMPLES / "pyside2_app", proj)
    build(proj, get_mirror("aliyun"), None, target=Platform.WINDOWS, keep_modules={"PySide2.QtGui"})

    exe = proj / "dist" / "pyside2_app.exe"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python39.dll").is_file(), "未找到 python39.dll（版本自动解析应为 3.9.13）"
    assert (proj / "dist" / "runtime" / "python39._pth").is_file(), "未生成 _pth"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "PySide2").is_dir(), "PySide2 未解包"

    env = {**os.environ, "WINEDEBUG": "-all", "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run(["wine", str(exe)], capture_output=True, text=True, timeout=300, env=env, check=False)
    combined = result.stdout + result.stderr
    if "hello from PySide2" not in combined and "DLL load failed" in combined:
        pytest.skip(f"wine 缺少系统 DLL，PySide2 Qt DLL 无法加载，真实 Windows 可运行: {combined!r}")
    assert "hello from PySide2" in combined, f"未在输出中发现 'hello from PySide2': {combined!r}"


@pytest.mark.slow
def test_build_and_run_pyqt5_cli(tmp_path: Path) -> None:
    """pyqt5_cli 示例：Python 3.12 + PyQt5，验证新版本 + PyQt5 兼容。

    PyQt5 的 Qt DLL 在 wine 上可能缺系统 DLL，缺时跳过运行断言。
    """
    from fspack.builder import build
    from fspack.loader import mingw_available
    from fspack.mirror import get_mirror
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "pyqt5_cli"
    shutil.copytree(_EXAMPLES / "pyqt5_cli", proj)
    build(proj, get_mirror("aliyun"), "3.12.0", target=Platform.WINDOWS, keep_modules={"PyQt5.QtCore", "PyQt5.QtGui"})

    exe = proj / "dist" / "pyqt5_cli.exe"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python312.dll").is_file(), "未找到 python312.dll"
    assert (proj / "dist" / "runtime" / "python312._pth").is_file(), "未生成 _pth"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "PyQt5").is_dir(), "PyQt5 未解包"

    env = {**os.environ, "WINEDEBUG": "-all", "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run(["wine", str(exe)], capture_output=True, text=True, timeout=300, env=env, check=False)
    combined = result.stdout + result.stderr
    if "hello from PyQt5" not in combined and "DLL load failed" in combined:
        pytest.skip(f"wine 缺少系统 DLL，PyQt5 Qt DLL 无法加载，真实 Windows 可运行: {combined!r}")
    assert "hello from PyQt5" in combined, f"未在输出中发现 'hello from PyQt5': {combined!r}"


@pytest.mark.slow
def test_build_and_run_snake(tmp_path: Path) -> None:
    """pygame_snake 示例：pygame 贪吃蛇，dummy 驱动验证。."""
    _build_and_run(
        "pygame_snake",
        "snake ready",
        tmp_path,
        extra_env={"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"},
    )
    proj = tmp_path / "pygame_snake"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "pygame").is_dir()


@pytest.mark.slow
def test_build_and_run_multi_entry(tmp_path: Path) -> None:
    """multi_entry 示例：多入口项目（cli+gui+web）共享 runtime/依赖。

    验证 [tool.fspack.entries] 解析、三入口 exe 生成、各自运行输出正确。
    .python-version=3.10 + requires-python=">=3.8,<3.11" 应解析到 3.10.11。
    GUI 入口（PySide2）在 wine 上可能缺系统 DLL，缺时 skip GUI 运行断言。
    """
    from fspack.builder import build
    from fspack.loader import mingw_available
    from fspack.mirror import get_mirror
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "multi_entry"
    shutil.copytree(_EXAMPLES / "multi_entry", proj)
    build(proj, get_mirror("aliyun"), None, target=Platform.WINDOWS, keep_modules={"PySide2.QtGui"})

    # 三个入口 exe 均应生成
    for ep_name in ("cli", "gui", "web"):
        exe = proj / "dist" / f"{ep_name}.exe"
        assert exe.is_file(), f"未生成入口 {ep_name} 的 exe: {exe}"

    # runtime 共享：python310.dll（.python-version=3.10 解析到 3.10.11）
    assert (proj / "dist" / "runtime" / "python310.dll").is_file(), "未找到 python310.dll"
    assert (proj / "dist" / "runtime" / "python310._pth").is_file(), "未生成 _pth"
    # 依赖共享：PySide2 与 flask 均解包到 site-packages
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "PySide2").is_dir(), "PySide2 未解包"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "flask").is_dir(), "flask 未解包"

    env = {**os.environ, "WINEDEBUG": "-all", "QT_QPA_PLATFORM": "offscreen"}

    # CLI 入口：wine 运行断言输出
    cli_exe = proj / "dist" / "cli.exe"
    result = subprocess.run(["wine", str(cli_exe)], capture_output=True, text=True, timeout=120, env=env, check=False)
    combined = result.stdout + result.stderr
    assert "hello from multi_entry cli" in combined, f"cli 入口输出异常: {combined!r}"

    # Web 入口：wine 运行断言输出（test_client 不启动服务器，可安全运行）
    web_exe = proj / "dist" / "web.exe"
    result = subprocess.run(["wine", str(web_exe)], capture_output=True, text=True, timeout=120, env=env, check=False)
    combined = result.stdout + result.stderr
    assert "hello from multi_entry web" in combined, f"web 入口输出异常: {combined!r}"

    # GUI 入口：PySide2 在 wine 上可能缺系统 DLL，缺时 skip
    gui_exe = proj / "dist" / "gui.exe"
    result = subprocess.run(["wine", str(gui_exe)], capture_output=True, text=True, timeout=300, env=env, check=False)
    combined = result.stdout + result.stderr
    if "hello from multi_entry gui" not in combined and "DLL load failed" in combined:
        pytest.skip(f"wine 缺少系统 DLL，PySide2 Qt DLL 无法加载，真实 Windows 可运行: {combined!r}")
    assert "hello from multi_entry gui" in combined, f"gui 入口输出异常: {combined!r}"


@pytest.mark.slow
def test_build_and_run_linux_helloworld(tmp_path: Path) -> None:
    """Linux 平台端到端：gcc 编译 + python-build-standalone 运行 cli_helloworld。

    python-build-standalone 的 20241016 release 只提供 3.11.10（非 3.11.9），
    故 Linux 目标使用 3.11.10。
    """
    from fspack.builder import build
    from fspack.loader import gcc_available
    from fspack.mirror import get_mirror
    from fspack.platform import Platform

    if not gcc_available():
        pytest.skip("gcc 未安装")

    proj = tmp_path / "cli_helloworld"
    shutil.copytree(_EXAMPLES / "cli_helloworld", proj)
    build(proj, get_mirror("aliyun"), "3.11.10", target=Platform.LINUX)

    exe = proj / "dist" / "cli_helloworld"
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

    proj = tmp_path / "cli_tool"
    shutil.copytree(_EXAMPLES / "cli_tool", proj)
    build(proj, get_mirror("aliyun"), "3.11.10", target=Platform.LINUX)

    exe = proj / "dist" / "cli_tool"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python" / "lib" / "python3.11" / "site-packages" / "requests").is_dir()

    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)
    combined = result.stdout + result.stderr
    assert "requests " in combined, f"未在输出中发现 'requests ': {combined!r}"


@pytest.mark.slow
def test_build_installer_helloworld_slow(tmp_path: Path) -> None:
    """NSIS 端到端：build cli_helloworld → makensis 编译 → 验证安装包产出。

    需 mingw-w64（Windows loader 编译）与 makensis（NSIS 安装包编译）。
    验证 dist/installer.nsi 生成正确、dist/release/cli_helloworld-setup.exe 产出为合法 PE 文件且非空。
    """
    from fspack.installer import build_installer
    from fspack.loader import mingw_available
    from fspack.mirror import get_mirror

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("makensis"):
        pytest.skip("makensis 未安装（sudo apt install -y nsis）")

    proj = tmp_path / "cli_helloworld"
    shutil.copytree(_EXAMPLES / "cli_helloworld", proj)

    out = build_installer(proj, get_mirror("aliyun"), "3.11.9", no_build=False)
    expected = proj / "dist" / "release" / "cli_helloworld-setup.exe"
    assert out == expected
    assert expected.is_file(), f"未生成安装包: {expected}"
    assert expected.stat().st_size > 1024 * 1024, f"安装包过小: {expected.stat().st_size} bytes"

    with expected.open("rb") as f:
        assert f.read(2) == b"MZ", "安装包非合法 PE 文件"

    nsi = proj / "dist" / "installer.nsi"
    assert nsi.is_file(), "未生成 installer.nsi"
    content = nsi.read_text(encoding="utf-8")
    assert 'Name "cli_helloworld 0.1.0"' in content
    assert 'OutFile "release\\cli_helloworld-setup.exe"' in content


@pytest.mark.slow
def test_build_linux_installer_helloworld_slow(tmp_path: Path) -> None:
    """Linux 安装包端到端：build cli_helloworld → tar.gz + .deb 真实产出。

    需 gcc（Linux loader 编译）与 dpkg-deb（.deb 构建）。
    验证 dist/release/cli_helloworld_0.1.0_amd64.deb 为合法 ar 归档，
    dist/release/cli_helloworld-0.1.0-linux.tar.gz 为合法 gzip。
    """
    from fspack.linux_installer import build_linux_installer
    from fspack.loader import gcc_available
    from fspack.mirror import get_mirror

    if not gcc_available():
        pytest.skip("gcc 未安装")
    if not shutil.which("dpkg-deb"):
        pytest.skip("dpkg-deb 未安装")

    proj = tmp_path / "cli_helloworld"
    shutil.copytree(_EXAMPLES / "cli_helloworld", proj)

    out = build_linux_installer(proj, get_mirror("aliyun"), "3.11.10", no_build=False)
    expected_deb = proj / "dist" / "release" / "cli_helloworld_0.1.0_amd64.deb"
    assert out == expected_deb
    assert expected_deb.is_file(), f"未生成 .deb: {expected_deb}"
    assert expected_deb.stat().st_size > 1024 * 1024, f".deb 过小: {expected_deb.stat().st_size} bytes"

    tarball = proj / "dist" / "release" / "cli_helloworld-0.1.0-linux.tar.gz"
    assert tarball.is_file(), f"未生成 tar.gz: {tarball}"
    assert tarball.stat().st_size > 1024 * 1024, f"tar.gz 过小: {tarball.stat().st_size} bytes"

    with expected_deb.open("rb") as f:
        magic = f.read(8)
    assert magic == b"!<arch>\n", f".deb 非 ar 归档: {magic!r}"

    with tarball.open("rb") as f:
        assert f.read(2) == b"\x1f\x8b", "tar.gz 非 gzip 格式"
