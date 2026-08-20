"""项目模板加载器测试：扫描 assets/templates/ 目录并解析元数据.

覆盖 :class:`ProjectTemplate` 的四个 classmethod：

- :meth:`ProjectTemplate.root_dir` — 定位 ``assets/templates`` 目录
- :meth:`ProjectTemplate.list_all` — 扫描全部模板（排序、过滤、元数据完整性）
- :meth:`ProjectTemplate.from_id` — 按 id 查询（命中/未命中/根目录缺失）
- :meth:`ProjectTemplate.from_dir` — 解析单个目录（完整字段/默认值/异常容错）

真实模板（``assets/templates/``）用于验证扫描与元数据；``tmp_path`` 构造的临时
模板用于验证 ``from_dir`` 的解析逻辑与默认值回退，避免依赖真实 assets。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.templates.project_template import ProjectTemplate

# ---- root_dir ----


def test_root_dir_points_to_assets_templates() -> None:
    """root_dir 指向 fspack 包内 ``assets/templates`` 目录且存在."""
    root = ProjectTemplate.root_dir()
    assert root.is_dir()
    assert root.name == "templates"
    assert root.parent.name == "assets"


# ---- list_all：真实模板扫描 ----


def test_list_all_returns_sorted() -> None:
    """list_all 返回非空列表，按分类+id 排序."""
    templates = ProjectTemplate.list_all()
    assert len(templates) > 0
    keys = [(t.category, t.id) for t in templates]
    assert keys == sorted(keys)


def test_list_all_has_known_entries() -> None:
    """列表中包含已知的模板项目（迁移自 examples/）."""
    ids = {t.id for t in ProjectTemplate.list_all()}
    assert "cli_complex" in ids
    assert "pyside2_qml_dashboard" in ids
    assert "sci_numpy" in ids


def test_list_all_metadata_complete() -> None:
    """每个模板的元数据字段完整（id/name/version/requires_python/dependencies）."""
    for tpl in ProjectTemplate.list_all():
        assert tpl.id, f"模板 id 为空: {tpl}"
        assert tpl.name, f"模板 {tpl.id} name 为空"
        assert tpl.version, f"模板 {tpl.id} version 为空"
        assert tpl.requires_python, f"模板 {tpl.id} requires_python 为空"
        assert isinstance(tpl.dependencies, tuple), f"模板 {tpl.id} dependencies 不是 tuple"
        assert tpl.dir.is_dir(), f"模板 {tpl.id} dir 不存在: {tpl.dir}"


def test_list_all_dir_matches_id() -> None:
    """每个模板的 dir 名与 id 一致."""
    for tpl in ProjectTemplate.list_all():
        assert tpl.dir.name == tpl.id


# ---- list_all：边界场景（用 tmp_path + monkeypatch 隔离） ----


def test_list_all_when_root_missing_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """root_dir 指向不存在路径时，list_all 返回空列表."""
    monkeypatch.setattr(ProjectTemplate, "root_dir", classmethod(lambda cls: tmp_path / "nonexistent"))
    assert ProjectTemplate.list_all() == []


def test_list_all_skips_non_dir_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """list_all 跳过根目录下的非目录条目（如 README.md 文件）."""
    (tmp_path / "README.md").write_text("not a template", encoding="utf-8")
    (tmp_path / "cli" / "valid_tpl").mkdir(parents=True)
    (tmp_path / "cli" / "valid_tpl" / "pyproject.toml").write_text(
        '[project]\nname = "valid_tpl"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    monkeypatch.setattr(ProjectTemplate, "root_dir", classmethod(lambda cls: tmp_path))
    templates = ProjectTemplate.list_all()
    assert [t.id for t in templates] == ["valid_tpl"]


def test_list_all_skips_dir_without_pyproject(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """list_all 跳过无 pyproject.toml 的目录（from_dir 返回 None 的项被过滤）."""
    (tmp_path / "cli" / "no_pyproject").mkdir(parents=True)
    (tmp_path / "cli" / "has_pyproject").mkdir(parents=True)
    (tmp_path / "cli" / "has_pyproject" / "pyproject.toml").write_text(
        '[project]\nname = "has_pyproject"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    monkeypatch.setattr(ProjectTemplate, "root_dir", classmethod(lambda cls: tmp_path))
    templates = ProjectTemplate.list_all()
    assert [t.id for t in templates] == ["has_pyproject"]


def test_list_all_skips_dir_with_invalid_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """list_all 跳过 pyproject.toml 解析失败的目录（不抛异常）."""
    (tmp_path / "cli" / "broken").mkdir(parents=True)
    (tmp_path / "cli" / "broken" / "pyproject.toml").write_text("[project\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "cli" / "good").mkdir(parents=True)
    (tmp_path / "cli" / "good" / "pyproject.toml").write_text(
        '[project]\nname = "good"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    monkeypatch.setattr(ProjectTemplate, "root_dir", classmethod(lambda cls: tmp_path))
    templates = ProjectTemplate.list_all()
    assert [t.id for t in templates] == ["good"]


# ---- from_id：真实模板查询 ----


def test_from_id_existing() -> None:
    """from_id 返回已知模板，元数据正确（tk_app 无依赖且用默认 requires-python）."""
    tpl = ProjectTemplate.from_id("tk_app")
    assert tpl is not None
    assert tpl.id == "tk_app"
    assert tpl.name == "tk_app"
    assert tpl.requires_python == ">=3.8"
    assert tpl.dependencies == ()


def test_from_id_pyside2_qml() -> None:
    """pyside2_qml_dashboard 模板含 requires-python <3.10 约束与 pyside2 依赖."""
    tpl = ProjectTemplate.from_id("pyside2_qml_dashboard")
    assert tpl is not None
    assert "<3.10" in tpl.requires_python
    assert any("pyside2" in d.lower() for d in tpl.dependencies)


def test_from_id_nonexistent_returns_none() -> None:
    """from_id 对不存在的 id 返回 None."""
    assert ProjectTemplate.from_id("nonexistent_template_xyz") is None


def test_from_id_when_root_missing_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """root_dir 指向不存在路径时，from_id 返回 None."""
    monkeypatch.setattr(ProjectTemplate, "root_dir", classmethod(lambda cls: tmp_path / "nonexistent"))
    assert ProjectTemplate.from_id("any_id") is None


# ---- from_dir：解析逻辑（用 tmp_path 构造临时模板，隔离真实 assets） ----


def test_from_dir_parses_full_pyproject(tmp_path: Path) -> None:
    """from_dir 解析完整 pyproject.toml，返回 ProjectTemplate 含所有字段."""
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
    tpl = ProjectTemplate.from_dir(tpl_dir)
    assert tpl is not None
    assert tpl.dir == tpl_dir
    assert tpl.id == "my_tpl"
    assert tpl.name == "my-app"
    assert tpl.version == "1.2.3"
    assert tpl.description == "测试模板"
    assert tpl.requires_python == ">=3.10,<3.13"
    assert tpl.dependencies == ("numpy>=1.0", "requests")
    assert tpl.app_type == "gui"


def test_from_dir_without_pyproject_returns_none(tmp_path: Path) -> None:
    """from_dir 对无 pyproject.toml 的目录返回 None."""
    tpl_dir = tmp_path / "empty_tpl"
    tpl_dir.mkdir()
    assert ProjectTemplate.from_dir(tpl_dir) is None


def test_from_dir_with_invalid_toml_returns_none(tmp_path: Path) -> None:
    """from_dir 对非法 TOML 返回 None（捕获 TOMLDecodeError，不抛异常）."""
    tpl_dir = tmp_path / "bad_tpl"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text("[project\nname = 'x'\n", encoding="utf-8")
    assert ProjectTemplate.from_dir(tpl_dir) is None


def test_from_dir_with_minimal_pyproject_uses_defaults(tmp_path: Path) -> None:
    """from_dir 对仅含 name 的 pyproject 使用默认值（version/requires-python/app_type）."""
    tpl_dir = tmp_path / "minimal_tpl"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text('[project]\nname = "minimal"\n', encoding="utf-8")
    tpl = ProjectTemplate.from_dir(tpl_dir)
    assert tpl is not None
    assert tpl.name == "minimal"
    assert tpl.version == "0.0.0"
    assert tpl.requires_python == ">=3.8"
    assert tpl.dependencies == ()
    assert tpl.app_type == "cli"
    assert tpl.description == ""


def test_from_dir_without_name_uses_dir_name(tmp_path: Path) -> None:
    """from_dir 对无 name 的 pyproject 使用目录名作为 name 回退."""
    tpl_dir = tmp_path / "fallback_name"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    tpl = ProjectTemplate.from_dir(tpl_dir)
    assert tpl is not None
    assert tpl.name == "fallback_name"
    assert tpl.id == "fallback_name"


def test_from_dir_app_type_defaults_to_cli(tmp_path: Path) -> None:
    """from_dir 无 [tool.fspack] 段时 app_type 默认为 cli."""
    tpl_dir = tmp_path / "no_fspack_section"
    tpl_dir.mkdir()
    (tpl_dir / "pyproject.toml").write_text('[project]\nname = "test"\n', encoding="utf-8")
    tpl = ProjectTemplate.from_dir(tpl_dir)
    assert tpl is not None
    assert tpl.app_type == "cli"


# ---- dataclass 不可变性 ----


def test_project_template_is_frozen() -> None:
    """ProjectTemplate 是 frozen dataclass，不可变."""
    tpl = ProjectTemplate.from_id("tk_app")
    assert tpl is not None
    with pytest.raises(AttributeError):
        tpl.id = "modified"  # type: ignore[misc]
