"""fspack CLI 入口 —— cargo 风格短命令（fsp b/c/r）.

顶部仅导入轻量标准库（argparse/pathlib/os/sys）与 ``__version__``。
重模块（``fspack.config``/``fspack.console``/``fspack.platform``）延迟到
实际使用时导入，使 ``fsp --help`` 无需加载 config/console 即可输出帮助；
``logging`` 同样延迟到使用点（console 顶层自会导入，``--help`` 类轻路径
可整段省去 logging 导入链，实测约 5-6ms）。
``--mirror`` 刻意不做 argparse choices 校验（choices 会在 parser 构建期
触发 config 导入），改由 :func:`_resolve_mirror` 在执行期校验。

parser 构建代码拆分到 :mod:`fspack.cli_parser`（argparse 声明集中维护），
本模块聚焦 ``main``/dispatch；``build_parser`` 经 re-export 保持既有引用
（测试 ``fspack.cli.build_parser``）兼容。manifest 子命令的执行逻辑
（``_run_manifest``/``_run_generate``/``_run_diff``）也在本模块，参数声明见
:mod:`fspack.cli_parser`。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fspack import __version__
from fspack.cli_parser import build_parser
from fspack.exceptions import FspackError

if TYPE_CHECKING:
    from fspack.config import MirrorConfig
    from fspack.platform import Platform

__all__ = ["build_parser", "discover_subprojects", "main"]


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


def _help_snapshot_usable() -> bool:
    """静态 help 快照是否可安全替代 argparse 输出.

    三个条件缺一不可：

    - 解释器次版本与快照生成版本一致（argparse 的 section 标题/换行随版本变化）
    - 未设置 ``COLUMNS``（argparse 会按该值换行，快照固定 80 列）
    - stdout 非 TTY（交互式终端按实际宽度换行，交给 argparse 保证观感）
    """
    from fspack.cli_help import HELP_SNAPSHOT_PY

    return sys.version_info[:2] == HELP_SNAPSHOT_PY and not os.environ.get("COLUMNS") and not sys.stdout.isatty()


def _try_fast_path(argv: list[str]) -> bool:
    """``-V/--version``、``-h/--help`` 单参数快速路径，命中即打印并 ``SystemExit(0)``.

    argparse 的 version/help action 本身就是打印后 ``SystemExit(0)``，此处保持
    相同退出语义（调用方/测试仍可 ``pytest.raises(SystemExit)``）。``--help``
    需要 parser 声明；``build_parser()`` 在 3.14 上约 59ms，非交互场景直接输出
    静态快照，交互式或版本/宽度不匹配时回退 argparse。
    """
    if len(argv) != 1:
        return False
    flag = argv[0]
    if flag in ("-V", "--version"):
        print(f"fspack {__version__}")
        raise SystemExit(0)
    if flag in ("-h", "--help") and _help_snapshot_usable():
        from fspack.cli_help import HELP_TEXT

        sys.stdout.write(HELP_TEXT)
        raise SystemExit(0)
    return False


def main(argv: list[str] | None = None) -> None:
    """主入口，解析参数并分发到子命令.

    单项目命令（build/package/run/clean/init/doctor/...）执行期抛出的
    :class:`fspack.exceptions.FspackError`（项目解析/入口识别/依赖/编译等领域
    错误）在此统一捕获：打印简洁中文错误并以退出码 2 退出，而非向用户抛出原始
    Python traceback。递归路径（``fsp b -r``）在 :func:`_run_recursive` 内逐项目
    捕获后继续，不会传播到此处；``SystemExit`` 原样放行（argparse ``--help``
    与显式退出码）。
    """
    if _try_fast_path(sys.argv[1:] if argv is None else list(argv)):
        return

    parser = build_parser()
    ns = parser.parse_args(argv)
    command = ns.command
    if command is None:
        parser.print_help()
        return

    # console 延迟导入：仅在实际执行子命令时加载 rich（~17ms）
    from fspack.console import console

    json_mode = bool(getattr(ns, "json", False))
    console.setup_output(machine=json_mode, quiet=bool(getattr(ns, "quiet", False)))
    console.setup_logging(verbose=ns.verbose)

    # 默认把日志里的绝对路径缩成单行（-v 保持完整路径），宽度按终端列数收敛
    from fspack import logutil

    logutil.configure(verbose=ns.verbose, path_width=console.path_width)

    # -O/--offline 单次约定：设 FSPACK_OFFLINE 使 is_offline() 生效，
    # 与环境变量 FSPACK_OFFLINE=1 等价，仅当前命令进程内有效
    if getattr(ns, "offline", False):
        os.environ["FSPACK_OFFLINE"] = "1"

    try:
        _dispatch(command, ns)
    except FspackError as exc:
        # 领域错误（如 pyproject.toml 语法/编码错误、未识别入口、未知 extras、
        # 依赖下载失败等）已携带清晰中文消息：打印后以退出码 2 退出，不暴露原始栈。
        # 完整堆栈仅在 --verbose（DEBUG）时记录，便于排查而不干扰普通用户。
        console.error(str(exc))
        console.result(False, str(exc))
        if json_mode:
            _emit_json(command, ns, error=exc)
        import logging

        logging.getLogger(__name__).debug("命令 %s 执行失败", command, exc_info=exc)
        raise SystemExit(2) from None
    except SystemExit as exc:
        # 子命令内部的显式退出（如输入校验失败）同样保持退出码不变，
        # 但 --json 下补一个错误信封，避免脚本只看到空 stdout。
        code = exc.code if isinstance(exc.code, int) else 1
        if json_mode and code != 0:
            _emit_json(command, ns, error_message=f"命令以退出码 {code} 结束")
        raise
    else:
        if json_mode:
            _emit_json(command, ns)
    finally:
        console.reset_output()
        logutil.reset()


_JSON_COMMANDS: dict[str, str] = {
    "b": "build",
    "build": "build",
    "p": "package",
    "package": "package",
    "clean": "clean",
    "init": "init",
    "i": "init",
    "doctor": "doctor",
    "d": "doctor",
    "verify": "verify",
}


def _emit_json(
    command: str,
    ns: argparse.Namespace,
    *,
    error: FspackError | None = None,
    error_message: str | None = None,
) -> None:
    """在 stdout 输出单行 JSON 信封（``--json`` 专用）.

    run/manifest/cache 等自带主输出的命令不在 :data:`_JSON_COMMANDS` 中，
    不输出信封以免污染其 stdout。退出码仍由调用方原有逻辑决定，信封只影响
    机读结果通道。
    """
    canonical = _JSON_COMMANDS.get(command)
    if canonical is None:
        return
    import json

    project = getattr(ns, "project", None)
    project_path = Path(project).resolve() if project else None
    data: dict[str, str | None] | None = None
    failed = error is not None or error_message is not None
    if not failed:
        data = {"project": str(project_path) if project_path else None}
        if canonical in ("build", "package") and project_path is not None:
            data["dist_dir"] = str(project_path / "dist")
    envelope: dict[str, Any] = {
        "ok": not failed,
        "command": canonical,
        "version": __version__,
        "data": data,
        "error": None,
    }
    if error is not None:
        envelope["error"] = {"type": type(error).__name__, "message": str(error)}
    elif error_message is not None:
        envelope["error"] = {"type": "ExecutionError", "message": error_message}
    print(json.dumps(envelope, ensure_ascii=False), file=sys.stdout, flush=True)


def _dispatch(command: str, ns: argparse.Namespace) -> None:
    """按子命令分发到对应执行函数（由 :func:`main` 包裹 FspackError 处理）."""
    if command in ("init", "i"):
        _run_init(ns)
        return

    if command in ("doctor", "d"):
        _run_doctor(ns)
        return

    if command == "cache":
        _run_cache(ns)
        return

    # manifest/verify 没有统一的 project 参数（manifest diff 读 JSON 路径、verify
    # 读产物路径），必须在访问 ns.project 之前分发
    early_handlers = {"manifest": _run_manifest, "m": _run_manifest, "verify": _run_verify}
    early_handler = early_handlers.get(command)
    if early_handler is not None:
        early_handler(ns)
        return

    project = Path(ns.project).resolve()
    if command in ("build", "b"):
        if ns.recursive:
            sys.exit(_run_recursive(project, "build", ns))
        _run_build(project, ns)
    elif command in ("run", "r"):
        from fspack.exceptions import ProjectError
        from fspack.packaging.profile_log import ProfileOptions
        from fspack.runner import RunOptions
        from fspack.runner import run as run_cmd

        if (getattr(ns, "profile_out", None) or getattr(ns, "profile_compare", None)) and not ns.profile:
            raise ProjectError("--profile-out/--profile-compare 需配合 --profile 使用")
        if getattr(ns, "profile_repeat", 1) != 1 and not ns.profile:
            raise ProjectError("--profile-repeat 需配合 --profile 使用")
        run_cmd(
            project,
            rest_args=_drop_separator(ns.rest),
            options=RunOptions(
                debug=ns.debug,
                entry=ns.entry,
                profile=ProfileOptions(
                    enabled=ns.profile,
                    out=Path(ns.profile_out).resolve() if ns.profile_out else None,
                    compare=ns.profile_compare,
                    repeat=ns.profile_repeat,
                ),
            ),
        )
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


def _in_ci_environment() -> bool:
    """是否运行在 CI（GitHub Actions/GitLab/Jenkins/Azure 等）."""
    return any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILD_NUMBER", "TF_BUILD"))


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
        no_stdlib_zip=ns.no_stdlib_zip or base.no_stdlib_zip,
        slim_stdlib=ns.slim_stdlib if ns.slim_stdlib is not None else base.slim_stdlib,
        splash=ns.splash or base.splash,
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
        lock=ns.lock or base.lock,
        frozen=ns.frozen or base.frozen,
        no_sbom=ns.no_sbom or base.no_sbom,
        no_manifest=getattr(ns, "no_manifest", False) or base.no_manifest,
        no_win7_scan=getattr(ns, "no_win7_scan", False) or base.no_win7_scan,
        no_win7_dll=getattr(ns, "no_win7_dll", False) or base.no_win7_dll,
        open_browser=ns.open_browser or base.open_browser,
        compiler=ns.compiler if getattr(ns, "compiler", None) is not None else base.compiler,
    )
    if ns.log_file:
        log_file = Path(ns.log_file).resolve()
    elif not getattr(ns, "no_log_file", False) and _in_ci_environment():
        # CI 日志默认落盘，失败时可直接上传/翻查；本地交互构建不产生额外文件。
        log_file = project / ".fspack" / "build.log"
    else:
        log_file = None
    log_format = LogFormat.parse(ns.log_format)
    if (ns.profile_out or ns.profile_compare) and not ns.profile:
        raise ProjectError("--profile-out/--profile-compare 需配合 --profile 使用")
    from fspack.packaging.profile_log import ProfileOptions

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
        profile=ProfileOptions(
            enabled=ns.profile,
            out=Path(ns.profile_out).resolve() if ns.profile_out else None,
            compare=ns.profile_compare,
        ),
        auto_clean=getattr(ns, "auto_clean", False),
    )


def _run_package(project: Path, ns: argparse.Namespace) -> None:
    """执行单项目 package 子命令."""
    from pathlib import Path as _Path

    from fspack.config import ProjectInfo
    from fspack.exceptions import ProjectError
    from fspack.packaging.installer import ReleaseRequest, SignOptions, build_release

    # extras 校验：CLI --extra 指定时校验未知分组（与 build 子命令一致）
    # 仅校验，不构造 BuildOptions——build_release 内部 _prepare_dist 用配置默认，
    # CLI 指定时通过 ReleaseRequest.extras 透传覆盖配置默认
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
    req = ReleaseRequest(
        project_dir=project,
        mirror=_resolve_mirror(ns.mirror),
        py_version=ns.py_version,
        no_build=ns.no_build,
        extras=cli_extras,
    )
    sign = SignOptions(
        codesign=ns.codesign,
        sign_exe=ns.sign_exe,
        sign_exe_certificate=sign_exe_cert,
        sign_exe_password=sign_exe_pwd,
        sign_deb=ns.sign_deb,
        sign_deb_key=sign_deb_key,
    )
    outputs = build_release(req, target=_parse_target(ns.target), fmt=ns.format, sign=sign)
    import logging

    for out in outputs:
        logging.getLogger(__name__).info("发行包已生成: %s", out)


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
        import logging

        ns.project_name = Path.cwd().name
        logging.getLogger(__name__).info("未指定项目名，使用当前目录名: %s", ns.project_name)

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
    """执行 doctor 子命令：环境诊断 + 可选的模板构建测试/基准剖析/缓存检查.

    无 ``--test``/``--check-cache`` 时仅执行环境诊断，输出三色诊断报告。
    ``--test`` 运行所有模板构建并打印汇总结果。``-P``/``-PO``/``-PC``
    （需 ``--test``）与 ``fsp b -P/-PC``、``fsp r -P/-PC`` 对齐：启用各
    模板构建阶段耗时报告，聚合落盘单个基准剖析日志并与历史对比。
    ``--check-cache`` 扫描 wheel 缓存目录的依赖解析缓存文件并删除损坏
    文件（iter-128，可与 ``--test`` 组合使用）。
    """
    from pathlib import Path

    from fspack.doctor import (
        print_doctor_report,
        run_doctor,
        run_doctor_cache_check,
        run_doctor_test,
    )
    from fspack.exceptions import ProjectError
    from fspack.packaging.profile_log import ProfileOptions

    test = getattr(ns, "test", False)
    profile = getattr(ns, "profile", False)
    profile_out = getattr(ns, "profile_out", None)
    profile_compare = getattr(ns, "profile_compare", None)
    # 参数校验前置：诊断报告需数秒，先报参数错误再跑诊断
    if (profile or profile_out or profile_compare) and not test:
        raise ProjectError("--profile/--profile-out/--profile-compare 需配合 --test 使用")
    if (profile_out or profile_compare) and not profile:
        raise ProjectError("--profile-out/--profile-compare 需配合 --profile 使用")

    # --bench-compare 独立路径：CI 门禁场景不需要环境诊断，跳过直接运行对比
    if getattr(ns, "bench_compare", False) and not test and not getattr(ns, "check_cache", False):
        from fspack.doctor.benchmark_compare import (
            DEFAULT_CATEGORIES,
            DEFAULT_THRESHOLD,
            compare_entry,
        )

        bench_dir = Path(ns.bench_dir).resolve() if getattr(ns, "bench_dir", None) else None
        bench_threshold = getattr(ns, "bench_threshold", None) or DEFAULT_THRESHOLD
        bench_no_cats = getattr(ns, "bench_no_categories", False)
        bench_list = getattr(ns, "bench_list_categories", False)

        categories = None if bench_no_cats else DEFAULT_CATEGORIES

        rc = compare_entry(
            bench_dir=bench_dir,
            threshold=bench_threshold,
            categories=categories,
            list_categories=bench_list,
        )
        sys.exit(rc)

    report = run_doctor()
    print_doctor_report(report)

    if getattr(ns, "check_cache", False):
        run_doctor_cache_check()

    if test:
        run_doctor_test(
            ProfileOptions(
                enabled=profile,
                out=Path(profile_out).resolve() if profile_out else None,
                compare=profile_compare,
            )
        )


def _run_cache(ns: argparse.Namespace) -> None:
    """执行 cache 子命令：缓存健康检查与清理.

    iter-139 引入：仅扫描 wheels。
    iter-148 扩展：默认扫描全部 cache 类型（wheels/embed/standalone/nuitka/loaders/
    ccache/tkinter），``--target <name>`` 限定单类型，``--stale`` 启用过期文件清理。

    ``fsp cache status [--target <name>]`` 扫描缓存目录健康状态并渲染详细报告。
    ``fsp cache clean [--dry-run] [--stale] [--target <name>]`` 清理损坏文件与
    wheels 的 stale/orphan，``--stale`` 额外清理非 wheels 类型的过期文件。
    """
    from fspack.doctor import run_cache_clean, run_cache_status

    action = getattr(ns, "cache_action", None)
    target = getattr(ns, "target", None)
    if action == "status":
        run_cache_status(target=target, full_verify=getattr(ns, "verify", False))
    elif action == "clean":
        run_cache_clean(
            dry_run=getattr(ns, "dry_run", False),
            include_stale=getattr(ns, "stale", False),
            target=target,
        )
    # argparse 已用 required=True 保证 cache_action 必填，到这里 action 必非 None


def _run_verify(ns: argparse.Namespace) -> None:
    """执行 verify 子命令：校验产物格式与架构，不匹配时以退出码 1 退出."""
    from fspack.console import console
    from fspack.packaging.verify import detect_binary, host_arch, verify_artifact
    from fspack.platform import Platform, detect_platform

    path = Path(ns.path)
    target = Platform(ns.target) if ns.target else detect_platform()
    arch = ns.arch or host_arch()
    problems = verify_artifact(path, target, arch)
    if problems:
        for problem in problems:
            console.error(problem)
        raise SystemExit(1)

    info = detect_binary(path)
    # problems 为空时 detect_binary 必然非 None（不可识别会先返回问题）
    assert info is not None
    console.success(f"{path}: {info.format.upper()} / {', '.join(info.archs)}（目标 {target.value} / {arch}）")


def _run_manifest(ns: argparse.Namespace) -> None:
    """执行 manifest 子命令（generate/diff）."""
    action = getattr(ns, "manifest_action", None)

    if action in ("generate", "g"):
        _run_generate(ns)
        return

    if action in ("diff", "d"):
        _run_diff(ns)
        return

    # 未指定子动作：用 manifest subparser 打印帮助
    # 复用 build_parser，parse_args(["manifest", "--help"]) 会自动 sys.exit(0)，无需捕获
    build_parser().parse_args(["manifest", "--help"])  # pragma: no cover - argparse --help 直接 sys.exit


def _run_generate(ns: argparse.Namespace) -> None:
    """执行 manifest generate：扫描 dist 重新生成 manifest."""
    import json

    from fspack import logutil
    from fspack.config import ProjectInfo
    from fspack.console import console
    from fspack.exceptions import ProjectError
    from fspack.fsutil import atomic_write_text
    from fspack.packaging.manifest import _format_size, _logger, collect_manifest
    from fspack.platform import Platform, detect_platform

    project = Path(ns.project).resolve()
    # 生成 ProjectInfo（复用 from_dir 缓存），用于填充 manifest.project 与默认文件名
    try:
        info = ProjectInfo.from_dir(project, ns.py_version)
    except ProjectError as e:
        console.error(str(e))
        raise SystemExit(2) from None

    dist = project / "dist"
    if not dist.is_dir():
        console.error(f"dist 目录不存在，请先构建: {dist}")
        raise SystemExit(2) from None

    data = collect_manifest(dist, info)
    if getattr(ns, "output", None):
        output = Path(ns.output).resolve()
    else:
        target = Platform(ns.target) if ns.target else detect_platform()
        release_dir = dist / "release"
        release_dir.mkdir(parents=True, exist_ok=True)
        output = release_dir / f"{info.name}-{info.version}-py{info.py_version}-{target.value}-manifest.json"
    atomic_write_text(output, json.dumps(data, ensure_ascii=False, indent=2))
    _logger.info("产物清单已生成: %s", logutil.short_path(output))
    _logger.debug(
        "产物清单统计: %d 个文件，共 %s",
        data["summary"]["total_files"],
        _format_size(data["summary"]["total_size"]),
    )
    console.success(f"manifest 已生成: {logutil.short_path(output)}")


def _run_diff(ns: argparse.Namespace) -> None:
    """执行 manifest diff：对比两份 manifest 并打印差异."""
    from fspack.console import console
    from fspack.packaging.manifest import diff_manifest, load_manifest, print_manifest_diff

    old_path = Path(ns.old)
    new_path = Path(ns.new)
    for p, label in ((old_path, "旧 manifest"), (new_path, "新 manifest")):
        if not p.is_file():
            console.error(f"{label}不存在: {p}")
            raise SystemExit(2) from None

    try:
        old = load_manifest(old_path)
        new = load_manifest(new_path)
    except ValueError as e:
        console.error(str(e))
        raise SystemExit(2) from None

    diff = diff_manifest(old, new)
    print_manifest_diff(diff)

    if getattr(ns, "exit_code", False) and not diff.is_empty:
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
            # with 显式关闭 scandir 迭代器：排序 key 抛异常时不依赖 GC 回收句柄
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
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
        except KeyboardInterrupt:
            # Ctrl+C 必须立即中断整个递归构建，不得记为单项目失败后继续循环
            raise
        except BaseException as exc:
            err_msg = _format_error(exc)
            failed.append((project, f"{rel}: {err_msg}"))
            console.error(f"{rel} {action_verb}失败: {err_msg}")
            import logging

            logging.getLogger(__name__).debug("%s 完整错误堆栈", rel, exc_info=exc)

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
