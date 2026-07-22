"""默认精简规则：非 Qt 库的兜底行为。

非 Qt 库（如 numpy、requests 等）采用保守精简策略：

- 顶层 ``__init__.py``/``_*.py`` 归 shared 始终保留
- 顶层 ``.pyd``/``.pyi``/``.so`` 按原文件名归类为子模块，仅当子模块在保留集合时保留
- 其他顶层文件（``.py``/``.dll``/``.py.typed`` 等）归 shared 始终保留
- 非必要子目录（``examples``/``docs``/``tests``/``testing`` 等）归 exclude 始终剥离
- 其他子目录归 shared 始终保留（不细分子模块）
- ``*.dist-info`` 元数据始终保留

无依赖闭包扩展（``expand_closure`` 直接返回输入集合的副本）。
"""

from __future__ import annotations

from pathlib import Path

from fspack.slim.base import SlimSpec, override

__all__ = ["DefaultSlimSpec"]

# 始终剥离的子目录：示例代码、文档、测试代码非运行时必需
_EXCLUDE_SUBDIRS = frozenset(
    {
        "examples",  # 示例代码
        "docs",  # 文档（多数包用复数）
        "doc",  # 文档（少数包用单数）
        "tests",  # 测试代码（多数包用复数）
        "test",  # 测试代码（少数包用单数）
        "testing",  # 测试辅助目录
    }
)


class DefaultSlimSpec(SlimSpec):
    """默认精简规则：兜底，``match`` 始终返回 ``True``。

    非 Qt 库走此规则。子模块按原文件名（不归一化）选择性保留。
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
        keep_subs: set[str],  # noqa: ARG003
    ) -> tuple[str, str | None]:
        """默认条目分类。

        - ``*.dist-info/**`` → metadata
        - 跨包文件 → shared
        - ``__init__.*``/``_*`` → shared
        - 顶层 ``.pyd``/``.pyi``/``.so`` → submodule（按原文件名 stem）
        - 其他文件 → shared
        - 非必要子目录（``examples``/``docs``/``tests`` 等） → exclude
        - 其他子目录 → shared（不细分子模块）

        ``keep_subs`` 在默认规则中未使用：是否保留子模块由 ``_slim_extract``
        在外层根据返回的 ``category == "submodule"`` 统一判断。
        """
        common = cls._classify_top_or_meta(entry, top_pkg)
        if common is not None:
            return common

        parts = entry.split("/")
        # 顶层文件（parts == 2）
        if len(parts) == 2:
            filename = parts[1]
            if filename.startswith("__init__.") or filename.startswith("_"):
                return ("shared", None)
            suffix = Path(filename).suffix.lower()
            stem = Path(filename).stem
            if suffix in cls.SUBMODULE_EXTS:
                # 非归一化，按原文件名归类
                return ("submodule", stem)
            return ("shared", None)

        # 子目录（len(parts) >= 3）：非必要目录剥离，其余归 shared
        if parts[1] in _EXCLUDE_SUBDIRS:
            return ("exclude", None)
        return ("shared", None)
