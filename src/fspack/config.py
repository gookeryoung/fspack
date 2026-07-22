"""fspack 配置数据结构."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from pathlib import Path

from fspack.platform import Platform

__all__ = [
    "AppType",
    "BuildConfig",
    "DependencyReport",
    "EntryPoint",
    "MirrorConfig",
    "ProjectInfo",
]


class AppType(enum.Enum):
    """应用类型：CLI 控制台或 GUI 窗口."""

    CLI = "cli"
    GUI = "gui"


@dataclass(frozen=True)
class MirrorConfig:
    """国内镜像源配置."""

    name: str
    python_base: str
    pypi_index: str

    def embed_url(self, version: str) -> str:
        """返回指定版本的 embed python zip 下载地址."""
        return f"{self.python_base}/{version}/python-{version}-embed-amd64.zip"


@dataclass(frozen=True)
class EntryPoint:
    """单个打包入口：用于多入口项目生成多个可执行文件."""

    name: str
    module: str
    file: Path
    app_type: AppType

    @classmethod
    def from_script(cls, name: str, script_path: Path) -> EntryPoint:
        """从入口名与脚本路径构造实例。

        ``module`` 取脚本文件名 stem；``app_type`` 惰性导入
        :func:`fspack.project.infer_app_type` 仅按脚本自身 import 推断
        （多入口项目共享 declared，不能据项目级依赖判断单个入口类型）。

        惰性导入打破 config ↔ project 循环依赖。
        """
        from fspack.project import infer_app_type

        return cls(
            name=name,
            module=script_path.stem,
            file=script_path,
            app_type=infer_app_type(script_path, ()),
        )

    def entry_rel(self, src_dir: Path) -> str:
        """入口脚本相对源码目录的 POSIX 路径（用于写入 .entry 文件）."""
        return self.file.relative_to(src_dir).as_posix()


@dataclass(frozen=True)
class ProjectInfo:
    """解析后的项目元信息."""

    name: str
    version: str
    src_dir: Path
    entry_module: str
    entry_file: Path
    app_type: AppType
    dependencies: tuple[str, ...]
    py_version: str
    requires_python: str | None = None
    entries: tuple[EntryPoint, ...] = ()
    icon: Path | None = None

    @classmethod
    def from_dir(cls, project_dir: Path, py_version: str | None = None) -> ProjectInfo:
        """从项目目录解析 pyproject.toml 并构造实例。

        惰性导入 :func:`fspack.project.parse_project` 打破 config ↔ project 循环依赖。
        """
        from fspack.project import parse_project

        return parse_project(project_dir, py_version)

    @property
    def exe_name(self) -> str:
        """生成的可执行文件名（单入口模式）."""
        return f"{self.name}.exe"

    @property
    def py_xy(self) -> str:
        """形如 python311 的版本前缀."""
        major, minor = self.py_version.split(".")[:2]
        return f"python{major}{minor}"

    @property
    def all_entries(self) -> tuple[EntryPoint, ...]:
        """所有入口：多入口模式返回 entries，单入口模式构造单一入口."""
        if self.entries:
            return self.entries
        return (EntryPoint(name=self.name, module=self.entry_module, file=self.entry_file, app_type=self.app_type),)


@dataclass(frozen=True)
class DependencyReport:
    """依赖分析结果."""

    declared: tuple[str, ...]
    ast_third_party: tuple[str, ...]
    ast_stdlib: tuple[str, ...]
    ast_local: tuple[str, ...]
    ast_submodules: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_src(cls, src_dir: Path, project_name: str, declared: tuple[str, ...]) -> DependencyReport:
        """扫描源码目录构造依赖分析报告。

        惰性导入 :func:`fspack.analyzer.analyze_dependencies` 打破 config ↔ analyzer 循环依赖。
        """
        from fspack.analyzer import analyze_dependencies

        return analyze_dependencies(src_dir, project_name, declared)

    @property
    def missing(self) -> tuple[str, ...]:
        """AST 发现但未在 pyproject 声明的第三方依赖."""
        declared_top = {
            re.split(r"[<>=!~;\[]", d, maxsplit=1)[0].strip().replace("-", "_").lower() for d in self.declared
        }
        return tuple(sorted(m for m in self.ast_third_party if m.lower() not in declared_top))


@dataclass(frozen=True)
class BuildConfig:
    """单次构建的运行参数."""

    project_dir: Path
    dist_dir: Path
    embed_cache_dir: Path
    mirror: MirrorConfig
    target: Platform = Platform.WINDOWS
