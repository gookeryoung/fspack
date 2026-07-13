"""fsp r —— 运行已打包项目（Linux 用 wine，Windows 直跑）。."""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path

from fspack.exceptions import FspackError
from fspack.project import parse_project

__all__ = ["run"]

_logger = logging.getLogger(__name__)


def run(project: Path, rest_args: list[str] | None = None) -> None:
    """运行 dist 下的可执行文件。."""
    info = parse_project(project)
    exe = _find_exe(project, info.name)
    if exe is None:
        raise FspackError(f"未找到已构建的可执行文件: {project}/dist/{info.name}[.exe]（请先执行 fsp b）")
    cmd = _build_cmd(exe) + (rest_args or [])
    _logger.info("运行: %s", " ".join(cmd))
    completed = subprocess.run(cmd, check=False)
    if completed.returncode != 0:
        raise FspackError(f"程序退出码非零: {completed.returncode}")


def _find_exe(project: Path, name: str) -> Path | None:
    """按当前平台查找 dist 下的可执行文件。

    Linux 优先找原生无后缀可执行文件，回退 .exe（wine 运行）；
    Windows 找 .exe。
    """
    dist = Path(project) / "dist"
    if platform.system() == "Linux":
        native = dist / name
        if native.is_file():
            return native
    win = dist / f"{name}.exe"
    if win.is_file():
        return win
    return None


def _build_cmd(exe: Path) -> list[str]:
    """构造运行命令：Linux 下 .exe 用 wine，原生可执行文件直跑。."""
    if exe.suffix == ".exe" and platform.system() == "Linux":
        wine = shutil.which("wine") or "wine"
        return [wine, str(exe)]
    return [str(exe)]
