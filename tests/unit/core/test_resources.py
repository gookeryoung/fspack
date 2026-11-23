"""包内资源存在性与定位守卫：所有资源必须经 fspack.core.resources 访问."""

from __future__ import annotations

from fspack.core.resources import PACKAGE_ROOT, RESOURCES_ROOT, resource_path, templates_root

REQUIRED = (
    "icons/app.ico",
    "icons/favicon.ico",
    "loader/loader_windows.c",
    "loader/loader_linux.c",
    "loader/loader_macos.c",
    "runtime/api-ms-win-core-path-l1-1-0.dll",
    "runtime/bcryptprimitives.dll",
)


def test_resources_root_layout() -> None:
    assert RESOURCES_ROOT == PACKAGE_ROOT / "resources"
    assert RESOURCES_ROOT.is_dir()


def test_required_resources_exist() -> None:
    missing = [rel for rel in REQUIRED if not resource_path(rel).is_file()]
    assert not missing, f"包内资源缺失（会漏进 wheel）：{missing}"


def test_template_roots_are_non_empty() -> None:
    for role in ("init", "doctor"):
        root = templates_root(role)
        assert root.is_dir(), f"模板目录不存在: {root}"
        assert any(p.is_dir() for p in root.iterdir()), f"{role} 下没有模板目录"


def test_unknown_template_role_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="未知模板角色"):
        templates_root("nope")
