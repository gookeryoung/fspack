"""pyproject.toml 解析与项目入口识别。."""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

from fspack.analyzer import collect_imports
from fspack.config import AppType, EntryPoint, ProjectInfo
from fspack.exceptions import ProjectError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]
    except ImportError as e:  # pragma: no cover
        raise ProjectError("解析 pyproject.toml 需要 tomli（Python<3.11），请安装 tomli") from e

__all__ = [
    "DEFAULT_LINUX_PY_VERSION",
    "DEFAULT_PY_VERSION",
    "KNOWN_EMBED_VERSIONS",
    "detect_entry",
    "infer_app_type",
    "parse_project",
    "resolve_py_version",
]

_logger = logging.getLogger(__name__)

DEFAULT_PY_VERSION = "3.11.9"
DEFAULT_LINUX_PY_VERSION = "3.11.10"

# 已知 embed python 版本映射：major.minor → 完整版本号
KNOWN_EMBED_VERSIONS: dict[str, str] = {
    "3.8": "3.8.10",
    "3.9": "3.9.13",
    "3.10": "3.10.11",
    "3.11": "3.11.9",
    "3.12": "3.12.0",
}

# 降序排列的完整版本列表，用于自动选择最高兼容版本
# 注意：必须按版本元组排序，字符串排序会让 "3.9.13" > "3.10.11"（因 '9' > '1'）


def _ver_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


_KNOWN_FULL_VERSIONS: list[str] = sorted(KNOWN_EMBED_VERSIONS.values(), key=_ver_key, reverse=True)

# PEP 440 版本规范符正则
_SPEC_RE = re.compile(r"(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.\d+)*)")

_GUI_HINTS = frozenset({"tkinter", "PySide2", "PySide6", "PyQt5", "PyQt6", "matplotlib", "wx", "win32gui"})


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

    if entries_tbl:
        entries = _parse_entries(project_dir, entries_tbl, deps)
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
    )


def _parse_entries(
    project_dir: Path,
    entries_tbl: dict[str, Any],
    deps: tuple[str, ...],
) -> tuple[EntryPoint, ...]:
    """解析 ``[tool.fspack.entries]`` 表为 EntryPoint 元组。

    键为入口名（用作 exe 名，须为合法标识符风格），值为入口脚本相对
    项目目录的路径。脚本路径不存在或为空时报错。Python 字典保持插入序，
    首个入口作为主入口（保持向后兼容）。

    ``deps`` 仅用于校验（保留参数兼容签名），多入口模式下每个入口的
    ``app_type`` 按脚本自身 import 推断，不看项目级 declared（不同入口
    可能是不同类型，如 cli/gui/web 混合）。
    """
    _ = deps  # 保留签名兼容，多入口模式不用 declared 推断 app_type
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


def resolve_py_version(
    project_dir: Path,
    explicit: str | None,
    requires_python: str | None,
    default: str = DEFAULT_PY_VERSION,
) -> str:
    """解析最终使用的 Python 版本。

    优先级：
    1. ``explicit``（``--py-version`` CLI 标志）—— 不满足 ``requires-python`` 时告警但仍使用
    2. ``.python-version`` 文件 —— 不满足 ``requires-python`` 时告警并回退到自动选择
    3. ``requires-python`` 约束 —— 自动选择最高兼容已知版本
    4. ``default``
    """
    if explicit:
        if requires_python and not _satisfies(explicit, requires_python):
            _logger.warning("Python %s 不满足 requires-python: %s", explicit, requires_python)
        return explicit

    pv_file = project_dir / ".python-version"
    if pv_file.is_file():
        pv = pv_file.read_text(encoding="utf-8").strip()
        full = KNOWN_EMBED_VERSIONS.get(pv, pv)
        if requires_python and not _satisfies(full, requires_python):
            _logger.warning(".python-version %s 不满足 requires-python: %s，自动选择兼容版本", full, requires_python)
        else:
            return full

    if requires_python:
        for ver in _KNOWN_FULL_VERSIONS:
            if _satisfies(ver, requires_python):
                return ver
        raise ProjectError(f"requires-python: {requires_python}，无已知兼容 embed python 版本")

    return default


def _satisfies(version: str, specifiers: str) -> bool:
    """检查版本是否满足 PEP 440 ``requires-python`` 规范符。."""
    ver_parts = tuple(int(x) for x in version.split("."))
    for op, spec_ver in _SPEC_RE.findall(specifiers):
        spec_parts = tuple(int(x) for x in spec_ver.split("."))
        length = max(len(ver_parts), len(spec_parts))
        ver = ver_parts + (0,) * (length - len(ver_parts))
        spec = spec_parts + (0,) * (length - len(spec_parts))
        if op == ">=":
            ok = ver >= spec
        elif op == "<=":
            ok = ver <= spec
        elif op == ">":
            ok = ver > spec
        elif op == "<":
            ok = ver < spec
        elif op == "==":
            ok = ver == spec
        elif op == "!=":
            ok = ver != spec
        else:
            continue
        if not ok:
            return False
    return True


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
