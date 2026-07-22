"""fsp c —— 清理 dist/，保留 installer.nsi 便于改代码后重新打包分发。."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

__all__ = ["run"]

_logger = logging.getLogger(__name__)

# 清理 dist 时保留的 NSIS 脚本文件名
_KEEP_NSI = "installer.nsi"


def run(project: Path) -> None:
    """清理项目下的 dist 目录，保留 installer.nsi 便于重新打包分发。."""
    dist = Path(project) / "dist"
    if not dist.is_dir():
        _logger.info("无 dist 目录可清理: %s", dist)
        return
    nsi_path = dist / _KEEP_NSI
    nsi_content: str | None = None
    if nsi_path.is_file():
        nsi_content = nsi_path.read_text(encoding="utf-8")
        _logger.info("保留 NSIS 脚本: %s", nsi_path)
    shutil.rmtree(dist)
    dist.mkdir(parents=True, exist_ok=True)
    if nsi_content is not None:
        nsi_path.write_text(nsi_content, encoding="utf-8")
    _logger.info("已清理: %s", dist)
