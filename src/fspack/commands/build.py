"""fsp b —— 打包项目。."""

from __future__ import annotations

import logging
from pathlib import Path

from fspack.builder import DEFAULT_PY_VERSION, build
from fspack.mirror import get_mirror
from fspack.platform import Platform

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
    build(project, mirror_cfg, py_version or DEFAULT_PY_VERSION, target=target)
