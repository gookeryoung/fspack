"""doctor 富示例模板加载测试：扫描 assets/templates/ 并解析元数据.

覆盖统一注册表的 doctor 加载路径：

- :func:`fspack.templates.registry._doctor_templates_root` — 定位 ``assets/templates``
- :func:`fspack.templates.list_templates` — 按 ``role="doctor"`` 扫描（排序、过滤、元数据完整性）
- :func:`fspack.templates.get_template` — 按 id 查询（命中/未命中）
- :func:`fspack.templates.registry._load_doctor_template` — 解析单个目录（完整字段/默认值/异常容错）

真实模板（``assets/templates/``）用于验证扫描与元数据；``tmp_path`` 构造的临时
模板用于验证解析逻辑与默认值回退，避免依赖真实 assets。替换扫描根目录的用例
必须清空注册表缓存，否则 ``lru_cache`` 会返回上一次的真实模板。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from fspack.templates.registry import (
    _doctor_templates_root,
    _load_doctor_template,
    clear_template_cache,
    get_template,
    list_templates,
)


@pytest.fixture(autouse=True)
def _reset_template_cache() -> Iterator[None]:
    """每个用例前后清缓存，避免 monkeypatch 根目录的结果泄漏到其他用例."""
    clear_template_cache()
    yield
    clear_template_cache()


# ---- 根目录 ----


def test_doctor_root_points_to_assets_templates() -> None:
    """根目录指向 fspack 包内 ``assets/templates`` 且存在."""
    root = _doctor_templates_root()
    assert root.is_dir()
    assert root.name == "templates"
    assert root.parent.name == "assets"


# ---- list_templates(role="doctor")：真实模板扫描 ----


def test_doctor_templates_returned_sorted() -> None:
    """返回非空列表，按分类+id 排序."""
    templates = list_templates(role="doctor")
    assert len(templates) > 0
    keys = [(t.category, t.id) for t in templates]
    assert keys == sorted(keys)


def test_doctor_templates_have_known_entries() -> None:
    """列表中包含已知的 doctor 模板."""
    ids = {t.id for t in list_templates(role="doctor")}
    assert "cli_complex" in ids
    assert "pyside2_qml_dashboard" in ids
    assert "sci_stack" in ids


def test_doctor_templates_metadata_complete() -> None:
    """每个模板的元数据字段完整（id/name/version/requires_python/dependencies）."""
    for tpl in list_templates(role="doctor"):
        assert tpl.id, f"模板 id 为空: {tpl}"
        assert tpl.name, f"模板 {tpl.id} name 为空"
        assert tpl.version, f"模板 {tpl.id} version 为空"
        assert tpl.requires_python, f"模板 {tpl.id} requires_python 为空"
        assert isinstance(tpl.dependencies, tuple), f"模板 {tpl.id} dependencies 不是 tuple"
        assert tpl.dir.is_dir(), f"模板 {tpl.id} dir 不存在: {tpl.dir}"


def test_doctor_template_dir_matches_id() -> None:
    """每个模板的 dir 名与 id 一致."""
    for tpl in list_templates(role="doctor"):
        assert tpl.dir.name == tpl.id


# ---- 扫描边界场景（用 tmp_path + monkeypatch 隔离） ----


def _patch_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """替换 doctor 根目录并清缓存."""
    monkeypatch.setattr("fspack.templates.registry._doctor_templates_root", lambda: root)
    clear_template_cache()


def test_doctor_scan_missing_root_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """根目录不存在时返回空元组."""
    _patch_root(monkeypatch, tmp_path / "nonexistent")
    assert list_templates(role="doctor") == ()


def test_doctor_scan_skips_non_dir_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """跳过根目录下的非目录条目（如 README.md 文件）."""
    (tmp_path / "README.md").write_text("not a template", encoding="utf-8")
    (tmp_path / "cli" / "valid_tpl").mkdir(parents=True)
    (tmp_path / "cli" / "valid_tpl" / "pyproject.toml").write_text(
        '[project]\nname = "valid_tpl"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    _patch_root(monkeypatch, tmp_path)
    assert [t.id for t in list_templates(role="doctor")] == ["valid_tpl"]


def test_doctor_scan_skips_dir_without_pyproject(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """跳过无 pyproject.toml 的目录."""
    (tmp_path / "cli" / "no_pyproject").mkdir(parents=True)
    (tmp_path / "cli" / "has_pyproject").mkdir(parents=True)
    (tmp_path / "cli" / "has_pyproject" / "pyproject.toml").write_text(
        '[project]\nname = "has_pyproject"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    _patch_root(monkeypatch, tmp_path)
    assert [t.id for t in list_templates(role="doctor")] == ["has_pyproject"]


def test_doctor_scan_skips_dir_with_invalid_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """跳过 pyproject.toml 解析失败的目录（不抛异常）."""
    (tmp_path / "cli" / "broken").mkdir(parents=True)
    (tmp_path / "cli" / "broken" / "pyproject.toml").write_text("[project\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "cli" / "good").mkdir(parents=True)
    (tmp_path / "cli" / "good" / "pyproject.toml").write_text(
        '[project]\nname = "good"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    _patch_root(monkeypatch, tmp_path)
    assert [t.id for t in list_templates(role="doctor")] == ["good"]


# ---- get_template：真实模板查询 ----


def test_get_doctor_template_existing() -> None:
    """返回已知模板，元数据正确（tk_app 无依赖且未声明 requires-python → 默认值）."""
    tpl = get_template("tk_app", role="doctor")
    assert tpl is not None
    assert tpl.id == "tk_app"
    assert tpl.name == "tk_app"
    assert tpl.requires_python == ">=3.8"
    assert tpl.dependencies == ()


def test_get_doctor_template_pyside2_qml() -> None:
    """pyside2_qml_dashboard 模板含 requires-python <3.11 约束与 pyside2 依赖."""
    tpl = get_template("pyside2_qml_dashboard", role="doctor")
    assert tpl is not None
    assert "<3.11" in tpl.requires_python
    assert any("pyside2" in dep.lower() for dep in tpl.dependencies)


def test_get_doctor_template_nonexistent_returns_none() -> None:
    """不存在的 id 返回 None."""
    assert get_template("nonexistent_template_xyz", role="doctor") is None


def test_get_doctor_template_missing_root_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """根目录不存在时按 id 查询返回 None."""
    _patch_root(monkeypatch, tmp_path / "nonexistent")
    assert get_template("any_id", role="doctor") is None


# ---- _load_doctor_template：解析逻辑（tmp_path 隔离真实 assets） ----


def test_load_doctor_template_parses_full_pyproject(tmp_path: Path) -> None:
    """解析完整 pyproject.toml，返回含所有字段的 Template."""
    tpl_dir = tmp_path / "my_tpl"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text(
        "[project]\n"
        'name = "my-app"\n'
        'version = "1.2.3"\n'
        'description = "测试模板"\n'
        'requires-python = ">=3.10,<3.13"\n'
        'dependencies = ["numpy>=1.0", "requests"]\n\n'
        "[tool.fspack]\n"
        'app-type = "gui"\n',
        encoding="utf-8",
    )
    tpl = _load_doctor_template(tpl_dir)
    assert tpl is not None
    assert tpl.dir == tpl_dir
    assert tpl.id == "my_tpl"
    assert tpl.name == "my-app"
    assert tpl.version == "1.2.3"
    assert tpl.description == "测试模板"
    assert tpl.requires_python == ">=3.10,<3.13"
    assert tpl.dependencies == ("numpy>=1.0", "requests")
    assert tpl.app_type == "gui"


def test_load_doctor_template_without_pyproject_returns_none(tmp_path: Path) -> None:
    """无 pyproject.toml 的目录返回 None."""
    tpl_dir = tmp_path / "empty_tpl"
    tpl_dir.mkdir()
    assert _load_doctor_template(tpl_dir) is None


def test_load_doctor_template_with_invalid_toml_returns_none(tmp_path: Path) -> None:
    """非法 TOML 返回 None（捕获 TOMLDecodeError，不抛异常）."""
    tpl_dir = tmp_path / "bad_tpl"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text("[project\nname = 'x'\n", encoding="utf-8")
    assert _load_doctor_template(tpl_dir) is None


def test_load_doctor_template_minimal_pyproject_uses_defaults(tmp_path: Path) -> None:
    """仅含 name 的 pyproject 使用默认值（version/requires-python/app_type）."""
    tpl_dir = tmp_path / "minimal_tpl"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text('[project]\nname = "minimal"\n', encoding="utf-8")
    tpl = _load_doctor_template(tpl_dir)
    assert tpl is not None
    assert tpl.name == "minimal"
    assert tpl.version == "0.0.0"
    assert tpl.requires_python == ">=3.8"
    assert tpl.dependencies == ()
    assert tpl.app_type == "cli"
    assert tpl.description == ""


def test_load_doctor_template_without_name_uses_dir_name(tmp_path: Path) -> None:
    """无 name 的 pyproject 使用目录名作为 name 回退."""
    tpl_dir = tmp_path / "fallback_name"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    tpl = _load_doctor_template(tpl_dir)
    assert tpl is not None
    assert tpl.name == "fallback_name"
    assert tpl.id == "fallback_name"


def test_load_doctor_template_app_type_defaults_to_cli(tmp_path: Path) -> None:
    """无 [tool.fspack] 段时 app_type 默认为 cli."""
    tpl_dir = tmp_path / "no_fspack_section"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text('[project]\nname = "test"\n', encoding="utf-8")
    tpl = _load_doctor_template(tpl_dir)
    assert tpl is not None
    assert tpl.app_type == "cli"


# ---- dataclass 不可变性 ----


def test_template_is_frozen() -> None:
    """Template 是 frozen dataclass，不可变."""
    tpl = get_template("tk_app", role="doctor")
    assert tpl is not None
    with pytest.raises(AttributeError):
        tpl.id = "modified"  # type: ignore[misc]
