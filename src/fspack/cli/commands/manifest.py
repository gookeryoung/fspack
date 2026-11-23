"""manifest 子命令：产物清单生成与差异对比."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

__all__ = ["_run_diff", "_run_generate", "_run_manifest"]


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
    from fspack.cli.parser import build_parser

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
