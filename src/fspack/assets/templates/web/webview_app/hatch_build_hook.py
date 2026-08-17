"""自定义 hatch 构建钩子，用于在打包前构建前端."""

import logging
import os
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CustomBuildHook(BuildHookInterface):
    """自定义构建钩子."""

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: ARG002
        """初始化构建钩子."""
        # 在构建前运行前端构建脚本
        frontend_dir = Path(__file__).parent / "src" / "webview_app" / "frontend"
        if not frontend_dir.exists():
            logger.info(f"警告: 前端目录不存在: {frontend_dir}")
            return

        # 检查是否已经有构建文件
        output_dir = frontend_dir / "deploy"
        if output_dir.exists() and any(output_dir.iterdir()):
            logger.info(f"前端文件夹 `{output_dir.name}` 已存在, 跳过构建")
            return

        # 执行前端构建
        logger.info(f"前端文件夹不存在: `{output_dir}`")
        logger.info("正在构建前端文件...")
        cmd_suffix = ".cmd" if sys.platform == "win32" else ""

        # 尝试使用各种命令构建前端
        cmds = ["yarn", "npm"]
        build_success = False
        for cmd in cmds:
            full_cmd = f"{cmd}{cmd_suffix}"
            if (
                os.system(
                    f"where {full_cmd} >nul 2>nul"
                    if sys.platform == "win32"
                    else f"which {full_cmd} > /dev/null 2>&1",
                )
                == 0
            ):
                logger.info(f"使用 {full_cmd} 构建前端...")
                try:
                    subprocess.run(
                        [full_cmd, "run", "build"],
                        cwd=str(frontend_dir),
                        check=True,
                        capture_output=True,
                    )
                    logger.info("前端构建成功!")
                    build_success = True
                    break
                except subprocess.CalledProcessError as e:
                    logger.info(f"使用 {full_cmd} 构建失败: {e}")
                    continue

        if not build_success:
            logger.info("警告: 前端构建失败, 可能需要手动构建")
            logger.info("请运行以下命令手动构建前端:")
            logger.info(f"cd {frontend_dir}")
            logger.info("npm install  # 或 yarn install")
            logger.info("npm run build  # 或 yarn build")
