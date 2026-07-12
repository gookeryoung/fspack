"""端到端慢测试：真实下载 embed python + mingw 编译 + wine 运行。

需 mingw-w64 与 wine，标 slow，默认门禁不执行。
覆盖 5 类典型项目：无库 CLI、有库 CLI、有库 GUI、有库 pygame、有库 web。
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
