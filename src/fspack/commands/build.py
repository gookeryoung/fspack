"""fsp b —— 打包项目."""

from __future__ import annotations

import logging
from pathlib import Path

from fspack.builder import build
from fspack.config import get_mirror
from fspack.platform import Platform, detect_platform

__all__ = ["run"]

_logger = logging.getLogger(__name__)


def run(  # noqa: PLR0913
    project: Path,
    mirror: str | None = None,
    py_version: str | None = None,
    target: Platform | None = None,
    keep_modules: set[str] | None = None,
    icon: Path | None = None,
    no_stdlib_trim: bool = False,
    no_pyc: bool = False,
    pyc_strip: bool = False,
) -> None:
    """执行项目构建."""
    mirror_cfg = get_mirror(mirror)
    resolved_target = target or detect_platform()
    build(
        project,
        mirror_cfg,
        py_version,
        target=resolved_target,
        keep_modules=keep_modules,
        icon=icon,
        no_stdlib_trim=no_stdlib_trim,
        no_pyc=no_pyc,
        pyc_strip=pyc_strip,
    )
