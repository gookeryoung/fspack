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
    exe = Path(project) / "dist" / info.exe_name
    if not exe.is_file():
        raise FspackError(f"未找到已构建的可执行文件: {exe}（请先执行 fsp b）")
    cmd = _build_cmd(exe) + (rest_args or [])
    _logger.info("运行: %s", " ".join(cmd))
    completed = subprocess.run(cmd, check=False)
    if completed.returncode != 0:
        raise FspackError(f"程序退出码非零: {completed.returncode}")


def _build_cmd(exe: Path) -> list[str]:
    if platform.system() == "Linux":
        wine = shutil.which("wine") or "wine"
        return [wine, str(exe)]
    return [str(exe)]
