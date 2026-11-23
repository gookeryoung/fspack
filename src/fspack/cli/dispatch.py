"""子命令分发（由 :func:`fspack.cli.main` 包裹 FspackError 处理）."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

__all__ = ["_dispatch"]


def _dispatch(command: str, ns: argparse.Namespace) -> None:
    """按子命令分发到对应执行函数（由 :func:`fspack.cli.main` 包裹 FspackError 处理）."""
    if command in ("init", "i"):
        from fspack.cli.commands.init import _run_init

        _run_init(ns)
        return

    if command in ("doctor", "d"):
        from fspack.cli.commands.doctor import _run_doctor

        _run_doctor(ns)
        return

    if command == "cache":
        from fspack.cli.commands.cache import _run_cache

        _run_cache(ns)
        return

    # manifest/verify 没有统一的 project 参数（manifest diff 读 JSON 路径、verify
    # 读产物路径），必须在访问 ns.project 之前分发
    if command in ("manifest", "m"):
        from fspack.cli.commands.manifest import _run_manifest

        _run_manifest(ns)
        return
    if command == "verify":
        from fspack.cli.commands.verify import _run_verify

        _run_verify(ns)
        return

    project = Path(ns.project).resolve()
    if command in ("build", "b"):
        if ns.recursive:
            from fspack.cli.recursive import _run_recursive

            sys.exit(_run_recursive(project, "build", ns))
        # 从包级命名空间解析，保持 ``fspack.cli._run_build`` monkeypatch 兼容
        from fspack.cli import _run_build

        _run_build(project, ns)
    elif command in ("run", "r"):
        from fspack.cli.commands.run import _run_run

        _run_run(project, ns)
    elif command in ("clean", "c"):
        from fspack.cli.commands.clean import _run_clean

        _run_clean(project)
    elif command in ("package", "p"):
        if ns.recursive:
            from fspack.cli.recursive import _run_recursive

            sys.exit(_run_recursive(project, "package", ns))
        # 从包级命名空间解析，保持 ``fspack.cli._run_package`` monkeypatch 兼容
        from fspack.cli import _run_package

        _run_package(project, ns)
