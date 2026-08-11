"""项目模板加载器：从 ``assets/templates/`` 目录扫描项目模板.

每个模板是 ``assets/templates/<category>/<name>/`` 下的完整项目（含
``pyproject.toml`` + 源码），用于：

- :command:`fsp doctor --test` — 运行所有模板构建，验证打包流程
- :command:`fsp doctor --bench` — 性能基准测试
- E2E 测试（取代原 ``examples/`` 目录）

与 :mod:`fspack.templates.registry` 的 ``fsp init`` 内联模板不同，本模块加载
的是更完整的项目模板（多文件、有 README、真实业务逻辑），用于集成测试与
性能基准，而非快速创建新项目。

模板目录结构（分类子目录 + 模板目录）::

    assets/templates/
    ├── cli/
    │   ├── cli_helloworld/
    │   │   ├── pyproject.toml
    │   │   └── helloworld.py
    │   └── cli_office/
    │       └── ...
    ├── gui/
    │   ├── pyside2_qml_dashboard/
    │   │   ├── pyproject.toml
    │   │   ├── main.py
    │   │   ├── views/
    │   │   └── ...
    │   └── ...
    └── ...

模板 id 为目录名（含分类前缀，如 ``cli_helloworld``），分类为父目录名（如 ``cli``）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fspack._compat import tomllib

__all__ = ["ProjectTemplate"]

_logger = logging.getLogger(__name__)

# 支持的分类目录名（与 fsp init 模板分类一致）
_CATEGORIES: frozenset[str] = frozenset({"cli", "gui", "game", "sci", "web", "config"})


@dataclass(frozen=True)
class ProjectTemplate:
    """项目模板：目录路径 + 元数据.

    :param dir: 模板目录绝对路径
    :param id: 模板唯一标识（目录名，含分类前缀如 ``cli_helloworld``）
    :param name: 项目名（从 ``pyproject.toml`` 读取）
    :param version: 项目版本
    :param requires_python: Python 版本约束（如 ``">=3.8,<3.11"``）
    :param dependencies: 依赖列表
    :param app_type: 应用类型（cli/gui/web）
    :param description: 项目描述
    :param category: 分类（cli/gui/game/sci/web/config，从父目录名推导）
    """

    dir: Path
    id: str
    name: str
    version: str
    requires_python: str
    dependencies: tuple[str, ...]
    app_type: str
    description: str
    category: str

    @classmethod
    def root_dir(cls) -> Path:
        """返回 ``assets/templates/`` 目录的绝对路径."""
        return Path(__file__).resolve().parent.parent / "assets" / "templates"

    @classmethod
    def list_all(cls) -> list[ProjectTemplate]:
        """扫描 ``assets/templates/<category>/`` 目录，返回所有有效项目模板（按 id 排序）.

        扫描分类子目录（cli/gui/game/sci/web/config），每个子目录下的模板目录
        含 ``pyproject.toml`` 即为有效模板。非分类目录（如临时文件）被跳过。
        """
        root = cls.root_dir()
        if not root.is_dir():
            _logger.warning("项目模板目录不存在: %s", root)
            return []

        templates: list[ProjectTemplate] = []
        for category_dir in sorted(root.iterdir()):
            if not category_dir.is_dir() or category_dir.name not in _CATEGORIES:
                continue
            for entry in sorted(category_dir.iterdir()):
                if not entry.is_dir():
                    continue
                tpl = cls.from_dir(entry)
                if tpl is not None:
                    templates.append(tpl)
        _logger.debug("加载 %d 个项目模板 from %s", len(templates), root)
        return templates

    @classmethod
    def from_id(cls, template_id: str) -> ProjectTemplate | None:
        """按目录名获取单个项目模板，不存在返回 ``None``.

        :param template_id: 模板 id（目录名，如 ``cli_helloworld``）
        :return: 模板对象或 ``None``

        在所有分类子目录下搜索同名目录。例如 ``cli_helloworld`` 会在
        ``cli/cli_helloworld/`` 找到。
        """
        root = cls.root_dir()
        if not root.is_dir():
            return None
        for category_dir in root.iterdir():
            if not category_dir.is_dir() or category_dir.name not in _CATEGORIES:
                continue
            candidate = category_dir / template_id
            if candidate.is_dir():
                return cls.from_dir(candidate)
        return None

    @classmethod
    def from_dir(cls, template_dir: Path) -> ProjectTemplate | None:
        """解析单个模板目录的 ``pyproject.toml``，返回 :class:`ProjectTemplate`.

        目录无 ``pyproject.toml`` 或解析失败时返回 ``None`` 并记录 warning。
        分类从父目录名推导；若父目录不在 :data:`_CATEGORIES` 中则分类为空字符串
        （兼容无分类子目录的场景，如测试用 ``tmp_path`` 构造的临时模板）。
        """
        pyproject = template_dir / "pyproject.toml"
        if not pyproject.is_file():
            _logger.warning("跳过无 pyproject.toml 的目录: %s", template_dir.name)
            return None

        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
            _logger.warning("解析 %s/pyproject.toml 失败: %s", template_dir.name, e)
            return None

        proj = data.get("project", {})
        fsp = data.get("tool", {}).get("fspack", {})
        # 分类从父目录名推导；父目录不在 _CATEGORIES 中时分类为空（兼容测试场景）
        parent_name = template_dir.parent.name
        category = parent_name if parent_name in _CATEGORIES else ""
        return ProjectTemplate(
            dir=template_dir,
            id=template_dir.name,
            name=proj.get("name", template_dir.name),
            version=proj.get("version", "0.0.0"),
            requires_python=proj.get("requires-python", ">=3.8"),
            dependencies=tuple(proj.get("dependencies", [])),
            app_type=fsp.get("app-type", "cli"),
            description=proj.get("description", ""),
            category=category,
        )
