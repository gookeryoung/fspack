"""clean 子命令：清理构建产物目录."""

from __future__ import annotations

from pathlib import Path

__all__ = ["_run_clean"]


def _run_clean(project: Path) -> None:
    """执行 clean 子命令."""
    from fspack.builder import clean_dist

    clean_dist(project)
