"""测试用工程 fixture 工厂.

集中构造「最小可解析工程」的 ``pyproject.toml`` + 入口脚本，替代各测试
文件里逐字复制的同一段模板。只归并真正重复的形态（最小工程、带依赖、
带 ``[tool.fspack]``、多入口、doctor 富示例复制）；**故意构造异常输入**
的用例（非法 TOML、非 UTF-8 字节、字段类型错误、缺 ``[project]`` 等）
保留在测试内联，工厂不做兜底，以免掩盖被测的容错分支。
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from fspack.templates import get_template

# 最小入口脚本：可被 ``detect_entry`` 识别且 ``def main`` 可调用
MINIMAL_ENTRY = "def main():\n    pass\n"


# 工厂以关键字参数覆盖各类最小工程，参数多属预期
def write_project(  # noqa: PLR0913
    root: Path,
    *,
    name: str = "app",
    version: str | None = "0.1",
    requires_python: str | None = None,
    dependencies: Sequence[str] = (),
    entry_file: str = "app.py",
    entry_source: str | None = MINIMAL_ENTRY,
    scripts: Mapping[str, str] | None = None,
    tool_fspack: str | None = None,
) -> Path:
    """在 ``root`` 写入最小工程并返回 ``root``.

    :param root: 工程根目录（须已存在）
    :param name: ``[project].name``
    :param version: ``[project].version``，``None`` 表示不写该字段
    :param requires_python: ``[project].requires-python``，``None`` 表示不写
    :param dependencies: ``[project].dependencies``，空序列表示不写该字段
    :param entry_file: 入口脚本相对路径（默认 ``app.py``）
    :param entry_source: 入口脚本内容，``None`` 表示不写入口文件
    :param scripts: ``[project.scripts]`` 表（多入口场景）
    :param tool_fspack: ``[tool.fspack]`` 表体（raw TOML 行），``None`` 表示不写
    """
    lines = ["[project]", f'name = "{name}"']
    if version is not None:
        lines.append(f'version = "{version}"')
    if requires_python is not None:
        lines.append(f'requires-python = "{requires_python}"')
    if dependencies:
        lines.append("dependencies = [" + ", ".join(f'"{dep}"' for dep in dependencies) + "]")
    if scripts:
        lines.extend(["", "[project.scripts]"])
        lines.extend(f'{key} = "{value}"' for key, value in scripts.items())
    if tool_fspack is not None:
        lines.extend(["", "[tool.fspack]"])
        lines.extend(tool_fspack.strip().splitlines())
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if entry_source is not None:
        (root / entry_file).write_text(entry_source, encoding="utf-8")
    return root


def doctor_template_dir(template_id: str) -> Path:
    """返回 doctor 富示例模板目录（``fsp doctor`` 产品样例兼测试 fixture）.

    :param template_id: doctor 模板 id（如 ``tk_app``）
    """
    tpl = get_template(template_id)
    assert tpl is not None and "doctor" in tpl.roles, f"doctor 模板 {template_id} 未注册"
    return tpl.dir


def copy_doctor_project(template_id: str, destination: Path) -> Path:
    """复制 doctor 富示例模板到 ``destination``，返回 ``destination``.

    :param template_id: doctor 模板 id（如 ``tk_app``）
    :param destination: 目标目录（须不存在）
    """
    shutil.copytree(
        doctor_template_dir(template_id),
        destination,
        ignore=shutil.ignore_patterns("dist", "__pycache__"),
    )
    return destination
