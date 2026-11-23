"""包内资源存在性与定位守卫（P2 起改走 fspack.core.resources）."""

from __future__ import annotations

from pathlib import Path

import fspack

PACKAGE_ROOT = Path(fspack.__file__).parent
ASSETS = PACKAGE_ROOT / "assets"

REQUIRED = (
    "icons/app.ico",
    "loader/loader_windows.c",
    "loader/loader_linux.c",
    "loader/loader_macos.c",
    "runtime/api-ms-win-core-path-l1-1-0.dll",
    "templates",
    "init_templates",
)


def test_package_assets_exist() -> None:
    missing = [rel for rel in REQUIRED if not (ASSETS / rel).exists()]
    assert not missing, f"包内资源缺失（会漏进 wheel）：{missing}"


def test_template_roots_are_non_empty() -> None:
    for name in ("templates", "init_templates"):
        root = ASSETS / name
        assert any(p.is_dir() for p in root.iterdir()), f"{name} 下没有模板目录"
