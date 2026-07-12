"""fsp c —— 清理 dist/。."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

__all__ = ["run"]

_logger = logging.getLogger(__name__)


def run(project: Path) -> None:
    """清理项目下的 dist 目录。."""
    dist = Path(project) / "dist"
    if dist.is_dir():
        shutil.rmtree(dist)
        _logger.info("已清理: %s", dist)
    else:
        _logger.info("无 dist 目录可清理: %s", dist)
