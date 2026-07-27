"""fspack CLI 入口 —— cargo 风格短命令（fsp b/c/r）.

顶部仅导入轻量标准库（argparse/logging/pathlib）与 ``__version__``。
重模块（``fspack.config``/``fspack.console``/``fspack.platform``）延迟到
实际使用时导入，使 ``fsp --help`` 无需加载 config/console 即可输出帮助。
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fspack import __version__

if TYPE_CHECKING:
    from fspack.platform import Platform

__all__ = ["build_parser", "main"]

_logger = logging.getLogger(__name__)


def _mirrors_choices() -> list[str]:
    """延迟导入 ``MIRRORS`` 避免 ``fsp --help`` 加载完整 config 模块.

    ``fspack.config`` 顶层导入会触发 ``config.models``/``config.parsing`` 加载
    （~10ms），仅 build/package 子命令需要 ``MIRRORS`` 作为 choices。
    """
    from fspack.config import MIRRORS

    return list(MIRRORS)


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器."""
    parser = argparse.ArgumentParser(
        prog="fspack",
        description="极速 Python 打包器（cargo 风格短命令）。",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示 DEBUG 级别日志")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    _add_build_subparser(sub)
    _add_run_subparser(sub)
    _add_clean_subparser(sub)
    _add_package_subparser(sub)
    return parser


def _add_build_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 build/b 子命令：打包项目."""
    p = sub.add_parser("build", aliases=["b"], help="打包项目")
    p.add_argument("project", nargs="?", default=".", help="项目目录（默认当前目录）")
    p.add_argument("--mirror", default=None, choices=_mirrors_choices(), help="镜像源")
    p.add_argument("--py-version", default=None, help="embed python 版本，如 3.11.9")
    p.add_argument("--target", default=None, choices=["windows", "linux"], help="目标平台（默认当前平台）")
    p.add_argument(
        "--keep-module",
        action="append",
        default=[],
        dest="keep_modules",
        help="显式保留子模块（如 PySide2.QtGui），可重复指定",
    )
    p.add_argument(
        "--icon",
        default=None,
        help=(
            "exe 图标文件路径（.ico/.png/.jpg 等），覆盖 [tool.fspack] icon；"
            "未指定时按 [tool.fspack] icon > 自动搜索 favicon.* > 默认 app.ico 解析"
        ),
    )
    p.add_argument(
        "--no-stdlib-trim",
        action="store_true",
        help="关闭标准库精简（默认剥离 Linux standalone 的 test/ensurepip/idlelib 等无用模块）",
    )
    p.add_argument(
        "--no-pyc",
        action="store_true",
        help="关闭字节码预编译（默认预编译 src+site-packages 为 .pyc 加速首次启动）",
    )
    p.add_argument(
        "--pyc-strip",
        action="store_true",
        help="剥离非 __init__.py 的 .py 源码（仅保留 .pyc，需配合预编译；保留包标识避免命名空间包问题）",
    )
    p.add_argument(
        "--pyc-optimize",
        type=int,
        default=None,
        choices=[0, 1, 2],
        help=(
            "字节码优化级别：0=保留 docstring/assert，1=剥离 assert，"
            "2=剥离 assert+docstring（-OO，体积减 5-15%%，启动提速 5-10%%，默认 2）"
        ),
    )
    p.add_argument(
        "--no-site",
        action="store_true",
        help="禁用 site.py 加载（_pth 省略 import site 行，节省 ~20-30ms 启动时间）",
    )
    p.add_argument(
        "--nuitka",
        action="store_true",
        help=(
            "启用 Nuitka 编译模式：用户源码编译为 .pyd 本机执行（速度提升 30-50%%）。"
            "Nuitka 自动装到本地缓存 ~/.fspack/cache/nuitka/，不污染 dist/runtime；交叉构建自动跳过；默认关闭"
        ),
    )
    p.add_argument(
        "--ccache",
        action="store_true",
        help=(
            "Nuitka 编译启用 ccache 缓存：首次下载 ccache 到 ~/.fspack/cache/ccache/，"
            "后续构建缓存 gcc 编译结果加速重复编译。需配合 --nuitka 使用；默认关闭"
        ),
    )
    p.add_argument(
        "--nuitka-pkg",
        action="append",
        default=None,
        metavar="PACKAGE",
        dest="nuitka_pkg",
        help=(
            "指定第三方依赖包名用 Nuitka 编译为 .pyd（可多次指定）。"
            "需配合 --nuitka 使用；编译 site-packages/<package>/ 下 .py 为 .pyd，"
            "编译成功删除 .py，失败保留回退 .pyc。风险由用户承担（动态导入/元编程可能不兼容）"
        ),
    )
    p.add_argument(
        "--extra-index-url",
        action="append",
        default=None,
        metavar="URL",
        dest="extra_index_urls",
        help=(
            "额外 PyPI 索引 URL（私有 PyPI 服务器，可多次指定），透传给 pip/uv 的 --extra-index-url。"
            "与 [tool.fspack] extra-index-urls 合并（CLI 追加在配置之后，去重保留首次出现）"
        ),
    )
    p.add_argument(
        "--find-links",
        action="append",
        default=None,
        metavar="PATH_OR_URL",
        dest="find_links",
        help=(
            "本地 wheel 目录或远程 wheel 索引页（可多次指定），透传给 pip/uv 的 --find-links。"
            "与 [tool.fspack] find-links 合并（CLI 追加在配置之后，去重保留首次出现）"
        ),
    )


def _add_run_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 run/r 子命令：运行已打包项目."""
    p = sub.add_parser("run", aliases=["r"], help="运行已打包项目")
    p.add_argument("project", nargs="?", default=".", help="项目目录")
    p.add_argument("rest", nargs="*", default=[], help="透传给目标程序的参数（以 -- 分隔）")
    p.add_argument("--debug", action="store_true", help="用 embed python 直跑入口脚本（绕过 GUI loader，输出可见）")
    p.add_argument(
        "--entry",
        default=None,
        help="多入口项目指定要运行的入口名（与 [tool.fspack.entries] 键匹配）",
    )


def _add_clean_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 clean/c 子命令：清理 dist/."""
    p = sub.add_parser("clean", aliases=["c"], help="清理 dist/")
    p.add_argument("project", nargs="?", default=".", help="项目目录")


def _add_package_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 package/p 子命令：生成发行包."""
    p = sub.add_parser("package", aliases=["p"], help="生成发行包")
    p.add_argument("project", nargs="?", default=".", help="项目目录")
    p.add_argument("--mirror", default=None, choices=_mirrors_choices(), help="镜像源")
    p.add_argument("--py-version", default=None, help="embed python 版本，如 3.11.9")
    p.add_argument("--target", default=None, choices=["windows", "linux"], help="目标平台（默认当前平台）")
    p.add_argument(
        "--no-build",
        action="store_true",
        help="不自动构建，dist 缺失时报错（默认 dist 存在则复用，避免 fsp b 后 fsp p 重复构建）",
    )
    p.add_argument(
        "--format",
        default="auto",
        choices=["auto", "zip", "nsis", "tar.gz", "deb", "all"],
        help=(
            "发行包格式：auto=平台默认（Win=nsis，Linux=tar.gz+deb），"
            "zip=跨平台便携包，nsis=Windows 安装包，tar.gz/deb=Linux，all=平台全部"
        ),
    )


def main(argv: list[str] | None = None) -> None:
    """主入口，解析参数并分发到子命令."""
    parser = build_parser()
    ns = parser.parse_args(argv)
    command = ns.command
    if command is None:
        parser.print_help()
        return

    # console 延迟导入：仅在实际执行子命令时加载 rich（~17ms）
    from fspack.console import console

    console.setup_logging(verbose=ns.verbose)

    project = Path(ns.project).resolve()
    if command in ("build", "b"):
        from dataclasses import replace

        from fspack.builder import build
        from fspack.config import ProjectInfo, build_options_from_defaults, get_mirror

        # 合并 [tool.fspack] 构建默认值与 CLI 标志：
        # - 先用 build_options_from_defaults 构造配置层 base（config or BuildOptions 默认值）
        # - 再用 replace() 应用 CLI 覆盖：布尔开关用 ``cli or base``（任一启用 → 启用），
        #   pyc_optimize 用 ``cli if cli is not None else base``（CLI 显式指定优先）
        info = ProjectInfo.from_dir(project, ns.py_version)
        base = build_options_from_defaults(info.build_defaults)
        options = replace(
            base,
            keep_modules=set(ns.keep_modules) if ns.keep_modules else base.keep_modules,
            icon=Path(ns.icon).resolve() if ns.icon else base.icon,
            no_stdlib_trim=ns.no_stdlib_trim or base.no_stdlib_trim,
            no_pyc=ns.no_pyc or base.no_pyc,
            pyc_strip=ns.pyc_strip or base.pyc_strip,
            pyc_optimize=ns.pyc_optimize if ns.pyc_optimize is not None else base.pyc_optimize,
            no_site=ns.no_site or base.no_site,
            nuitka=ns.nuitka or base.nuitka,
            ccache=ns.ccache or base.ccache,
            nuitka_packages=tuple(dict.fromkeys((*base.nuitka_packages, *(ns.nuitka_pkg or [])))),
        )
        build(
            project,
            get_mirror(ns.mirror),
            ns.py_version,
            target=_parse_target(ns.target),
            options=options,
            extra_index_urls=tuple(ns.extra_index_urls or ()),
            find_links=tuple(ns.find_links or ()),
        )
    elif command in ("run", "r"):
        from fspack.runner import run as run_cmd

        run_cmd(project, rest_args=_drop_separator(ns.rest), debug=ns.debug, entry=ns.entry)
    elif command in ("clean", "c"):
        from fspack.builder import clean_dist

        clean_dist(project)
    elif command in ("package", "p"):
        from fspack.config import get_mirror
        from fspack.packaging.installer import build_release

        outputs = build_release(
            project,
            get_mirror(ns.mirror),
            ns.py_version,
            no_build=ns.no_build,
            target=_parse_target(ns.target),
            fmt=ns.format,
        )
        for out in outputs:
            _logger.info("发行包已生成: %s", out)


def _drop_separator(rest: list[str]) -> list[str]:
    """剔除 argparse REMAINDER 捕获的首个 -- 分隔符."""
    if rest and rest[0] == "--":
        return rest[1:]
    return rest


def _parse_target(value: str | None) -> Platform | None:
    """将 CLI 字符串转为 Platform 枚举，None 表示用当前平台."""
    if value is None:
        return None
    from fspack.platform import Platform

    if value == "windows":
        return Platform.WINDOWS
    return Platform.LINUX


if __name__ == "__main__":
    main()
