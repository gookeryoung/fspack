"""run 子命令：运行已构建产物或调试入口."""

from __future__ import annotations

import argparse
from pathlib import Path

__all__ = ["_drop_separator", "_run_run"]


def _drop_separator(rest: list[str]) -> list[str]:
    """剔除 argparse REMAINDER 捕获的首个 -- 分隔符."""
    if rest and rest[0] == "--":
        return rest[1:]
    return rest


def _run_run(project: Path, ns: argparse.Namespace) -> None:
    """执行 run 子命令."""
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
