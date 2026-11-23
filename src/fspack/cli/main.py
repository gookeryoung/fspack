"""fspack CLI 入口 —— cargo 风格短命令（fsp b/c/r）.

顶部仅导入轻量标准库（argparse/pathlib/os/sys）与 ``__version__``。
重模块（``fspack.config``/``fspack.ui.console``/``fspack.core.platform``）延迟到
实际使用时导入，使 ``fsp --help`` 无需加载 config/console 即可输出帮助；
``logging`` 同样延迟到使用点（console 顶层自会导入，``--help`` 类轻路径
可整段省去 logging 导入链，实测约 5-6ms）。
``--mirror`` 刻意不做 argparse choices 校验（choices 会在 parser 构建期
触发 config 导入），改由 :func:`fspack.cli.commands.build._resolve_mirror`
在执行期校验。

parser 构建代码在 :mod:`fspack.cli.parser`，命令实现按子命令拆分到
:mod:`fspack.cli.commands.*`，本模块只保留 ``main``/fast-path/JSON 信封。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from fspack import __version__
from fspack.core.exceptions import FspackError

__all__ = ["main"]


def _help_snapshot_usable() -> bool:
    """静态 help 快照是否可安全替代 argparse 输出.

    三个条件缺一不可：

    - 解释器次版本与快照生成版本一致（argparse 的 section 标题/换行随版本变化）
    - 未设置 ``COLUMNS``（argparse 会按该值换行，快照固定 80 列）
    - stdout 非 TTY（交互式终端按实际宽度换行，交给 argparse 保证观感）
    """
    from fspack.cli.help import HELP_SNAPSHOT_PY

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
        from fspack.cli.help import HELP_TEXT

        sys.stdout.write(HELP_TEXT)
        raise SystemExit(0)
    return False


def main(argv: list[str] | None = None) -> None:
    """主入口，解析参数并分发到子命令.

    单项目命令（build/package/run/clean/init/doctor/...）执行期抛出的
    :class:`fspack.core.exceptions.FspackError`（项目解析/入口识别/依赖/编译等领域
    错误）在此统一捕获：打印简洁中文错误并以退出码 2 退出，而非向用户抛出原始
    Python traceback。递归路径（``fsp b -r``）在
    :func:`fspack.cli.recursive._run_recursive` 内逐项目捕获后继续，不会传播到
    此处；``SystemExit`` 原样放行（argparse ``--help`` 与显式退出码）。
    """
    if _try_fast_path(sys.argv[1:] if argv is None else list(argv)):
        return

    # 通过包级命名空间解析，保持 ``fspack.cli.build_parser`` monkeypatch 兼容
    from fspack.cli import _dispatch, build_parser

    parser = build_parser()
    ns = parser.parse_args(argv)
    command = ns.command
    if command is None:
        parser.print_help()
        return

    # console 延迟导入：仅在实际执行子命令时加载 rich（~17ms）
    from fspack.ui.console import console

    json_mode = bool(getattr(ns, "json", False))
    console.setup_output(machine=json_mode, quiet=bool(getattr(ns, "quiet", False)))
    console.setup_logging(verbose=ns.verbose)

    # 默认把日志里的绝对路径缩成单行（-v 保持完整路径），宽度按终端列数收敛
    from fspack.core import logutil

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
        console.error_panel(str(exc), hint="加 -v 查看详细日志；运行 `fsp d` 检查环境是否就绪。")
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


if __name__ == "__main__":
    main()
