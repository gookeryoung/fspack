"""入口包装器源码生成。

fspack 在 dist 根目录为每个入口生成 ``_entry_<name>.py`` 包装器，由 C loader
通过 ``.entry`` 文件加载运行。包装器负责：

1. **设置 Qt 插件路径**：PySide2/PySide6/PyQt5/PyQt6 的 ``QT_PLUGIN_PATH``
   必须在 import 用户代码前设置，否则 ``QApplication`` 找不到平台插件。
2. **包式入口支持**：若入口脚本位于包内（所在目录链直至首个包目录都有
   ``__init__.py``），用 :func:`runpy.run_module` 以包上下文运行，使相对导入
   （``from .conf import ...``）可用；否则用 :func:`runpy.run_path` 直接运行
   顶层脚本。

包模式下 wrapper 将 ``pkg_root`` 加入 ``sys.path`` 使首层包可 import。对于
src-layout 项目（包在 ``src/<pkg>/`` 下，``src/`` 是容器而非包），wrapper
加入 ``dist/src/src`` 使 ``<pkg>`` 可 import。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["EntryWrapper"]

# wrapper 源码模板：{entry_name}/{module_dotted}/{pkg_root_rel}/{entry_rel} 由 format 填入。
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
_PKG_ROOT_REL = {pkg_root_rel!r}
_PKG_ROOT = os.path.normpath(os.path.join(_DIST_DIR, _PKG_ROOT_REL))

if _ENTRY_MODULE:
    # 包模式：加入包根让首层包可 import，run_module 保留包上下文（相对导入可用）
    if _PKG_ROOT not in sys.path:
        sys.path.insert(0, _PKG_ROOT)
    runpy.run_module(_ENTRY_MODULE, run_name="__main__", alter_sys=True)
else:
    # 顶层模式：直接 run_path（sys.path 已含 src 自身，绝对导入可用）
    runpy.run_path(os.path.join(_SRC_DIR, _ENTRY_REL), run_name="__main__")
'''


class EntryWrapper:
    """入口包装器生成器.

    封装入口脚本的 dotted 模块名计算与包装器源码生成。两个方法均为静态方法，
    无状态，通过类名直接调用：``EntryWrapper.dotted_module_name(...)``。
    """

    _TEMPLATE = _WRAPPER_TEMPLATE

    @staticmethod
    def dotted_module_name(src_dir: Path, entry_file: Path) -> tuple[str, str] | None:
        """计算入口脚本的 dotted 模块名与包根路径。

        fspack 把 ``src_dir`` 内容复制到 ``dist/src``。若入口在包内（目录链中
        首个包目录起直至入口都有 ``__init__.py``），返回 ``(module_dotted,
        pkg_root_rel)`` 供 :func:`runpy.run_module` 使用；否则返回 ``None``，
        wrapper 用 :func:`runpy.run_path` 运行顶层脚本。

        返回值 ``(module_dotted, pkg_root_rel)``：

        - ``module_dotted``：dotted 模块名，如 ``"src.game"`` 或 ``"fuscan.__main__"``。
        - ``pkg_root_rel``：包根相对 dist 的 POSIX 路径，wrapper 将其加入
          ``sys.path`` 使 ``module_dotted`` 的首层包可 import。

        返回值规则：

        - 入口在 ``src_dir`` 顶层且 ``src_dir`` 有 ``__init__.py``：返回
          ``("src.<stem>", ".")``——``dist/src`` 自身是名为 ``src`` 的包，
          ``sys.path`` 加入 dist 根即可 import。
        - 入口在 ``src_dir`` 顶层且 ``src_dir`` 无 ``__init__.py``：返回 ``None``
          （顶层模块，``sys.path`` 已含 ``src`` 自身）。
        - 入口在 ``src_dir`` 子目录且目录链从首个包起都有 ``__init__.py``：

          * ``src_dir`` 有 ``__init__.py``：返回 ``("src.<pkg>.<stem>", ".")``
          * ``src_dir`` 无 ``__init__.py``，无容器前缀：返回
            ``("<pkg>.<stem>", "src")``（包在 ``dist/src/<pkg>/``）
          * ``src_dir`` 无 ``__init__.py``，有容器前缀（src-layout，如
            ``src/`` 无 ``__init__.py`` 但其下 ``<pkg>/`` 有）：返回
            ``("<pkg>.<stem>", "src/<containers>")``（包在
            ``dist/src/<containers>/<pkg>/``）

        - 入口在 ``src_dir`` 子目录且首个包之后某级目录无 ``__init__.py``：
          返回 ``None``（退化为顶层，用 ``run_path``）。
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

        src_is_pkg = (src_dir / "__init__.py").is_file()

        # 入口在 src_dir 顶层：src_dir 是包则 'src.<stem>'，否则顶层模块
        if not dir_parts:
            return (f"src.{module_stem}", ".") if src_is_pkg else None

        # 入口在子目录，遍历目录链：
        # - src_dir 非包时，允许前缀无 __init__.py 的目录作为容器（src-layout 的 src/）
        # - 遇到首个包（有 __init__.py）后，后续目录必须都是包
        current = src_dir
        pkg_parts: list[str] = []
        container_parts: list[str] = []
        for part in dir_parts:
            current = current / part
            if not (current / "__init__.py").is_file():
                # 无 __init__.py：仅当 src_dir 非包且尚未遇到包时，视为容器目录
                if not src_is_pkg and not pkg_parts:
                    container_parts.append(part)
                    continue
                return None
            pkg_parts.append(part)

        if not pkg_parts:
            return None  # 目录链全无 __init__.py，退化为顶层

        # 构造模块名与包根路径
        if src_is_pkg:
            # src_dir 是包：module = src.<pkgs>.<stem>, pkg_root = dist
            module = ".".join(("src", *pkg_parts, module_stem))
            pkg_root = "."
        else:
            # src_dir 非包：module = <pkgs>.<stem>
            module = ".".join((*pkg_parts, module_stem))
            # 包根 = dist/src/<containers>（无容器时为 dist/src）
            pkg_root = "/".join(("src", *container_parts))

        return (module, pkg_root)

    @staticmethod
    def generate_wrapper_source(
        entry_name: str,
        module_dotted: str | None,
        entry_rel: str,
        pkg_root_rel: str = ".",
    ) -> str:
        """生成入口包装器源码。

        entry_name: 入口名（用于文档注释，便于区分多入口项目的不同 wrapper）。
        module_dotted: :meth:`dotted_module_name` 返回的 dotted 模块名；``None``
            表示顶层模式，wrapper 用 :func:`runpy.run_path`。
        entry_rel: 入口相对 ``src_dir`` 的 POSIX 路径（如 ``"game.py"``），
            顶层模式用其定位脚本。
        pkg_root_rel: 包根相对 dist 的 POSIX 路径（如 ``"."``、``"src"``、
            ``"src/src"``），包模式时 wrapper 将其加入 ``sys.path`` 使首层包
            可 import。顶层模式不使用此参数。
        """
        return EntryWrapper._TEMPLATE.format(
            entry_name=entry_name,
            module_dotted=module_dotted,
            entry_rel=entry_rel,
            pkg_root_rel=pkg_root_rel,
        )
