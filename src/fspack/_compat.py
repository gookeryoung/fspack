"""标准库 shim，集中导入跨模块复用的符号（零第三方依赖）.

本模块保持零第三方依赖：``override``/``tomllib`` 的消费方众多
（slim/packaging/templates 等），顶部若引入重依赖（如 rich）
会被所有消费方连带加载。

当前导出：

- :func:`override` — PEP 698，3.12+ 进入 ``typing``
- :mod:`tomllib` — 3.11+ 标准库，解析 ``pyproject.toml``/``template.toml`` 用

CI 环境兼容 shim（:class:`CICompat`）依赖 rich，位于其唯一消费方
:mod:`fspack.console`。
"""

from __future__ import annotations

import tomllib
from typing import override

__all__ = ["override", "tomllib"]
