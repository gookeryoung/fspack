"""manifest 子命令参数声明与执行逻辑."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

__all__ = ["_add_manifest_subparser", "_run_manifest"]


def _add_manifest_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """添加 manifest/m 子命令：生成产物清单与差异对比.

    含两个子动作：
    - ``generate``：读取项目 dist 目录生成 manifest JSON（CLI 显式重新生成，
      跳过构建阶段）
    - ``diff``：对比两份 manifest JSON 的差异（新增/删除/修改 + 分类汇总）
    """
    g = sub.add_parser(
        "manifest",
        aliases=["m"],
        help="产物清单生成与差异对比",
    )
    sub2 = g.add_subparsers(dest="manifest_action", metavar="<action>")

    p_gen = sub2.add_parser(
        "generate",
        aliases=["g"],
        help="扫描 dist 目录生成 manifest JSON（显式重新生成）",
    )
    p_gen.add_argument("project", nargs="?", default=".", help="项目目录")
    p_gen.add_argument(
        "--py-version",
        default=None,
        help="embed python 版本（用于构建 ProjectInfo，不传则从项目解析）",
    )
    p_gen.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="OUTPUT",
        help="输出 manifest 路径，默认写入 dist/release/<name>-<version>-manifest.json",
    )

    p_diff = sub2.add_parser(
        "diff",
        aliases=["d"],
        help="对比两份 manifest JSON 的差异（新增/删除/修改 + 分类汇总）",
    )
    p_diff.add_argument("old", help="旧 manifest JSON 路径")
    p_diff.add_argument("new", help="新 manifest JSON 路径")
    p_diff.add_argument(
        "--exit-code",
        action="store_true",
        dest="exit_code",
        help=("有差异时以退出码 1 退出（便于 CI 判断变更）；默认仅打印差异不改变退出码"),
    )


def _run_manifest(ns: argparse.Namespace) -> None:
    """执行 manifest 子命令（generate/diff）."""
    action = getattr(ns, "manifest_action", None)

    if action in ("generate", "g"):
        _run_generate(ns)
        return

    if action in ("diff", "d"):
        _run_diff(ns)
        return

    # 未指定子动作：用当前 manifest subparser 打印帮助
    # 构造：复用 build_parser，parse_args(["manifest", "--help"]) 会自动
    # sys.exit(0)，无需捕获
    from fspack.cli_parser import build_parser

    build_parser().parse_args(["manifest", "--help"])  # pragma: no cover - argparse --help 直接 sys.exit


def _run_generate(ns: argparse.Namespace) -> None:
    """执行 manifest generate：扫描 dist 重新生成 manifest."""
    from fspack._util.fsutil import atomic_write_text
    from fspack.config import ProjectInfo
    from fspack.exceptions import ProjectError
    from fspack.packaging.manifest import _format_size, _logger, collect_manifest

    project = Path(ns.project).resolve()
    # 生成 ProjectInfo（复用 from_dir 缓存），用于填充 manifest.project 与默认文件名
    try:
        info = ProjectInfo.from_dir(project, ns.py_version)
    except ProjectError as e:
        from fspack.console import console

        console.error(str(e))
        raise SystemExit(2) from None

    dist = project / "dist"
    if not dist.is_dir():
        from fspack.console import console

        console.error(f"dist 目录不存在，请先构建: {dist}")
        raise SystemExit(2) from None

    data = collect_manifest(dist, info)
    if getattr(ns, "output", None):
        output = Path(ns.output).resolve()
    else:
        release_dir = dist / "release"
        release_dir.mkdir(parents=True, exist_ok=True)
        output = release_dir / f"{info.name}-{info.version}-manifest.json"
    atomic_write_text(output, json.dumps(data, ensure_ascii=False, indent=2))
    _logger.info(
        "产物清单已生成: %s（%d 个文件，共 %s）",
        output,
        data["summary"]["total_files"],
        _format_size(data["summary"]["total_size"]),
    )
    from fspack.console import console

    console.success(f"manifest 已生成: {output}")


def _run_diff(ns: argparse.Namespace) -> None:
    """执行 manifest diff：对比两份 manifest 并打印差异."""
    from fspack.packaging.manifest import diff_manifest, load_manifest, print_manifest_diff

    old_path = Path(ns.old)
    new_path = Path(ns.new)
    for p, label in ((old_path, "旧 manifest"), (new_path, "新 manifest")):
        if not p.is_file():
            from fspack.console import console

            console.error(f"{label}不存在: {p}")
            raise SystemExit(2) from None

    try:
        old = load_manifest(old_path)
        new = load_manifest(new_path)
    except ValueError as e:
        from fspack.console import console

        console.error(str(e))
        raise SystemExit(2) from None

    diff = diff_manifest(old, new)
    print_manifest_diff(diff)

    if getattr(ns, "exit_code", False) and not diff.is_empty:
        sys.exit(1)
