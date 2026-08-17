"""ELF 依赖解析（objdump -p）.

解析 ``NEEDED libfoo.so.6`` 条目。通过 :func:`_D` 延迟从 dep_analyzer facade
取 ``subprocess`` 模块，确保 ``monkeypatch.setattr("dep_analyzer.subprocess.run", ...)``
patch 生效。
"""

from __future__ import annotations

import logging
import subprocess as _default_subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

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


def _parse_objdump_deps(path: Path) -> list[str] | None:
    """用 ``objdump -p`` 解析 ELF 依赖（NEEDED 条目）."""
    subprocess_dispatch: Any = _D("subprocess", _default_subprocess)
    try:
        result = subprocess_dispatch.run(
            ["objdump", "-p", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except _default_subprocess.TimeoutExpired:
        _logger.warning("objdump 解析超时（10s），跳过: %s", path)
        return None
    except OSError as e:
        # objdump 不存在（FileNotFoundError）或权限/IO 异常：跳过该文件
        _logger.warning("objdump 调用失败，跳过: %s（%s）", path, e)
        return None

    if result.returncode != 0:
        return None

    deps: list[str] = []
    for raw_line in result.stdout.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("NEEDED "):
            deps.append(stripped[len("NEEDED ") :].strip())
    return deps
