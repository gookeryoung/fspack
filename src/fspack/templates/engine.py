"""模板渲染引擎：基于 :class:`string.Template` 的文件树生成.

提供 :func:`render_template` 函数，将 :class:`Template` 渲染为
``{输出路径: 内容}`` 字典。占位符替换遵循 :class:`string.Template` 规则：

- ``$variable`` — 简单变量替换（变量名匹配 ``[a-zA-Z_][a-zA-Z0-9_]*``）
- ``${variable}`` — 大括号包裹的变量替换（变量名含特殊字符时用）
- ``$$`` — 转义为字面量 ``$``

公共 API：

- :func:`render_template` — 渲染模板文件树
- :func:`render_string` — 渲染单个字符串
- :func:`default_variables` — 构造默认变量字典
- :class:`TemplateRenderError` — 渲染错误（缺失占位符值等）
"""

from __future__ import annotations

import string
from pathlib import Path

from fspack.templates.registry import Template

__all__ = ["TemplateRenderError", "default_variables", "render_string", "render_template"]


class TemplateRenderError(Exception):
    """模板渲染错误（缺失占位符值、无效变量名等）."""


def default_variables(
    project_name: str,
    *,
    description: str = "",
    entry_module: str | None = None,
    **extra: str,
) -> dict[str, str]:
    """构造模板渲染默认变量字典.

    :param project_name: 项目名（写入 ``pyproject.toml`` 的 ``name`` 字段）
    :param description: 项目描述（默认空字符串）
    :param entry_module: 入口模块名（默认用项目名，连字符转下划线）
    :param extra: 额外变量（覆盖默认值）
    :return: 变量字典，键为占位符名，值为替换内容

    项目名规范化：

    - 入口模块名：连字符 ``-`` 转下划线 ``_``（如 ``my-app`` → ``my_app``）
    - 仅作为 Python 模块名规范，``project_name`` 本身不转换（保留 PyPI 包名）
    """
    if entry_module is None:
        entry_module = project_name.replace("-", "_")
    variables: dict[str, str] = {
        "project_name": project_name,
        "description": description,
        "entry_module": entry_module,
    }
    variables.update(extra)
    return variables


def render_string(template_str: str, variables: dict[str, str]) -> str:
    """渲染单个字符串，返回替换后的内容.

    :param template_str: 含 ``$variable``/``${variable}`` 占位符的模板字符串
    :param variables: 变量字典
    :return: 渲染后的字符串
    :raises TemplateRenderError: 模板含未提供值的占位符
    """
    template = string.Template(template_str)
    try:
        return template.substitute(variables)
    except KeyError as exc:
        missing_key = str(exc).strip("'\"")
        raise TemplateRenderError(
            f"模板渲染失败：缺少占位符变量 {missing_key!r}，已提供变量: {sorted(variables.keys())}"
        ) from exc
    except ValueError as exc:
        raise TemplateRenderError(f"模板渲染失败：占位符语法错误: {exc}") from exc


def render_template(template: Template, variables: dict[str, str]) -> dict[Path, str]:
    """渲染模板文件树，返回 ``{输出路径: 内容}`` 字典.

    :param template: 模板对象
    :param variables: 渲染变量字典（由 :func:`default_variables` 构造）
    :return: 渲染后的文件树，键为输出文件相对路径，值为文件内容
    :raises TemplateRenderError: 任意文件渲染失败

    ``rel_path`` 与 ``content`` 均用 :func:`render_string` 替换占位符。
    ``rel_path`` 渲染后转 :class:`pathlib.Path`（POSIX 风格路径自动适配平台）。
    """
    rendered: dict[Path, str] = {}
    for file in template.files:
        rel_path_str = render_string(file.rel_path, variables)
        content = render_string(file.content, variables)
        rendered[Path(rel_path_str)] = content
    return rendered
