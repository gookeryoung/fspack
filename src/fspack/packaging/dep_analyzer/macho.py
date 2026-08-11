"""Mach-O 依赖解析（otool -L）.

输出行 ``@rpath/libfoo.dylib (compatibility version ...)`` 截取首个空格前路径。
``subprocess`` 通过 :func:`_D` dispatch 取 facade 属性，保证 patch 生效。
"""

from __future__ import annotations

import subprocess as _default_subprocess
from pathlib import Path
from typing import Any

# dep_analyzer facade 延迟 dispatch：subprocess 模块 patch 点
_da_mod_holder: list[Any] = [None]


def _D(attr_name: str, fallback: Any) -> Any:
    """从 ``fspack.packaging.dep_analyzer`` 取模块属性，取不到时回退 fallback."""
    mod = _da_mod_holder[0]
    if mod is None:
        try:
            from fspack.packaging import dep_analyzer as _da_mod

            mod = _da_mod
            _da_mod_holder[0] = mod
        except ImportError:
            return fallback
    return getattr(mod, attr_name, fallback)


def _parse_otool_deps(path: Path) -> list[str] | None:
    """用 ``otool -L`` 解析 Mach-O 依赖."""
    subprocess_dispatch: Any = _D("subprocess", _default_subprocess)
    try:
        result = subprocess_dispatch.run(
            ["otool", "-L", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, _default_subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    deps: list[str] = []
    lines = result.stdout.splitlines()
    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        dep = stripped.split(" ", 1)[0].strip()
        if dep:
            deps.append(dep)
    return deps
