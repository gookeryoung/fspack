"""fsp b —— 打包项目。."""

from __future__ import annotations

import logging
from pathlib import Path

from fspack.builder import build
from fspack.mirror import get_mirror
from fspack.platform import Platform, detect_platform

__all__ = ["run"]

_logger = logging.getLogger(__name__)


def run(
    project: Path,
    mirror: str | None = None,
    py_version: str | None = None,
    target: Platform | None = None,
) -> None:
    """执行项目构建。."""
    mirror_cfg = get_mirror(mirror)
    resolved_target = target or detect_platform()
    build(project, mirror_cfg, py_version, target=resolved_target)
