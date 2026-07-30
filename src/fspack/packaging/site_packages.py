"""site-packages 路径定位与包名规范化辅助.

集中放置跨模块共享的 site-packages 查找与 PEP 503 包名规范化逻辑，
避免 :mod:`fspack.packaging.size_report`/`:mod:`fspack.packaging.sbom`/
:mod:`fspack.packaging.pipeline.stages` 等后处理模块各自重复定义。

公共 API：

- :data:`SITE_PACKAGES_GLOBS` — 跨平台 site-packages 目录 glob 模式
- :func:`find_site_packages` — 在 dist 目录下定位 site-packages 目录
- :func:`normalize_pkg_name` — PEP 503 包名规范化（``-_.`` → ``-``，转小写）
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["SITE_PACKAGES_GLOBS", "find_site_packages", "normalize_pkg_name"]

# site-packages 目录的 glob 模式（相对 dist 目录）：
# - Windows embed python: runtime/Lib/site-packages
# - Linux/macOS standalone python: runtime/python/lib/python<X.Y>/site-packages
SITE_PACKAGES_GLOBS: tuple[str, ...] = (
    "runtime/Lib/site-packages",
    "runtime/python/lib/python*/site-packages",
)


def find_site_packages(dist_dir: Path) -> Path | None:
    """在 dist 目录下定位 site-packages 目录，找不到返回 None.

    按 :data:`SITE_PACKAGES_GLOBS` 中的 glob 模式依次查找，返回首个存在且
    为目录的匹配项。覆盖两种 runtime 布局：

    - Windows embed python：``dist/runtime/Lib/site-packages``
    - Linux/macOS standalone python：``dist/runtime/python/lib/python<X.Y>/site-packages``
    """
    for pattern in SITE_PACKAGES_GLOBS:
        for sp in dist_dir.glob(pattern):
            if sp.is_dir():
                return sp
    return None


def normalize_pkg_name(name: str) -> str:
    """按 PEP 503 规范化包名：连续的 ``-_.`` 替换为单 ``-``，转小写.

    使 ``ordered_set``/``ordered-set``/``Ordered.Set`` 均映射到 ``ordered-set``，
    便于跨命名风格匹配 dist-info 目录。
    """
    return re.sub(r"[-_.]+", "-", name).lower()
