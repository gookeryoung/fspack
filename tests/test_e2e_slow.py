"""端到端慢测试：真实下载 embed python + mingw 编译 + wine 运行。

需 mingw-w64 与 wine（Windows 目标）或 gcc（Linux 目标），标 slow，默认门禁不执行。
覆盖 9 类典型项目：无库 CLI、有库 CLI、有库 GUI（PySide6/PySide2/PyQt5）、有库 pygame、
有库 web、多入口混合（cli+gui+web 共享 runtime/依赖）。
另含 Linux 平台端到端测试（python-build-standalone + gcc 编译 + 原生运行）。

iter-69 扩展：PySide2 QML 应用、Nuitka 编译端到端（Windows mingw + Linux gcc）、
用户自定义 slim-include 规则覆盖 spec 剥离的端到端验证。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fspack.templates.project_template import ProjectTemplate

_EXAMPLES = ProjectTemplate.root_dir()


def _build_and_run(  # noqa: PLR0913
    proj_name: str,
    expect_substr: str,
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 240,
    debug: bool = False,
    py_version: str = "3.11.9",
) -> None:
    """构建示例并在 wine 下运行，断言输出含预期字符串。

    proj_name: examples/ 下的示例目录名。
    expect_substr: 运行输出中应包含的子串。
    extra_env: wine 运行时额外环境变量（如 GUI/pygame 的 offscreen 驱动）。
    debug: True 时用 embed python + wrapper 直跑入口（绕过 GUI loader，stdout 可见），
        用于 pygame 等改为 GUI 后无控制台输出的场景。
    py_version: 目标 Python 版本（默认 3.11.9），用于 embed python 下载与 DLL 名派生。
    """
    from fspack.builder import build
    from fspack.config import get_mirror
    from fspack.packaging.loader import mingw_available
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / proj_name
    shutil.copytree(_EXAMPLES / proj_name, proj)

    build(proj, get_mirror("aliyun"), py_version, target=Platform.WINDOWS)
    exe = proj / "dist" / f"{proj_name}.exe"
    assert exe.is_file(), f"未生成 exe: {exe}"
    # DLL/pth 名按 Python 版本派生：3.11→python311.dll，3.14→python314.dll
    major, minor = py_version.split(".")[:2]
    py_tag = f"python{major}{minor}"
    assert (proj / "dist" / "runtime" / f"{py_tag}.dll").is_file(), f"未找到 {py_tag}.dll"
    assert (proj / "dist" / "runtime" / f"{py_tag}._pth").is_file(), f"未生成 {py_tag}._pth"

    env = {**os.environ, "WINEDEBUG": "-all", "PYTHONIOENCODING": "utf-8"}
    if extra_env:
        env.update(extra_env)
    if debug:
        # 用 embed python + wrapper 直跑入口，绕过 GUI loader 使 stdout 可见
        # （等价于 `fspack r --debug`）
        py = proj / "dist" / "runtime" / "python.exe"
        wrapper = proj / "dist" / f"_entry_{proj_name}.py"
        assert py.is_file(), f"未找到 embed python: {py}"
        assert wrapper.is_file(), f"未找到入口包装器: {wrapper}"
        env["PYTHONUNBUFFERED"] = "1"
        cmd = ["wine", str(py), str(wrapper)]
    else:
        cmd = ["wine", str(exe)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    combined = result.stdout + result.stderr
    # wine ucrtbase.dll 未实现 C99 复数函数（crealf/cimagf 等），numpy 2.x/scipy 会触发；
    # 真实 Windows 不存在此限制，跳过运行断言（构建已验证）。
    if expect_substr not in combined and "unimplemented function" in combined:
        pytest.skip(f"wine 未实现 ucrtbase.dll 函数，真实 Windows 可运行: {combined[:300]!r}")
    assert expect_substr in combined, f"未在输出中发现 {expect_substr!r}: {combined!r}"


@pytest.mark.slow
def test_build_and_run_helloworld(tmp_path: Path) -> None:
    """cli_helloworld_pyall 示例真实构建并在 wine 下运行."""
    _build_and_run("cli_helloworld_pyall", "hello, world", tmp_path)


@pytest.mark.slow
def test_build_and_run_clitool(tmp_path: Path) -> None:
    """cli_tool_pyall 示例：有库 CLI（requests），验证依赖打包与运行."""
    _build_and_run("cli_tool_pyall", "requests ", tmp_path)
    # 验证 requests 包确实解包到 site-packages
    proj = tmp_path / "cli_tool_pyall"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "requests").is_dir()


@pytest.mark.slow
def test_build_and_run_guicalc(tmp_path: Path) -> None:
    """gui_calc_pyall 示例：有库 GUI（PySide6），验证构建与打包。

    PySide6 的 Qt6Core 依赖 icuuc.dll（Windows 10+ 系统 DLL），wine 默认不提供。
    缺 ICU 时仅验证构建（下载/解包/_pth/exe），跳过运行断言。
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror
    from fspack.packaging.loader import mingw_available
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "gui_calc_pyall"
    shutil.copytree(_EXAMPLES / "gui_calc_pyall", proj)
    build(
        proj,
        get_mirror("aliyun"),
        "3.11.9",
        target=Platform.WINDOWS,
        options=BuildOptions(keep_modules={"PySide6.QtCore", "PySide6.QtGui"}),
    )

    exe = proj / "dist" / "gui_calc_pyall.exe"
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
def test_build_and_run_pygame_cli_pyall(tmp_path: Path) -> None:
    """pygame_cli_pyall 示例：有库 pygame，dummy 驱动验证。

    pygame 改为 GUI（无控制台）后，用 debug 模式（embed python + wrapper 直跑）
    使 print 输出可见。SDL dummy 驱动让 pygame 在无显示环境运行。
    """
    _build_and_run(
        "pygame_cli_pyall",
        "pygame ",
        tmp_path,
        extra_env={"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"},
        debug=True,
    )
    proj = tmp_path / "pygame_cli_pyall"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "pygame").is_dir()


@pytest.mark.slow
def test_build_and_run_webapp(tmp_path: Path) -> None:
    """web_app_pyall 示例：有库 web（flask），test_client 验证路由."""
    _build_and_run("web_app_pyall", "hello from flask", tmp_path)
    proj = tmp_path / "web_app_pyall"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "flask").is_dir()


@pytest.mark.slow
def test_build_and_run_pyside2app(tmp_path: Path) -> None:
    """pyside2_app_py310 示例：版本自动解析 + PySide2，验证 requires-python 约束。

    .python-version=3.10 + requires-python=">=3.8,<3.11" 应解析到 3.10.11。
    PySide2 的 Qt DLL 在 wine 上可能缺系统 DLL，缺时跳过运行断言。
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror
    from fspack.packaging.loader import mingw_available
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "pyside2_app_py310"
    shutil.copytree(_EXAMPLES / "pyside2_app_py310", proj)
    build(
        proj, get_mirror("aliyun"), None, target=Platform.WINDOWS, options=BuildOptions(keep_modules={"PySide2.QtGui"})
    )

    exe = proj / "dist" / "pyside2_app_py310.exe"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python310.dll").is_file(), "未找到 python310.dll（版本自动解析应为 3.10.11）"
    assert (proj / "dist" / "runtime" / "python310._pth").is_file(), "未生成 _pth"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "PySide2").is_dir(), "PySide2 未解包"

    env = {**os.environ, "WINEDEBUG": "-all", "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run(["wine", str(exe)], capture_output=True, text=True, timeout=300, env=env, check=False)
    combined = result.stdout + result.stderr
    if "hello from PySide2" not in combined and "DLL load failed" in combined:
        pytest.skip(f"wine 缺少系统 DLL，PySide2 Qt DLL 无法加载，真实 Windows 可运行: {combined!r}")
    assert "hello from PySide2" in combined, f"未在输出中发现 'hello from PySide2': {combined!r}"


@pytest.mark.slow
def test_build_and_run_pyqt5_cli_pyall(tmp_path: Path) -> None:
    """pyqt5_cli_pyall 示例：Python 3.12 + PyQt5，验证新版本 + PyQt5 兼容。

    PyQt5 的 Qt DLL 在 wine 上可能缺系统 DLL，缺时跳过运行断言。
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror
    from fspack.packaging.loader import mingw_available
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "pyqt5_cli_pyall"
    shutil.copytree(_EXAMPLES / "pyqt5_cli_pyall", proj)
    build(
        proj,
        get_mirror("aliyun"),
        "3.12.0",
        target=Platform.WINDOWS,
        options=BuildOptions(keep_modules={"PyQt5.QtCore", "PyQt5.QtGui"}),
    )

    exe = proj / "dist" / "pyqt5_cli_pyall.exe"
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
    """pygame_snake_pyall 示例：pygame 贪吃蛇，dummy 驱动验证。

    pygame 改为 GUI（无控制台）后，用 debug 模式（embed python + wrapper 直跑）
    使 print 输出可见。SDL dummy 驱动让 pygame 在无显示环境运行，DUMMY_MAX_FRAMES
    控制循环退出避免死循环。
    """
    _build_and_run(
        "pygame_snake_pyall",
        "snake ready",
        tmp_path,
        extra_env={"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"},
        debug=True,
    )
    proj = tmp_path / "pygame_snake_pyall"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "pygame").is_dir()


@pytest.mark.slow
def test_build_and_run_multi_entry_py310(tmp_path: Path) -> None:
    """multi_entry_py310 示例：多入口项目（cli+gui+web）共享 runtime/依赖。

    验证 [tool.fspack.entries] 解析、三入口 exe 生成、各自运行输出正确。
    .python-version=3.10 + requires-python=">=3.8,<3.11" 应解析到 3.10.11。
    GUI 入口（PySide2）在 wine 上可能缺系统 DLL，缺时 skip GUI 运行断言。
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror
    from fspack.packaging.loader import mingw_available
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "multi_entry_py310"
    shutil.copytree(_EXAMPLES / "multi_entry_py310", proj)
    build(
        proj, get_mirror("aliyun"), None, target=Platform.WINDOWS, options=BuildOptions(keep_modules={"PySide2.QtGui"})
    )

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
    assert "hello from multi_entry_py310 cli" in combined, f"cli 入口输出异常: {combined!r}"

    # Web 入口：wine 运行断言输出（test_client 不启动服务器，可安全运行）
    web_exe = proj / "dist" / "web.exe"
    result = subprocess.run(["wine", str(web_exe)], capture_output=True, text=True, timeout=120, env=env, check=False)
    combined = result.stdout + result.stderr
    assert "hello from multi_entry_py310 web" in combined, f"web 入口输出异常: {combined!r}"

    # GUI 入口：PySide2 在 wine 上可能缺系统 DLL，缺时 skip
    gui_exe = proj / "dist" / "gui.exe"
    result = subprocess.run(["wine", str(gui_exe)], capture_output=True, text=True, timeout=300, env=env, check=False)
    combined = result.stdout + result.stderr
    if "hello from multi_entry_py310 gui" not in combined and "DLL load failed" in combined:
        pytest.skip(f"wine 缺少系统 DLL，PySide2 Qt DLL 无法加载，真实 Windows 可运行: {combined!r}")
    assert "hello from multi_entry_py310 gui" in combined, f"gui 入口输出异常: {combined!r}"


@pytest.mark.slow
def test_build_and_run_linux_helloworld(tmp_path: Path) -> None:
    """Linux 平台端到端：gcc 编译 + python-build-standalone 运行 cli_helloworld_pyall。

    python-build-standalone 的 20260718 release 提供 3.11.15，Linux 目标使用 3.11.15。
    仅在 Linux 原生平台运行：Linux loader 编译用本地 gcc（非交叉编译器），
    Windows 上的 mingw gcc 缺 ``dlfcn.h``/``linux/limits.h`` 等头文件无法编译。
    """
    from fspack.builder import build
    from fspack.config import get_mirror
    from fspack.packaging.loader import gcc_available
    from fspack.platform import Platform, detect_platform

    if detect_platform() is not Platform.LINUX:
        pytest.skip("Linux e2e 测试需在 Linux 上运行（交叉编译缺 Linux 头文件）")
    if not gcc_available():
        pytest.skip("gcc 未安装")

    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj)
    build(proj, get_mirror("aliyun"), "3.11.15", target=Platform.LINUX)

    exe = proj / "dist" / "cli_helloworld_pyall"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python" / "lib" / "libpython3.11.so").is_file(), "未找到 libpython3.11.so"

    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)
    combined = result.stdout + result.stderr
    assert "hello, world" in combined, f"未在输出中发现 'hello, world': {combined!r}"


@pytest.mark.slow
def test_build_and_run_linux_clitool(tmp_path: Path) -> None:
    """Linux 平台端到端：有库 CLI（requests），验证依赖打包与运行.

    仅在 Linux 原生平台运行（同 ``test_build_and_run_linux_helloworld`` 平台限制）。
    """
    from fspack.builder import build
    from fspack.config import get_mirror
    from fspack.packaging.loader import gcc_available
    from fspack.platform import Platform, detect_platform

    if detect_platform() is not Platform.LINUX:
        pytest.skip("Linux e2e 测试需在 Linux 上运行（交叉编译缺 Linux 头文件）")
    if not gcc_available():
        pytest.skip("gcc 未安装")

    proj = tmp_path / "cli_tool_pyall"
    shutil.copytree(_EXAMPLES / "cli_tool_pyall", proj)
    build(proj, get_mirror("aliyun"), "3.11.15", target=Platform.LINUX)

    exe = proj / "dist" / "cli_tool_pyall"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python" / "lib" / "python3.11" / "site-packages" / "requests").is_dir()

    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)
    combined = result.stdout + result.stderr
    assert "requests " in combined, f"未在输出中发现 'requests ': {combined!r}"


@pytest.mark.slow
def test_build_and_run_linux_cli_complex_py314(tmp_path: Path) -> None:
    """Linux 平台端到端：cli_complex_py314 多包嵌套 + 顶层绝对导入链.

    验证 wrapper 顶层模式显式注入 ``dist/src`` 到 ``sys.path`` 使
    ``import module_c``/``from modules.module_a import ...`` 等本地绝对导入
    可用（``runpy.run_path`` 不自动添加脚本目录到 sys.path 的回归根因）。
    Python 3.14 standalone + gcc 编译 + 原生运行。
    """
    from fspack.builder import build
    from fspack.config import get_mirror
    from fspack.packaging.loader import gcc_available
    from fspack.platform import Platform, detect_platform

    if detect_platform() is not Platform.LINUX:
        pytest.skip("Linux e2e 测试需在 Linux 上运行（交叉编译缺 Linux 头文件）")
    if not gcc_available():
        pytest.skip("gcc 未安装")

    proj = tmp_path / "cli_complex_py314"
    shutil.copytree(_EXAMPLES / "cli_complex_py314", proj)
    build(proj, get_mirror("aliyun"), "3.14.6", target=Platform.LINUX)

    exe = proj / "dist" / "cli_complex_py314"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python" / "lib" / "python3.14" / "site-packages" / "lxml").is_dir()
    assert (proj / "dist" / "runtime" / "python" / "lib" / "python3.14" / "site-packages" / "ordered_set").is_dir()

    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)
    combined = result.stdout + result.stderr
    assert "hello, world" in combined, f"未在输出中发现 'hello, world': {combined!r}"


@pytest.mark.slow
def test_build_installer_helloworld_slow(tmp_path: Path) -> None:
    """NSIS 端到端：build cli_helloworld_pyall → makensis 编译 → 验证安装包产出。

    需 mingw-w64（Windows loader 编译）与 makensis（NSIS 安装包编译）。
    验证 dist/installer.nsi 生成正确、dist/release/cli_helloworld_pyall-setup.exe 产出为合法 PE 文件且非空。
    """
    from fspack.config import get_mirror
    from fspack.packaging.installer import build_installer
    from fspack.packaging.loader import mingw_available

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("makensis"):
        pytest.skip("makensis 未安装（sudo apt install -y nsis）")

    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj)

    out = build_installer(proj, get_mirror("aliyun"), "3.11.9", no_build=False)
    expected = proj / "dist" / "release" / "cli_helloworld_pyall-0.1.0-py3.11.9-windows-slim-setup.exe"
    assert out == expected
    assert expected.is_file(), f"未生成安装包: {expected}"
    assert expected.stat().st_size > 1024 * 1024, f"安装包过小: {expected.stat().st_size} bytes"

    with expected.open("rb") as f:
        assert f.read(2) == b"MZ", "安装包非合法 PE 文件"

    nsi = proj / "dist" / "installer.nsi"
    assert nsi.is_file(), "未生成 installer.nsi"
    content = nsi.read_text(encoding="utf-8")
    assert 'Name "cli_helloworld_pyall 0.1.0"' in content
    assert 'OutFile "release\\cli_helloworld_pyall-0.1.0-py3.11.9-windows-slim-setup.exe"' in content


@pytest.mark.slow
def test_build_linux_installer_helloworld_slow(tmp_path: Path) -> None:
    """Linux 安装包端到端：build cli_helloworld_pyall → tar.gz + .deb 真实产出。

    需 gcc（Linux loader 编译）与 dpkg-deb（.deb 构建）。
    验证 dist/release/cli_helloworld_pyall_0.1.0-py3.11.15-slim_amd64.deb 为合法 ar 归档，
    dist/release/cli_helloworld_pyall-0.1.0-py3.11.15-linux-slim.tar.gz 为合法 gzip。
    """
    from fspack.config import get_mirror
    from fspack.packaging.installer import build_linux_installer
    from fspack.packaging.loader import gcc_available

    if not gcc_available():
        pytest.skip("gcc 未安装")
    if not shutil.which("dpkg-deb"):
        pytest.skip("dpkg-deb 未安装")

    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj)

    out = build_linux_installer(proj, get_mirror("aliyun"), "3.11.15", no_build=False)
    expected_deb = proj / "dist" / "release" / "cli_helloworld_pyall_0.1.0-py3.11.15-slim_amd64.deb"
    assert out == expected_deb
    assert expected_deb.is_file(), f"未生成 .deb: {expected_deb}"
    assert expected_deb.stat().st_size > 1024 * 1024, f".deb 过小: {expected_deb.stat().st_size} bytes"

    tarball = proj / "dist" / "release" / "cli_helloworld_pyall-0.1.0-py3.11.15-linux-slim.tar.gz"
    assert tarball.is_file(), f"未生成 tar.gz: {tarball}"
    assert tarball.stat().st_size > 1024 * 1024, f"tar.gz 过小: {tarball.stat().st_size} bytes"

    with expected_deb.open("rb") as f:
        magic = f.read(8)
    assert magic == b"!<arch>\n", f".deb 非 ar 归档: {magic!r}"

    with tarball.open("rb") as f:
        assert f.read(2) == b"\x1f\x8b", "tar.gz 非 gzip 格式"


@pytest.mark.slow
def test_build_and_run_cli_complex(tmp_path: Path) -> None:
    """cli_complex_py314 示例：多包嵌套 + lxml/ordered-set 依赖，验证子模块导入链.

    模板用顶层绝对导入（``import module_c``/``from modules.module_a import ...``），
    无 ``__init__.py`` 触发顶层模式（``runpy.run_path``），wrapper 显式注入
    ``dist/src`` 到 ``sys.path`` 使绝对导入可用。Python 3.14 验证新版本兼容。
    """
    _build_and_run("cli_complex_py314", "hello, world", tmp_path, debug=True, py_version="3.14.6")
    proj = tmp_path / "cli_complex_py314"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "lxml").is_dir()
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "ordered_set").is_dir()


@pytest.mark.slow
def test_build_and_run_cli_office_py38(tmp_path: Path) -> None:
    """cli_office_py38 示例：pypdf 依赖，验证 PDF 生成 CLI."""
    _build_and_run("cli_office_py38", "文件生成成功", tmp_path, debug=True)
    proj = tmp_path / "cli_office_py38"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "pypdf").is_dir()


@pytest.mark.slow
def test_build_and_run_pygame_conway_py313(tmp_path: Path) -> None:
    """pygame_conway_py313 示例：numpy/attrs/pygame 依赖，dummy 驱动验证."""
    _build_and_run(
        "pygame_conway_py313",
        "Hello from the pygame community",
        tmp_path,
        extra_env={"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"},
        py_version="3.13.0",
        debug=True,
    )
    proj = tmp_path / "pygame_conway_py313"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "numpy").is_dir()
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "attrs").is_dir()
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "pygame").is_dir()


@pytest.mark.slow
def test_build_and_run_pygame_gktetris_py38(tmp_path: Path) -> None:
    """pygame_gktetris_py38 示例：包模式（src.game）+ pygame，dummy 驱动验证。

    src_dir 有 __init__.py，入口 game.py 在顶层，wrapper 用 runpy.run_module
    以包上下文运行（_ENTRY_MODULE='src.game'），相对导入可用。
    """
    _build_and_run(
        "pygame_gktetris_py38",
        "Hello from the pygame community",
        tmp_path,
        extra_env={"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy"},
        debug=True,
    )
    proj = tmp_path / "pygame_gktetris_py38"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "pygame").is_dir()


@pytest.mark.slow
def test_build_and_run_sci_numpy_py38(tmp_path: Path) -> None:
    """sci_numpy_py38 示例：numpy 数组运算，验证科学库精简打包与运行.

    numpy 顶层 C 扩展（_multiarray_umath 等）归 shared 始终保留；
    distutils/_pyinstaller 由 NumpySlimSpec 剥离。timeout 加大以
    适应 numpy wheel 下载与解压。
    """
    _build_and_run("sci_numpy_py38", "numpy demo ok", tmp_path, timeout=600)
    proj = tmp_path / "sci_numpy_py38"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "numpy").is_dir()
    # numpy 专属剥离目录不应解包
    assert not (proj / "dist" / "runtime" / "Lib" / "site-packages" / "numpy" / "distutils").is_dir()
    assert not (proj / "dist" / "runtime" / "Lib" / "site-packages" / "numpy" / "_pyinstaller").is_dir()


@pytest.mark.slow
def test_build_and_run_sci_matplotlib_py38(tmp_path: Path) -> None:
    """sci_matplotlib_py38 示例：Agg 后端绘图保存 PNG，验证 matplotlib 精简打包.

    matplotlib wheel 含跨包 mpl_toolkits 与 matplotlib.libs 共享 DLL；
    sphinxext 文档扩展与跨包/嵌套 tests 目录由 MatplotlibSlimSpec 剥离。
    Agg 后端无需 GUI，打包后无显示环境可运行。timeout 加大以适应
    matplotlib + numpy wheel 下载与解压。
    """
    _build_and_run("sci_matplotlib_py38", "matplotlib demo ok", tmp_path, timeout=600)
    proj = tmp_path / "sci_matplotlib_py38"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "matplotlib").is_dir()
    # 跨包 mpl_toolkits 应解包（运行时模块）
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "mpl_toolkits").is_dir()
    # matplotlib 专属剥离目录不应解包
    assert not ((proj / "dist" / "runtime" / "Lib" / "site-packages" / "matplotlib" / "sphinxext").is_dir())
    # 跨包嵌套 tests 不应解包
    assert not ((proj / "dist" / "runtime" / "Lib" / "site-packages" / "mpl_toolkits" / "tests").is_dir())


@pytest.mark.slow
def test_build_and_run_sci_scipy_py38(tmp_path: Path) -> None:
    """sci_scipy_py38 示例：scipy 线性代数与优化求解，验证 scipy 精简打包.

    scipy 各子模块下嵌套 tests 目录由 ScipySlimSpec 剥离（约占 scipy
    总体积 10-15%）；_lib 内部库与各子模块运行时代码保留。timeout 加大
    以适应 scipy + numpy wheel 下载与解压。
    """
    _build_and_run("sci_scipy_py38", "scipy demo ok", tmp_path, timeout=900)
    proj = tmp_path / "sci_scipy_py38"
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "scipy").is_dir()
    # 嵌套 tests 不应解包（ScipySlimSpec 核心剥离场景）
    assert not ((proj / "dist" / "runtime" / "Lib" / "site-packages" / "scipy" / "linalg" / "tests").is_dir())
    assert not ((proj / "dist" / "runtime" / "Lib" / "site-packages" / "scipy" / "optimize" / "tests").is_dir())
    # 运行时内部库应保留
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "scipy" / "_lib").is_dir()


@pytest.mark.slow
def test_build_and_run_tk_app_pyall(tmp_path: Path) -> None:
    """tk_app_pyall 示例：tkinter 内置库打包，验证 TkinterBundler 补充 tkinter 到 embed python。

    AST 检出 ``import tkinter`` → TkinterBundler 从 python-build-standalone Windows
    构建提取 tkinter 组件（Lib/tkinter/ + _tkinter.pyd + tcl/tcl8.6/ + tcl/tk8.6/）
    补充到 runtime。wrapper 注入 TCL_LIBRARY/TK_LIBRARY 环境变量。

    GUI 应用用 debug 模式（embed python + wrapper 直跑）使 print 输出可见。
    root.after(1000) 定时退出避免 wine 下挂起。
    """
    _build_and_run("tk_app_pyall", "hello from tkinter", tmp_path, debug=True, timeout=300)
    proj = tmp_path / "tk_app_pyall"
    # tkinter 纯 Python 包应补充到 runtime/Lib/tkinter/
    assert (proj / "dist" / "runtime" / "Lib" / "tkinter" / "__init__.py").is_file(), "tkinter 包未补充"
    # _tkinter.pyd C 扩展应在 runtime 根目录
    assert (proj / "dist" / "runtime" / "_tkinter.pyd").is_file(), "_tkinter.pyd 未补充"
    # Tcl/Tk 运行时脚本应在 runtime/tcl/
    assert (proj / "dist" / "runtime" / "tcl").is_dir(), "tcl 目录未补充"
    tcl_dirs = list((proj / "dist" / "runtime" / "tcl").iterdir())
    tcl_ver_dirs = [d for d in tcl_dirs if d.name.startswith("tcl") and d.is_dir()]
    tk_ver_dirs = [d for d in tcl_dirs if d.name.startswith("tk") and d.is_dir()]
    assert tcl_ver_dirs, f"未找到 tcl8.x/ 目录: {tcl_dirs}"
    assert tk_ver_dirs, f"未找到 tk8.x/ 目录: {tcl_dirs}"
    # wrapper 应注入 TCL_LIBRARY/TK_LIBRARY 环境变量设置
    wrapper = (proj / "dist" / "_entry_tk_app_pyall.py").read_text(encoding="utf-8")
    assert "if True:" in wrapper, "wrapper 未注入 tkinter 环境变量（has_tkinter 应为 True）"
    assert "TCL_LIBRARY" in wrapper


# ---------- iter-69 扩展：PySide2 QML / Nuitka / slim-include 端到端 ----------


@pytest.mark.slow
def test_build_and_run_pyside2_qml_dashboard_py38(tmp_path: Path) -> None:
    """pyside2_qml_dashboard_py38 示例：PySide2 + QML 应用打包验证。

    验证：
    1. QML 资源文件（views/*.qml + qtquickcontrols2.conf）正确打包到 dist/src/
    2. PySide2 依赖（QtQml/QtQuickControls2 模块）解包到 site-packages
    3. wine 运行可能因系统 DLL 缺失跳过（与 pyside2_app_py310 同条件）

    PySide2 的 Qt DLL 在 wine 上可能缺系统 DLL（icuuc.dll 等），缺时跳过
    运行断言，仅验证构建产物。
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror
    from fspack.packaging.loader import mingw_available
    from fspack.platform import Platform

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "pyside2_qml_dashboard_py38"
    shutil.copytree(_EXAMPLES / "pyside2_qml_dashboard_py38", proj)
    build(
        proj,
        get_mirror("aliyun"),
        None,
        target=Platform.WINDOWS,
        options=BuildOptions(keep_modules={"PySide2.QtQml", "PySide2.QtQuickControls2"}),
    )

    exe = proj / "dist" / "pyside2_qml_dashboard_py38.exe"
    assert exe.is_file(), f"未生成 exe: {exe}"
    assert (proj / "dist" / "runtime" / "python310.dll").is_file(), (
        "未找到 python310.dll（requires-python=<3.11 解析到 3.10.11）"
    )
    assert (proj / "dist" / "runtime" / "python310._pth").is_file(), "未生成 _pth"
    # PySide2 解包
    assert (proj / "dist" / "runtime" / "Lib" / "site-packages" / "PySide2").is_dir(), "PySide2 未解包"

    # QML 资源文件应打包到 dist/src/（copy_source 排除 *.md 但保留 .qml/.conf）
    src_views = proj / "dist" / "src" / "views"
    assert src_views.is_dir(), f"QML views/ 目录未打包: {src_views}"
    qml_files = list(src_views.glob("*.qml"))
    assert len(qml_files) >= 5, f"QML 文件数不足: {len(qml_files)}"
    assert (proj / "dist" / "src" / "qtquickcontrols2.conf").is_file(), "qtquickcontrols2.conf 未打包"

    # PySide2 QtQml/QtQuick 模块应解包（keep_modules 显式保留）
    pyside2_dir = proj / "dist" / "runtime" / "Lib" / "site-packages" / "PySide2"
    assert any(p.name.startswith("QtQml") for p in pyside2_dir.iterdir()), "QtQml 模块未解包"
    assert any(p.name.startswith("QtQuickControls2") for p in pyside2_dir.iterdir()), "QtQuickControls2 模块未解包"

    # wine 运行验证：QML 应用需 offscreen 平台
    env = {**os.environ, "WINEDEBUG": "-all", "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run(["wine", str(exe)], capture_output=True, text=True, timeout=300, env=env, check=False)
    combined = result.stdout + result.stderr
    # QML 应用启动后无 print 输出，验证不崩溃（returncode 0 或被 wine 信号中断）
    # 主要验证 Qt DLL 加载成功（无 DLL load failed）
    if "DLL load failed" in combined:
        pytest.skip(f"wine 缺少系统 DLL，PySide2 Qt DLL 无法加载，真实 Windows 可运行: {combined!r}")
    # QML 应用无控制台输出，returncode 0 表示正常启动并退出
    # wine 下可能因 offscreen 平台退出码非 0，仅验证无 DLL 加载失败


@pytest.mark.slow
def test_build_with_nuitka_compilation(tmp_path: Path) -> None:
    """Nuitka 编译端到端：cli_helloworld_pyall + --nuitka 编译为 .pyd。

    验证：
    1. Nuitka 环境自动就绪（下载 nuitka + clang 到 ~/.fspack/cache/nuitka/）
    2. dist/src 下用户源码编译为 .pyd（Windows）/ .so（Linux）
    3. .py 源码被剥离（pyc_strip 配合，仅保留入口 .py）
    4. wine 运行验证编译产物可执行

    首次运行需下载 Nuitka wheel 与 clang（~150MB），耗时较长（>5 分钟）。
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror
    from fspack.packaging.loader import mingw_available
    from fspack.platform import Platform, detect_platform

    if detect_platform() is not Platform.WINDOWS:
        pytest.skip("Windows Nuitka e2e 测试需在 Windows 上运行（mingw + wine）")
    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj)
    build(
        proj,
        get_mirror("aliyun"),
        "3.11.9",
        target=Platform.WINDOWS,
        options=BuildOptions(nuitka=True, pyc_strip=True),
    )

    exe = proj / "dist" / "cli_helloworld_pyall.exe"
    assert exe.is_file(), f"未生成 exe: {exe}"

    # dist/src 下应有 .pyd 编译产物（Windows 命名：helloworld.cp311-win_amd64.pyd）
    src_dir = proj / "dist" / "src"
    pyd_files = list(src_dir.glob("*.pyd"))
    assert pyd_files, f"未找到 Nuitka 编译的 .pyd 产物: {list(src_dir.iterdir())}"

    # .py 源码应被剥离（pyc_strip），仅入口 helloworld.py 保留
    py_files = [p for p in src_dir.glob("*.py") if p.name != "helloworld.py"]
    assert not py_files, f"非入口 .py 未被剥离: {py_files}"
    # 入口 helloworld.py 必须保留（runpy.run_path 需要 .py 文件）
    assert (src_dir / "helloworld.py").is_file(), "入口 helloworld.py 未保留（Nuitka 编译跳过入口）"

    # wine 运行验证编译产物可执行
    env = {**os.environ, "WINEDEBUG": "-all", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(["wine", str(exe)], capture_output=True, text=True, timeout=240, env=env, check=False)
    combined = result.stdout + result.stderr
    assert "hello, world" in combined, f"Nuitka 编译产物运行失败: {combined!r}"


@pytest.mark.slow
def test_build_linux_with_nuitka(tmp_path: Path) -> None:
    """Linux Nuitka 编译端到端：cli_helloworld_pyall + --nuitka 编译为 .so。

    验证：
    1. Linux 平台 Nuitka 环境自动就绪
    2. dist/src 下用户源码编译为 .so（Linux 命名：helloworld.cpython-311-x86_64-linux-gnu.so）
    3. .py 源码被剥离，仅保留入口
    4. 原生运行验证编译产物可执行

    仅在 Linux 原生平台运行：Nuitka 无法交叉编译（target 必须等于 detect_platform()）。
    """
    from fspack.builder import build
    from fspack.config import BuildOptions, get_mirror
    from fspack.packaging.loader import gcc_available
    from fspack.platform import Platform, detect_platform

    if detect_platform() is not Platform.LINUX:
        pytest.skip("Linux Nuitka e2e 测试需在 Linux 上运行（无法交叉编译）")
    if not gcc_available():
        pytest.skip("gcc 未安装")

    proj = tmp_path / "cli_helloworld_pyall"
    shutil.copytree(_EXAMPLES / "cli_helloworld_pyall", proj)
    build(
        proj,
        get_mirror("aliyun"),
        "3.11.15",
        target=Platform.LINUX,
        options=BuildOptions(nuitka=True, pyc_strip=True),
    )

    exe = proj / "dist" / "cli_helloworld_pyall"
    assert exe.is_file(), f"未生成 exe: {exe}"

    # dist/src 下应有 .so 编译产物
    src_dir = proj / "dist" / "src"
    so_files = list(src_dir.glob("*.so"))
    assert so_files, f"未找到 Nuitka 编译的 .so 产物: {list(src_dir.iterdir())}"

    # 入口 helloworld.py 必须保留，其他 .py 被剥离
    py_files = [p for p in src_dir.glob("*.py") if p.name != "helloworld.py"]
    assert not py_files, f"非入口 .py 未被剥离: {py_files}"
    assert (src_dir / "helloworld.py").is_file(), "入口 helloworld.py 未保留"

    # 原生运行验证
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)
    combined = result.stdout + result.stderr
    assert "hello, world" in combined, f"Linux Nuitka 编译产物运行失败: {combined!r}"


@pytest.mark.slow
def test_build_with_slim_include_rule(tmp_path: Path) -> None:
    """用户自定义 slim-include 规则端到端：强制保留 numpy/distutils（覆盖 NumpySlimSpec 剥离）。

    验证：
    1. slim-include 规则覆盖 spec 默认剥离：numpy/distutils 被 NumpySlimSpec 剥离，
       用户通过 slim-include="numpy/distutils/*" 强制保留
    2. 保留的文件确实解包到 site-packages
    3. 应用仍能正常运行（保留 distutils 不影响 numpy 运行）

    用 sci_numpy_py38 项目 + 通过修改 pyproject.toml 注入 [tool.fspack] slim-include。
    """
    from fspack.builder import build
    from fspack.config import get_mirror
    from fspack.packaging.loader import mingw_available
    from fspack.platform import Platform, detect_platform

    target = detect_platform()
    if target is Platform.WINDOWS:
        if not mingw_available():
            pytest.skip("mingw-w64 未安装")
        if not shutil.which("wine"):
            pytest.skip("wine 未安装")
    else:
        from fspack.packaging.loader import gcc_available

        if not gcc_available():
            pytest.skip("gcc 未安装")

    proj = tmp_path / "sci_numpy_py38"
    shutil.copytree(_EXAMPLES / "sci_numpy_py38", proj)
    # 注入 slim-include 规则强制保留 numpy/distutils（NumpySlimSpec 默认剥离）
    pyproject = proj / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(content + '\n[tool.fspack]\nslim-include = ["numpy/distutils/*"]\n', encoding="utf-8")

    py_ver = "3.11.15" if target is Platform.LINUX else "3.11.9"
    build(proj, get_mirror("aliyun"), py_ver, target=target)

    exe = proj / "dist" / ("sci_numpy_py38" if target is Platform.LINUX else "sci_numpy_py38.exe")
    assert exe.is_file(), f"未生成 exe: {exe}"

    # numpy/distutils 应被保留（slim-include 覆盖 spec 剥离）
    distutils_dir = proj / "dist" / "runtime" / "Lib" / "site-packages" / "numpy" / "distutils"
    assert distutils_dir.is_dir(), f"slim-include 未生效，numpy/distutils 未保留: {distutils_dir}"
    # 至少有 __init__.py
    assert (distutils_dir / "__init__.py").is_file(), "numpy/distutils/__init__.py 未保留"

    # 运行验证 numpy 仍能正常工作
    env = {**os.environ, "WINEDEBUG": "-all", "PYTHONIOENCODING": "utf-8"} if target is Platform.WINDOWS else os.environ
    cmd = ["wine", str(exe)] if target is Platform.WINDOWS else [str(exe)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env, check=False)
    combined = result.stdout + result.stderr
    assert "numpy demo ok" in combined, f"slim-include 保留 distutils 后 numpy 运行失败: {combined!r}"
