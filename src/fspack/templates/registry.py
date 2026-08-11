"""统一模板数据结构与加载器.

定义 :class:`Template`/``TemplateFile`` 数据结构，并从两个目录加载模板：

- ``assets/init_templates/<category>/<name>/`` — ``fsp init`` 内联模板
  - 含 ``template.toml`` 元数据清单 + ``$variable`` 占位符源文件
  - 渲染路径：:func:`fspack.templates.engine.render_template`
  - 默认 ``roles = {"init", "doctor"}``

- ``assets/templates/<category>/<name>/`` — ``fsp doctor`` 富示例模板
  - 含 ``pyproject.toml``（无占位符，doctor 流程直接 ``copytree``）
  - 默认 ``roles = {"doctor"}``

模板用 ``frozen=True`` 的 dataclass 描述，便于作为不可变值传递。
模板注册表通过 :func:`list_templates` 与 :func:`get_template` 公开查询，
支持按 ``role`` 过滤（``"init"``/``"doctor"``/``None`` 全量）。

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

# 默认角色集合：init 模板默认同时可用于 init 与 doctor
_DEFAULT_ROLES: frozenset[str] = frozenset({"init", "doctor"})


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
    """统一项目模板：id + 名称 + 描述 + 分类 + 文件列表 + 依赖 + 元数据.

    合并原 ``fsp init`` 内联模板与 ``fsp doctor`` 富示例模板的元数据结构。
    init 模板的 ``files`` 含占位符源文件（用于渲染），doctor 模板的 ``files``
    为空元组（doctor 流程用 ``dir`` 直接 ``copytree``，不走渲染路径）。

    :param id: 模板唯一标识（如 ``helloworld``/``cli_helloworld``）
    :param name: 显示名称（如 ``Hello World``）
    :param description: 简短描述（一行）
    :param category: 分类（cli/gui/game/sci/web/config）
    :param files: 模板文件元组（init 模板含占位符源文件，doctor 模板为空）
    :param dependencies: 项目依赖元组（写入 ``pyproject.toml`` 的 ``dependencies``）
    :param app_type: 应用类型（cli/gui/web），影响 loader 编译与控制台窗口
    :param py_version: 推荐 Python 版本（如 ``3.11.9``），``None`` 用默认
    :param extra_config: 额外 ``[tool.fspack]`` 配置行（如 ``icon = "assets/app.ico"``）
    :param dir: 模板目录绝对路径（doctor 流程 ``copytree`` 用，init 模板也填充）
    :param version: 项目版本（从 ``pyproject.toml`` 解析，默认 ``0.1.0``）
    :param requires_python: Python 版本约束（从 ``pyproject.toml`` 解析，默认 ``>=3.8``）
    :param roles: 角色集合（``"init"``/``"doctor"``），决定模板出现在哪些命令中
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
    dir: Path = Path()
    version: str = "0.1.0"
    requires_python: str = ">=3.8"
    roles: frozenset[str] = _DEFAULT_ROLES


def _init_templates_root() -> Path:
    """返回 ``assets/init_templates/`` 目录的绝对路径（``fsp init`` 内联模板）."""
    return Path(__file__).resolve().parent.parent / "assets" / "init_templates"


def _doctor_templates_root() -> Path:
    """返回 ``assets/templates/`` 目录的绝对路径（``fsp doctor`` 富示例模板）."""
    return Path(__file__).resolve().parent.parent / "assets" / "templates"


def _templates_root() -> Path:
    """返回 ``assets/init_templates/`` 目录的绝对路径.

    .. deprecated:: 向后兼容别名，新代码用 :func:`_init_templates_root`.
    """
    return _init_templates_root()


def _roles_from_data(data: dict[str, object]) -> frozenset[str]:
    """从 ``template.toml`` 解析的字典中提取 ``roles`` 字段.

    :param data: ``template.toml`` 解析后的字典
    :return: 角色集合，未指定时返回 :data:`_DEFAULT_ROLES`
    """
    raw = data.get("roles")
    if not isinstance(raw, list):
        return _DEFAULT_ROLES
    return frozenset(str(r) for r in raw if isinstance(r, str))


def _load_template(tpl_dir: Path) -> Template | None:
    """从 init 模板目录加载单个模板（含 ``template.toml`` + 占位符源文件）.

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
        dir=tpl_dir,
        version=data.get("version", "0.1.0"),
        requires_python=data.get("requires_python", ">=3.8"),
        roles=_roles_from_data(data),
    )


def _load_doctor_template(tpl_dir: Path) -> Template | None:
    """从 doctor 富示例模板目录加载单个模板（含 ``pyproject.toml``，无占位符）.

    :param tpl_dir: 模板目录路径（含 ``pyproject.toml`` + 源文件）
    :return: 模板对象或 ``None``（无 ``pyproject.toml`` 或解析失败）

    解析 ``pyproject.toml`` 提取元数据（name/version/requires-python/dependencies/
    app-type），``files`` 为空元组（doctor 流程用 ``dir`` 直接 ``copytree``）。
    分类从父目录名推导；``roles`` 固定为 ``{"doctor"}``。
    """
    pyproject = tpl_dir / "pyproject.toml"
    if not pyproject.is_file():
        _logger.warning("跳过无 pyproject.toml 的 doctor 模板目录: %s", tpl_dir.name)
        return None

    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
        _logger.warning("解析 %s/pyproject.toml 失败: %s", tpl_dir.name, e)
        return None

    proj = data.get("project", {})
    fsp = data.get("tool", {}).get("fspack", {})
    parent_name = tpl_dir.parent.name
    category = parent_name if parent_name in _CATEGORIES else ""

    return Template(
        id=tpl_dir.name,
        name=proj.get("name", tpl_dir.name),
        description=proj.get("description", ""),
        category=category,
        files=(),
        dependencies=tuple(proj.get("dependencies", [])),
        app_type=fsp.get("app-type", "cli"),
        dir=tpl_dir,
        version=proj.get("version", "0.0.0"),
        requires_python=proj.get("requires-python", ">=3.8"),
        roles=frozenset({"doctor"}),
    )


def _scan_category_dir(root: Path, loader: object) -> list[Template]:
    """扫描分类目录下的所有模板，用指定加载器加载.

    :param root: 模板根目录（如 ``assets/init_templates/``）
    :param loader: 加载函数（``_load_template`` 或 ``_load_doctor_template``）
    :return: 加载成功的模板列表
    """
    if not root.is_dir():
        return []
    templates: list[Template] = []
    for category_dir in sorted(root.iterdir()):
        if not category_dir.is_dir() or category_dir.name not in _CATEGORIES:
            continue
        for tpl_dir in sorted(category_dir.iterdir()):
            if not tpl_dir.is_dir():
                continue
            tpl = loader(tpl_dir)  # type: ignore[operator]
            if tpl is not None:
                templates.append(tpl)
    return templates


def _load_all() -> tuple[Template, ...]:
    """扫描两个模板目录，加载所有模板（init + doctor）.

    :return: 模板元组，按 (category, id) 字母序排序
    """
    init_templates = _scan_category_dir(_init_templates_root(), _load_template)
    doctor_templates = _scan_category_dir(_doctor_templates_root(), _load_doctor_template)
    all_templates = init_templates + doctor_templates
    _logger.debug(
        "加载 %d 个模板（init=%d, doctor=%d）",
        len(all_templates),
        len(init_templates),
        len(doctor_templates),
    )
    return tuple(all_templates)


def list_templates(role: str | None = None) -> tuple[Template, ...]:
    """返回所有已注册模板，按 (category, id) 字母序排序.

    :param role: 角色过滤（``"init"``/``"doctor"``），``None`` 返回全量
    :return: 模板元组，按 (category, id) 字母序排序

    排序保证 ``--list`` 输出稳定，便于测试与用户查找。
    """
    all_templates = _load_all()
    if role is None:
        return tuple(sorted(all_templates, key=lambda t: (t.category, t.id)))
    filtered = [t for t in all_templates if role in t.roles]
    return tuple(sorted(filtered, key=lambda t: (t.category, t.id)))


def get_template(template_id: str, role: str | None = None) -> Template | None:
    """按 id 查询模板，未找到返回 ``None``.

    :param template_id: 模板 id（如 ``helloworld``/``cli_helloworld``）
    :param role: 角色过滤（``"init"``/``"doctor"``），``None`` 在所有模板中查
    :return: 模板对象或 ``None``
    """
    for tpl in _load_all():
        if tpl.id != template_id:
            continue
        if role is not None and role not in tpl.roles:
            continue
        return tpl
    return None
