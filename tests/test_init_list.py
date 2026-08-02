"""init 命令模板列表与交互式选择测试.

覆盖 :func:`fspack.cli_init.prompt_template_selection` 与 CLI 入口的
``--template`` 未指定时的交互式选择分发逻辑。

测试场景：

- 非 TTY 环境（CI/管道）→ ``prompt_template_selection`` 返回 ``helloworld`` 默认值
- TTY 环境 + 用户输入数字 → 返回对应模板 id
- TTY 环境 + 用户 Ctrl+C → 抛 ``KeyboardInterrupt``
- CLI ``--list`` → 打印模板列表后退出（不创建项目）
- CLI 未指定 ``--template`` + 非 TTY → 用 helloworld 创建项目
- CLI 显式 ``--template helloworld`` → 跳过交互，直接创建
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fspack.cli import main as cli_main
from fspack.cli_init import prompt_template_selection

# ---- prompt_template_selection ----


def test_prompt_template_selection_non_tty_returns_helloworld() -> None:
    """非 TTY 环境（如 CI/管道）→ 返回 helloworld 默认值，不阻塞等待输入.

    测试运行环境本身是非 TTY（pytest 捕获 stdin），直接验证回退行为。
    """
    result = prompt_template_selection()
    assert result == "helloworld"


def test_prompt_template_selection_tty_returns_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY 环境 + 用户输入 helloworld 在列表中的编号 → 返回 helloworld.

    list_templates 按 (category, id) 字母序排序，helloworld 并非第 1 个，
    动态查询其位置避免硬编码（cli 分类按 args/click/helloworld/... 排序）。
    """
    from fspack.templates import list_templates

    templates = list_templates()
    helloworld_index = next(i for i, t in enumerate(templates, 1) if t.id == "helloworld")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def fake_ask(prompt: str, *, choices: list[str], default: str, console: Any = None) -> int:
        assert str(helloworld_index) in choices
        return helloworld_index

    monkeypatch.setattr("rich.prompt.IntPrompt.ask", staticmethod(fake_ask))
    result = prompt_template_selection()
    assert result == "helloworld"


def test_prompt_template_selection_tty_returns_second_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY 环境 + 用户输入注入模板在列表中的编号 → 返回注入的模板 id.

    注入 id=``zzz-custom`` 的模板，按 (category, id) 字母序排在 cli 分类末尾。
    动态查询其位置避免硬编码。
    """
    from fspack.templates import Template, TemplateFile, list_templates
    from fspack.templates import registry as registry_mod

    # 临时注入第二个模板
    extra_template = Template(
        id="zzz-custom",
        name="Custom",
        description="test",
        category="cli",
        files=(TemplateFile(rel_path="main.py", content="pass\n"),),
    )
    original = registry_mod._TEMPLATES
    registry_mod._TEMPLATES = (*original, extra_template)
    try:
        templates = list_templates()
        custom_index = next(i for i, t in enumerate(templates, 1) if t.id == "zzz-custom")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("rich.prompt.IntPrompt.ask", staticmethod(lambda *a, **kw: custom_index))
        result = prompt_template_selection()
        assert result == "zzz-custom"
    finally:
        registry_mod._TEMPLATES = original


def test_prompt_template_selection_empty_registry_returns_helloworld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空模板注册表 → 返回 helloworld 默认值（防御性回退）."""
    from fspack.templates import registry as registry_mod

    original = registry_mod._TEMPLATES
    registry_mod._TEMPLATES = ()
    try:
        result = prompt_template_selection()
        assert result == "helloworld"
    finally:
        registry_mod._TEMPLATES = original


# ---- CLI --list 分发 ----


def test_cli_init_list_prints_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    """fsp init --list → 打印模板列表后退出，不创建项目."""
    cli_main(["init", "--list"])
    captured = capsys.readouterr()
    assert "helloworld" in captured.out
    assert "可用项目模板" in captured.out


def test_cli_init_list_with_alias_i(capsys: pytest.CaptureFixture[str]) -> None:
    """fsp i --list → 别名 i 同样支持 --list."""
    cli_main(["i", "--list"])
    captured = capsys.readouterr()
    assert "helloworld" in captured.out


# ---- CLI 未指定 --template + 非 TTY → 用 helloworld ----


def test_cli_init_no_template_non_tty_uses_helloworld(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fsp init <name> + 非 TTY → 用 helloworld 模板创建项目.

    测试环境是非 TTY（pytest 捕获 stdin），prompt_template_selection 返回 helloworld。
    """
    monkeypatch.chdir(tmp_path)
    cli_main(["init", "auto-app"])
    target = tmp_path / "auto-app"
    assert target.is_dir()
    assert (target / "pyproject.toml").is_file()
    assert (target / "auto_app.py").is_file()  # 连字符转下划线


# ---- CLI 显式 --template → 跳过交互 ----


def test_cli_init_explicit_template_skips_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fsp init <name> --template helloworld → 直接用 helloworld 创建，不调用交互选择."""
    monkeypatch.chdir(tmp_path)

    # 守卫：若误触发交互选择，抛错使测试失败
    def fail_prompt() -> str:
        raise AssertionError("显式指定 --template 时不应触发交互选择")

    monkeypatch.setattr("fspack.cli_init.prompt_template_selection", fail_prompt)
    cli_main(["init", "my-app", "--template", "helloworld"])
    target = tmp_path / "my-app"
    assert target.is_dir()
    assert (target / "pyproject.toml").is_file()


def test_cli_init_unknown_template_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """fsp init <name> --template nonexistent → 打印错误并退出码 1."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["init", "app", "--template", "nonexistent"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "未知模板 id" in captured.out


# ---- CLI --description 透传 ----


def test_cli_init_description_passed_to_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fsp init <name> --description <desc> → 描述写入 pyproject.toml."""
    monkeypatch.chdir(tmp_path)
    cli_main(["init", "app", "--template", "helloworld", "--description", "my desc"])
    pyproject = (tmp_path / "app" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'description = "my desc"' in pyproject


# ---- CLI --directory 指定父目录 ----


def test_cli_init_directory_option(tmp_path: Path) -> None:
    """fsp init <name> --directory <path> → 项目创建在指定目录下."""
    parent = tmp_path / "parent"
    parent.mkdir()
    cli_main(["init", "sub-app", "--template", "helloworld", "--directory", str(parent)])
    target = parent / "sub-app"
    assert target.is_dir()
    assert (target / "pyproject.toml").is_file()
