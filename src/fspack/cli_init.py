"""``fsp init`` 命令：从模板创建新项目.

基于 :mod:`fspack.templates` 渲染引擎生成本地项目目录结构：

- ``fsp init [project_name]`` — stdin 是 TTY 时弹出交互式选择，否则用 helloworld
- ``fsp init --template <id>`` — 指定模板 id，跳过交互
- ``fsp init --python-version <X.Y>`` — 覆盖模板默认 ``requires-python`` 下限
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
import re
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

# Win7 不支持的模板：依赖链含 pydantic-core 等 Rust 扩展，仅提供 Win10+ wheel。
# FastAPI 0.100+ 依赖 pydantic 2.x，pydantic 2.x 依赖 pydantic-core（Rust 编写），
# pydantic-core 的 Windows wheel 调用 Win8+ API（如 PathCchSkipRoot），Win7 无法加载。
_WIN7_UNSUPPORTED_TEMPLATES = frozenset({"fastapi"})

# requires-python 行的正则：匹配 `requires-python = "..."` 形式（含可选空白）
_REQUIRES_PYTHON_RE = re.compile(r'^requires-python = "[^"]*"$', re.MULTILINE)


def _is_windows_7() -> bool:
    """检测当前系统是否是 Windows 7.

    Win7 的 NT 版本号是 6.1（RTM/SP1），Win8 是 6.2，Win8.1 是 6.3，
    Win10/11 是 10.0+。非 Windows 系统返回 ``False``。

    :return: 当前系统是 Win7 返回 ``True``，否则 ``False``
    """
    if not sys.platform.startswith("win"):
        return False
    # sys.getwindowsversion() 仅 Windows 存在；hasattr 运行时守卫 + type: ignore
    # 抑制 pyrefly 在 Linux 上对 sys.getwindowsversion 的 union-attr 报错
    if not hasattr(sys, "getwindowsversion"):
        return False
    win_ver = sys.getwindowsversion()  # type: ignore[union-attr]
    return (win_ver.major, win_ver.minor) == (6, 1)


def _format_requires_python(python_version: str) -> str:
    """根据用户指定的 Python 版本构造 ``requires-python`` 约束字符串.

    :param python_version: Python 版本号（``X.Y`` 格式，如 ``3.8``/``3.10``）
    :return: ``requires-python`` 约束（如 ``>=3.10``），只设下限不设上界，
        保留用户主动选择版本时的灵活性
    :raises ValueError: 版本号格式无效（非 ``X.Y`` 格式或非数字）

    示例：

    - ``3.8`` → ``>=3.8``
    - ``3.10`` → ``>=3.10``
    - ``3.11`` → ``>=3.11``
    """
    parts = python_version.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"无效的 Python 版本号: {python_version!r}，应为 X.Y 格式（如 3.8、3.10）")
    major, minor = parts[0], parts[1]
    return f">={major}.{minor}"


def init_project(
    project_name: str,
    *,
    template_id: str = "helloworld",
    directory: Path | None = None,
    description: str = "",
    python_version: str | None = None,
) -> Path:
    """用指定模板创建项目到目标目录，返回项目根路径.

    :param project_name: 项目名（同时作为目录名与 ``pyproject.toml`` 的 ``name``）
    :param template_id: 模板 id（默认 ``helloworld``）
    :param directory: 父目录（默认当前目录），项目创建在 ``directory / project_name``
    :param description: 项目描述（写入 ``pyproject.toml``）
    :param python_version: 指定 Python 版本（``X.Y`` 格式），覆盖模板默认
        ``requires-python`` 下限；``None`` 用模板默认约束
    :return: 项目根路径
    :raises ValueError: 模板 id 不存在、项目目录已存在、Win7 下选择不兼容模板、
        或 ``python_version`` 格式无效

    创建流程：

    1. 查询模板 id，不存在抛 ``ValueError``
    2. Win7 兼容性检查：FastAPI 依赖 pydantic-core 仅提供 Win10+ wheel，Win7 直接报错
    3. 解析目标目录 ``directory / project_name``，已存在抛 ``ValueError``
    4. 构造渲染变量字典（:func:`default_variables`）
    5. 渲染模板文件树（:func:`render_template`）
    6. 若指定 ``python_version``，覆盖 ``pyproject.toml`` 的 ``requires-python`` 行
    7. 写入文件（``mkdir parents=True`` + ``write_text``）
    8. 打印创建成功提示与下一步命令
    """
    template = get_template(template_id, role="init")
    if template is None:
        available = ", ".join(t.id for t in list_templates(role="init"))
        raise ValueError(f"未知模板 id: {template_id!r}，可用模板: {available}")

    # Win7 兼容性检查：FastAPI 等模板依赖 pydantic-core，Win7 无法运行
    if template_id in _WIN7_UNSUPPORTED_TEMPLATES and _is_windows_7():
        raise ValueError(
            f"模板 {template_id!r} 在 Win7 下不可用：依赖 pydantic 2.x / pydantic-core，"
            "其 Windows wheel 调用 Win8+ API（如 PathCchSkipRoot），Win7 无法加载。"
            "请升级到 Win10+ 或换用 flask 模板。"
        )

    parent = directory or Path.cwd()
    target = parent / project_name
    if target.exists():
        raise ValueError(f"目标目录已存在: {target}")

    variables = default_variables(project_name, description=description)
    try:
        files = render_template(template, variables)
    except TemplateRenderError as exc:
        raise ValueError(f"模板渲染失败: {exc}") from exc

    # 用户指定 Python 版本：覆盖 pyproject.toml 的 requires-python 行
    if python_version is not None:
        requires_python = _format_requires_python(python_version)
        pyproject_path = Path("pyproject.toml")
        if pyproject_path in files:
            content = files[pyproject_path]
            new_line = f'requires-python = "{requires_python}"'
            if _REQUIRES_PYTHON_RE.search(content):
                files[pyproject_path] = _REQUIRES_PYTHON_RE.sub(new_line, content)
            else:
                # 无 requires-python 行时追加到 description 行后
                files[pyproject_path] = re.sub(
                    r'(^description = "[^"]*"$)',
                    rf"\1\n{new_line}",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )

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
    templates = list_templates(role="init")
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
    templates = list_templates(role="init")
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
