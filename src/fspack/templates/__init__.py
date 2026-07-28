"""项目模板包：``fsp init`` 命令的模板渲染引擎与注册表.

提供基于 :class:`string.Template` 的轻量模板引擎，支持 ``$variable`` 与
``${variable}`` 占位符替换。模板由 :class:`Template` 数据结构描述，
包含 id/名称/描述/分类/文件列表与依赖声明。

公共 API：

- :class:`Template` — 模板数据结构（id/name/description/category/files/dependencies）
- :class:`TemplateFile` — 模板文件（相对路径 + 内容模板）
- :func:`render_template` — 渲染模板文件树，返回 ``{输出路径: 内容}``
- :func:`list_templates` — 列出所有已注册模板
- :func:`get_template` — 按 id 查询模板
"""

from __future__ import annotations

from fspack.templates.engine import (
    TemplateRenderError,
    default_variables,
    render_string,
    render_template,
)
from fspack.templates.registry import Template, TemplateFile, get_template, list_templates

__all__ = [
    "Template",
    "TemplateFile",
    "TemplateRenderError",
    "default_variables",
    "get_template",
    "list_templates",
    "render_string",
    "render_template",
]
