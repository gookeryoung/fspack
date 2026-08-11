"""``fspack.packaging.site_packages` 共享模块单元测试.

覆盖从 :mod:`fspack.packaging.size_report`/`:mod:`fspack.packaging.sbom`/
`:mod:`fspack.packaging.pipeline.stages` 抽取到本模块的共享逻辑：

- :func:`find_site_packages` — 跨平台 site-packages 目录定位
- :func:`normalize_pkg_name` — PEP 503 包名规范化
"""

from __future__ import annotations

from pathlib import Path

from fspack.packaging.site_packages import find_site_packages, normalize_pkg_name

# ---- 辅助函数 ----


def _make_dist_with_site_packages(tmp_path: Path) -> Path:
    """创建含 site-packages 的 dist 目录（dist/site-packages，与 runtime 平级）."""
    dist = tmp_path / "dist"
    sp = dist / "site-packages"
    sp.mkdir(parents=True)
    (sp / "pkg1.py").write_text("x = 1\n")
    return dist


# ---- find_site_packages ----


def test_find_site_packages_unified(tmp_path: Path) -> None:
    """site-packages 统一平铺到 dist/site-packages（与 runtime 平级）."""
    dist = _make_dist_with_site_packages(tmp_path)
    sp = find_site_packages(dist)
    assert sp is not None
    assert sp.name == "site-packages"
    assert sp.parent == dist


def test_find_site_packages_not_found(tmp_path: Path) -> None:
    """无 site-packages 时返回 None."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "runtime").mkdir()
    assert find_site_packages(dist) is None


def test_find_site_packages_skips_non_dir_match(tmp_path: Path) -> None:
    """glob 命中文件而非目录时跳过，继续查找下一个模式."""
    dist = tmp_path / "dist"
    # dist/site-packages 是文件而非目录
    fake_sp = dist / "site-packages"
    fake_sp.parent.mkdir(parents=True)
    fake_sp.write_text("not a directory")
    assert find_site_packages(dist) is None


# ---- normalize_pkg_name ----


def test_normalize_pkg_name_replaces_separators() -> None:
    """连续的 -_. 替换为单 -，转小写."""
    assert normalize_pkg_name("Ordered.Set") == "ordered-set"
    assert normalize_pkg_name("foo_bar") == "foo-bar"
    assert normalize_pkg_name("FOO__BAR") == "foo-bar"


def test_normalize_pkg_name_already_normalized() -> None:
    """已是规范形式时返回原值（小写）."""
    assert normalize_pkg_name("requests") == "requests"
    assert normalize_pkg_name("ordered-set") == "ordered-set"


def test_normalize_pkg_name_mixed_separators() -> None:
    """混合分隔符连续出现时合并为单 -."""
    assert normalize_pkg_name("foo_.-bar") == "foo-bar"
    assert normalize_pkg_name("A.B_C-D") == "a-b-c-d"
