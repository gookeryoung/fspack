"""fspack 精简打包 facade：从 spec/unpack 两个模块 re-export 公开 API.

本模块是 :mod:`fspack.slim.base` 的入口与 API 索引，无业务逻辑。原
``base.py``（526 行）按职责拆分到两个模块：

- :mod:`fspack.slim.spec`：``SlimSpec`` 抽象基类 + ``WheelInfo`` + 注册表
  （``register_spec``/``get_spec``/``classify_entry``）+ ``normalize_name``
- :mod:`fspack.slim.unpack`：``slim_unpack`` 按需解压实现 + ``_unpack_wheel_dispatch``
  + ``_slim_extract`` + ``_detect_top_pkg``

参考 fspacker ``packers.libspec`` 设计：按包名分发到对应的 ``SlimSpec``
子类。每个子类描述一组包（如 Qt 库、普通库）的精简规则——子模块归一化、
依赖闭包扩展、wheel 条目分类。

新增包精简规则时只需：

1. 继承 ``SlimSpec``，实现 ``match``/``classify_entry``；
   ``normalize_submodule``/``expand_closure`` 有默认实现（原样返回/返回副本），
   仅需在有归一化或依赖闭包需求时覆盖（如 Qt 库）
2. 用 ``register_spec`` 注册（``DefaultSlimSpec`` 兜底，必须最后注册）

无需修改 :func:`slim_unpack` 与 :func:`classify_entry` 的分发逻辑。
"""

from __future__ import annotations

# re-export 公开 API 与私有辅助：保持 ``from fspack.slim.base import X`` 路径兼容
from fspack.slim.spec import (
    _SPECS,  # noqa: F401
    _WHEEL_RE,  # noqa: F401
    SlimSpec,
    WheelInfo,
    classify_entry,
    get_spec,
    normalize_name,
    register_spec,
)
from fspack.slim.unpack import (
    _PARALLEL_WHEEL_THRESHOLD,  # noqa: F401
    _detect_top_pkg,  # noqa: F401
    _full_unpack,  # noqa: F401
    _safe_extract,  # noqa: F401
    _slim_extract,  # noqa: F401
    _unpack_one_wheel,  # noqa: F401
    _unpack_wheel_dispatch,  # noqa: F401
    slim_unpack,
)

__all__ = [
    "SlimSpec",
    "WheelInfo",
    "classify_entry",
    "get_spec",
    "normalize_name",
    "register_spec",
    "slim_unpack",
]
