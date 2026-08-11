"""模板数据结构与文件模板加载器.

定义 :class:`Template`/``TemplateFile`` 数据结构，并从
``assets/init_templates/<category>/<name>/`` 目录加载文件模板。

模板用 ``frozen=True`` 的 dataclass 描述，便于作为不可变值传递。每个模板目录含：

- ``template.toml`` — 元数据清单（id/名称/描述/分类/依赖/应用类型等）
- 源文件 — 含 ``$variable``/``${variable}`` 占位符（:class:`string.Template` 语法）

模板分类（``category``）：

- ``cli`` — 命令行工具
- ``gui`` — 桌面 GUI 应用
- ``game`` — 游戏开发
- ``sci`` — 科学计算
- ``web`` — Web 服务
- ``config`` — 配置示例

模板注册表通过 :func:`list_templates` 与 :func:`get_template` 公开查询。

注意：模板内容用 :class:`string.Template` 渲染，``$variable``/``${variable}``
是占位符。代码中字面量 ``$`` 需用 ``$$`` 转义。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fspack._compat import tomllib

__all__ = ["Template", "TemplateFile", "get_template", "list_templates"]

_logger = logging.getLogger(__name__)

# 支持的分类目录名
_CATEGORIES: frozenset[str] = frozenset({"cli", "gui", "game", "sci", "web", "config"})


@dataclass(frozen=True)
class TemplateFile:
    """模板文件：相对路径 + 内容模板（``string.Template`` 语法）.

    ``rel_path`` 支持占位符（如 ``$project_name/main.py``），渲染时与
    ``content`` 一并替换。``content`` 中的 ``$variable`` 与 ``${variable}``
    占位符按 :class:`string.Template` 规则替换。

    :param rel_path: 模板内相对路径（POSIX 风格，渲染后转 ``Path``）
    :param content: 文件内容模板
    """

    rel_path: str
    content: str


@dataclass(frozen=True)
class Template:
    """项目模板：id + 名称 + 描述 + 分类 + 文件列表 + 依赖.

    :param id: 模板唯一标识（如 ``helloworld``/``pyside2``）
    :param name: 显示名称（如 ``Hello World``）
    :param description: 简短描述（一行）
    :param category: 分类（cli/gui/game/sci/web/config）
    :param files: 模板文件元组
    :param dependencies: 项目依赖元组（写入 ``pyproject.toml`` 的 ``dependencies``）
    :param app_type: 应用类型（cli/gui/web），影响 loader 编译与控制台窗口
    :param py_version: 推荐 Python 版本（如 ``3.11.9``），``None`` 用默认
    :param extra_config: 额外 ``[tool.fspack]`` 配置行（如 ``icon = "assets/app.ico"``）
    """

    id: str
    name: str
    description: str
    category: str
    files: tuple[TemplateFile, ...]
    dependencies: tuple[str, ...] = ()
    app_type: str = "cli"
    py_version: str | None = None
    extra_config: str = ""


def _templates_root() -> Path:
    """返回 ``assets/init_templates/`` 目录的绝对路径."""
    return Path(__file__).resolve().parent.parent / "assets" / "init_templates"


def _load_template(tpl_dir: Path) -> Template | None:
    """从模板目录加载单个模板.

    :param tpl_dir: 模板目录路径（含 ``template.toml`` + 源文件）
    :return: 模板对象或 ``None``（无 ``template.toml`` 或解析失败）

    读取 ``template.toml`` 元数据，扫描目录下所有非 ``template.toml`` 文件
    作为模板源文件（含 ``$variable`` 占位符原样读入）。
    """
    manifest = tpl_dir / "template.toml"
    if not manifest.is_file():
        _logger.warning("跳过无 template.toml 的目录: %s", tpl_dir.name)
        return None

    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        _logger.warning("解析 %s/template.toml 失败: %s", tpl_dir.name, e)
        return None

    # 扫描模板源文件（排除 template.toml），按相对路径排序保证 list 输出稳定
    files: list[TemplateFile] = []
    for file_path in sorted(tpl_dir.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.name == "template.toml":
            continue
        rel_path = file_path.relative_to(tpl_dir).as_posix()
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as e:
            _logger.warning("读取模板文件 %s/%s 失败: %s", tpl_dir.name, rel_path, e)
            continue
        files.append(TemplateFile(rel_path=rel_path, content=content))

    if not files:
        _logger.warning("模板 %s 无源文件", tpl_dir.name)
        return None

    return Template(
        id=data["id"],
        name=data["name"],
        description=data["description"],
        category=data["category"],
        files=tuple(files),
        dependencies=tuple(data.get("dependencies", [])),
        app_type=data.get("app_type", "cli"),
        py_version=data.get("py_version"),
        extra_config=data.get("extra_config", ""),
    )


def _load_all() -> tuple[Template, ...]:
    """扫描 ``assets/init_templates/<category>/`` 目录，加载所有模板.

    :return: 模板元组，按 (category, id) 字母序排序
    """
    root = _templates_root()
    if not root.is_dir():
        _logger.warning("init 模板目录不存在: %s", root)
        return ()

    templates: list[Template] = []
    for category_dir in sorted(root.iterdir()):
        if not category_dir.is_dir() or category_dir.name not in _CATEGORIES:
            continue
        for tpl_dir in sorted(category_dir.iterdir()):
            if not tpl_dir.is_dir():
                continue
            tpl = _load_template(tpl_dir)
            if tpl is not None:
                templates.append(tpl)
    _logger.debug("加载 %d 个 init 模板 from %s", len(templates), root)
    return tuple(templates)


def list_templates() -> tuple[Template, ...]:
    """返回所有已注册模板，按 (category, id) 字母序排序.

    排序保证 ``--list`` 输出稳定，便于测试与用户查找。
    """
    return tuple(sorted(_load_all(), key=lambda t: (t.category, t.id)))


def get_template(template_id: str) -> Template | None:
    """按 id 查询模板，未找到返回 ``None``.

    :param template_id: 模板 id（如 ``helloworld``）
    :return: 模板对象或 ``None``
    """
    for tpl in _load_all():
        if tpl.id == template_id:
            return tpl
    return None
