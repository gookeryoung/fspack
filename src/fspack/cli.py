"""fspack CLI 入口 —— cargo 风格短命令（fsp b/c/r）。."""

from __future__ import annotations

import argparse
from pathlib import Path

from fspack import __version__
from fspack.commands import build as build_cmd
from fspack.commands import clean as clean_cmd
from fspack.commands import package as package_cmd
from fspack.commands import run as run_cmd
from fspack.mirror import MIRRORS
from fspack.platform import Platform

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器。."""
    parser = argparse.ArgumentParser(
        prog="fspack",
        description="极速 Python 打包器（cargo 风格短命令）。",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_build = sub.add_parser("build", aliases=["b"], help="打包项目")
    p_build.add_argument("project", nargs="?", default=".", help="项目目录（默认当前目录）")
    p_build.add_argument("--mirror", default=None, choices=list(MIRRORS), help="镜像源")
    p_build.add_argument("--py-version", default=None, help="embed python 版本，如 3.11.9")
    p_build.add_argument("--target", default=None, choices=["windows", "linux"], help="目标平台（默认当前平台）")

    p_run = sub.add_parser("run", aliases=["r"], help="运行已打包项目")
    p_run.add_argument("project", nargs="?", default=".", help="项目目录")
    p_run.add_argument("rest", nargs=argparse.REMAINDER, default=[], help="透传给目标程序的参数（以 -- 分隔）")

    p_clean = sub.add_parser("clean", aliases=["c"], help="清理 dist/")
    p_clean.add_argument("project", nargs="?", default=".", help="项目目录")

    p_pkg = sub.add_parser("package", aliases=["p"], help="生成安装包")
    p_pkg.add_argument("project", nargs="?", default=".", help="项目目录")
    p_pkg.add_argument("--mirror", default=None, choices=list(MIRRORS), help="镜像源")
    p_pkg.add_argument("--py-version", default=None, help="embed python 版本，如 3.11.9")
    p_pkg.add_argument("--target", default=None, choices=["windows", "linux"], help="目标平台（默认当前平台）")
    p_pkg.add_argument("--no-build", action="store_true", help="跳过重建，直接打包已有 dist")
    return parser


def main(argv: list[str] | None = None) -> None:
    """主入口，解析参数并分发到子命令。."""
    parser = build_parser()
    ns = parser.parse_args(argv)
    command = ns.command
    if command is None:
        parser.print_help()
        return

    project = Path(ns.project).resolve()
    if command in ("build", "b"):
        build_cmd.run(project, mirror=ns.mirror, py_version=ns.py_version, target=_parse_target(ns.target))
    elif command in ("run", "r"):
        run_cmd.run(project, rest_args=_drop_separator(ns.rest))
    elif command in ("clean", "c"):
        clean_cmd.run(project)
    elif command in ("package", "p"):
        package_cmd.run(
            project, mirror=ns.mirror, py_version=ns.py_version, no_build=ns.no_build, target=_parse_target(ns.target)
        )


def _drop_separator(rest: list[str]) -> list[str]:
    """剔除 argparse REMAINDER 捕获的首个 -- 分隔符。."""
    if rest and rest[0] == "--":
        return rest[1:]
    return rest


def _parse_target(value: str | None) -> Platform | None:
    """将 CLI 字符串转为 Platform 枚举，None 表示用当前平台。."""
    if value is None:
        return None
    if value == "windows":
        return Platform.WINDOWS
    return Platform.LINUX


if __name__ == "__main__":
    main()
