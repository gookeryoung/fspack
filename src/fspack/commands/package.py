"""fsp p —— 生成发行包（Windows NSIS / Linux .deb + tar.gz / 跨平台 zip）。"""

from __future__ import annotations

import logging
from pathlib import Path

from fspack.config import get_mirror
from fspack.packaging.installer import build_release
from fspack.platform import Platform, detect_platform

__all__ = ["run"]

_logger = logging.getLogger(__name__)


def run(  # noqa: PLR0913
    project: Path,
    mirror: str | None = None,
    py_version: str | None = None,
    no_build: bool = False,
    target: Platform | None = None,
    fmt: str = "auto",
) -> None:
    """生成发行包到 dist/release/。

    fmt 取值见 :func:`fspack.packaging.installer._resolve_formats`：
    ``auto``（平台默认）/``zip``（跨平台便携包）/``nsis``（Windows）/
    ``tar.gz``/``deb``（Linux）/``all``（平台全部）。
    """
    mirror_cfg = get_mirror(mirror)
    resolved_target = target or detect_platform()
    outputs = build_release(
        project,
        mirror_cfg,
        py_version,
        no_build=no_build,
        target=resolved_target,
        fmt=fmt,
    )
    for out in outputs:
        _logger.info("发行包已生成: %s", out)
