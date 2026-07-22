"""精简打包：按子模块 import 分析选择性解压 wheel。

按包分发到对应的 ``SlimSpec`` 子类实现精简规则：

- :class:`fspack.slim.qt.QtSlimSpec`：Qt 库（PySide2/PySide6/PyQt5/PyQt6）白名单 +
  子模块依赖闭包（``import QtWidgets`` 自动加入 ``Gui``/``Core``）
- :class:`fspack.slim.default.DefaultSlimSpec`：兜底规则，非 Qt 库按子模块
  选择性保留 ``.pyd``/``.pyi``/``.so``，其他全保留

新增包精简规则只需继承 ``SlimSpec`` 并在 ``_register_builtin_specs`` 中
``register_spec`` 注册，``slim_unpack``/``classify_entry`` 自动分发。

公共 API：

- :func:`slim_unpack`：按需解压 wheel 列表
- :func:`classify_entry`：分类 wheel 条目归属
- :class:`SlimSpec`：精简规则抽象基类
- :func:`register_spec`/``get_spec``：注册表接口

向后兼容：``_qt_module_closure``/``_qt_dll_submodule``/``_normalize_qt_sub``
从 :mod:`fspack.slim.qt` 重新导出；``zipfile`` 暴露在模块命名空间供外部 monkeypatch。
"""

from __future__ import annotations

import zipfile  # noqa: F401  暴露在 fspack.slim 命名空间，供外部 monkeypatch 使用

from fspack.slim.base import (
    SlimSpec,
    classify_entry,
    get_spec,
    register_spec,
    slim_unpack,
)
from fspack.slim.default import DefaultSlimSpec
from fspack.slim.qt import (
    QT_PACKAGES,
    QtSlimSpec,
    _normalize_qt_sub,
    _qt_dll_submodule,
    _qt_module_closure,
)

# 显式按顺序注册内置 spec：
# QtSlimSpec 优先于 DefaultSlimSpec（match 始终 True，兜底）。
# Python 模块只初始化一次（除 reload），无需额外去重。
# 不依赖 from-import 顺序，避免 isort 重排导致注册顺序错误。
register_spec(QtSlimSpec)
register_spec(DefaultSlimSpec)

__all__ = [
    "QT_PACKAGES",
    "QtSlimSpec",
    "SlimSpec",
    "_normalize_qt_sub",
    "_qt_dll_submodule",
    "_qt_module_closure",
    "classify_entry",
    "get_spec",
    "register_spec",
    "slim_unpack",
]
