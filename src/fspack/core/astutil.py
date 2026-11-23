"""轻量 AST 工具：从语法树提取 import 模块名.

独立于 :mod:`fspack.analyzer`：``config`` 的 app_type 推断只需要「有哪些顶层
import」，不需要依赖分析/轮子匹配等重量能力。把这一原语放在叶子模块，
``config`` 与 ``analyzer`` 都向下依赖它，消除 config ↔ analyzer 的跨层往返
（此前靠函数内惰性导入掩盖）。
"""

from __future__ import annotations

import ast

__all__ = ["collect_imports"]


def collect_imports(tree: ast.AST) -> list[str]:
    """收集 AST 中所有 import 的顶层模块名，去重保序.

    内存优化：用 ``dict`` 作保序集合（3.7+ ``dict`` 插入序稳定），同时承担
    "已见集合"与"结果保序"两份职责，省掉独立 ``set`` + ``list`` 双结构对象。
    """
    ord_dict: dict[str, None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                ord_dict.setdefault(alias.name.split(".")[0], None)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            ord_dict.setdefault(node.module.split(".")[0], None)
    return list(ord_dict.keys())
