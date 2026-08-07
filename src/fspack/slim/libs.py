"""常用大型库精简规则。

针对体积大或含 C 扩展的常用库，在通用剥离目录（``COMMON_EXCLUDE_SUBDIRS``）
之外，额外剥离库专属的非必要目录（如开发辅助、编译工具、C 头文件、嵌套
测试目录等）。

新增库精简规则时只需：

1. 继承 ``SlimSpec``，实现 ``match``/``classify_entry``；
   ``normalize_submodule``/``expand_closure`` 用基类默认实现即可
2. ``classify_entry`` 委托 :meth:`SlimSpec._default_classify`，传入库专属
   剥离集合作为 ``extra_excludes``（二级目录）与 ``nested_excludes``
   （任意层级，含跨包）
3. 在 ``slim/__init__.py`` 注册（在 ``DefaultSlimSpec`` 之前）
"""

from __future__ import annotations

from fspack._compat import override
from fspack.slim.base import SlimSpec

__all__ = [
    "LxmlSlimSpec",
    "MatplotlibSlimSpec",
    "NumpySlimSpec",
    "PyarrowSlimSpec",
    "ScipySlimSpec",
    "SklearnSlimSpec",
]


class NumpySlimSpec(SlimSpec):
    """numpy 精简规则：剥离已弃用构建工具与 PyInstaller hook 子目录。

    通用剥离（examples/docs/tests 等）由 :meth:`_default_classify` 处理，
    本规则扩展剥离 numpy 专属非运行时目录：

    - ``distutils``：已弃用的构建工具（NumPy 2.0+ 不再随包分发）
    - ``_pyinstaller``：PyInstaller hook（fspack 不依赖 PyInstaller）

    注意：``f2py`` 和 ``testing`` 不能剥离——scipy 的 ``array_api_compat``
    运行时执行 ``from numpy import *``，触发 ``numpy.__getattr__`` 导入
    ``numpy.f2py`` 和 ``numpy.testing``，剥离会导致 ``ModuleNotFoundError``。
    """

    _EXTRA_EXCLUDES = frozenset(
        {
            "distutils",  # 已弃用构建工具
            "_pyinstaller",  # PyInstaller hook
        }
    )

    # numpy/_core/tests/ 下运行时必需的非测试文件（其余 test_*.py 是真测试，仍剥离）：
    # - _natype.py：定义 pandas NA 兼容占位对象 pd_NA，numpy.testing._private.utils
    #   运行时执行 ``from numpy._core.tests._natype import pd_NA``，是 numpy.testing
    #   断言工具的运行时依赖，并非测试代码
    # - _locales.py：运行时可能被依赖
    # 见 :meth:`classify_entry` 豁免逻辑，避免被 :attr:`SlimSpec.NESTED_TEST_DIRS`
    # 的 ``"tests"`` 嵌套规则误剥离
    _CORE_TESTS_RUNTIME_FILES: frozenset[str] = frozenset({"_natype.py", "_locales.py"})

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:
        """匹配归一化包名 ``numpy``."""
        return whl_pkg == "numpy"

    @classmethod
    @override
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """numpy 条目分类：保留 ``testing`` 子目录与 ``_core/tests/`` 运行时必需文件，
        其余委托 :meth:`_default_classify`。

        ``numpy/testing/`` 是 ``numpy.testing`` 公共 API 模块（非测试代码），
        scipy 通过 ``from numpy import *`` 触发 ``numpy.__getattr__("testing")``
        导入，不能被 ``COMMON_EXCLUDE_SUBDIRS`` 的 ``"testing"`` 剥离。
        ``numpy/tests/``（复数）是真正的测试代码，仍由通用规则剥离。

        ``numpy/_core/tests/`` 下的 ``_natype.py``/``_locales.py`` 是 numpy.testing
        运行时依赖（``numpy.testing._private.utils`` 执行
        ``from numpy._core.tests._natype import pd_NA``），不能被嵌套 tests 规则
        （:attr:`SlimSpec.NESTED_TEST_DIRS`）剥离；同目录其余 ``test_*.py`` 是真测试，仍剥离。
        """
        parts = entry.split("/")
        if len(parts) >= 2 and parts[0] == top_pkg and parts[1] == "testing":
            return ("shared", None)
        if (
            len(parts) == 4
            and parts[0] == top_pkg
            and parts[1] == "_core"
            and parts[2] == "tests"
            and parts[3] in cls._CORE_TESTS_RUNTIME_FILES
        ):
            return ("shared", None)
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
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """lxml 条目分类，委托 :meth:`_default_classify` + 库专属剥离集合."""
        return cls._default_classify(entry, top_pkg, keep_subs, cls._EXTRA_EXCLUDES)


# 嵌套测试目录名已提升到 SlimSpec.NESTED_TEST_DIRS，所有走 _default_classify
# 的 spec 自动剥离任意层级的 tests 目录（含跨包 mpl_toolkits/tests/、
# scipy/<sub>/tests/、pandas/<sub>/tests/ 等），无需各 spec 显式声明。


class MatplotlibSlimSpec(SlimSpec):
    """matplotlib 精简规则：剥离 sphinxext 与跨包/嵌套 tests 目录。

    matplotlib wheel 含跨包目录 ``mpl_toolkits/``（独立顶层包）与
    ``matplotlib.libs/``（共享 DLL）。通用剥离（``matplotlib/tests``、
    ``matplotlib/docs`` 等）由 :meth:`_default_classify` 处理，本规则扩展：

    - ``sphinxext``：matplotlib 二级目录，Sphinx 文档构建扩展（运行时不需要）
    - ``tests``（嵌套）：由 :attr:`SlimSpec.NESTED_TEST_DIRS` 自动剥离
      （``mpl_toolkits/<sub>/tests/``、``matplotlib/tests/`` 等）
    - 顶层 C 扩展始终保留：``ft2font.pyd`` 是 ``__init__._check_versions()``
      硬依赖（``from . import ft2font``），剥离即 ImportError。通过
      ``top_ext_always_shared=True`` 将顶层 ``.pyd``/``.so`` 归
      shared 始终保留，不做子模块选择性剥离（``.pyi`` 已由 :attr:`STRIP_EXTS`
      统一剥离，无需 spec 处理）。

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
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """matplotlib 条目分类，委托 :meth:`_default_classify` + 库专属剥离集合.

        ``top_ext_always_shared=True``：顶层 C 扩展（ft2font 等）始终保留，
        不做子模块选择性剥离。嵌套 tests 由基类 :attr:`NESTED_TEST_DIRS` 自动剥离。
        """
        return cls._default_classify(entry, top_pkg, keep_subs, cls._EXTRA_EXCLUDES, frozenset(), True)


class ScipySlimSpec(SlimSpec):
    """scipy 精简规则：剥离各子模块下的嵌套 tests 目录。

    scipy 各子模块（``linalg``/``fft``/``optimize``/``stats`` 等）下均含
    ``tests/`` 子目录，约占 scipy 总体积 10-15%。嵌套 tests 剥离由
    :attr:`SlimSpec.NESTED_TEST_DIRS` 自动处理（基类 :meth:`_default_classify`
    合并到 ``nested_excludes``），本 spec 仅需匹配包名即可。

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
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """scipy 条目分类，委托 :meth:`_default_classify`（嵌套 tests 由基类自动剥离）."""
        return cls._default_classify(entry, top_pkg, keep_subs)


class SklearnSlimSpec(SlimSpec):
    """scikit-learn 精简规则：剥离 datasets 下的描述文件与示例图片。

    scikit-learn 的 ``sklearn/datasets/`` 模块含三类附属资源：

    - ``data/``：CSV 数据文件（iris.csv 等），``load_iris``/``load_wine`` 等函数
      运行时读取，**不可剥离**
    - ``descr/``：数据集描述文件（.rst），仅 ``load_iris().DESCR`` 文本展示用，
      不影响 ``import sklearn`` 与算法运算 → 剥离
    - ``images/``：示例图片（china.jpg 等），仅 ``load_sample_image`` 使用，
      非核心功能 → 剥离

    剥离后 ``import sklearn``、``fit``/``predict``/``transform`` 等算法 API
    完全正常，仅 ``DESCR`` 属性返回 None 与 ``load_sample_image`` 不可用。
    嵌套 ``tests/`` 由 :attr:`SlimSpec.NESTED_TEST_DIRS` 自动剥离。
    """

    # sklearn/datasets/ 下可剥离的三级子目录（data/ 保留，运行时必需）
    _DATASETS_STRIP_SUBDIRS: frozenset[str] = frozenset({"descr", "images"})

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:
        """匹配归一化包名 ``scikit-learn`` 与顶层目录名 ``sklearn``.

        scikit-learn wheel 文件名归一化为 ``scikit-learn``，但 wheel 内顶层
        目录为 ``sklearn``（归一化仍为 ``sklearn``）。``_detect_top_pkg`` 用
        顶层目录名的归一化形式查找 spec，故需同时匹配两者，否则 sklearn
        wheel 会被当作无匹配 top_pkg 走全量解压。
        """
        return whl_pkg in ("scikit-learn", "sklearn")

    @classmethod
    @override
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """sklearn 条目分类：剥离 datasets/descr/ 与 datasets/images/，其余委托 :meth:`_default_classify`."""
        parts = entry.split("/")
        # sklearn/datasets/descr/ 和 sklearn/datasets/images/ 剥离
        # 保留 sklearn/datasets/data/（load_iris 等运行时读取）
        if (
            len(parts) >= 4
            and parts[0] == top_pkg
            and parts[1] == "datasets"
            and parts[2] in cls._DATASETS_STRIP_SUBDIRS
        ):
            return ("exclude", None)
        return cls._default_classify(entry, top_pkg, keep_subs)


class PyarrowSlimSpec(SlimSpec):
    """pyarrow 精简规则：剥离 C++ 头文件目录，顶层 C 扩展始终保留。

     pyarrow 的 ``pyarrow/includes/`` 含 C++ 头文件（.h）与 Cython 定义文件
    （.pxd），仅供第三方 C++/Cython 扩展编译时 ``#include`` 使用，
     ``import pyarrow`` 运行时不读取。

     - ``.h`` 文件已由 :attr:`SlimSpec.STRIP_EXTS` 剥离
     - ``.pxd`` 文件不在 ``STRIP_EXTS`` 中，需通过本 spec 剥离整个 ``includes/``
       二级目录覆盖

     顶层 C 扩展（``lib.pyd``/``_compute.pyd`` 等）是 ``pyarrow.__init__`` 硬依赖
    （``from pyarrow.lib import ...``），剥离即 ImportError。通过
    ``top_ext_always_shared=True`` 将顶层 ``.pyd``/``.so`` 归 shared
    始终保留，不做子模块选择性剥离（``.pyi`` 已由 :attr:`STRIP_EXTS`
    统一剥离，与 :class:`MatplotlibSlimSpec` 同模式）。

     与 :class:`LxmlSlimSpec` 模式一致（lxml 剥离 ``includes/`` C 头文件目录）。
     嵌套 ``tests/`` 由 :attr:`SlimSpec.NESTED_TEST_DIRS` 自动剥离。
    """

    _EXTRA_EXCLUDES = frozenset(
        {
            "includes",  # C++ 头文件与 Cython 定义
        }
    )

    @classmethod
    @override
    def match(cls, whl_pkg: str) -> bool:
        """匹配归一化包名 ``pyarrow``."""
        return whl_pkg == "pyarrow"

    @classmethod
    @override
    def classify_entry(
        cls,
        entry: str,
        top_pkg: str,
        keep_subs: set[str],
    ) -> tuple[str, str | None]:
        """pyarrow 条目分类，委托 :meth:`_default_classify` + 库专属剥离集合.

        ``top_ext_always_shared=True``：顶层 C 扩展（lib.pyd 等）始终保留，
        不做子模块选择性剥离。
        """
        return cls._default_classify(entry, top_pkg, keep_subs, cls._EXTRA_EXCLUDES, frozenset(), True)
