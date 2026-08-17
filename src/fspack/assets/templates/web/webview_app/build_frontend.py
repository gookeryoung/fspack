#!/usr/bin/env python3
"""构建前端脚本的简单脚本，用于在打包前执行."""

import subprocess
import sys
from pathlib import Path
from shutil import rmtree, which


def check_command_available(cmd: str) -> bool:
    """检查可执行文件是否存在."""
    return which(cmd) is not None


def build_frontend() -> bool:
    """构建前端文件."""
    frontend_dir = Path(__file__).parent / "src" / "webview_app" / "frontend"
    if not frontend_dir.exists():
        return False

    # 删除旧的输出目录（如果存在）
    output_dir = frontend_dir / "deploy"
    if output_dir.exists():
        rmtree(output_dir)

    # 获取命令后缀（Windows 下是 .cmd）
    cmd_suffix = ".cmd" if sys.platform == "win32" else ""

    # 尝试使用各种命令构建前端
    cmds = ["vite", "yarn", "npm"]
    for cmd in cmds:
        full_cmd = f"{cmd}{cmd_suffix}"
        if check_command_available(full_cmd):
            try:
                subprocess.run([full_cmd, "build"], cwd=str(frontend_dir), check=True)
            except subprocess.CalledProcessError:
                continue
            else:
                return True

    return False
