from __future__ import annotations

import platform
import sys

import webview


class BaseApi:
    """基础API."""


class SystemApi(BaseApi):
    """系统API."""

    def minimize_window(self) -> None:
        """最小化窗口."""
        try:
            webview.windows[0].minimize()
        except AttributeError as e:
            print(f"最小化窗口失败: {e}")
        else:
            print("最小化窗口")

    def maximize_window(self) -> None:
        """最大化/还原窗口."""
        try:
            webview.windows[0].maximize()
        except AttributeError as e:
            print(f"最大化窗口失败: 未找到窗口: {e}")
        else:
            print("最大化窗口")

    def close_window(self) -> None:
        """关闭窗口."""
        try:
            webview.windows[0].destroy()
        except AttributeError as e:
            print(f"关闭窗口失败: {e}, 尝试退出应用...")
            sys.exit(0)
        else:
            print("关闭窗口")

    def get_system_info(self) -> dict:
        """获取系统信息."""
        return {
            "platform": platform.system(),
            "architecture": platform.architecture()[0],
            "version": platform.version(),
            "python_version": platform.python_version(),
            "machine": platform.machine(),
        }

    def get_app_version(self) -> str:
        """获取应用版本."""
        return "1.0.0"
