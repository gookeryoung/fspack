"""fspack CLI 入口 —— cargo 风格短命令（fsp b/c/r）.

顶部仅导入轻量标准库（argparse/logging/pathlib/os/sys）与 ``__version__``。
重模块（``fspack.config``/``fspack.console``/``fspack.platform``）延迟到
实际使用时导入，使 ``fsp --help`` 无需加载 config/console 即可输出帮助。
``--mirror`` 刻意不做 argparse choices 校验（choices 会在 parser 构建期
触发 config 导入），改由 :func:`_resolve_mirror` 在执行期校验。

parser 构建代码拆分到 :mod:`fspack.cli_parser`（argparse 声明集中维护），
本模块聚焦 ``main``/dispatch；``build_parser`` 经 re-export 保持既有引用
（测试 ``fspack.cli.build_parser``）兼容。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fspack.cli_parser import build_parser

if TYPE_CHECKING:
    from fspack.config import MirrorConfig
    from fspack.platform import Platform

__all__ = ["build_parser", "discover_subprojects", "main"]

_logger = logging.getLogger(__name__)


# 递归扫描子项目时跳过的目录名。
# 与 analyzer._EXCLUDED_DIRS 语义不同：本集合用于"找 pyproject.toml"（不进入
# .venv/dist/build 等），_EXCLUDED_DIRS 用于"扫描 .py/.qml 源码"（额外排除
# examples/tests/docs/templates 等开发期目录）。两者各自独立维护，不强行合并
# 以避免语义混淆。
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

    if command in ("doctor", "d"):
        _run_doctor(ns)
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


def _resolve_mirror(value: str | None) -> MirrorConfig:
    """执行期解析 ``--mirror``：非法值报错并以退出码 2 退出（与 argparse 一致）.

    choices 校验刻意从 parser 构建期移到执行期，避免 ``fsp --help`` 等
    轻命令为 choices 加载 ``fspack.config``（~20ms）。
    """
    from fspack.config import get_mirror

    try:
        return get_mirror(value)
    except KeyError as exc:
        from fspack.console import console

        console.error(exc.args[0])
        raise SystemExit(2) from None


def _run_build(project: Path, ns: argparse.Namespace) -> None:
    """执行单项目 build 子命令."""
    from dataclasses import replace
    from pathlib import Path

    from fspack.builder import build
    from fspack.config import ProjectInfo, build_options_from_defaults
    from fspack.exceptions import ProjectError
    from fspack.packaging.log_file import LogFormat

    # 合并 [tool.fspack] 构建默认值与 CLI 标志：
    # - 先用 build_options_from_defaults 构造配置层 base（config or BuildOptions 默认值）
    # - 再用 replace() 应用 CLI 覆盖：布尔开关用 ``cli or base``（任一启用 → 启用），
    #   pyc_optimize 用 ``cli if cli is not None else base``（CLI 显式指定优先）
    info = ProjectInfo.from_dir(project, ns.py_version)
    base = build_options_from_defaults(info.build_defaults)
    # extras 合并：CLI --extra 指定时完全覆盖配置默认（集合语义，非合并）；
    # 未指定时用 [tool.fspack] extras 配置默认。校验未知分组名。
    cli_extras = ns.extras if getattr(ns, "extras", None) else None
    enabled_extras = frozenset(cli_extras) if cli_extras is not None else base.extras
    unknown = enabled_extras - set(info.optional_dependencies)
    if unknown:
        raise ProjectError(f"未知的 extras 分组: {sorted(unknown)}，可选: {sorted(info.optional_dependencies)}")
    options = replace(
        base,
        keep_modules=set(ns.keep_modules) if ns.keep_modules else base.keep_modules,
        icon=Path(ns.icon).resolve() if ns.icon else base.icon,
        no_stdlib_trim=ns.no_stdlib_trim or base.no_stdlib_trim,
        no_slim_runtime=ns.no_slim_runtime or base.no_slim_runtime,
        no_pyc=ns.no_pyc or base.no_pyc,
        pyc_strip=ns.pyc_strip or base.pyc_strip,
        pyc_optimize=ns.pyc_optimize if ns.pyc_optimize is not None else base.pyc_optimize,
        no_site=ns.no_site or base.no_site,
        nuitka=ns.nuitka or base.nuitka,
        ccache=ns.ccache or base.ccache,
        nuitka_packages=tuple(dict.fromkeys((*base.nuitka_packages, *(ns.nuitka_pkg or [])))),
        no_size_report=ns.no_size_report or base.no_size_report,
        analyze_deps=ns.analyze_deps or base.analyze_deps,
        extras=enabled_extras,
        lazy_imports=_parse_lazy_imports(ns.lazy_imports, base.lazy_imports),
        require_hashes=ns.require_hashes or base.require_hashes,
        no_sbom=ns.no_sbom or base.no_sbom,
    )
    log_file = Path(ns.log_file).resolve() if ns.log_file else None
    log_format = LogFormat.parse(ns.log_format)
    build(
        project,
        _resolve_mirror(ns.mirror),
        ns.py_version,
        target=_parse_target(ns.target),
        options=options,
        extra_index_urls=tuple(ns.extra_index_urls or ()),
        find_links=tuple(ns.find_links or ()),
        dry_run=ns.dry_run,
        log_file=log_file,
        log_format=log_format,
        profile=ns.profile,
    )


def _run_package(project: Path, ns: argparse.Namespace) -> None:
    """执行单项目 package 子命令."""
    from pathlib import Path as _Path

    from fspack.config import ProjectInfo
    from fspack.exceptions import ProjectError
    from fspack.packaging.installer import build_release

    # extras 校验：CLI --extra 指定时校验未知分组（与 build 子命令一致）
    # 仅校验，不构造 BuildOptions——build_release 内部 _prepare_dist 用配置默认，
    # CLI 指定时通过 extras 参数透传覆盖配置默认
    cli_extras = ns.extras if getattr(ns, "extras", None) else None
    if cli_extras:
        info = ProjectInfo.from_dir(project, ns.py_version)
        unknown = set(cli_extras) - set(info.optional_dependencies)
        if unknown:
            raise ProjectError(f"未知的 extras 分组: {sorted(unknown)}，可选: {sorted(info.optional_dependencies)}")

    # 安全加固签名：CLI 优先合并配置默认（与 extras 不同，签名证书/密钥
    # 用 CLI 优先 + 配置回退语义，避免 --sign-exe 显式开关与配置证书路径分离）
    info_for_sign = ProjectInfo.from_dir(project, ns.py_version) if (ns.sign_exe or ns.sign_deb) else None
    cfg_cert = info_for_sign.build_defaults.sign_exe_certificate if info_for_sign else None
    sign_exe_cert = (
        _Path(ns.sign_exe_certificate).resolve()
        if ns.sign_exe_certificate
        else (_Path(cfg_cert).resolve() if cfg_cert else None)
    )
    sign_exe_pwd = (
        ns.sign_exe_password
        if ns.sign_exe_password is not None
        else (info_for_sign.build_defaults.sign_exe_password if info_for_sign else None)
    )
    sign_deb_key = (
        ns.sign_deb_key
        if ns.sign_deb_key is not None
        else (info_for_sign.build_defaults.sign_deb_key if info_for_sign else None)
    )
    outputs = build_release(
        project,
        _resolve_mirror(ns.mirror),
        ns.py_version,
        no_build=ns.no_build,
        target=_parse_target(ns.target),
        fmt=ns.format,
        codesign=ns.codesign,
        extras=cli_extras,
        sign_exe=ns.sign_exe,
        sign_exe_certificate=sign_exe_cert,
        sign_exe_password=sign_exe_pwd,
        sign_deb=ns.sign_deb,
        sign_deb_key=sign_deb_key,
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
            python_version=ns.python_version,
        )
    except ValueError as exc:
        from fspack.console import console

        console.error(str(exc))
        sys.exit(1)


def _run_doctor(ns: argparse.Namespace) -> None:
    """执行 doctor 子命令：环境诊断 + 可选的模板构建测试/性能基准.

    无 ``--test``/``--bench`` 时仅执行环境诊断，输出三色诊断报告。
    ``--test`` 运行所有模板构建并打印汇总结果。``--bench`` 额外收集
    性能数据并输出性能分析报告。
    """
    from fspack.cli_doctor import print_doctor_report, run_doctor, run_doctor_bench, run_doctor_test

    report = run_doctor()
    print_doctor_report(report)

    if getattr(ns, "bench", False):
        run_doctor_bench()
    elif getattr(ns, "test", False):
        run_doctor_test()


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
    if value == "macos":
        return Platform.MACOS
    return Platform.LINUX


def _parse_lazy_imports(cli_value: str | None, base: tuple[str, ...]) -> tuple[str, ...]:
    """解析 ``--lazy-import`` 逗号分隔字符串为模块名元组.

    ``cli_value`` 为 None 时用配置默认 ``base``；为空字符串时返回空元组（用户
    显式清除）；非空时按逗号分割并去空白、去重。CLI 完全覆盖配置默认（与 extras
    语义一致，非合并）。
    """
    if cli_value is None:
        return base
    parts = [s.strip() for s in cli_value.split(",") if s.strip()]
    return tuple(dict.fromkeys(parts))


if __name__ == "__main__":
    main()
