"""Wheel 依赖的环境标记（marker）预过滤.

``pip download --python-version`` 只用**运行 pip 的解释器**求值环境标记，
不会按目标版本/目标平台求值：

- 跨平台目标（如 Linux 宿主构建 Windows 包）会把 ``sys_platform == 'win32'``
  的依赖按宿主环境丢弃；
- 反向场景（Linux 目标下载到 ``pywin32; sys_platform == 'win32'``）在旧实现
  剥掉标记后无条件保留，导致给 Linux 目标误下载 Windows 专用依赖；
- 跨版本目标（宿主 3.13、目标 3.8）会把 ``python_version <= '3.8'`` 的依赖
  按宿主版本丢弃。

本模块在调用 pip 前按**目标** Python 版本与目标平台构造求值环境，完整求值
marker 表达式（``and``/``or``/括号/比较/``in``/``not in``），只在求值为真时
保留依赖：

- 目标环境为真且宿主环境同样为真：保留 marker 原文（pip 复评不会丢弃）；
- 目标环境为真但宿主环境为假：剥离 marker（保留原文会被 pip 按宿主环境误丢弃）；
- 目标环境为假：剔除依赖；
- 无法解析或含未知变量的 marker：保守保留原文，交由 pip 处理。

仅使用标准库：``packaging`` 不是 fspack 的运行时依赖（只在 pip 所在解释器
可用），不能用于本模块。
"""

from __future__ import annotations

import logging
import platform as _platform
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from fspack.config.versions import _split_t_suffix

__all__ = [
    "_build_marker_env",
    "_eval_marker",
    "_filter_by_python_version",
]

_logger = logging.getLogger(__name__)

# marker 词法单元：括号、比较运算符、and/or/in/not、引号字符串、变量名
_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<lparen>\()|
        (?P<rparen>\))|
        (?P<op>==|!=|<=|>=|<|>)|
        (?P<keyword>and\b|or\b|in\b|not\b)|
        (?P<value>'[^']*'|"[^"]*")|
        (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
    )""",
    re.VERBOSE | re.IGNORECASE,
)

# 按版本号（数值段）比较的变量，其余变量按字符串比较
_VERSION_VARS = frozenset({"python_version", "python_full_version"})


class _MarkerError(ValueError):
    """marker 无法解析或含未知变量：保守保留原文，交由 pip 处理."""


def _host_python_version() -> str:
    """当前运行解释器的 ``major.minor.micro`` 版本串."""
    info = sys.version_info
    return f"{info.major}.{info.minor}.{info.micro}"


def _system_of_tags(platform_tags: Sequence[str]) -> str:
    """从 pip ``--platform`` 标签推断目标系统；空标签回退宿主系统."""
    for tag in platform_tags:
        low = tag.lower()
        if low.startswith("win") or "win" in low:
            return "windows"
        if "linux" in low:
            return "linux"
        if "macosx" in low or low.startswith("darwin"):
            return "darwin"
    return "windows" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")


def _system_env(system: str, *, host: bool) -> dict[str, str]:
    """目标/宿主系统的 marker 变量集合（``sys_platform`` 等）."""
    if system == "windows":
        machine = _platform.machine() if host else "AMD64"
        return {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt", "platform_machine": machine}
    if system == "darwin":
        machine = _platform.machine() if host else "x86_64"
        return {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix", "platform_machine": machine}
    machine = _platform.machine() if host else "x86_64"
    return {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": machine}


def _build_marker_env(
    py_version: str,
    platform_tags: Sequence[str] = (),
    *,
    host: bool = False,
) -> dict[str, str]:
    """构造 marker 求值环境.

    Args:
        py_version: 目标 Python 版本（如 ``"3.8.10"``/``"3.13t"``）；``host=True``
            时应传宿主解释器版本.
        platform_tags: pip ``--platform`` 标签（如 ``("win_amd64",)``），用于推断
            目标系统；为空时回退宿主系统.
        host: 是否构造宿主环境（平台机器名取真实值）.

    Returns:
        变量名 → 取值的映射，供 :func:`_eval_marker` 使用.
    """
    base, _ = _split_t_suffix(py_version)
    parts = base.split(".")
    python_version = ".".join(parts[:2])
    python_full_version = base if len(parts) >= 3 else ".".join([*parts, *(["0"] * (3 - len(parts)))])
    env = {
        "python_version": python_version,
        "python_full_version": python_full_version,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "extra": "",
    }
    system = _system_of_tags(platform_tags)
    env.update(_system_env(system, host=host))
    if host:
        env["platform_release"] = _platform.release()
    return env


def _filter_by_python_version(
    packages: Sequence[str],
    py_version: str,
    platform_tags: Sequence[str] = (),
    *,
    host_env: Mapping[str, str] | None = None,
) -> list[str]:
    """按目标环境的 marker 求值结果过滤依赖列表.

    ``pip download --python-version`` 只用运行 pip 的解释器求值 marker，需在
    调用前按目标 Python 版本与目标平台完整求值（见模块文档）。

    Args:
        packages: 依赖声明列表（可含 ``; marker``）。
        py_version: 目标 Python 版本.
        platform_tags: pip ``--platform`` 标签，用于推断目标平台.
        host_env: 宿主 marker 环境（测试注入用）；默认按当前解释器构造.

    Returns:
        保留的依赖列表：目标环境为真的依赖，宿主环境同样为真时保留 marker
        原文，否则剥离 marker；目标环境为假的依赖被剔除。
    """
    target_env = _build_marker_env(py_version, platform_tags)
    if host_env is None:
        host_env = _build_marker_env(_host_python_version(), host=True)
    result: list[str] = []
    for pkg in packages:
        spec, sep, marker = pkg.partition(";")
        if not sep or not marker.strip():
            result.append(pkg.strip())
            continue
        spec = spec.strip()
        marker_text = marker.strip()
        try:
            matched = _eval_marker(marker_text, target_env)
        except _MarkerError:
            _logger.debug("环境标记无法求值，保守保留: %s", pkg)
            result.append(pkg.strip())
            continue
        if not matched:
            continue
        try:
            host_matched = _eval_marker(marker_text, host_env)
        except _MarkerError:
            host_matched = False
        # 宿主求值为真才保留 marker 原文；否则 pip 会用宿主环境复评并丢弃该依赖
        result.append(f"{spec}; {marker_text}" if host_matched else spec)
    return result


def _eval_marker(marker: str, env: Mapping[str, str]) -> bool:
    """按给定环境求值 marker 表达式.

    Raises:
        _MarkerError: 表达式无法解析、含未知变量或使用不支持的运算符.
    """
    tokens = _tokenize(marker)
    if not tokens:
        raise _MarkerError(f"空环境标记: {marker!r}")
    parser = _MarkersParser(tokens, env)
    value = parser.parse()
    return value


def _tokenize(marker: str) -> list[tuple[str, str]]:
    """将 marker 拆为 ``(kind, text)`` 词法单元列表；无法识别时抛错."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(marker):
        if marker[pos:].strip() == "":
            break
        m = _TOKEN_RE.match(marker, pos)
        if m is None:
            raise _MarkerError(f"无法解析环境标记: {marker!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind is None:  # pragma: no cover - 正则保证每组至少一个命中
            raise _MarkerError(f"无法解析环境标记: {marker!r}")
        text = m.group(kind)
        if kind == "value":
            text = text[1:-1]
        tokens.append((kind, text))
    return tokens


class _MarkersParser:
    """marker 递归下降解析器：``or`` < ``and`` < 原子（括号/比较）."""

    def __init__(self, tokens: list[tuple[str, str]], env: Mapping[str, str]) -> None:
        self._tokens = tokens
        self._env = env
        self._pos = 0

    def parse(self) -> bool:
        value = self._or_expr()
        if self._pos != len(self._tokens):
            raise _MarkerError("环境标记存在多余内容")
        return value

    def _peek(self) -> tuple[str, str] | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _advance(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise _MarkerError("环境标记意外结束")
        self._pos += 1
        return token

    def _match_keyword(self, keyword: str) -> bool:
        token = self._peek()
        if token is not None and token[0] == "keyword" and token[1].lower() == keyword:
            self._pos += 1
            return True
        return False

    def _or_expr(self) -> bool:
        value = self._and_expr()
        while self._match_keyword("or"):
            rhs = self._and_expr()
            value = value or rhs
        return value

    def _and_expr(self) -> bool:
        value = self._atom()
        while self._match_keyword("and"):
            rhs = self._atom()
            value = value and rhs
        return value

    def _atom(self) -> bool:
        token = self._peek()
        if token is None:
            raise _MarkerError("环境标记意外结束")
        kind, _text = token
        if kind == "lparen":
            self._pos += 1
            value = self._or_expr()
            closing = self._advance()
            if closing[0] != "rparen":
                raise _MarkerError("环境标记括号不匹配")
            return value
        return self._comparison()

    def _comparison(self) -> bool:
        kind, name = self._advance()
        if kind != "name":
            raise _MarkerError(f"环境标记缺少变量名: {name!r}")
        op = self._operator()
        kind, right = self._advance()
        if kind not in ("value", "name"):
            raise _MarkerError(f"环境标记缺少比较值: {right!r}")
        left_value = self._lookup(name)
        right_value = self._lookup(right) if kind == "name" else right
        return _compare(left_value, op, right_value, name.lower() in _VERSION_VARS)

    def _operator(self) -> str:
        kind, text = self._advance()
        if kind == "op":
            return text
        if kind == "keyword" and text.lower() == "in":
            return "in"
        if kind == "keyword" and text.lower() == "not":
            kind2, text2 = self._advance()
            if kind2 == "keyword" and text2.lower() == "in":
                return "not in"
        raise _MarkerError(f"环境标记运算符无效: {text!r}")

    def _lookup(self, name: str) -> str:
        value = self._env.get(name)
        if value is None:
            raise _MarkerError(f"环境标记含未知变量: {name!r}")
        return value


def _compare(left: str, op: str, right: str, version: bool) -> bool:
    """比较两个 marker 取值；版本类变量按数值段比较，其余按字符串比较."""
    if op in ("in", "not in"):
        return (left in right) == (op == "in")
    if version:
        left_key = _version_key(left)
        right_key = _version_key(right)
        if left_key is not None and right_key is not None:
            length = max(len(left_key), len(right_key))
            left_key = left_key + (0,) * (length - len(left_key))
            right_key = right_key + (0,) * (length - len(right_key))
            if op in ("==", "!="):
                return (left_key == right_key) == (op == "==")
            return _ordered(left_key, op, right_key)
    if op in ("==", "!="):
        return (left == right) == (op == "==")
    return _ordered(left, op, right)


def _version_key(value: str) -> tuple[int, ...] | None:
    """将 ``3.10.1`` 类版本串转为数值元组；非纯数字段返回 ``None``."""
    parts = value.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def _ordered(left: Any, op: str, right: Any) -> bool:
    """按 ``op`` 做有序比较（``<``/``<=``/``>``/``>=``）.

    ``left``/``right`` 同为 ``tuple[int, ...]``（版本）或同为 ``str``（其他变量）.
    """
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise _MarkerError(f"环境标记运算符不支持: {op!r}")
