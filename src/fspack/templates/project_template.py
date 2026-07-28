"""项目模板加载器：从 ``assets/templates/`` 目录扫描项目模板.

每个子目录是一个完整的项目模板（含 ``pyproject.toml`` + 源码），用于：

- :command:`fsp doctor --test` — 运行所有模板构建，验证打包流程
- :command:`fsp doctor --bench` — 性能基准测试
- E2E 测试（取代原 ``examples/`` 目录）

与 :mod:`fspack.templates.registry` 的 ``fsp init`` 内联模板不同，本模块加载
的是更完整的项目模板（多文件、有 README、真实业务逻辑），用于集成测试与
性能基准，而非快速创建新项目。

模板目录结构::

    assets/templates/
    ├── cli_helloworld_pyall/
    │   ├── pyproject.toml
    │   └── helloworld.py
    ├── pyside2_qml_dashboard_py38/
    │   ├── pyproject.toml
    │   ├── main.py
    │   ├── views/
    │   │   ├── Main.qml
    │   │   └── ...
    │   └── ...
    └── ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fspack._compat import tomllib

__all__ = ["ProjectTemplate"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectTemplate:
    """项目模板：目录路径 + 元数据.

    :param dir: 模板目录绝对路径
    :param id: 模板唯一标识（目录名）
    :param name: 项目名（从 ``pyproject.toml`` 读取）
    :param version: 项目版本
    :param requires_python: Python 版本约束（如 ``">=3.8,<3.11"``）
    :param dependencies: 依赖列表
    :param app_type: 应用类型（cli/gui/web）
    :param description: 项目描述
    """

    dir: Path
    id: str
    name: str
    version: str
    requires_python: str
    dependencies: tuple[str, ...]
    app_type: str
    description: str

    @classmethod
    def root_dir(cls) -> Path:
        """返回 ``assets/templates/`` 目录的绝对路径."""
        return Path(__file__).resolve().parent.parent / "assets" / "templates"

    @classmethod
    def list_all(cls) -> list[ProjectTemplate]:
        """扫描 ``assets/templates/`` 目录，返回所有有效项目模板（按 id 排序）."""
        root = cls.root_dir()
        if not root.is_dir():
            _logger.warning("项目模板目录不存在: %s", root)
            return []

        templates: list[ProjectTemplate] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            tpl = cls.from_dir(entry)
            if tpl is not None:
                templates.append(tpl)
        _logger.debug("加载 %d 个项目模板 from %s", len(templates), root)
        return templates

    @classmethod
    def from_id(cls, template_id: str) -> ProjectTemplate | None:
        """按目录名获取单个项目模板，不存在返回 ``None``."""
        dir_path = cls.root_dir() / template_id
        if not dir_path.is_dir():
            return None
        return cls.from_dir(dir_path)

    @classmethod
    def from_dir(cls, template_dir: Path) -> ProjectTemplate | None:
        """解析单个模板目录的 ``pyproject.toml``，返回 :class:`ProjectTemplate`.

        目录无 ``pyproject.toml`` 或解析失败时返回 ``None`` 并记录 warning。
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
        return ProjectTemplate(
            dir=template_dir,
            id=template_dir.name,
            name=proj.get("name", template_dir.name),
            version=proj.get("version", "0.0.0"),
            requires_python=proj.get("requires-python", ">=3.8"),
            dependencies=tuple(proj.get("dependencies", [])),
            app_type=fsp.get("app-type", "cli"),
            description=proj.get("description", ""),
        )
