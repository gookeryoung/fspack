"""包内资源定位：唯一允许硬编码 ``resources/`` 布局的模块.

所有需要读取包内数据（icon/loader/runtime/templates）的模块都应通过本模块
提供的函数定位，而不是各自用 ``Path(__file__).parent...`` 拼路径。资源目录
随 wheel 分发，布局变更只需改这里与 ``pyproject.toml`` 的 ``data-dirs``。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["PACKAGE_ROOT", "RESOURCES_ROOT", "resource_path", "templates_root"]

# core/resources.py -> core -> fspack
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RESOURCES_ROOT = PACKAGE_ROOT / "resources"

# 模板角色 → 目录名；新增角色时同步 pyproject.toml data-dirs 与 test_resources.py
_TEMPLATE_ROLES = frozenset({"init", "doctor"})


def resource_path(*parts: str) -> Path:
    """返回 ``resources/<parts...>`` 的绝对路径（不做存在性检查）."""
    return RESOURCES_ROOT.joinpath(*parts)


def templates_root(role: str) -> Path:
    """返回 ``resources/templates/<role>`` 的绝对路径.

    :param role: ``"init"``（``fsp init`` 内联模板）或 ``"doctor"``（富示例模板）
    :raises ValueError: 未知角色
    """
    if role not in _TEMPLATE_ROLES:
        raise ValueError(f"未知模板角色: {role!r}，可选: {sorted(_TEMPLATE_ROLES)}")
    return resource_path("templates", role)
