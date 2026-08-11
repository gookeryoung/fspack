"""init 子命令参数声明."""

from __future__ import annotations

import argparse


def _add_init_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 init/i 子命令：从模板创建新项目."""
    p = sub.add_parser("init", aliases=["i"], help="从模板创建新项目")
    p.add_argument("project_name", nargs="?", help="项目名（默认当前目录名）")
    p.add_argument(
        "--template",
        default=None,
        help="模板 id（未指定且 stdin 是 TTY 时弹出交互式选择；非 TTY 用 helloworld）",
    )
    p.add_argument("--list", action="store_true", help="列出所有可用模板后退出")
    p.add_argument(
        "--directory",
        default=None,
        help="项目父目录（默认当前目录），项目创建在 <directory>/<project_name>",
    )
    p.add_argument(
        "--description",
        default="",
        help="项目描述（写入 pyproject.toml 的 description 字段）",
    )
    p.add_argument(
        "--python-version",
        default=None,
        metavar="X.Y",
        help="指定目标 Python 版本（如 3.8、3.10），覆盖模板默认 requires-python 下限",
    )


__all__ = ["_add_init_subparser"]
