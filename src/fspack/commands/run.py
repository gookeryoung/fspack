"""fsp r —— 运行已打包项目（Linux 用 wine，Windows 直跑）."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path

from fspack.config import AppType, EntryPoint, ProjectInfo
from fspack.exceptions import FspackError

__all__ = ["run"]

_logger = logging.getLogger(__name__)


def run(
    project: Path,
    rest_args: list[str] | None = None,
    debug: bool = False,
    entry: str | None = None,
) -> None:
    """运行 dist 下的可执行文件。

    ``debug=True`` 时绕过 loader exe，用 embed python 直接跑入口脚本，
    使 GUI 应用（Windows subsystem）的 stdout/stderr 可见。

    ``entry`` 指定多入口项目中要运行的入口名（与 ``[tool.fspack.entries]``
    键匹配）；单入口项目或 ``entry=None`` 时使用默认入口。
    """
    info = ProjectInfo.from_dir(project)
    rest = rest_args or []
    ep = _select_entry(info, entry)
    if debug:
        cmd = _build_debug_cmd(project, ep) + rest
        debug_env: dict[str, str] = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if platform.system() != "Windows":
            debug_env["PYTHONHOME"] = str(Path(project) / "dist" / "runtime" / "python")
        env: dict[str, str] | None = debug_env
    else:
        exe = _find_exe(project, ep.name)
        if exe is None:
            raise FspackError(f"未找到已构建的可执行文件: {project}/dist/{ep.name}[.exe]（请先执行 fsp b）")
        cmd = _build_cmd(exe) + rest
        env = None
    _logger.info("运行入口 %s: %s", ep.name, " ".join(cmd))
    completed = subprocess.run(cmd, check=False, env=env)
    if completed.returncode != 0:
        if ep.app_type is AppType.GUI and not debug:
            _logger.warning("GUI 应用输出被 Windows subsystem 吞掉，如需查看输出请用 `fspack r --debug`")
        raise FspackError(f"程序退出码非零: {completed.returncode}")


def _select_entry(info: ProjectInfo, entry: str | None) -> EntryPoint:
    """从项目入口中选择要运行的入口。

    ``entry=None`` 时返回首个入口（多入口项目日志提示可指定 ``--entry``）；
    ``entry`` 非空时按名匹配，未找到则报错列出可用入口。
    """
    all_entries = info.all_entries
    if entry is None:
        if len(all_entries) > 1:
            names = ", ".join(ep.name for ep in all_entries)
            _logger.info("多入口项目未指定 --entry，使用首个入口 %s（可用: %s）", all_entries[0].name, names)
        return all_entries[0]
    for ep in all_entries:
        if ep.name == entry:
            return ep
    available = ", ".join(ep.name for ep in all_entries)
    raise FspackError(f"未找到入口: {entry}（可用入口: {available}）")


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
    """构造运行命令：Linux 下 .exe 用 wine，原生可执行文件直跑."""
    if exe.suffix == ".exe" and platform.system() == "Linux":
        wine = shutil.which("wine") or "wine"
        return [wine, str(exe)]
    return [str(exe)]


def _build_debug_cmd(project: Path, ep: EntryPoint) -> list[str]:
    """构造调试命令：用 embed python 直跑入口包装器（绕过 GUI loader）。

    Windows 用 ``dist/runtime/python.exe``，Linux 用 ``dist/runtime/python/bin/python3.X``。
    embed python 是 console 子系统，print 输出可见；运行 ``dist/_entry_<name>.py``
    包装器（与 loader 一致），由 wrapper 设置 sys.path、Qt 插件路径与包上下文
    后调 :func:`runpy.run_module`/:func:`runpy.run_path` 执行用户入口，使相对
    导入可用。
    """
    dist = Path(project) / "dist"
    wrapper = dist / f"_entry_{ep.name}.py"
    if not wrapper.is_file():
        raise FspackError(f"未找到入口包装器: {wrapper}（请先执行 fsp b）")
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
    return [str(py), str(wrapper)]
