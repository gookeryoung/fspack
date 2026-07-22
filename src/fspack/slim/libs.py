"""常用大型库精简规则。

针对体积大或含 C 扩展的常用库，在通用剥离目录（``COMMON_EXCLUDE_SUBDIRS``）
之外，额外剥离库专属的非必要目录（如开发辅助、编译工具、C 头文件等）。

新增库精简规则时只需：

1. 继承 ``SlimSpec``，实现 ``match``/``normalize_submodule``/``expand_closure``
2. ``classify_entry`` 委托 :meth:`SlimSpec._default_classify`，传入库专属
   剥离集合作为 ``extra_excludes``
3. 在 ``slim/__init__.py`` 注册（在 ``DefaultSlimSpec`` 之前）
"""

from __future__ import annotations

from fspack.slim.base import SlimSpec, override

__all__ = [
    "LxmlSlimSpec",
    "NumpySlimSpec",
]


class NumpySlimSpec(SlimSpec):
    """numpy 精简规则：剥离 Fortran 编译工具与 PyInstaller hook 子目录。

    通用剥离（examples/docs/tests 等）由 :meth:`_default_classify` 处理，
    本规则扩展剥离 numpy 专属非运行时目录：

    - ``f2py``：Fortran 编译工具（运行时不需要，仅构建时用）
    - ``distutils``：已弃用的构建工具（NumPy 2.0+ 不再随包分发）
    - ``_pyinstaller``：PyInstaller hook（fspack 不依赖 PyInstaller）
    """

    _EXTRA_EXCLUDES = frozenset(
        {
            "f2py",  # Fortran 编译工具
            "distutils",  # 已弃用构建工具
            "_pyinstaller",  # PyInstaller hook
        }
    )

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:
        """匹配归一化包名 ``numpy``."""
        return whl_pkg == "numpy"

    @classmethod
    @override
    def normalize_submodule(cls, sub: str) -> str:
        """numpy 不做子模块归一化，原样返回."""
        return sub

    @classmethod
    @override
    def expand_closure(cls, subs: set[str]) -> set[str]:
        """numpy 子模块无显式依赖闭包，返回输入集合的副本."""
        return set(subs)

    @classmethod
    @override
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """numpy 条目分类，委托 :meth:`_default_classify` + 库专属剥离集合."""
        return cls._default_classify(entry, top_pkg, keep_subs, cls._EXTRA_EXCLUDES)


class LxmlSlimSpec(SlimSpec):
    """lxml 精简规则：剥离 C 头文件目录。

    通用剥离（examples/docs/tests 等）由 :meth:`_default_classify` 处理，
    本规则扩展剥离 lxml 专属非运行时目录：

    - ``includes``：C 扩展开发用头文件（``lxml/includes/libxml/``、
      ``lxml/includes/libxslt/`` 等），约 100 个 ``.h`` 文件，运行时不需要
    """

    _EXTRA_EXCLUDES = frozenset(
        {
            "includes",  # C 头文件
        }
    )

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:
        """匹配归一化包名 ``lxml``."""
        return whl_pkg == "lxml"

    @classmethod
    @override
    def normalize_submodule(cls, sub: str) -> str:
        """lxml 不做子模块归一化，原样返回."""
        return sub

    @classmethod
    @override
    def expand_closure(cls, subs: set[str]) -> set[str]:
        """lxml 子模块无显式依赖闭包，返回输入集合的副本."""
        return set(subs)

    @classmethod
    @override
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """lxml 条目分类，委托 :meth:`_default_classify` + 库专属剥离集合."""
        return cls._default_classify(entry, top_pkg, keep_subs, cls._EXTRA_EXCLUDES)
