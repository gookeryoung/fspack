"""pyproject.toml 解析与项目入口识别.

本模块从 :mod:`fspack.config` 抽离，含 :func:`parse_project` 入口解析流程、
``[tool.fspack]`` 配置项解析、入口脚本 AST 识别与 app 类型推断。
``config.py`` 通过 re-export 保持公开 API 不变。

依赖 :mod:`fspack.config.models` 提供 dataclass 与 :func:`_parse_string_list_cfg`，
:mod:`fspack.config.versions` 提供默认 Python 版本，:mod:`fspack.analyzer`
在 :func:`infer_app_type` 中延迟导入打破循环依赖。
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

from fspack.config.models import (
    AppType,
    BuildDefaults,
    EntryPoint,
    ProjectInfo,
    SlimRules,
    _parse_string_list_cfg,
)
from fspack.config.versions import DEFAULT_PY_VERSION
from fspack.exceptions import ProjectError

__all__ = [
    "detect_entry",
    "infer_app_type",
    "parse_project",
]

_logger = logging.getLogger(__name__)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]
    except ImportError as e:  # pragma: no cover
        raise ProjectError("解析 pyproject.toml 需要 tomli（Python<3.11），请安装 tomli") from e


# [tool.fspack] 构建默认值键名与 BuildDefaults 字段的映射
_BUILD_DEFAULT_KEYS: dict[str, str] = {
    "nuitka": "nuitka",
    "pyc_strip": "pyc_strip",
    "pyc_optimize": "pyc_optimize",
    "no_site": "no_site",
    "no_pyc": "no_pyc",
    "no_stdlib_trim": "no_stdlib_trim",
    "ccache": "ccache",
}

# GUI 框架导入名集合：用于按入口脚本 import 推断 AppType
_GUI_HINTS = frozenset({"tkinter", "PySide2", "PySide6", "PyQt5", "PyQt6", "matplotlib", "wx", "win32gui", "pygame"})


def parse_project(project_dir: Path, py_version: str | None = None) -> ProjectInfo:
    """解析 pyproject.toml 并识别入口，返回项目元信息。

    支持多入口声明 ``[tool.fspack.entries]``：键为入口名（用作 exe 名），
    值为入口脚本相对项目目录的路径（POSIX 风格）。声明多入口时，
    ``ProjectInfo.entries`` 非空，``entry_module``/``entry_file``/``app_type``
    取首个入口（保持向后兼容）。
    """
    project_dir = Path(project_dir).resolve()
    pp = project_dir / "pyproject.toml"
    if not pp.is_file():
        raise ProjectError(f"未找到 pyproject.toml: {pp}")
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ProjectError(f"pyproject.toml 语法错误: {e}") from e

    proj = data.get("project", {})
    if not isinstance(proj, dict):
        raise ProjectError("pyproject.toml [project] 节格式异常")
    name = str(proj.get("name") or project_dir.name)
    version = str(proj.get("version", "0.0.0"))
    deps = tuple(str(d) for d in proj.get("dependencies", []))
    requires_python = str(proj.get("requires-python") or "") or None

    tool: dict[str, Any] = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
    fspack_cfg: dict[str, Any] = tool.get("fspack", {}) if isinstance(tool.get("fspack"), dict) else {}
    entries_tbl: dict[str, Any] = fspack_cfg.get("entries", {}) if isinstance(fspack_cfg.get("entries"), dict) else {}
    icon_rel = fspack_cfg.get("icon")
    icon_path = _resolve_icon(project_dir, icon_rel)

    # [tool.fspack] exclude：额外排除目录/文件模式（合并到 copy_source 内置 _EXCLUDE）
    exclude_dirs = _parse_exclude_dirs(fspack_cfg.get("exclude"))
    # [tool.fspack] 构建默认值：CLI 标志覆盖
    build_defaults = _parse_build_defaults(fspack_cfg)
    # [tool.fspack] 私有包源：extra-index-urls / find-links 透传给 pip/uv
    extra_index_urls = _parse_string_list_cfg(fspack_cfg.get("extra-index-urls"), "extra-index-urls")
    find_links = _parse_string_list_cfg(fspack_cfg.get("find-links"), "find-links")
    # [tool.fspack] wheel 精简用户规则
    slim_rules = SlimRules.from_config(fspack_cfg)

    if entries_tbl:
        entries = _parse_entries(project_dir, entries_tbl)
        first = entries[0]
        return ProjectInfo(
            name=name,
            version=version,
            src_dir=project_dir,
            entry_module=first.module,
            entry_file=first.file,
            app_type=first.app_type,
            dependencies=deps,
            py_version=py_version or DEFAULT_PY_VERSION,
            requires_python=requires_python,
            entries=entries,
            icon=icon_path,
            exclude_dirs=exclude_dirs,
            build_defaults=build_defaults,
            extra_index_urls=extra_index_urls,
            find_links=find_links,
            slim_rules=slim_rules,
        )

    entry_module, entry_file, app_type = detect_entry(project_dir, name, deps)
    return ProjectInfo(
        name=name,
        version=version,
        src_dir=project_dir,
        entry_module=entry_module,
        entry_file=entry_file,
        app_type=app_type,
        dependencies=deps,
        py_version=py_version or DEFAULT_PY_VERSION,
        requires_python=requires_python,
        icon=icon_path,
        exclude_dirs=exclude_dirs,
        build_defaults=build_defaults,
        extra_index_urls=extra_index_urls,
        find_links=find_links,
        slim_rules=slim_rules,
    )


def _parse_entries(
    project_dir: Path,
    entries_tbl: dict[str, Any],
) -> tuple[EntryPoint, ...]:
    """解析 ``[tool.fspack.entries]`` 表为 EntryPoint 元组。

    键为入口名（用作 exe 名，须为合法标识符风格），值为入口脚本相对
    项目目录的路径。脚本路径不存在或为空时报错。Python 字典保持插入序，
    首个入口作为主入口（保持向后兼容）。

    多入口模式下每个入口的 ``app_type`` 按脚本自身 import 推断，不看项目级
    declared（不同入口可能是不同类型，如 cli/gui/web 混合）。
    """
    if not entries_tbl:
        raise ProjectError("[tool.fspack.entries] 为空，请删除该表或至少声明一个入口")
    entries: list[EntryPoint] = []
    for entry_name, script_rel in entries_tbl.items():
        if not isinstance(entry_name, str) or not entry_name:
            raise ProjectError(f"[tool.fspack.entries] 入口名无效: {entry_name!r}")
        if not isinstance(script_rel, str) or not script_rel.strip():
            raise ProjectError(f"[tool.fspack.entries] {entry_name} 的脚本路径为空")
        script_path = (project_dir / script_rel).resolve()
        if not script_path.is_file():
            raise ProjectError(f"[tool.fspack.entries] {entry_name} 的脚本不存在: {script_rel}")
        entries.append(EntryPoint.from_script(entry_name, script_path))
    return tuple(entries)


def _parse_exclude_dirs(value: object) -> tuple[str, ...]:
    """解析 ``[tool.fspack] exclude`` 配置为排除模式元组（空元素报错）."""
    return _parse_string_list_cfg(value, "exclude", reject_empty=True)


def _parse_build_defaults(fspack_cfg: dict[str, Any]) -> BuildDefaults:
    """从 ``[tool.fspack]`` 解析构建默认值.

    识别 ``nuitka``/``pyc_strip``/``pyc_optimize``/``no_site``/``no_pyc``/
    ``no_stdlib_trim``/``ccache``/``nuitka_packages`` 键，其余键忽略
    （如 ``icon``/``entries``/``exclude``）。类型不匹配时报错，避免静默忽略错误配置。
    """
    kwargs: dict[str, bool | int | None | tuple[str, ...]] = {}
    for cfg_key, field_name in _BUILD_DEFAULT_KEYS.items():
        raw = fspack_cfg.get(cfg_key)
        if raw is None:
            continue
        if field_name == "pyc_optimize":
            if not isinstance(raw, int) or raw not in (0, 1, 2):
                raise ProjectError(f"[tool.fspack] {cfg_key} 必须是 0/1/2，得到 {raw!r}")
            kwargs[field_name] = raw
        else:
            if not isinstance(raw, bool):
                raise ProjectError(f"[tool.fspack] {cfg_key} 必须是布尔值，得到 {raw!r}")
            kwargs[field_name] = raw
    # nuitka_packages 为字符串列表，单独解析
    raw_pkgs = fspack_cfg.get("nuitka_packages")
    if raw_pkgs is not None:
        if not isinstance(raw_pkgs, list) or not all(isinstance(x, str) for x in raw_pkgs):
            raise ProjectError(f"[tool.fspack] nuitka_packages 必须是字符串列表，得到 {raw_pkgs!r}")
        kwargs["nuitka_packages"] = tuple(raw_pkgs)
    return BuildDefaults(**kwargs)  # type: ignore[arg-type]


def _resolve_icon(project_dir: Path, icon_rel: object) -> Path | None:
    """解析 ``[tool.fspack] icon`` 配置为绝对路径。

    ``icon_rel`` 为相对项目目录的路径字符串（POSIX 或原生均可）。为空时返回
    ``None``（由 builder 回退到默认 icon）。路径不存在时报错，避免构建时
    才发现 windres 找不到文件。
    """
    if not icon_rel:
        return None
    if not isinstance(icon_rel, str) or not icon_rel.strip():
        raise ProjectError(f"[tool.fspack] icon 配置无效: {icon_rel!r}")
    icon_path = (project_dir / icon_rel.strip()).resolve()
    if not icon_path.is_file():
        raise ProjectError(f"[tool.fspack] icon 文件不存在: {icon_rel}")
    return icon_path


def detect_entry(
    src_dir: Path,
    name: str,
    deps: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, Path, AppType]:
    """识别入口模块，返回 (module, file, app_type)。

    优先匹配 <name>.py 与 <name>/__main__.py，再兜底扫描顶层 .py。
    入口判定：含 def main() 或 if __name__ == "__main__" 块。
    """
    declared = tuple(deps or ())
    candidates: list[tuple[str, Path]] = []
    direct = src_dir / f"{name}.py"
    if direct.is_file():
        candidates.append((name, direct))
    pkg_main = src_dir / name / "__main__.py"
    if pkg_main.is_file():
        candidates.append((name, pkg_main))
    for py in sorted(src_dir.glob("*.py")):
        candidates.append((py.stem, py))

    seen: set[str] = set()
    for mod, path in candidates:
        if mod not in seen and path.is_file():
            seen.add(mod)
            if _has_entry(path):
                return mod, path, infer_app_type(path, declared)
    raise ProjectError(f"未识别到入口（需 def main() 或 if __name__=='__main__'）: {src_dir}")


def _has_entry(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return True
        if isinstance(node, ast.If) and _is_main_check(node.test):
            return True
    return False


def _is_main_check(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "__name__"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == "__main__"
    )


def infer_app_type(path: Path, declared: tuple[str, ...]) -> AppType:
    """根据 import 与声明依赖推断 CLI/GUI 类型.

    惰性导入 :func:`fspack.analyzer.collect_imports` 打破 config ↔ analyzer 循环依赖。
    """
    from fspack.analyzer import collect_imports

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for top in collect_imports(tree):
        if top in _GUI_HINTS:
            return AppType.GUI
    for dep in declared:
        top = re.split(r"[<>=!~;\[]", dep, maxsplit=1)[0].strip().replace("-", "_")
        if top in _GUI_HINTS:
            return AppType.GUI
    return AppType.CLI
