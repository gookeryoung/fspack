"""``fsp init`` 命令：从模板创建新项目.

基于 :mod:`fspack.templates` 渲染引擎生成本地项目目录结构：

- ``fsp init [project_name]`` — stdin 是 TTY 时弹出交互式选择，否则用 helloworld
- ``fsp init --template <id>`` — 指定模板 id，跳过交互
- ``fsp init --python-version <X.Y>`` — 覆盖模板默认 ``requires-python`` 下限
- ``fsp init --list`` — 列出所有可用模板

交互式选择为两步向导（:mod:`fspack.wizard`，↑/↓ 移动 + Enter 确认，
Esc/q 取消）：先选项目类型（分类），再选该类型下的具体模板，高亮项下方
动态显示描述、依赖与生成文件结构。零新增依赖（rich 已是 fspack 依赖）。
非 TTY 环境（CI/管道）自动跳过交互。

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
from typing import Final

from fspack.console import console
from fspack.templates import (
    Template,
    TemplateRenderError,
    default_variables,
    get_template,
    list_templates,
    render_template,
)
from fspack.wizard import select_item

__all__ = ["init_project", "print_template_list", "prompt_template_selection"]

_logger = logging.getLogger(__name__)

# Win7 不支持的模板：依赖链含 pydantic-core 等 Rust 扩展，仅提供 Win10+ wheel。
# FastAPI 0.100+ 依赖 pydantic 2.x，pydantic 2.x 依赖 pydantic-core（Rust 编写），
# pydantic-core 的 Windows wheel 调用 Win8+ API（如 PathCchSkipRoot），Win7 无法加载。
_WIN7_UNSUPPORTED_TEMPLATES = frozenset({"fastapi"})

# requires-python 行的正则：匹配 `requires-python = "..."` 形式（含可选空白）
_REQUIRES_PYTHON_RE = re.compile(r'^requires-python = "[^"]*"$', re.MULTILINE)

# 模板 pyproject.toml 内容中的 requires-python 行（详情区提取约束显示用）
_TEMPLATE_REQUIRES_RE = re.compile(r'^requires-python = "([^"]+)"$', re.MULTILINE)

# 视为基线的模板 requires-python 约束（当前全部模板的默认值，偏离时才在详情区显示）
_DEFAULT_TEMPLATE_REQUIRES: Final = ">=3.8,<3.12"

# 详情区结构行最多显示的文件数（超出折叠为 "等 N 个文件"）
_DETAIL_MAX_FILES: Final = 6

# 分类目录名 → 向导第一步显示的项目类型标签
_CATEGORY_LABELS: Final[dict[str, str]] = {
    "cli": "CLI 命令行工具",
    "config": "工程配置范例",
    "game": "游戏开发",
    "gui": "桌面 GUI 应用",
    "sci": "科学计算",
    "web": "Web 服务",
}


def _category_label(category: str) -> str:
    """返回分类的向导显示标签，未知分类回退分类名本身.

    :param category: 分类目录名（cli/gui/game/sci/web/config）
    :return: 中文标签（如 ``CLI 命令行工具``），未知分类返回原名，
        空分类名返回 ``其他``
    """
    return _CATEGORY_LABELS.get(category, category or "其他")


def _display_rel_path(rel_path: str) -> str:
    """将模板相对路径中的占位符替换为用户可读形式（详情区显示用）.

    :param rel_path: 模板文件相对路径（可能含 ``$entry_module``/``$project_name``
        及其大括号形式占位符）
    :return: 可读路径（如 ``$entry_module.py`` → ``<项目名>.py``）
    """
    return (
        rel_path.replace("${entry_module}", "<项目名>")
        .replace("$entry_module", "<项目名>")
        .replace("${project_name}", "<项目名>")
        .replace("$project_name", "<项目名>")
    )


def _template_requires_python(tpl: Template) -> str:
    """从模板 pyproject.toml 内容提取 requires-python 约束.

    :param tpl: 模板对象
    :return: 约束字符串（如 ``>=3.8,<3.10``），未声明返回空串
    """
    for file in tpl.files:
        if file.rel_path != "pyproject.toml":
            continue
        matched = _TEMPLATE_REQUIRES_RE.search(file.content)
        return matched.group(1) if matched else ""
    return ""


def _template_detail(tpl: Template) -> str:
    """构造向导第二步高亮模板的详情文本（描述 + 依赖 + 结构 + Python 约束）.

    结构行从模板文件列表自动推导（与实际生成文件树一致，无需在
    template.toml 手工维护）；Python 行仅在约束偏离模板基线
    （``>=3.8,<3.12``）时显示，聚焦例外模板（如 pyside2 需 ``<3.10``）。

    :param tpl: 模板对象
    :return: 详情文本（描述行 + 依赖行 + 结构行 + 可选 Python 行，全空返回空串）
    """
    lines: list[str] = []
    if tpl.description:
        lines.append(tpl.description)
    if tpl.dependencies:
        lines.append(f"依赖: {', '.join(tpl.dependencies)}")
    if tpl.files:
        rel_paths = [f.rel_path for f in tpl.files]
        shown = ", ".join(_display_rel_path(p) for p in rel_paths[:_DETAIL_MAX_FILES])
        suffix = f" 等 {len(rel_paths)} 个文件" if len(rel_paths) > _DETAIL_MAX_FILES else ""
        lines.append(f"结构: {shown}{suffix}")
    requires = _template_requires_python(tpl)
    if requires and requires != _DEFAULT_TEMPLATE_REQUIRES:
        lines.append(f"Python: {requires}")
    return "\n".join(lines)


def _insert_after_project_header(content: str, new_line: str) -> str:
    """在 pyproject.toml 的 ``[project]`` 节头后插入一行.

    requires-python 行与 description 行都不存在时（模板缺两者）的兜底：
    按行扫描找 ``[project]`` 行，紧随其后插入 ``new_line``；未找到
    ``[project]`` 节时原样返回（无法定位插入点，不强行追加避免 TOML 错位）。

    :param content: pyproject.toml 原始内容
    :param new_line: 待插入的行（如 ``requires-python = ">=3.10"``）
    :return: 插入后的内容
    """
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == "[project]":
            lines.insert(i + 1, new_line + "\n")
            return "".join(lines)
    return content


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

    :param python_version: Python 版本号（``X.Y`` 格式，如 ``3.8``/``3.10``/
        ``3.13t`` 自由线程版本）
    :return: ``requires-python`` 约束（如 ``>=3.10``），只设下限不设上界，
        保留用户主动选择版本时的灵活性
    :raises ValueError: 版本号格式无效（非 ``X.Y`` 格式或非数字）

    示例：

    - ``3.8`` → ``>=3.8``
    - ``3.10`` → ``>=3.10``
    - ``3.11`` → ``>=3.11``
    - ``3.13t`` → ``>=3.13``（free-threaded build 的 t 后缀仅影响运行时
        选择，``requires-python`` 不区分 t 变体，标准版与 free-threaded
        版本号主体相同，``>=3.13`` 同时匹配两者）
    """
    # 剥离 free-threaded build 的 t 后缀（PEP 703/779），requires-python
    # 不区分 t 变体——下游 _satisfies 比较时也剥离后缀按纯数字版本判定
    base = python_version[:-1] if python_version.endswith("t") else python_version
    parts = base.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"无效的 Python 版本号: {python_version!r}，应为 X.Y 格式（如 3.8、3.10、3.13t）")
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
                # 已有 requires-python 行：直接替换
                files[pyproject_path] = _REQUIRES_PYTHON_RE.sub(new_line, content)
            else:
                # 无 requires-python 行：优先追加到 description 行后
                new_content = re.sub(
                    r'(^description = "[^"]*"$)',
                    rf"\1\n{new_line}",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )
                if new_content == content:
                    # description 行也不存在（sub 不命中时静默返回原串）：
                    # 兜底在 [project] 节头后插入，避免覆盖静默失效
                    new_content = _insert_after_project_header(content, new_line)
                files[pyproject_path] = new_content

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
    """向导式选择模板：先选项目类型，再选该类型下的具体模板.

    TTY 环境下两步向导（↑/↓ 移动 + Enter 确认，Esc/q 取消），非 TTY
    环境直接返回 ``helloworld`` 默认值（避免阻塞 CI）。

    :return: 选中的模板 id
    :raises KeyboardInterrupt: 用户按 Esc/q 或 Ctrl+C 中断选择

    交互流程（两步，第二步高亮项下方动态显示描述/依赖/生成结构）::

        ? [1/2] 选择项目类型
        > CLI 命令行工具（6 个模板）
          桌面 GUI 应用（6 个模板）
          ...

        ? [2/2] 选择模板 · CLI 命令行工具
        > helloworld — Hello World
          args — argparse 命令行参数
          ...

          最小 Hello World 示例，验证基础流水线
    """
    templates = list_templates(role="init")
    if not templates:
        _logger.warning("无可用模板，使用 helloworld")
        return "helloworld"

    # 非 TTY 环境（CI/管道）跳过交互，用默认值
    if not sys.stdin.isatty():
        _logger.info("非交互式环境，使用默认模板 helloworld")
        return "helloworld"

    # 第一步：按分类聚合。list_templates 已按 (category, id) 字母序排序，
    # dict 按首次出现顺序插入，分类即保持字母序，无需重排
    by_category: dict[str, list[Template]] = {}
    for tpl in templates:
        by_category.setdefault(tpl.category, []).append(tpl)
    categories = list(by_category)

    cat_idx = select_item(
        "选择项目类型",
        [f"{_category_label(cat)}（{len(by_category[cat])} 个模板）" for cat in categories],
        step=(1, 2),
    )
    category = categories[cat_idx]

    # 第二步：选中分类下的模板列表（保持字母序），高亮项动态显示详情
    candidates = by_category[category]
    tpl_idx = select_item(
        f"选择模板 · {_category_label(category)}",
        [f"{tpl.id} — {tpl.name}" for tpl in candidates],
        detail=lambda i: _template_detail(candidates[i]),
        step=(2, 2),
    )
    selected = candidates[tpl_idx]
    _logger.info("已选择模板: %s (%s)", selected.id, selected.name)
    console.success(f"已选择模板: {selected.id}（{selected.name}）")
    return selected.id
