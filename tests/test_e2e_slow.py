"""端到端慢测试：真实下载 embed python + mingw 编译 + wine 运行。

需 mingw-w64 与 wine，标 slow，默认门禁不执行。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).parent / "examples"


@pytest.mark.slow
def test_build_and_run_helloworld(tmp_path: Path) -> None:
    """helloworld 示例真实构建并在 wine 下运行。."""
    from fspack.builder import build
    from fspack.loader import mingw_available
    from fspack.mirror import get_mirror

    if not mingw_available():
        pytest.skip("mingw-w64 未安装")
    if not shutil.which("wine"):
        pytest.skip("wine 未安装")

    proj = tmp_path / "helloworld"
    shutil.copytree(_EXAMPLES / "helloworld", proj)

    build(proj, get_mirror("huawei"), "3.11.9")
    exe = proj / "dist" / "helloworld.exe"
    assert exe.is_file()
    assert (proj / "dist" / "runtime" / "python311.dll").is_file()
    assert (proj / "dist" / "python311._pth").is_file()

    env = {**os.environ, "WINEDEBUG": "-all"}
    result = subprocess.run(["wine", str(exe)], capture_output=True, text=True, timeout=240, env=env, check=False)
    combined = result.stdout + result.stderr
    assert "hello, world" in combined, f"未在输出中发现 hello, world: {combined!r}"
