"""精简打包：按子模块 import 分析选择性解压 wheel。

按包分发到对应的 ``SlimSpec`` 子类实现精简规则：

- :class:`fspack.slim.qt.QtSlimSpec`：Qt 库（PySide2/PySide6/PyQt5/PyQt6）白名单 +
  子模块依赖闭包（``import QtWidgets`` 自动加入 ``Gui``/``Core``）
- :class:`fspack.slim.libs.NumpySlimSpec`：numpy 剥离 ``distutils``/
  ``_pyinstaller`` 等已弃用构建工具与 PyInstaller hook 子目录（``f2py`` 保留，
  scipy 运行时通过 ``from numpy import *`` 触发导入）
- :class:`fspack.slim.libs.MatplotlibSlimSpec`：matplotlib 剥离 ``sphinxext``
  文档扩展与跨包/嵌套 ``tests`` 目录（含 ``mpl_toolkits/tests/``）
- :class:`fspack.slim.libs.ScipySlimSpec`：scipy 剥离各子模块下的嵌套
  ``tests`` 目录（如 ``scipy/linalg/tests/``）
- :class:`fspack.slim.libs.LxmlSlimSpec`：lxml 剥离 ``includes`` C 头文件目录
- :class:`fspack.slim.default.DefaultSlimSpec`：兜底规则，非 Qt 库按子模块
  选择性保留 ``.pyd``/``.pyi``/``.so``，其他全保留

新增包精简规则只需继承 ``SlimSpec`` 并在下方 ``register_spec`` 注册，
``slim_unpack``/``classify_entry`` 自动分发。

公共 API：

- :func:`slim_unpack`：按需解压 wheel 列表
- :func:`classify_entry`：分类 wheel 条目归属
- :class:`SlimSpec`：精简规则抽象基类
- :func:`register_spec`/``get_spec``：注册表接口
"""

from __future__ import annotations

from fspack.slim.base import (
    SlimSpec,
    classify_entry,
    get_spec,
    register_spec,
    slim_unpack,
)
from fspack.slim.default import DefaultSlimSpec
from fspack.slim.libs import (
    LxmlSlimSpec,
    MatplotlibSlimSpec,
    NumpySlimSpec,
    ScipySlimSpec,
)
from fspack.slim.qt import QT_PACKAGES, QtSlimSpec

# 显式按顺序注册内置 spec：
# - QtSlimSpec：match 限定为 Qt 包名，优先匹配
# - NumpySlimSpec/MatplotlibSlimSpec/ScipySlimSpec/LxmlSlimSpec：match 限定为
#   具体包名，优先于兜底
# - DefaultSlimSpec：match 始终 True，必须最后注册（兜底）
# Python 模块只初始化一次（除 reload），无需额外去重。
# 不依赖 from-import 顺序，避免 isort 重排导致注册顺序错误。
register_spec(QtSlimSpec)
register_spec(NumpySlimSpec)
register_spec(MatplotlibSlimSpec)
register_spec(ScipySlimSpec)
register_spec(LxmlSlimSpec)
register_spec(DefaultSlimSpec)

__all__ = [
    "QT_PACKAGES",
    "LxmlSlimSpec",
    "MatplotlibSlimSpec",
    "NumpySlimSpec",
    "QtSlimSpec",
    "ScipySlimSpec",
    "SlimSpec",
    "classify_entry",
    "get_spec",
    "register_spec",
    "slim_unpack",
]
