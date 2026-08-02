"""跨 Python 版本兼容性 shim（零第三方依赖）.

集中放置版本相关的回退导入，避免在各模块散落
``# type: ignore[import-not-found]``。本模块刻意保持零第三方依赖：
``override``/``tomllib`` 的消费方众多（slim/packaging/templates 等），
顶部若引入重依赖（如 rich）会被所有消费方连带加载。

当前导出：

- :func:`override` — PEP 698，3.12+ 进入 ``typing``；低版本类型检查期回退
  ``typing_extensions``，运行时为 no-op（行为与 typing_extensions 等价）
- :mod:`tomllib` — 3.11+ 标准库，低版本回退 ``tomli``（解析 ``pyproject.toml`` 用）

CI 环境兼容 shim（:class:`CICompat`）依赖 rich，位于其唯一消费方
:mod:`fspack.console`。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TypeVar

if sys.version_info >= (3, 12):
    from typing import override
elif TYPE_CHECKING:
    # 类型检查期用 typing_extensions 保留 pyrefly 对 @override 的语义检查
    from typing_extensions import override
else:
    # 运行时 no-op：typing_extensions.override 运行时行为等价于返回原函数
    # （仅尝试设置 __override__ 标记），此处直接定义避免 typing_extensions
    # 导入开销（~20ms）与 embed python 3.8 携带过新版本时的 AttributeError。
    _F = TypeVar("_F")

    def override(method: _F, /) -> _F:
        """运行时 no-op 的 @override（类型标记由 TYPE_CHECKING 分支提供）."""
        return method


try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("解析 pyproject.toml 需要 tomli（Python<3.11），请安装 tomli") from e

__all__ = ["override", "tomllib"]
