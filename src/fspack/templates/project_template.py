"""``ProjectTemplate`` 兼容 shim.

本模块原是独立的 doctor 富示例模板加载器，iter-1 起与 :mod:`fspack.templates.registry`
合并为统一 :class:`fspack.templates.registry.Template` 数据结构。本模块保留
``ProjectTemplate`` 类名作为 :class:`Template` 的子类，提供旧 classmethod 接口
（``root_dir``/``list_all``/``from_id``/``from_dir``）以兼容现有调用方
（``doctor_templates.py`` 与 ``test_template_loader.py``）。

新代码应直接使用 :func:`fspack.templates.list_templates` 与
:func:`fspack.templates.get_template`，按 ``role="doctor"`` 过滤。
"""

from __future__ import annotations

from pathlib import Path

from fspack.templates.registry import (
    Template,
    _doctor_templates_root,
    _load_doctor_template,
)

__all__ = ["ProjectTemplate"]


class ProjectTemplate(Template):
    """doctor 富示例模板兼容 shim.

    继承 :class:`fspack.templates.registry.Template`，不新增字段。
    保留旧 classmethod 接口以兼容 :mod:`fspack.doctor_templates` 与测试。

    新代码应直接用 :func:`fspack.templates.list_templates(role="doctor")`
    与 :func:`fspack.templates.get_template(id, role="doctor")`。
    """

    @classmethod
    def root_dir(cls) -> Path:
        """返回 ``assets/templates/`` 目录的绝对路径."""
        return _doctor_templates_root()

    @classmethod
    def list_all(cls) -> list[Template]:
        """扫描 ``assets/templates/<category>/`` 目录，返回所有 doctor 模板（按 id 排序）.

        :return: 模板列表，按 (category, id) 字母序排序
        """
        from fspack.templates.registry import _CATEGORIES

        root = cls.root_dir()
        if not root.is_dir():
            return []
        # 复用 _scan_category_dir 的扫描逻辑，但通过 cls.root_dir() 以支持 monkeypatch
        templates: list[Template] = []
        for category_dir in sorted(root.iterdir()):
            if not category_dir.is_dir() or category_dir.name not in _CATEGORIES:
                continue
            for tpl_dir in sorted(category_dir.iterdir()):
                if not tpl_dir.is_dir():
                    continue
                tpl = _load_doctor_template(tpl_dir)
                if tpl is not None:
                    templates.append(tpl)
        return sorted(templates, key=lambda t: (t.category, t.id))

    @classmethod
    def from_id(cls, template_id: str) -> Template | None:
        """按目录名获取单个 doctor 模板，不存在返回 ``None``.

        :param template_id: 模板 id（目录名，如 ``cli_helloworld``）
        :return: 模板对象或 ``None``
        """
        from fspack.templates.registry import _CATEGORIES

        root = cls.root_dir()
        if not root.is_dir():
            return None
        for category_dir in root.iterdir():
            if not category_dir.is_dir() or category_dir.name not in _CATEGORIES:
                continue
            candidate = category_dir / template_id
            if candidate.is_dir():
                return _load_doctor_template(candidate)
        return None

    @classmethod
    def from_dir(cls, template_dir: Path) -> Template | None:
        """解析单个 doctor 模板目录的 ``pyproject.toml``，返回 :class:`Template`.

        :param template_dir: 模板目录路径（含 ``pyproject.toml``）
        :return: 模板对象或 ``None``（无 ``pyproject.toml`` 或解析失败）

        分类从父目录名推导；若父目录不在分类集合中则分类为空字符串
        （兼容无分类子目录的场景，如测试用 ``tmp_path`` 构造的临时模板）。
        """
        return _load_doctor_template(template_dir)
