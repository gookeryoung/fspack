"""常用大型库精简规则。

针对体积大或含 C 扩展的常用库，在通用剥离目录（``COMMON_EXCLUDE_SUBDIRS``）
之外，额外剥离库专属的非必要目录（如开发辅助、编译工具、C 头文件、嵌套
测试目录等）。

新增库精简规则时只需：

1. 继承 ``SlimSpec``，实现 ``match``/``normalize_submodule``/``expand_closure``
2. ``classify_entry`` 委托 :meth:`SlimSpec._default_classify`，传入库专属
   剥离集合作为 ``extra_excludes``（二级目录）与 ``nested_excludes``
   （任意层级，含跨包）
3. 在 ``slim/__init__.py`` 注册（在 ``DefaultSlimSpec`` 之前）
"""

from __future__ import annotations

from fspack.slim.base import SlimSpec, override

__all__ = [
    "LxmlSlimSpec",
    "MatplotlibSlimSpec",
    "NumpySlimSpec",
    "ScipySlimSpec",
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


# 嵌套测试目录名：matplotlib/scipy 等科学库各子模块下均含 tests/，
# 剥离任意层级的 tests 目录（含跨包 mpl_toolkits/tests/）
_NESTED_TEST_DIRS = frozenset({"tests"})


class MatplotlibSlimSpec(SlimSpec):
    """matplotlib 精简规则：剥离 sphinxext 与跨包/嵌套 tests 目录。

    matplotlib wheel 含跨包目录 ``mpl_toolkits/``（独立顶层包）与
    ``matplotlib.libs/``（共享 DLL）。通用剥离（``matplotlib/tests``、
    ``matplotlib/docs`` 等）由 :meth:`_default_classify` 处理，本规则扩展：

    - ``sphinxext``：matplotlib 二级目录，Sphinx 文档构建扩展（运行时不需要）
    - ``tests``（嵌套）：剥离 ``mpl_toolkits/<sub>/tests/``、
      ``matplotlib/tests/``（后者与 COMMON_EXCLUDE_SUBDIRS 冗余但无害）
    - 顶层 C 扩展始终保留：``ft2font.pyd`` 是 ``__init__._check_versions()``
      硬依赖（``from . import ft2font``），剥离即 ImportError。通过
      ``top_ext_always_shared=True`` 将顶层 ``.pyd``/``.pyi``/``.so`` 归
      shared 始终保留，不做子模块选择性剥离。

    运行时保留：``matplotlib/mpl-data/``（字体/样式）、``matplotlib/backends/``、
    ``matplotlib.libs/``（共享 DLL）、``mpl_toolkits/``（非 tests 部分）、
    ``pylab.py``、所有顶层 C 扩展（``ft2font``/``_image``/``_path`` 等）。
    """

    _EXTRA_EXCLUDES = frozenset(
        {
            "sphinxext",  # Sphinx 文档构建扩展
        }
    )

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:
        """匹配归一化包名 ``matplotlib``."""
        return whl_pkg == "matplotlib"

    @classmethod
    @override
    def normalize_submodule(cls, sub: str) -> str:
        """matplotlib 不做子模块归一化，原样返回."""
        return sub

    @classmethod
    @override
    def expand_closure(cls, subs: set[str]) -> set[str]:
        """matplotlib 子模块无显式依赖闭包，返回输入集合的副本."""
        return set(subs)

    @classmethod
    @override
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """matplotlib 条目分类，委托 :meth:`_default_classify` + 库专属剥离集合.

        ``top_ext_always_shared=True``：顶层 C 扩展（ft2font 等）始终保留，
        不做子模块选择性剥离。
        """
        return cls._default_classify(entry, top_pkg, keep_subs, cls._EXTRA_EXCLUDES, _NESTED_TEST_DIRS, True)


class ScipySlimSpec(SlimSpec):
    """scipy 精简规则：剥离各子模块下的嵌套 tests 目录。

    scipy 各子模块（``linalg``/``fft``/``optimize``/``stats`` 等）下均含
    ``tests/`` 子目录，约占 scipy 总体积 10-15%。``COMMON_EXCLUDE_SUBDIRS``
    仅检查 ``parts[1]``（二级目录），无法剥离 ``scipy/linalg/tests/`` 这类
    三级嵌套。本规则通过 ``nested_excludes`` 在任意层级剥离 ``tests``：

    - ``tests``（嵌套）：剥离 ``scipy/<sub>/tests/``、``scipy/<sub>/<deep>/tests/``

    运行时保留：``scipy/_lib/``（内部库）、``scipy/<sub>/``（非 tests 部分）、
    ``scipy.libs/``（共享 DLL）。
    """

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:
        """匹配归一化包名 ``scipy``."""
        return whl_pkg == "scipy"

    @classmethod
    @override
    def normalize_submodule(cls, sub: str) -> str:
        """scipy 不做子模块归一化，原样返回."""
        return sub

    @classmethod
    @override
    def expand_closure(cls, subs: set[str]) -> set[str]:
        """scipy 子模块无显式依赖闭包，返回输入集合的副本."""
        return set(subs)

    @classmethod
    @override
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """scipy 条目分类，委托 :meth:`_default_classify` + 嵌套 tests 剥离."""
        return cls._default_classify(entry, top_pkg, keep_subs, frozenset(), _NESTED_TEST_DIRS)
