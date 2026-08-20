from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import cached_property
from pathlib import Path
from typing import Optional

import webview

from webview_app.api import BaseApi

try:
    # 探测须与 pywebview __generate_ssl_cert 的导入集完全一致（深层导入才会
    # 加载 _rust 扩展）：裸 import cryptography 顶层恒成功（纯 Python __init__），
    # 而 cryptography 43+ 的 _rust 由 Rust 1.78+ 编译、最低要求 Win10，
    # Win7 上深层导入 ImportError。此处探测失败降级 http，应用仍可启动。
    from cryptography import x509  # noqa: F401
    from cryptography.hazmat.backends import default_backend  # noqa: F401
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: F401
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
    from cryptography.x509.oid import NameOID  # noqa: F401
except ImportError:
    USE_SSL = False
else:
    USE_SSL = True

WIN = sys.platform == "win32"


class NativeServer:
    """封装前端服务."""

    CWD = Path(__file__).parent
    DIR_FRONTEND = CWD / "frontend"
    DIR_FRONTEND_DIST = DIR_FRONTEND / "deploy"
    DIR_FRONTEND_NODE_MODULES = DIR_FRONTEND / "node_modules"
    PATH_INDEX = DIR_FRONTEND_DIST / "index.html"

    def start(
        self,
        title: str = "PyWebApp Demo",
        api_instance: Optional[BaseApi] = None,
        *,
        debug: bool = False,
    ) -> None:
        """启动服务."""
        assert self.DIR_FRONTEND.exists(), "前端目录不存在"

        # 仅在构建产物（deploy）缺失时才走安装/构建流程（开发首跑场景）。
        # 打包分发场景 deploy 已随包就绪，node_modules 不随包分发也不需要——
        # 终端用户机器无 node 环境，按缺失即装会在启动时必然失败。
        if not self.DIR_FRONTEND_DIST.exists():
            if not self.DIR_FRONTEND_NODE_MODULES.exists():
                print("未找到前端依赖, 尝试安装依赖...")
                self.install_dependencies()
            print("未找到前端发布文件, 尝试构建前端...")
            self.build()

        assert self.PATH_INDEX.exists(), "未找到前端发布文件"
        print("启动服务...")
        try:
            webview.create_window(
                title,
                str(self.PATH_INDEX),
                width=1080,
                height=900,
                min_size=(800, 600),
                js_api=api_instance,
            )

            webview.start(debug=debug, ssl=USE_SSL)
        except (webview.WebViewException, RuntimeError) as e:
            print(f"应用启动失败: {e}")

    def install_dependencies(self) -> None:
        """安装前端依赖."""
        cmd = self.package_cmd

        origin_dir = Path.cwd()
        os.chdir(self.DIR_FRONTEND)
        try:
            subprocess.run([cmd, "install"], shell=True, check=True)
        except (subprocess.CalledProcessError, OSError):
            print("安装前端依赖失败")
        finally:
            os.chdir(origin_dir)

    def build(self) -> None:
        """构建前端."""
        cmd = self.package_cmd

        origin_dir = Path.cwd()
        os.chdir(self.DIR_FRONTEND)
        try:
            subprocess.run([cmd, "run", "build"], shell=True, check=True)
        except (subprocess.CalledProcessError, OSError):
            print("打包前端失败, 删除前端发布文件...")
            shutil.rmtree(self.DIR_FRONTEND_DIST, ignore_errors=True)
            return
        finally:
            os.chdir(origin_dir)

    def development(self) -> None:
        """启动前端开发服务."""
        cmd = self.package_cmd

        origin_dir = Path.cwd()
        os.chdir(self.DIR_FRONTEND)
        try:
            subprocess.run([cmd, "run", "dev"], shell=True, check=True)
        except (subprocess.CalledProcessError, OSError):
            print("启动前端开发服务失败")
        finally:
            os.chdir(origin_dir)

    @cached_property
    def package_cmd(self) -> str:
        """获取包管理器命令."""
        suffix = ".cmd" if WIN else ""
        for cmd in ["pnpm", "yarn", "npm"]:
            if shutil.which(cmd):
                print(f"使用 {cmd} 构建前端")
                return f"{cmd}{suffix}"
        msg = "未找到包管理器"
        raise RuntimeError(msg)
