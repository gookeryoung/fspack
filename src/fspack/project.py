"""pyproject.toml 解析与项目入口识别。."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from fspack.analyzer import collect_imports
from fspack.config import AppType, ProjectInfo
from fspack.exceptions import ProjectError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]
    except ImportError as e:  # pragma: no cover
        raise ProjectError("解析 pyproject.toml 需要 tomli（Python<3.11），请安装 tomli") from e

__all__ = ["DEFAULT_PY_VERSION", "detect_entry", "parse_project"]

DEFAULT_PY_VERSION = "3.11.9"

_GUI_HINTS = frozenset({"tkinter", "PySide2", "PySide6", "PyQt5", "PyQt6", "matplotlib", "wx", "win32gui"})


def parse_project(project_dir: Path, py_version: str = DEFAULT_PY_VERSION) -> ProjectInfo:
    """解析 pyproject.toml 并识别入口，返回项目元信息。."""
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

    entry_module, entry_file, app_type = detect_entry(project_dir, name, deps)
    return ProjectInfo(
        name=name,
        version=version,
        src_dir=project_dir,
        entry_module=entry_module,
        entry_file=entry_file,
        app_type=app_type,
        dependencies=deps,
        py_version=py_version,
    )


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
                return mod, path, _infer_app_type(path, declared)
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


def _infer_app_type(path: Path, declared: tuple[str, ...]) -> AppType:
    """根据 import 与声明依赖推断 CLI/GUI 类型。."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for top in collect_imports(tree):
        if top in _GUI_HINTS:
            return AppType.GUI
    for dep in declared:
        top = re.split(r"[<>=!~;\[]", dep, maxsplit=1)[0].strip().replace("-", "_")
        if top in _GUI_HINTS:
            return AppType.GUI
    return AppType.CLI
