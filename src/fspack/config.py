"""fspack 配置数据结构。."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AppType",
    "BuildConfig",
    "DependencyReport",
    "MirrorConfig",
    "ProjectInfo",
]


class AppType(enum.Enum):
    """应用类型：CLI 控制台或 GUI 窗口。."""

    CLI = "cli"
    GUI = "gui"


@dataclass(frozen=True)
class MirrorConfig:
    """国内镜像源配置。."""

    name: str
    python_base: str
    pypi_index: str

    def embed_url(self, version: str) -> str:
        """返回指定版本的 embed python zip 下载地址。."""
        return f"{self.python_base}/{version}/python-{version}-embed-amd64.zip"


@dataclass(frozen=True)
class ProjectInfo:
    """解析后的项目元信息。."""

    name: str
    version: str
    src_dir: Path
    entry_module: str
    entry_file: Path
    app_type: AppType
    dependencies: tuple[str, ...]
    py_version: str

    @property
    def exe_name(self) -> str:
        """生成的可执行文件名。."""
        return f"{self.name}.exe"

    @property
    def py_xy(self) -> str:
        """形如 python311 的版本前缀。."""
        major, minor = self.py_version.split(".")[:2]
        return f"python{major}{minor}"


@dataclass(frozen=True)
class DependencyReport:
    """依赖分析结果。."""

    declared: tuple[str, ...]
    ast_third_party: tuple[str, ...]
    ast_stdlib: tuple[str, ...]
    ast_local: tuple[str, ...]

    @property
    def missing(self) -> tuple[str, ...]:
        """AST 发现但未在 pyproject 声明的第三方依赖。."""
        declared_top = {
            re.split(r"[<>=!~;\[]", d, maxsplit=1)[0].strip().replace("-", "_").lower() for d in self.declared
        }
        return tuple(sorted(m for m in self.ast_third_party if m.lower() not in declared_top))


@dataclass(frozen=True)
class BuildConfig:
    """单次构建的运行参数。."""

    project_dir: Path
    dist_dir: Path
    embed_cache_dir: Path
    mirror: MirrorConfig
    arch: str = "win_amd64"
