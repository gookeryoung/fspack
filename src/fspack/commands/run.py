"""fsp r —— 运行已打包项目（Linux 用 wine，Windows 直跑）。."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path

from fspack.config import AppType, ProjectInfo
from fspack.exceptions import FspackError

__all__ = ["run"]

_logger = logging.getLogger(__name__)


def run(project: Path, rest_args: list[str] | None = None, debug: bool = False) -> None:
    """运行 dist 下的可执行文件。

    ``debug=True`` 时绕过 loader exe，用 embed python 直接跑入口脚本，
    使 GUI 应用（Windows subsystem）的 stdout/stderr 可见。
    """
    info = ProjectInfo.from_dir(project)
    rest = rest_args or []
    if debug:
        cmd = _build_debug_cmd(project, info) + rest
        debug_env: dict[str, str] = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if platform.system() != "Windows":
            debug_env["PYTHONHOME"] = str(Path(project) / "dist" / "runtime" / "python")
        env: dict[str, str] | None = debug_env
    else:
        exe = _find_exe(project, info.name)
        if exe is None:
            raise FspackError(f"未找到已构建的可执行文件: {project}/dist/{info.name}[.exe]（请先执行 fsp b）")
        cmd = _build_cmd(exe) + rest
        env = None
    _logger.info("运行: %s", " ".join(cmd))
    completed = subprocess.run(cmd, check=False, env=env)
    if completed.returncode != 0:
        if info.app_type is AppType.GUI and not debug:
            _logger.warning("GUI 应用输出被 Windows subsystem 吞掉，如需查看输出请用 `fspack r --debug`")
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


def _build_debug_cmd(project: Path, info: ProjectInfo) -> list[str]:
    """构造调试命令：用 embed python 直跑入口脚本（绕过 GUI loader）。

    Windows 用 ``dist/runtime/python.exe``，Linux 用 ``dist/runtime/python/bin/python3.X``。
    embed python 是 console 子系统，print 输出可见；``_pth`` 控制 sys.path 含用户源码与依赖。
    """
    dist = Path(project) / "dist"
    entry_rel = info.entry_file.relative_to(info.src_dir).as_posix()
    entry_in_src = dist / "src" / entry_rel
    if not entry_in_src.is_file():
        raise FspackError(f"未找到入口脚本: {entry_in_src}（请先执行 fsp b）")
    if platform.system() == "Windows":
        py = dist / "runtime" / "python.exe"
    else:
        bin_dir = dist / "runtime" / "python" / "bin"
        pys = sorted(bin_dir.glob("python3.*"))
        if not pys:
            raise FspackError(f"未找到 embed python: {bin_dir}（请先执行 fsp b）")
        py = pys[0]
    if not py.is_file():
        raise FspackError(f"未找到 embed python: {py}（请先执行 fsp b）")
    return [str(py), str(entry_in_src)]
