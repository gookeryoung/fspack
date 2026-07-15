"""入口包装器源码生成。

fspack 在 dist 根目录为每个入口生成 ``_entry_<name>.py`` 包装器，由 C loader
通过 ``.entry`` 文件加载运行。包装器负责：

1. **设置 Qt 插件路径**：PySide2/PySide6/PyQt5/PyQt6 的 ``QT_PLUGIN_PATH``
   必须在 import 用户代码前设置，否则 ``QApplication`` 找不到平台插件。
2. **包式入口支持**：若入口脚本位于包内（所在目录链直至 ``src_dir`` 都有
   ``__init__.py``），用 :func:`runpy.run_module` 以包上下文运行，使相对导入
   （``from .conf import ...``）可用；否则用 :func:`runpy.run_path` 直接运行
   顶层脚本。

包模式下设 ``src`` 包可被 import（``sys.path`` 加入 dist 根），模块名形如
``src.game``；顶层模式直接 ``run_path`` 入口脚本，``sys.path`` 已含 ``src``
自身（由 ``_pth`` 配置），绝对导入可用。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["dotted_module_name", "generate_wrapper_source"]

# wrapper 源码模板：{entry_name}/{module_dotted}/{entry_rel} 由 format 填入。
# module_dotted 为 None 时走顶层模式（run_path），否则走包模式（run_module）。
_WRAPPER_TEMPLATE = '''\
"""fspack 生成的入口包装器（{entry_name}）。

设置 Qt 插件路径后以包上下文运行用户入口，使相对导入可用。
此文件由 fspack 构建时生成，不要手动编辑。
"""
import os
import runpy
import sys

_DIST_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(_DIST_DIR, "src")
_SITE_PACKAGES = os.path.join(_DIST_DIR, "runtime", "Lib", "site-packages")

# Qt 插件路径（PySide2/PySide6/PyQt5/PyQt6）——必须在 import 用户代码前设置，
# 否则 QApplication 启动时报 "Failed to load platform plugin windows"
for _qt_pkg in ("PySide2", "PySide6", "PyQt5", "PyQt6"):
    _qt_plugins = os.path.join(_SITE_PACKAGES, _qt_pkg, "plugins")
    if os.path.isdir(_qt_plugins):
        os.environ.setdefault("QT_PLUGIN_PATH", _qt_plugins)
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", _qt_plugins)
        break

_ENTRY_MODULE = {module_dotted!r}
_ENTRY_REL = {entry_rel!r}

if _ENTRY_MODULE:
    # 包模式：加入 dist 根让包可 import，run_module 保留包上下文（相对导入可用）
    if _DIST_DIR not in sys.path:
        sys.path.insert(0, _DIST_DIR)
    runpy.run_module(_ENTRY_MODULE, run_name="__main__", alter_sys=True)
else:
    # 顶层模式：直接 run_path（sys.path 已含 src 自身，绝对导入可用）
    runpy.run_path(os.path.join(_SRC_DIR, _ENTRY_REL), run_name="__main__")
'''


def dotted_module_name(src_dir: Path, entry_file: Path) -> str | None:
    """计算入口脚本的 dotted 模块名（基于 dist 结构）。

    fspack 把 ``src_dir`` 内容复制到 ``dist/src``。若入口在包内（入口所在
    目录链直至 ``src_dir`` 都有 ``__init__.py``），返回 dotted 模块名供
    :func:`runpy.run_module` 使用，使相对导入可用；否则返回 ``None``，
    wrapper 用 :func:`runpy.run_path` 运行顶层脚本。

    返回值规则：

    - 入口在 ``src_dir`` 顶层且 ``src_dir`` 有 ``__init__.py``：返回
      ``"src.<stem>"``（如 ``"src.game"``），fspack 复制后 ``dist/src`` 自身
      是名为 ``src`` 的包，``sys.path`` 加入 dist 根即可 import。
    - 入口在 ``src_dir`` 顶层且 ``src_dir`` 无 ``__init__.py``：返回 ``None``
      （顶层模块，``sys.path`` 已含 ``src`` 自身）。
    - 入口在 ``src_dir`` 子目录且目录链都有 ``__init__.py``：

      * ``src_dir`` 有 ``__init__.py``：返回 ``"src.<pkg>.<stem>"``
      * ``src_dir`` 无 ``__init__.py``：返回 ``"<pkg>.<stem>"``（``sys.path``
        含 ``src`` 自身，子包可直接 import）

    - 入口在 ``src_dir`` 子目录且某级目录无 ``__init__.py``：返回 ``None``
      （退化为顶层，用 ``run_path``）。
    """
    try:
        rel = entry_file.relative_to(src_dir)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None

    dir_parts = parts[:-1]
    last = parts[-1]
    module_stem = last[: -len(".py")] if last.endswith(".py") else last

    # 入口在 src_dir 顶层：src_dir 是包则 'src.<stem>'，否则顶层模块
    if not dir_parts:
        return f"src.{module_stem}" if (src_dir / "__init__.py").is_file() else None

    # 入口在子目录，检查目录链是否都是包
    current = src_dir
    for part in dir_parts:
        current = current / part
        if not (current / "__init__.py").is_file():
            return None

    # 目录链都是包：src_dir 是包则前缀 'src'，否则从子包起
    prefix = ("src",) if (src_dir / "__init__.py").is_file() else ()
    return ".".join((*prefix, *dir_parts, module_stem))


def generate_wrapper_source(
    entry_name: str,
    module_dotted: str | None,
    entry_rel: str,
) -> str:
    """生成入口包装器源码。

    entry_name: 入口名（用于文档注释，便于区分多入口项目的不同 wrapper）。
    module_dotted: :func:`dotted_module_name` 返回的 dotted 模块名；``None``
        表示顶层模式，wrapper 用 :func:`runpy.run_path`。
    entry_rel: 入口相对 ``src_dir`` 的 POSIX 路径（如 ``"game.py"``），
        顶层模式用其定位脚本。
    """
    return _WRAPPER_TEMPLATE.format(
        entry_name=entry_name,
        module_dotted=module_dotted,
        entry_rel=entry_rel,
    )
