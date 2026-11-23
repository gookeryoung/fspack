"""verify 子命令：校验产物格式与架构."""

from __future__ import annotations

import argparse
from pathlib import Path

__all__ = ["_run_verify"]


def _run_verify(ns: argparse.Namespace) -> None:
    """执行 verify 子命令：校验产物格式与架构，不匹配时以退出码 1 退出."""
    from fspack.core.platform import Platform, detect_platform
    from fspack.packaging.verify import detect_binary, host_arch, verify_artifact
    from fspack.ui.console import console

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
