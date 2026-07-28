"""fspack CLI 入口 —— cargo 风格短命令（fsp b/c/r）.

顶部仅导入轻量标准库（argparse/logging/pathlib/os/sys）与 ``__version__``。
重模块（``fspack.config``/``fspack.console``/``fspack.platform``）延迟到
实际使用时导入，使 ``fsp --help`` 无需加载 config/console 即可输出帮助。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fspack import __version__

if TYPE_CHECKING:
    from fspack.platform import Platform

__all__ = ["build_parser", "discover_subprojects", "main"]

_logger = logging.getLogger(__name__)


# 递归扫描子项目时跳过的目录名（与 analyzer._EXCLUDED_DIRS 共用语义）。
# 这些目录下的 pyproject.toml 不应被视为可打包项目（如 .venv 内的 pip
# 包含 pyproject.toml；dist 内是已构建产物；build 是临时构建目录）。
_RECURSIVE_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "dist",
        "build",
        ".git",
        "__pycache__",
        ".venv",
        ".tox",
        ".fspack",
        ".trae",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".pyrefly_cache",
        ".uv-cache",
        "htmlcov",
        "node_modules",
    }
)


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
    _add_init_subparser(sub)
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
    p.add_argument(
        "-R",
        "--recursive",
        action="store_true",
        help=(
            "递归扫描 project 目录下所有含 pyproject.toml 的子项目，依次构建。"
            "跳过 .venv/dist/build/.git 等开发期目录；单项目失败不中断，"
            "最后汇总成功/失败列表"
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
    p.add_argument(
        "-R",
        "--recursive",
        action="store_true",
        help=(
            "递归扫描 project 目录下所有含 pyproject.toml 的子项目，依次打包。"
            "跳过 .venv/dist/build/.git 等开发期目录；单项目失败不中断，"
            "最后汇总成功/失败列表"
        ),
    )


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

    if command in ("init", "i"):
        _run_init(ns)
        return

    project = Path(ns.project).resolve()
    if command in ("build", "b"):
        if ns.recursive:
            sys.exit(_run_recursive(project, "build", ns))
        _run_build(project, ns)
    elif command in ("run", "r"):
        from fspack.runner import run as run_cmd

        run_cmd(project, rest_args=_drop_separator(ns.rest), debug=ns.debug, entry=ns.entry)
    elif command in ("clean", "c"):
        from fspack.builder import clean_dist

        clean_dist(project)
    elif command in ("package", "p"):
        if ns.recursive:
            sys.exit(_run_recursive(project, "package", ns))
        _run_package(project, ns)


def _run_build(project: Path, ns: argparse.Namespace) -> None:
    """执行单项目 build 子命令."""
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


def _run_package(project: Path, ns: argparse.Namespace) -> None:
    """执行单项目 package 子命令."""
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


def _run_init(ns: argparse.Namespace) -> None:
    """执行 init/i 子命令：从模板创建新项目.

    分发逻辑：

    - ``--list`` → 打印模板列表后退出
    - ``--template`` 显式指定 → 用指定模板
    - ``--template`` 未指定 → 调 :func:`prompt_template_selection` 交互式选择
      （非 TTY 环境自动回退到 helloworld）
    """
    from fspack.cli_init import init_project, print_template_list, prompt_template_selection

    if ns.list:
        print_template_list()
        return

    if not ns.project_name:
        # 未指定项目名且未 --list：用当前目录名作为项目名
        ns.project_name = Path.cwd().name
        _logger.info("未指定项目名，使用当前目录名: %s", ns.project_name)

    template_id = ns.template
    if template_id is None:
        # 未指定 --template：交互式选择（非 TTY 自动回退 helloworld）
        try:
            template_id = prompt_template_selection()
        except KeyboardInterrupt:
            from fspack.console import console

            console.rich.print("\n[yellow]已取消[/]")
            sys.exit(1)

    directory = Path(ns.directory).resolve() if ns.directory else None
    try:
        init_project(
            ns.project_name,
            template_id=template_id,
            directory=directory,
            description=ns.description,
        )
    except ValueError as exc:
        from fspack.console import console

        console.error(str(exc))
        sys.exit(1)


def discover_subprojects(root: Path) -> list[Path]:
    """递归扫描 root 目录下所有含 ``pyproject.toml`` 的子项目（含 root 自身）.

    跳过 :data:`_RECURSIVE_SKIP_DIRS` 中的开发期目录（如 ``.venv``/``dist``/
    ``build``/``.git``），避免误识别这些目录下的 ``pyproject.toml``（如
    ``.venv`` 内 pip 的 ``pyproject.toml``）。

    返回按路径字母序排序的子项目目录列表（含 root 自身，若 root 含
    ``pyproject.toml``），便于稳定输出与可重复构建。

    用 :func:`os.scandir` 递归遍历，``DirEntry`` 复用枚举时的 stat 缓存
    减少 stat 调用。子目录递归前先检查目录名是否在跳过集合中。
    """
    projects: list[Path] = []
    seen: set[Path] = set()

    def _scan(current: Path) -> None:
        # 多次调用同一目录去重（理论不会，但兜底防御符号链接循环）
        try:
            real = current.resolve()
        except OSError:
            return
        if real in seen:
            return
        seen.add(real)

        if (current / "pyproject.toml").is_file():
            projects.append(current)

        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in _RECURSIVE_SKIP_DIRS or entry.name.endswith(".egg-info"):
                continue
            _scan(Path(entry.path))

    _scan(root)
    return projects


def _run_recursive(root: Path, action: str, ns: argparse.Namespace) -> int:
    """递归执行 build/package 子命令，返回退出码（0=全部成功，1=有失败）.

    扫描 root 下所有子项目，依次调用 ``_run_build`` 或 ``_run_package``。
    单项目失败时打印错误并继续，最后汇总成功/失败列表。

    输出格式示例::

        > 递归扫描子项目：发现 3 个
        > [1/3] 构建 app1 ...
        √ app1 构建成功
        > [2/3] 构建 app2 ...
        × app2 构建失败: <错误>
        > [3/3] 构建 app3 ...
        √ app3 构建成功

        递归构建汇总：成功 2，失败 1
        失败项目：
          - app2: <错误摘要>

    退出码：全部成功返回 0，任一失败返回 1（便于 CI 检测）。
    """
    from fspack.console import console

    projects = discover_subprojects(root)
    total = len(projects)
    if total == 0:
        console.warn(f"未在 {root} 下发现含 pyproject.toml 的子项目")
        return 0

    console.step(f"递归扫描子项目：发现 {total} 个")
    succeeded: list[tuple[Path, str]] = []  # (project, summary)
    failed: list[tuple[Path, str]] = []  # (project, error_message)

    action_verb = "构建" if action == "build" else "打包"

    for index, project in enumerate(projects, 1):
        rel = _format_project_path(project, root)
        console.step(f"[{index}/{total}] {action_verb} {rel} ...")
        try:
            if action == "build":
                _run_build(project, ns)
            else:
                _run_package(project, ns)
            succeeded.append((project, rel))
            console.success(f"{rel} {action_verb}成功")
        except SystemExit:
            # _run_build/_run_package 不主动调 sys.exit，但防御性捕获
            raise
        except BaseException as exc:
            err_msg = _format_error(exc)
            failed.append((project, f"{rel}: {err_msg}"))
            console.error(f"{rel} {action_verb}失败: {err_msg}")
            _logger.debug("%s 完整错误堆栈", rel, exc_info=exc)

    _print_recursive_summary(action_verb, succeeded, failed)
    return 1 if failed else 0


def _format_project_path(project: Path, root: Path) -> str:
    """格式化子项目路径用于显示：相对 root 的路径，root 自身显示为 '.'."""
    try:
        rel = project.relative_to(root)
    except ValueError:
        return str(project)
    return "." if str(rel) == "." else rel.as_posix()


def _format_error(exc: BaseException) -> str:
    """格式化异常为单行错误消息（去除换行，截断超长消息）."""
    msg = str(exc).strip() or exc.__class__.__name__
    # 取首行，避免多行错误消息破坏汇总表
    first_line = msg.splitlines()[0]
    if len(first_line) > 200:
        first_line = first_line[:197] + "..."
    return first_line


def _print_recursive_summary(
    action_verb: str, succeeded: list[tuple[Path, str]], failed: list[tuple[Path, str]]
) -> None:
    """打印递归执行汇总：成功数、失败数、失败项目列表."""
    from fspack.console import console

    console.rich.print()  # 空行分隔
    console.step(f"递归{action_verb}汇总：成功 {len(succeeded)}，失败 {len(failed)}")
    if failed:
        console.error("失败项目：")
        for _project, msg in failed:
            console.rich.print(f"  - {msg}")
    elif succeeded:
        console.success(f"全部 {len(succeeded)} 个子项目{action_verb}成功")


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
