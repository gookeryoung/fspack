"""Wheel 依赖的 ``python_version`` 环境标记预过滤.

``pip download --python-version`` 不评估命令行参数中的环境标记（marker），
需在调用前按目标 Python 版本预过滤。本模块封装标记解析与评估逻辑，
供 :mod:`fspack.packaging.wheel_pip` 在下载前剔除不匹配目标版本的依赖。

仅处理 ``python_version`` 标记；其他标记（如 ``platform_system``）视为 True
（保守保留，让 pip 自行处理）。
"""

from __future__ import annotations

import logging
import re
from typing import Sequence

__all__ = [
    "_MARKER_PY_VER_RE",
    "_eval_python_version_marker",
    "_eval_single_marker",
    "_filter_by_python_version",
]

_logger = logging.getLogger(__name__)

# 匹配 python_version 环境标记中的比较表达式
_MARKER_PY_VER_RE = re.compile(r"""python_version\s*(<=|>=|<|>|==|!=)\s*['"](\d+(?:\.\d+)*)['"]""")


def _filter_by_python_version(packages: Sequence[str], py_version: str) -> list[str]:
    """按 ``python_version`` 环境标记过滤依赖列表。

    ``pip download --python-version`` 不评估命令行参数中的环境标记（marker），
    需在调用前预过滤。标记匹配目标 Python 版本的依赖去掉标记后返回
    （避免 pip 用运行时 Python 版本评估标记导致误跳过）；
    不匹配的依赖被剔除。

    仅处理 ``python_version`` 标记；其他标记（如 ``platform_system``）视为 True
    （保守保留，让 pip 自行处理）。
    """
    py_parts = tuple(int(x) for x in py_version.split(".")[:2])
    result: list[str] = []
    for pkg in packages:
        if ";" not in pkg:
            result.append(pkg)
            continue
        spec, _, marker = pkg.partition(";")
        spec = spec.strip()
        marker = marker.strip()
        if _eval_python_version_marker(marker, py_parts):
            result.append(spec)
    return result


def _eval_python_version_marker(marker: str, py_parts: tuple[int, ...]) -> bool:
    """评估标记表达式中的 ``python_version`` 条件是否满足。

    支持 ``and``/``or`` 组合。非 ``python_version`` 标记视为 True（保守保留）。
    """
    or_parts = re.split(r"\s+or\s+", marker, flags=re.IGNORECASE)
    for or_part in or_parts:
        and_parts = re.split(r"\s+and\s+", or_part, flags=re.IGNORECASE)
        if all(_eval_single_marker(part.strip(), py_parts) for part in and_parts):
            return True
    return False


def _eval_single_marker(expr: str, py_parts: tuple[int, ...]) -> bool:
    """评估单个标记表达式。"""
    m = _MARKER_PY_VER_RE.match(expr)
    if not m:
        return True  # 非 python_version 标记，保守保留
    op, ver = m.groups()
    ver_parts = tuple(int(x) for x in ver.split("."))
    length = max(len(py_parts), len(ver_parts))
    py = py_parts + (0,) * (length - len(py_parts))
    spec = ver_parts + (0,) * (length - len(ver_parts))
    return {
        ">=": py >= spec,
        "<=": py <= spec,
        ">": py > spec,
        "<": py < spec,
        "==": py == spec,
        "!=": py != spec,
    }.get(op, True)
