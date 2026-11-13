"""模板一致性护栏：以人工评审的契约期望表作为独立 oracle.

护栏目标：模板是最接近用户契约的一层（生成什么文件、声明哪些依赖、允许
哪些 Python 版本），但 ``template.toml`` 与 ``pyproject.toml`` 各自维护同一
份依赖，容易出现「改一处忘另一处」。本测试把关键契约固化为一手维护的
期望表，模板改动必须显式同步此表——**不要**从模板自身派生断言，否则模板
写错时测试会跟着错。

本文件只读模板，不改模板与业务代码（Phase 0 护栏，零行为变更）。
"""

from __future__ import annotations

import pytest

from fspack._compat import tomllib
from fspack.templates import get_template, list_templates

# 人工评审的 init 模板契约期望表（独立 oracle）：
# id: (category, app_type, dependencies, requires-python)
# requires-python 取自模板渲染产物 pyproject.toml，是用户实际拿到的约束。
EXPECTED_INIT_CONTRACT: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "helloworld": ("cli", "cli", (), ">=3.8,<3.12"),
    "requests": ("cli", "cli", ("requests",), ">=3.8,<3.12"),
    "rich": ("cli", "cli", ("rich",), ">=3.8,<3.12"),
    "typer": ("cli", "cli", ("typer",), ">=3.8,<3.12"),
    "full-config": ("config", "cli", (), ">=3.8,<3.12"),
    "multi-entry": ("config", "cli", (), ">=3.8,<3.12"),
    "pyinstaller": ("config", "cli", (), ">=3.8,<3.12"),
    "snake": ("game", "gui", ("pygame",), ">=3.8,<3.12"),
    # PySide2 不支持 Python 3.10+，上界与其余模板不同
    "pyside2": ("gui", "gui", ("PySide2",), ">=3.8,<3.10"),
    "pyside6": ("gui", "gui", ("PySide6",), ">=3.8,<3.12"),
    "pyside6-qml": ("gui", "gui", ("PySide6",), ">=3.8,<3.12"),
    "tkinter": ("gui", "gui", (), ">=3.8,<3.12"),
    "matplotlib": ("sci", "gui", ("matplotlib",), ">=3.8,<3.12"),
    "numpy": ("sci", "cli", ("numpy",), ">=3.8,<3.12"),
    "scipy": ("sci", "cli", ("numpy", "scipy"), ">=3.8,<3.12"),
    "fastapi": ("web", "cli", ("fastapi", "uvicorn"), ">=3.8,<3.12"),
    "flask": ("web", "cli", ("flask",), ">=3.8,<3.12"),
    "web-fastapi-react": ("web", "web", ("fastapi", "uvicorn"), ">=3.8,<3.12"),
}

# doctor 富示例模板 id 集合：测试与 ``fsp doctor`` 共用这些目录，误删会
# 同时打穿产品样例与 fixture，必须显式改本表。
EXPECTED_DOCTOR_IDS: frozenset[str] = frozenset(
    {
        "cli_complex",
        "multi_entry",
        "pygame_snake",
        "pygame_tetris",
        "pyside2_qml_dashboard",
        "tk_app",
        "sci_stack",
        "webview_app",
    }
)

_INIT_IDS = tuple(EXPECTED_INIT_CONTRACT)
# 除 pyside2 外的 init 模板共用同一 requires-python 口径
_INIT_BASELINE_REQUIRES = ">=3.8,<3.12"


def _pyproject(tpl_id: str) -> dict[str, object]:
    """读取并解析 init 模板目录下的 pyproject.toml（占位符不影响 TOML 解析）."""
    tpl = get_template(tpl_id)
    assert tpl is not None, f"模板 {tpl_id} 未注册"
    return tomllib.loads((tpl.dir / "pyproject.toml").read_text(encoding="utf-8"))


def _project(tpl_id: str) -> dict[str, object]:
    proj = _pyproject(tpl_id).get("project")
    assert isinstance(proj, dict), f"模板 {tpl_id} 缺少 [project] 表"
    return proj


def _project_dependencies(tpl_id: str) -> tuple[str, ...]:
    raw = _project(tpl_id).get("dependencies", [])
    assert isinstance(raw, list), f"模板 {tpl_id} 的 dependencies 不是列表"
    return tuple(str(dep) for dep in raw)


def test_init_template_ids_match_contract() -> None:
    """init 模板 id 集合与期望表完全一致（新增/删除必须显式改期望表）."""
    actual = {tpl.id for tpl in list_templates(role="init")}
    assert actual == set(EXPECTED_INIT_CONTRACT), (
        f"init 模板集合漂移：多出 {sorted(actual - set(EXPECTED_INIT_CONTRACT))}，"
        f"缺少 {sorted(set(EXPECTED_INIT_CONTRACT) - actual)}"
    )


@pytest.mark.parametrize("tpl_id", _INIT_IDS)
def test_init_template_metadata_matches_contract(tpl_id: str) -> None:
    """template.toml 元数据（分类/应用类型/依赖）与期望表一致."""
    tpl = get_template(tpl_id)
    assert tpl is not None, f"模板 {tpl_id} 未注册"
    category, app_type, dependencies, _ = EXPECTED_INIT_CONTRACT[tpl_id]
    assert tpl.category == category, f"{tpl_id} 分类应为 {category}"
    assert tpl.app_type == app_type, f"{tpl_id} app_type 应为 {app_type}"
    assert tpl.dependencies == dependencies, f"{tpl_id} 依赖应为 {dependencies}"


@pytest.mark.parametrize("tpl_id", _INIT_IDS)
def test_init_template_dependencies_not_duplicated(tpl_id: str) -> None:
    """同一份依赖不得在两处写出不同值：template.toml 与 pyproject.toml 必须相等."""
    tpl = get_template(tpl_id)
    assert tpl is not None
    declared = _project_dependencies(tpl_id)
    assert declared == tpl.dependencies, f"{tpl_id} 的依赖声明在两份文件中不一致"


@pytest.mark.parametrize("tpl_id", _INIT_IDS)
def test_init_template_requires_python_matches_contract(tpl_id: str) -> None:
    """渲染产物 pyproject.toml 的 requires-python 与期望表一致."""
    _, _, _, expected = EXPECTED_INIT_CONTRACT[tpl_id]
    assert _project(tpl_id).get("requires-python") == expected


def test_init_template_requires_python_single_source() -> None:
    """除 pyside2（PySide2 不支持 3.10+）外，init 模板共用同一版本口径."""
    actual = {tpl_id: _project(tpl_id).get("requires-python") for tpl_id in _INIT_IDS if tpl_id != "pyside2"}
    assert set(actual.values()) == {_INIT_BASELINE_REQUIRES}, f"版本口径分叉：{actual}"


@pytest.mark.parametrize("tpl_id", _INIT_IDS)
def test_init_template_has_renderable_entry(tpl_id: str) -> None:
    """每个 init 模板要么含 ``$entry_module.py``，要么声明 [project.scripts]."""
    tpl = get_template(tpl_id)
    assert tpl is not None
    rel_paths = {f.rel_path for f in tpl.files}
    assert "pyproject.toml" in rel_paths, f"{tpl_id} 缺少 pyproject.toml"
    pyproject_text = next(f.content for f in tpl.files if f.rel_path == "pyproject.toml")
    assert "$entry_module.py" in rel_paths or "[project.scripts]" in pyproject_text, (
        f"{tpl_id} 既无 $entry_module.py 也未声明 [project.scripts]，入口约定缺失"
    )


def test_doctor_template_ids_match_contract() -> None:
    """doctor 模板 id 集合与期望表完全一致（产品样例与 fixture 共用）."""
    actual = {tpl.id for tpl in list_templates(role="doctor")}
    assert actual == EXPECTED_DOCTOR_IDS, (
        f"doctor 模板集合漂移：多出 {sorted(actual - EXPECTED_DOCTOR_IDS)}，缺少 {sorted(EXPECTED_DOCTOR_IDS - actual)}"
    )


def test_template_ids_unique_across_roles() -> None:
    """全量模板 id 唯一：get_template 按 id 查询，重名会静默遮蔽其中一个."""
    ids = [tpl.id for tpl in list_templates(role=None)]
    duplicates = {tpl_id for tpl_id in ids if ids.count(tpl_id) > 1}
    assert not duplicates, f"模板 id 重复：{sorted(duplicates)}"
