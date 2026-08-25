"""init 命令模板列表与交互式选择测试.

覆盖 :func:`fspack.cli_init.prompt_template_selection` 与 CLI 入口的
``--template`` 未指定时的交互式选择分发逻辑。

测试场景：

- 非 TTY 环境（CI/管道）→ ``prompt_template_selection`` 返回 ``helloworld`` 默认值
- TTY 环境 + 向导导航（down/enter 按键序列）→ 返回对应模板 id
- TTY 环境 + Esc 取消 → 抛 ``KeyboardInterrupt``
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


def _wizard_keys_to(template_id: str, templates: list[Any]) -> list[str]:
    """构造向导两步选择指定模板的按键序列（down×N + enter，两步）.

    第一步选模板所在分类（分类按字母序），第二步选分类内模板（字母序），
    位置动态推导避免硬编码（cli 分类按 args/click/helloworld/... 排序）。
    """
    target = next(t for t in templates if t.id == template_id)
    categories = list(dict.fromkeys(t.category for t in templates))
    cat_index = categories.index(target.category)
    in_category = [t for t in templates if t.category == target.category]
    tpl_index = next(i for i, t in enumerate(in_category) if t.id == template_id)
    return ["down"] * cat_index + ["enter"] + ["down"] * tpl_index + ["enter"]


def _patch_wizard_keys(monkeypatch: pytest.MonkeyPatch, keys: list[str]) -> None:
    """将向导读键替换为按序弹出按键事件的桩（TTY 模拟）."""
    from fspack import wizard

    seq = iter(keys)
    monkeypatch.setattr(wizard, "_read_key", lambda: next(seq))


def test_prompt_template_selection_tty_returns_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY 环境 + 向导导航到 helloworld → 返回 helloworld.

    list_templates 按 (category, id) 字母序排序，按键序列动态推导：
    第一步选中 helloworld 所在分类，第二步选中分类内的 helloworld。
    """
    from fspack.templates import list_templates

    templates = list_templates(role="init")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_wizard_keys(monkeypatch, _wizard_keys_to("helloworld", list(templates)))
    result = prompt_template_selection()
    assert result == "helloworld"


def test_prompt_template_selection_tty_returns_second_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY 环境 + 向导导航到注入模板 → 返回注入的模板 id.

    注入 id=``zzz-custom`` 的模板，按 (category, id) 字母序排在 cli 分类末尾。
    注入模板的 roles 须含 ``"init"`` 才会被 ``prompt_template_selection``
    的 ``role="init"`` 过滤命中。
    """
    from fspack.templates import Template, TemplateFile, list_templates
    from fspack.templates import registry as registry_mod

    # 临时注入第二个模板：monkeypatch _load_all 在原模板基础上追加自定义模板
    extra_template = Template(
        id="zzz-custom",
        name="Custom",
        description="test",
        category="cli",
        files=(TemplateFile(rel_path="main.py", content="pass\n"),),
        roles=frozenset({"init"}),
    )
    original_load_all = registry_mod._load_all
    monkeypatch.setattr(registry_mod, "_load_all", lambda: (*original_load_all(), extra_template))
    templates = list(list_templates(role="init"))

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_wizard_keys(monkeypatch, _wizard_keys_to("zzz-custom", templates))
    result = prompt_template_selection()
    assert result == "zzz-custom"


def test_prompt_template_selection_tty_cancel_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY 环境 + Esc 取消 → 抛 KeyboardInterrupt（CLI 层打印已取消）."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_wizard_keys(monkeypatch, ["esc"])
    with pytest.raises(KeyboardInterrupt):
        prompt_template_selection()


def test_prompt_template_selection_empty_registry_returns_helloworld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空模板注册表 → 返回 helloworld 默认值（防御性回退）."""
    from fspack.templates import registry as registry_mod

    monkeypatch.setattr(registry_mod, "_load_all", lambda: ())
    result = prompt_template_selection()
    assert result == "helloworld"


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


# ---- CLI --python-version 覆盖 requires-python ----


def test_cli_init_python_version_overrides_requires_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fsp init <name> --python-version 3.10 → pyproject.toml requires-python = ">=3.10"."""
    monkeypatch.chdir(tmp_path)
    cli_main(["init", "ver-app", "--template", "helloworld", "--python-version", "3.10"])
    pyproject = (tmp_path / "ver-app" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in pyproject
    # 模板默认约束被覆盖（helloworld 默认 >=3.8,<3.12）
    assert "<3.12" not in pyproject


def test_cli_init_python_version_3_8_for_pyside2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fsp init --python-version 3.8 --template pyside2 → requires-python = ">=3.8".

    覆盖 PySide2 模板默认的 ``>=3.8,<3.11`` 约束为 ``>=3.8``（去掉上界）。
    """
    monkeypatch.chdir(tmp_path)
    cli_main(["init", "p2-app", "--template", "pyside2", "--python-version", "3.8"])
    pyproject = (tmp_path / "p2-app" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.8"' in pyproject
    assert "<3.11" not in pyproject


def test_cli_init_python_version_invalid_format_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """fsp init --python-version 3 → 无效格式（缺 minor），打印错误并退出码 1."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["init", "bad-app", "--template", "helloworld", "--python-version", "3"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "无效的 Python 版本号" in captured.out


def test_cli_init_python_version_non_numeric_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """fsp init --python-version 3.x → 非数字 minor，打印错误并退出码 1."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["init", "bad-app2", "--template", "helloworld", "--python-version", "3.x"])
    assert exc_info.value.code == 1


def test_init_project_python_version_none_keeps_default(tmp_path: Path) -> None:
    """init_project(python_version=None) → 保持模板默认 requires-python."""
    from fspack.cli_init import init_project

    target = init_project("def-app", template_id="helloworld", directory=tmp_path)
    pyproject = (target / "pyproject.toml").read_text(encoding="utf-8")
    # helloworld 默认约束 >=3.8,<3.12
    assert 'requires-python = ">=3.8,<3.12"' in pyproject


# ---- _format_requires_python 单元测试 ----


def test_format_requires_python_3_8() -> None:
    """3.8 → >=3.8."""
    from fspack.cli_init import _format_requires_python

    assert _format_requires_python("3.8") == ">=3.8"


def test_format_requires_python_3_10() -> None:
    """3.10 → >=3.10."""
    from fspack.cli_init import _format_requires_python

    assert _format_requires_python("3.10") == ">=3.10"


def test_format_requires_python_3_11() -> None:
    """3.11 → >=3.11."""
    from fspack.cli_init import _format_requires_python

    assert _format_requires_python("3.11") == ">=3.11"


def test_format_requires_python_invalid_single_component() -> None:
    """3 → ValueError（缺 minor）."""
    from fspack.cli_init import _format_requires_python

    with pytest.raises(ValueError, match="无效的 Python 版本号"):
        _format_requires_python("3")


def test_format_requires_python_invalid_non_numeric() -> None:
    """3.x → ValueError（minor 非数字）."""
    from fspack.cli_init import _format_requires_python

    with pytest.raises(ValueError, match="无效的 Python 版本号"):
        _format_requires_python("3.x")


def test_format_requires_python_invalid_empty() -> None:
    """空字符串 → ValueError."""
    from fspack.cli_init import _format_requires_python

    with pytest.raises(ValueError, match="无效的 Python 版本号"):
        _format_requires_python("")


# ---- Win7 + fastapi 拦截 ----


def test_is_windows_7_non_windows_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 系统 → _is_windows_7 返回 False."""
    from fspack.cli_init import _is_windows_7

    monkeypatch.setattr("sys.platform", "linux")
    assert _is_windows_7() is False


def test_is_windows_7_win10_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Win10（NT 10.0）→ _is_windows_7 返回 False."""
    from fspack.cli_init import _is_windows_7

    monkeypatch.setattr("sys.platform", "win32")

    class _FakeWinVer:
        major = 10
        minor = 0

    monkeypatch.setattr("sys.getwindowsversion", _FakeWinVer, raising=False)
    assert _is_windows_7() is False


def test_is_windows_7_win7_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Win7（NT 6.1）→ _is_windows_7 返回 True."""
    from fspack.cli_init import _is_windows_7

    monkeypatch.setattr("sys.platform", "win32")

    class _FakeWinVer:
        major = 6
        minor = 1

    monkeypatch.setattr("sys.getwindowsversion", _FakeWinVer, raising=False)
    assert _is_windows_7() is True


def test_init_project_fastapi_on_win7_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Win7 下选择 fastapi 模板 → ValueError 提示 Win7 不可用."""
    from fspack.cli_init import init_project

    monkeypatch.setattr("fspack.cli_init._is_windows_7", lambda: True)
    with pytest.raises(ValueError, match="Win7 下不可用"):
        init_project("api-app", template_id="fastapi", directory=tmp_path)


def test_init_project_fastapi_on_non_win7_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Win7 下选择 fastapi 模板 → 正常创建项目."""
    from fspack.cli_init import init_project

    monkeypatch.setattr("fspack.cli_init._is_windows_7", lambda: False)
    target = init_project("api-ok", template_id="fastapi", directory=tmp_path)
    assert target.is_dir()
    entry = (target / "api_ok.py").read_text(encoding="utf-8")
    assert "from fastapi" in entry


def test_init_project_helloworld_on_win7_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Win7 下选择 helloworld 模板 → 正常创建（helloworld 不在 Win7 黑名单）."""
    from fspack.cli_init import init_project

    monkeypatch.setattr("fspack.cli_init._is_windows_7", lambda: True)
    target = init_project("hw-win7", template_id="helloworld", directory=tmp_path)
    assert target.is_dir()


def test_cli_init_fastapi_on_win7_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """fsp init --template fastapi（Win7）→ 打印错误并退出码 1."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("fspack.cli_init._is_windows_7", lambda: True)
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["init", "win7-api", "--template", "fastapi"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Win7" in captured.out
    assert "fastapi" in captured.out.lower() or "fastapi" in captured.out
