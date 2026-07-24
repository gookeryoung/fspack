"""fspack CLI 入口 —— cargo 风格短命令（fsp b/c/r）."""

from __future__ import annotations

import argparse
from pathlib import Path

from fspack import __version__
from fspack.config import MIRRORS
from fspack.console import console
from fspack.platform import Platform

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器."""
    parser = argparse.ArgumentParser(
        prog="fspack",
        description="极速 Python 打包器（cargo 风格短命令）。",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示 DEBUG 级别日志")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_build = sub.add_parser("build", aliases=["b"], help="打包项目")
    p_build.add_argument("project", nargs="?", default=".", help="项目目录（默认当前目录）")
    p_build.add_argument("--mirror", default=None, choices=list(MIRRORS), help="镜像源")
    p_build.add_argument("--py-version", default=None, help="embed python 版本，如 3.11.9")
    p_build.add_argument("--target", default=None, choices=["windows", "linux"], help="目标平台（默认当前平台）")
    p_build.add_argument(
        "--keep-module",
        action="append",
        default=[],
        dest="keep_modules",
        help="显式保留子模块（如 PySide2.QtGui），可重复指定",
    )
    p_build.add_argument(
        "--icon",
        default=None,
        help=(
            "exe 图标文件路径（.ico/.png/.jpg 等），覆盖 [tool.fspack] icon；"
            "未指定时按 [tool.fspack] icon > 自动搜索 favicon.* > 默认 app.ico 解析"
        ),
    )
    p_build.add_argument(
        "--no-stdlib-trim",
        action="store_true",
        help="关闭标准库精简（默认剥离 Linux standalone 的 test/ensurepip/idlelib 等无用模块）",
    )
    p_build.add_argument(
        "--no-pyc",
        action="store_true",
        help="关闭字节码预编译（默认预编译 src+site-packages 为 .pyc 加速首次启动）",
    )
    p_build.add_argument(
        "--pyc-strip",
        action="store_true",
        help="剥离非 __init__.py 的 .py 源码（仅保留 .pyc，需配合预编译；保留包标识避免命名空间包问题）",
    )

    p_run = sub.add_parser("run", aliases=["r"], help="运行已打包项目")
    p_run.add_argument("project", nargs="?", default=".", help="项目目录")
    p_run.add_argument("rest", nargs="*", default=[], help="透传给目标程序的参数（以 -- 分隔）")
    p_run.add_argument("--debug", action="store_true", help="用 embed python 直跑入口脚本（绕过 GUI loader，输出可见）")
    p_run.add_argument(
        "--entry",
        default=None,
        help="多入口项目指定要运行的入口名（与 [tool.fspack.entries] 键匹配）",
    )

    p_clean = sub.add_parser("clean", aliases=["c"], help="清理 dist/")
    p_clean.add_argument("project", nargs="?", default=".", help="项目目录")

    p_pkg = sub.add_parser("package", aliases=["p"], help="生成发行包")
    p_pkg.add_argument("project", nargs="?", default=".", help="项目目录")
    p_pkg.add_argument("--mirror", default=None, choices=list(MIRRORS), help="镜像源")
    p_pkg.add_argument("--py-version", default=None, help="embed python 版本，如 3.11.9")
    p_pkg.add_argument("--target", default=None, choices=["windows", "linux"], help="目标平台（默认当前平台）")
    p_pkg.add_argument("--no-build", action="store_true", help="跳过重建，直接打包已有 dist")
    p_pkg.add_argument(
        "--format",
        default="auto",
        choices=["auto", "zip", "nsis", "tar.gz", "deb", "all"],
        help=(
            "发行包格式：auto=平台默认（Win=nsis，Linux=tar.gz+deb），"
            "zip=跨平台便携包，nsis=Windows 安装包，tar.gz/deb=Linux，all=平台全部"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """主入口，解析参数并分发到子命令."""
    parser = build_parser()
    ns = parser.parse_args(argv)
    command = ns.command
    if command is None:
        parser.print_help()
        return

    console.setup_logging(verbose=ns.verbose)

    project = Path(ns.project).resolve()
    if command in ("build", "b"):
        from fspack.commands import build as build_cmd

        build_cmd.run(
            project,
            mirror=ns.mirror,
            py_version=ns.py_version,
            target=_parse_target(ns.target),
            keep_modules=set(ns.keep_modules) if ns.keep_modules else None,
            icon=Path(ns.icon).resolve() if ns.icon else None,
            no_stdlib_trim=ns.no_stdlib_trim,
            no_pyc=ns.no_pyc,
            pyc_strip=ns.pyc_strip,
        )
    elif command in ("run", "r"):
        from fspack.commands import run as run_cmd

        run_cmd.run(project, rest_args=_drop_separator(ns.rest), debug=ns.debug, entry=ns.entry)
    elif command in ("clean", "c"):
        from fspack.commands import clean as clean_cmd

        clean_cmd.run(project)
    elif command in ("package", "p"):
        from fspack.commands import package as package_cmd

        package_cmd.run(
            project,
            mirror=ns.mirror,
            py_version=ns.py_version,
            no_build=ns.no_build,
            target=_parse_target(ns.target),
            fmt=ns.format,
        )


def _drop_separator(rest: list[str]) -> list[str]:
    """剔除 argparse REMAINDER 捕获的首个 -- 分隔符."""
    if rest and rest[0] == "--":
        return rest[1:]
    return rest


def _parse_target(value: str | None) -> Platform | None:
    """将 CLI 字符串转为 Platform 枚举，None 表示用当前平台."""
    if value is None:
        return None
    if value == "windows":
        return Platform.WINDOWS
    return Platform.LINUX


if __name__ == "__main__":
    main()
