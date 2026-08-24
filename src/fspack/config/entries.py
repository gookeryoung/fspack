"""入口脚本识别：``[project.scripts]`` 解析与兜底扫描.

从 :mod:`fspack.config.parsing` 拆分而来，封装两类入口识别逻辑：

1. ``[project.scripts]`` 解析（:func:`_parse_project_scripts`，PEP 621）：
   ``name = "module:function"``，自动识别 flat/src layout 将 dotted module
   解析为脚本文件路径。
2. 兜底扫描（:func:`detect_entry`）：无任何入口声明时按 ``<name>.py``/
   ``<name>/__main__.py``/顶层 ``*.py`` 顺序识别含 ``def main()`` 或
   ``if __name__ == "__main__"`` 的脚本。

依赖 :mod:`fspack.config.models` 提供 ``EntryPoint``/``AppType``，
:mod:`fspack.config.app_type` 提供类型推断。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from fspack.config.app_type import infer_app_type
from fspack.config.models import AppType, EntryPoint
from fspack.exceptions import ProjectError

__all__ = ["detect_entry"]


def _parse_project_scripts(
    project_dir: Path,
    scripts_tbl: dict[str, Any],
) -> tuple[EntryPoint, ...]:
    """解析 ``[project.scripts]`` 表（PEP 621）为 EntryPoint 元组.

    PEP 621 入口点格式：``name = "module:function"``，其中：

    - ``name``：可执行文件名（用作 exe 名）。
    - ``module``：dotted 模块路径（如 ``fspack.cli``、``cli``），fspack 将其
      解析为脚本文件路径。``function`` 部分被忽略——fspack 用
      :func:`runpy.run_path`/``run_module`` 运行整个模块而非调用特定函数。
    - ``function``：入口函数名（如 ``main``），仅作元数据保留，运行时不使用。

    项目 layout 自动识别（按优先级尝试，首个命中即用）：

    - **flat layout**：``<project>/<pkg>/...`` 或 ``<project>/<name>.py``。
    - **src layout**：``<project>/src/<pkg>/...`` 或 ``<project>/src/<name>.py``。

    dotted module 到文件路径的映射规则：

    - 多段（``fspack.cli``）：``<pkg>/cli.py``（flat）或 ``src/<pkg>/cli.py``（src）。
    - 单段（``fspack``）：``fspack.py`` 或 ``fspack/__main__.py``
      （flat），``src/fspack.py`` 或 ``src/fspack/__main__.py``（src）。

    键为入口名（须为非空字符串），值须为 ``"module:function"`` 格式字符串。
    缺少 ``:function`` 时视整段为 module（向后兼容纯模块名写法）。
    Python 字典保持插入序，首个入口作为主入口（保持向后兼容）。
    """
    if not scripts_tbl:
        raise ProjectError("[project.scripts] 为空，请删除该表或至少声明一个入口")
    entries: list[EntryPoint] = []
    for entry_name, spec in scripts_tbl.items():
        if not isinstance(entry_name, str) or not entry_name:
            raise ProjectError(f"[project.scripts] 入口名无效: {entry_name!r}")
        if not isinstance(spec, str) or not spec.strip():
            raise ProjectError(f"[project.scripts] {entry_name} 的入口规范为空")
        # PEP 621: "module:function"，function 可省略（纯模块名也接受）
        module_part = spec.split(":", 1)[0].strip()
        if not module_part:
            raise ProjectError(f"[project.scripts] {entry_name} 的模块名无效: {spec!r}")
        script_path = _resolve_module_script(project_dir, module_part)
        if script_path is None:
            raise ProjectError(
                f"[project.scripts] {entry_name} 的模块 {module_part!r} 未找到对应脚本（已尝试 flat 与 src layout）"
            )
        entries.append(EntryPoint.from_script(entry_name, script_path))
    return tuple(entries)


def _resolve_module_script(project_dir: Path, module_dotted: str) -> Path | None:
    """将 dotted 模块名解析为脚本文件绝对路径，自动识别 flat/src layout.

    查找规则（按优先级尝试，首个命中即返回）：

    1. **flat layout**：在 ``project_dir`` 下查找
       - 多段 ``a.b`` → ``<project>/a/b.py``
       - 单段 ``a`` → ``<project>/a.py`` 或 ``<project>/a/__main__.py``
    2. **src layout**：在 ``project_dir/src`` 下重复上述查找

    所有候选路径都不存在时返回 ``None``，由调用方决定报错或回退。

    单段 module 优先 ``a.py``（顶层脚本），再 ``a/__main__.py``（包入口），
    与 :func:`detect_entry` 的优先级一致。
    """
    parts = module_dotted.split(".")
    # 多段 → <...>/a/b.py；单段 → a.py 或 a/__main__.py
    rel_candidates: list[Path] = []
    if len(parts) >= 2:
        rel_candidates.append(Path(*parts).with_suffix(".py"))
    else:
        first = parts[0]
        rel_candidates.append(Path(f"{first}.py"))
        rel_candidates.append(Path(first, "__main__.py"))

    for base in (project_dir, project_dir / "src"):
        for rel in rel_candidates:
            candidate = (base / rel).resolve()
            if candidate.is_file():
                return candidate
    return None


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
    except (SyntaxError, OSError, UnicodeDecodeError):
        # UnicodeDecodeError（ValueError 子类，非 OSError）：非 UTF-8 源文件无法
        # 作为入口候选解析，与语法错误/读取失败同等视为"非入口"跳过，避免崩溃。
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
