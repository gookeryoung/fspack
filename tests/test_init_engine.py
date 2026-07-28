"""模板渲染引擎与 init 命令单元测试.

覆盖 :mod:`fspack.templates.engine`（渲染引擎）与 :mod:`fspack.cli_init`
（init 命令骨架）的核心场景：

- :func:`render_string`：变量替换、缺失变量报错、``$$`` 转义
- :func:`default_variables`：默认变量构造、连字符转下划线、extra 覆盖
- :func:`render_template`：文件树渲染、rel_path 占位符替换
- :func:`get_template`/``list_templates``：模板查询与列表
- :func:`init_project`：项目创建、目录已存在报错、未知模板报错
- :func:`print_template_list`：``--list`` 输出
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fspack.cli_init import init_project, print_template_list
from fspack.templates import (
    Template,
    TemplateFile,
    TemplateRenderError,
    default_variables,
    get_template,
    list_templates,
    render_string,
    render_template,
)

# ---- render_string ----


def test_render_string_substitutes_variable() -> None:
    """render_string 替换 $variable 占位符."""
    assert render_string("hello $name", {"name": "world"}) == "hello world"


def test_render_string_substitutes_braced_variable() -> None:
    """render_string 替换 ${variable} 占位符（变量名含特殊字符时用）."""
    assert render_string("hello ${name}!", {"name": "world"}) == "hello world!"


def test_render_string_escapes_dollar_sign() -> None:
    """render_string 将 $$ 转义为字面量 $."""
    assert render_string("price: $$100", {"name": "x"}) == "price: $100"


def test_render_string_missing_variable_raises() -> None:
    """render_string 遇到未提供的占位符 → 抛 TemplateRenderError."""
    with pytest.raises(TemplateRenderError, match=r"缺少占位符变量 'name'"):
        render_string("hello $name", {})


def test_render_string_invalid_placeholder_syntax_raises() -> None:
    """render_string 遇到无效占位符语法（如未闭合的 ${）→ 抛 TemplateRenderError."""
    with pytest.raises(TemplateRenderError, match=r"占位符语法错误"):
        render_string("hello ${unclosed", {})


def test_render_string_preserves_unchanged_sections() -> None:
    """render_string 仅替换占位符，保留其他文本不变."""
    template = "project: $project_name\nversion: 0.1.0\nentry: $entry_module"
    result = render_string(template, {"project_name": "my-app", "entry_module": "my_app"})
    assert result == "project: my-app\nversion: 0.1.0\nentry: my_app"


# ---- default_variables ----


def test_default_variables_with_project_name_only() -> None:
    """default_variables 仅传 project_name 时用默认值填充 description 与 entry_module."""
    variables = default_variables("my-app")
    assert variables["project_name"] == "my-app"
    assert variables["description"] == ""
    assert variables["entry_module"] == "my_app"  # 连字符转下划线


def test_default_variables_with_explicit_entry_module() -> None:
    """default_variables 显式传 entry_module 时不自动转换."""
    variables = default_variables("my-app", entry_module="custom_main")
    assert variables["entry_module"] == "custom_main"


def test_default_variables_extra_overrides_defaults() -> None:
    """default_variables extra 参数覆盖默认值."""
    variables = default_variables("app", description="custom", author="me")
    assert variables["description"] == "custom"
    assert variables["author"] == "me"


def test_default_variables_underscore_in_project_name_preserved() -> None:
    """default_variables 保留 project_name 中的下划线（不转换）."""
    variables = default_variables("my_app")
    assert variables["project_name"] == "my_app"
    assert variables["entry_module"] == "my_app"


# ---- render_template ----


def test_render_template_renders_all_files() -> None:
    """render_template 渲染模板所有文件，返回 {Path: content} 字典."""
    template = Template(
        id="test",
        name="Test",
        description="test",
        category="cli",
        files=(
            TemplateFile(rel_path="$project_name.py", content='"""$project_name"""\n'),
            TemplateFile(rel_path="pyproject.toml", content='name = "$project_name"\n'),
        ),
    )
    variables = default_variables("my-app")
    rendered = render_template(template, variables)
    assert Path("my-app.py") in rendered
    assert Path("pyproject.toml") in rendered
    assert "my-app" in rendered[Path("my-app.py")]
    assert 'name = "my-app"' in rendered[Path("pyproject.toml")]


def test_render_template_substitutes_rel_path_placeholders() -> None:
    """render_template 渲染 rel_path 中的占位符（如 $entry_module/main.py）."""
    template = Template(
        id="test",
        name="Test",
        description="test",
        category="cli",
        files=(TemplateFile(rel_path="$entry_module/main.py", content="pass\n"),),
    )
    variables = default_variables("my-app")
    rendered = render_template(template, variables)
    assert Path("my_app/main.py") in rendered


def test_render_template_missing_variable_raises() -> None:
    """render_template 渲染时缺少占位符变量 → 抛 TemplateRenderError."""
    template = Template(
        id="test",
        name="Test",
        description="test",
        category="cli",
        files=(TemplateFile(rel_path="main.py", content="$missing_var"),),
    )
    with pytest.raises(TemplateRenderError, match=r"缺少占位符变量"):
        render_template(template, {"project_name": "app"})


# ---- get_template / list_templates ----


def test_get_template_helloworld_exists() -> None:
    """get_template('helloworld') 返回 helloworld 模板."""
    tpl = get_template("helloworld")
    assert tpl is not None
    assert tpl.id == "helloworld"
    assert tpl.name == "Hello World"
    assert tpl.category == "cli"


def test_get_template_unknown_returns_none() -> None:
    """get_template('unknown') 返回 None."""
    assert get_template("nonexistent-template-id") is None


def test_list_templates_returns_sorted_tuple() -> None:
    """list_templates 返回按 (category, id) 排序的元组."""
    templates = list_templates()
    assert isinstance(templates, tuple)
    assert len(templates) >= 1
    # 验证排序：每个元素的 (category, id) 不小于前一个
    keys = [(t.category, t.id) for t in templates]
    assert keys == sorted(keys)


def test_list_templates_contains_helloworld() -> None:
    """list_templates 包含 helloworld 模板."""
    templates = list_templates()
    ids = [t.id for t in templates]
    assert "helloworld" in ids


# ---- init_project ----


def test_init_project_creates_helloworld(tmp_path: Path) -> None:
    """init_project 用 helloworld 模板创建项目，生成 pyproject.toml 与入口脚本."""
    target = init_project("my-app", directory=tmp_path)
    assert target == tmp_path / "my-app"
    assert target.is_dir()
    # pyproject.toml 含项目名
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "my-app"' in pyproject
    # 入口脚本（entry_module = my_app，连字符转下划线）
    entry = target / "my_app.py"
    assert entry.is_file()
    assert "hello, world" in entry.read_text(encoding="utf-8")


def test_init_project_with_description(tmp_path: Path) -> None:
    """init_project 将 description 写入 pyproject.toml."""
    target = init_project("app", directory=tmp_path, description="my description")
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    assert 'description = "my description"' in pyproject


def test_init_project_directory_exists_raises(tmp_path: Path) -> None:
    """init_project 目标目录已存在 → 抛 ValueError."""
    (tmp_path / "existing").mkdir()
    with pytest.raises(ValueError, match=r"目标目录已存在"):
        init_project("existing", directory=tmp_path)


def test_init_project_unknown_template_raises(tmp_path: Path) -> None:
    """init_project 未知模板 id → 抛 ValueError，错误信息列出可用模板."""
    with pytest.raises(ValueError, match=r"未知模板 id.*可用模板"):
        init_project("app", template_id="nonexistent", directory=tmp_path)


def test_init_project_default_directory_is_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init_project directory=None 时用当前目录作为父目录."""
    monkeypatch.chdir(tmp_path)
    target = init_project("auto-app")
    assert target == tmp_path / "auto-app"
    assert target.is_dir()


def test_init_project_helloworld_is_buildable(tmp_path: Path) -> None:
    """init_project 生成的 helloworld 项目可被 ProjectInfo.from_dir 解析.

    验证模板生成的 pyproject.toml 格式正确，能被 fspack 解析。
    不实际执行 build（需 mingw），仅验证项目元信息解析。
    """
    from fspack.config import ProjectInfo

    target = init_project("test-app", directory=tmp_path, description="test")
    info = ProjectInfo.from_dir(target)
    assert info.name == "test-app"
    assert info.version == "0.1.0"


# ---- print_template_list ----


def test_print_template_list_outputs_templates(capsys: pytest.CaptureFixture[str]) -> None:
    """print_template_list 输出包含 helloworld 模板信息."""
    print_template_list()
    captured = capsys.readouterr()
    assert "helloworld" in captured.out
    assert "Hello World" in captured.out
    assert "可用项目模板" in captured.out


def test_print_template_list_shows_usage(capsys: pytest.CaptureFixture[str]) -> None:
    """print_template_list 输出含用法提示."""
    print_template_list()
    captured = capsys.readouterr()
    assert "fsp init" in captured.out
    assert "--template" in captured.out
