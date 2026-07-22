"""默认精简规则：非 Qt 库的兜底行为。

非 Qt 库（如 numpy、requests 等）采用保守精简策略：

- 顶层 ``__init__.py``/``_*.py`` 归 shared 始终保留
- 顶层 ``.pyd``/``.pyi``/``.so`` 按原文件名归类为子模块，仅当子模块在保留集合时保留
- 其他顶层文件（``.py``/``.dll``/``.py.typed`` 等）归 shared 始终保留
- 通用非必要子目录（``examples``/``docs``/``tests`` 等，见
  :attr:`SlimSpec.COMMON_EXCLUDE_SUBDIRS`）归 exclude 始终剥离
- 其他子目录归 shared 始终保留（不细分子模块）
- ``*.dist-info`` 元数据始终保留

无依赖闭包扩展（``expand_closure`` 直接返回输入集合的副本）。
"""

from __future__ import annotations

from fspack.slim.base import SlimSpec, override

__all__ = ["DefaultSlimSpec"]


class DefaultSlimSpec(SlimSpec):
    """默认精简规则：兜底，``match`` 始终返回 ``True``。

    非 Qt 库走此规则。子模块按原文件名（不归一化）选择性保留。
    分类逻辑委托 :meth:`SlimSpec._default_classify`，仅应用通用剥离目录
    （无库专属剥离）。
    """

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:  # noqa: ARG003
        """兜底匹配：始终返回 ``True``."""
        return True

    @classmethod
    @override
    def normalize_submodule(cls, sub: str) -> str:
        """非 Qt 库不做归一化，原样返回."""
        return sub

    @classmethod
    @override
    def expand_closure(cls, subs: set[str]) -> set[str]:
        """无依赖闭包扩展，返回输入集合的副本（不就地修改）."""
        return set(subs)

    @classmethod
    @override
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """默认条目分类，委托 :meth:`SlimSpec._default_classify`."""
        return cls._default_classify(entry, top_pkg, keep_subs)
