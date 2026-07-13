"""fsp p —— 生成安装包（Windows NSIS / Linux .deb + tar.gz）。."""

from __future__ import annotations

import logging
from pathlib import Path

from fspack.installer import build_installer
from fspack.linux_installer import build_linux_installer
from fspack.mirror import get_mirror
from fspack.platform import Platform, detect_platform

__all__ = ["run"]

_logger = logging.getLogger(__name__)


def run(
    project: Path,
    mirror: str | None = None,
    py_version: str | None = None,
    no_build: bool = False,
    target: Platform | None = None,
) -> None:
    """生成安装包到 dist/release/。."""
    mirror_cfg = get_mirror(mirror)
    resolved_target = target or detect_platform()
    if resolved_target is Platform.LINUX:
        out = build_linux_installer(project, mirror_cfg, py_version, no_build=no_build)
    else:
        out = build_installer(project, mirror_cfg, py_version, no_build=no_build)
    _logger.info("安装包已生成: %s", out)
