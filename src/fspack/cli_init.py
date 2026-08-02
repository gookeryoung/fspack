"""``fsp init`` 命令：从模板创建新项目.

基于 :mod:`fspack.templates` 渲染引擎生成本地项目目录结构：

- ``fsp init [project_name]`` — stdin 是 TTY 时弹出交互式选择，否则用 helloworld
- ``fsp init --template <id>`` — 指定模板 id，跳过交互
- ``fsp init --list`` — 列出所有可用模板

交互式选择用 rich 渲染分类列表 + :class:`rich.prompt.IntPrompt` 接收数字选择，
零依赖（rich 已是 fspack 依赖）。非 TTY 环境（CI/管道）自动跳过交互。

公共 API：

- :func:`init_project` — 创建项目到指定目录（供 CLI 调用）
- :func:`print_template_list` — 打印模板列表（供 ``--list`` 调用）
- :func:`prompt_template_selection` — 交互式选择模板（供 ``--template`` 未指定时调用）
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from fspack.console import console
from fspack.templates import (
    TemplateRenderError,
    default_variables,
    get_template,
    list_templates,
    render_template,
)

__all__ = ["init_project", "print_template_list", "prompt_template_selection"]

_logger = logging.getLogger(__name__)


def init_project(
    project_name: str,
    *,
    template_id: str = "helloworld",
    directory: Path | None = None,
    description: str = "",
) -> Path:
    """用指定模板创建项目到目标目录，返回项目根路径.

    :param project_name: 项目名（同时作为目录名与 ``pyproject.toml`` 的 ``name``）
    :param template_id: 模板 id（默认 ``helloworld``）
    :param directory: 父目录（默认当前目录），项目创建在 ``directory / project_name``
    :param description: 项目描述（写入 ``pyproject.toml``）
    :return: 项目根路径
    :raises ValueError: 模板 id 不存在或项目目录已存在

    创建流程：

    1. 查询模板 id，不存在抛 ``ValueError``
    2. 解析目标目录 ``directory / project_name``，已存在抛 ``ValueError``
    3. 构造渲染变量字典（:func:`default_variables`）
    4. 渲染模板文件树（:func:`render_template`）
    5. 写入文件（``mkdir parents=True`` + ``write_text``）
    6. 打印创建成功提示与下一步命令
    """
    template = get_template(template_id)
    if template is None:
        available = ", ".join(t.id for t in list_templates())
        raise ValueError(f"未知模板 id: {template_id!r}，可用模板: {available}")

    parent = directory or Path.cwd()
    target = parent / project_name
    if target.exists():
        raise ValueError(f"目标目录已存在: {target}")

    variables = default_variables(project_name, description=description)
    try:
        files = render_template(template, variables)
    except TemplateRenderError as exc:
        raise ValueError(f"模板渲染失败: {exc}") from exc

    target.mkdir(parents=True, exist_ok=False)
    for rel_path, content in files.items():
        abs_path = target / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    _logger.info("已创建项目: %s（模板: %s）", target, template.name)
    console.success(f"已创建项目 {project_name}（模板: {template.name}）")
    console.rich.print(f"  目录: {target}")
    console.rich.print("  下一步:")
    console.rich.print(f"    cd {project_name}")
    console.rich.print("    fsp b                    # 打包")
    return target


def print_template_list() -> None:
    """打印所有可用模板列表（供 ``--list`` 调用）.

    按 (category, id) 字母序输出，格式::

        可用项目模板（共 N 个）：

          [cli] helloworld — Hello World
                  最小 Hello World 示例，验证基础流水线

          ...
    """
    templates = list_templates()
    console.step(f"可用项目模板（共 {len(templates)} 个）")
    console.rich.print()
    current_category = ""
    for tpl in templates:
        if tpl.category != current_category:
            current_category = tpl.category
            console.rich.print(f"  [bold cyan][{tpl.category}][/]")
        console.rich.print(f"    [bold]{tpl.id}[/] — {tpl.name}")
        console.rich.print(f"      {tpl.description}")
        if tpl.dependencies:
            deps = ", ".join(tpl.dependencies)
            console.rich.print(f"      [dim]依赖: {deps}[/]")
    console.rich.print()
    console.rich.print("用法: fsp init <project_name> --template <id>")


def prompt_template_selection() -> str:
    """交互式选择模板，返回选中的模板 id.

    用 rich 渲染分类编号列表，用 :class:`rich.prompt.IntPrompt` 接收数字选择。
    非 TTY 环境调用此函数会直接返回 ``helloworld`` 默认值（避免阻塞 CI）。

    :return: 选中的模板 id
    :raises KeyboardInterrupt: 用户按 Ctrl+C 中断选择

    输出格式::

        可用项目模板（共 N 个）：

          [cli]
            1. helloworld — Hello World
            2. args — argparse 命令行参数
          [gui]
            7. pyside2 — PySide2 桌面 GUI

        请选择模板 [1-N] (默认 1):
    """
    templates = list_templates()
    if not templates:
        _logger.warning("无可用模板，使用 helloworld")
        return "helloworld"

    # 非 TTY 环境（CI/管道）跳过交互，用默认值
    if not sys.stdin.isatty():
        _logger.info("非交互式环境，使用默认模板 helloworld")
        return "helloworld"

    console.step(f"可用项目模板（共 {len(templates)} 个）")
    console.rich.print()
    current_category = ""
    for index, tpl in enumerate(templates, 1):
        if tpl.category != current_category:
            current_category = tpl.category
            console.rich.print(f"  [bold cyan][{tpl.category}][/]")
        console.rich.print(f"    [bold]{index:>2}[/]. {tpl.id} — {tpl.name}")
        console.rich.print(f"        [dim]{tpl.description}[/]")
    console.rich.print()

    from rich.prompt import IntPrompt

    total = len(templates)
    choice = int(
        IntPrompt.ask(
            "[bold]请选择模板[/]",
            choices=[str(i) for i in range(1, total + 1)],
            default="1",
            console=console.rich,
        )
    )
    selected = templates[choice - 1]
    _logger.info("已选择模板: %s (%s)", selected.id, selected.name)
    return selected.id
