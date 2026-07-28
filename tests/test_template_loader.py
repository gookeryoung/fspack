"""项目模板加载器测试：扫描 assets/templates/ 目录并解析元数据."""

from __future__ import annotations

import pytest

from fspack.templates.loader import (
    get_project_template,
    list_project_templates,
    project_templates_dir,
)


def test_project_templates_dir_exists() -> None:
    """项目模板目录存在且是 fspack 包内子目录."""
    tpl_dir = project_templates_dir()
    assert tpl_dir.is_dir()
    assert tpl_dir.name == "templates"
    assert "assets" in str(tpl_dir)


def test_list_project_templates_returns_sorted() -> None:
    """list_project_templates 返回非空列表，按 id 排序."""
    templates = list_project_templates()
    assert len(templates) > 0
    ids = [t.id for t in templates]
    assert ids == sorted(ids)


def test_list_project_templates_has_known_entries() -> None:
    """列表中包含已知的模板项目（迁移自 examples/）."""
    templates = list_project_templates()
    ids = {t.id for t in templates}
    assert "cli_helloworld_pyall" in ids
    assert "pyside2_qml_dashboard_py38" in ids
    assert "sci_numpy_py38" in ids


def test_list_project_templates_metadata_complete() -> None:
    """每个模板的元数据字段完整（id/name/version/requires_python/dependencies）."""
    templates = list_project_templates()
    for tpl in templates:
        assert tpl.id, f"模板 id 为空: {tpl}"
        assert tpl.name, f"模板 {tpl.id} name 为空"
        assert tpl.version, f"模板 {tpl.id} version 为空"
        assert tpl.requires_python, f"模板 {tpl.id} requires_python 为空"
        assert isinstance(tpl.dependencies, tuple), f"模板 {tpl.id} dependencies 不是 tuple"
        assert tpl.dir.is_dir(), f"模板 {tpl.id} dir 不存在: {tpl.dir}"


def test_list_project_templates_dir_matches_id() -> None:
    """每个模板的 dir 名与 id 一致."""
    for tpl in list_project_templates():
        assert tpl.dir.name == tpl.id


def test_get_project_template_existing() -> None:
    """get_project_template 返回已知模板."""
    tpl = get_project_template("cli_helloworld_pyall")
    assert tpl is not None
    assert tpl.id == "cli_helloworld_pyall"
    assert tpl.name == "cli_helloworld_pyall"
    assert tpl.requires_python == ">=3.8"
    assert tpl.dependencies == ()


def test_get_project_template_pyside2_qml() -> None:
    """pyside2_qml_dashboard_py38 模板含 requires-python <3.11 约束."""
    tpl = get_project_template("pyside2_qml_dashboard_py38")
    assert tpl is not None
    assert "<3.11" in tpl.requires_python
    assert any("pyside2" in d.lower() for d in tpl.dependencies)


def test_get_project_template_nonexistent() -> None:
    """get_project_template 对不存在的 id 返回 None."""
    assert get_project_template("nonexistent_template_xyz") is None


def test_project_template_is_frozen() -> None:
    """ProjectTemplate 是 frozen dataclass，不可变."""
    tpl = get_project_template("cli_helloworld_pyall")
    assert tpl is not None
    with pytest.raises(AttributeError):
        tpl.id = "modified"  # type: ignore[misc]
