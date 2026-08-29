"""入口脚本 AppType 推断：按 import 与声明依赖识别 CLI/GUI/WEB.

从 :mod:`fspack.config.parsing` 拆分而来，封装应用类型推断的判定表与
判定函数。判定优先级：GUI > WEB > CLI（matplotlib 等可视化库偶尔与
web 框架共存，按 GUI 处理关闭控制台更合理）。

依赖 :mod:`fspack.config.models` 提供 ``AppType`` 枚举；
:func:`fspack.analyzer.collect_imports` 惰性导入打破 config ↔ analyzer
循环依赖。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from fspack.config.models import AppType

__all__ = ["infer_app_type"]

# GUI 框架导入名集合：用于按入口脚本 import 推断 AppType。
# webview/pywebview 桌面应用须归 GUI：console subsystem 的打包产物会常驻
# 黑色控制台窗口，且 pywebview 在 Win7 上的 GetDpiForWindow 警告会直接
# 打到控制台惊扰终端用户，按 GUI 关闭控制台后二者均消失。
_GUI_HINTS = frozenset(
    {
        "tkinter",
        "PySide2",
        "PySide6",
        "PyQt5",
        "PyQt6",
        "matplotlib",
        "wx",
        "win32gui",
        "pygame",
        "webview",
        "pywebview",
    }
)

# Web 框架导入名集合：用于按入口脚本 import 推断 AppType.WEB。
# 含 ASGI/WSGI 服务器（uvicorn/hypercorn）与框架本体（flask/fastapi 等），
# 任一 import 即判定为 WEB 类型。GUI 优先级高于 WEB（matplotlib 等 GUI 框架
# 偶尔与 web 框架共存，按 GUI 处理关闭控制台更合理）。
_WEB_HINTS = frozenset({"flask", "fastapi", "sanic", "django", "tornado", "starlette", "uvicorn", "hypercorn", "quart"})


def infer_app_type(path: Path, declared: tuple[str, ...]) -> AppType:
    """根据 import 与声明依赖推断 CLI/GUI/WEB 类型.

    优先级：GUI > WEB > CLI。GUI 框架（PySide/tkinter/matplotlib 等）优先于
    Web 框架（Flask/FastAPI 等），因 matplotlib 等可视化库偶尔与 web 框架共存，
    按 GUI 处理关闭控制台更合理。

    惰性导入 :func:`fspack.analyzer.collect_imports` 打破 config ↔ analyzer 循环依赖。
    """
    from fspack.analyzer import collect_imports

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        # 入口脚本无法读取（非 UTF-8）或语法非法时不崩溃：跳过 import 分析，
        # 仅按声明依赖推断（多入口 declared 为空则回退 CLI，保留控制台最安全）。
        # 语法错误留待后续构建阶段以更明确的上下文报错，此处不阻断类型推断。
        imports: frozenset[str] = frozenset()
    else:
        imports = frozenset(collect_imports(tree))
    # 先查 GUI：matplotlib 等可视化库优先于 web 框架
    for top in imports:
        if top in _GUI_HINTS:
            return AppType.GUI
    # 再查 WEB：flask/fastapi 等任一 import 即判定
    for top in imports:
        if top in _WEB_HINTS:
            return AppType.WEB
    # 声明依赖回退：入口脚本未直接 import 但 pyproject 声明依赖
    for dep in declared:
        top = re.split(r"[<>=!~;\[]", dep, maxsplit=1)[0].strip().replace("-", "_")
        if top in _GUI_HINTS:
            return AppType.GUI
        if top in _WEB_HINTS:
            return AppType.WEB
    return AppType.CLI
