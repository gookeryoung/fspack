"""$project_name 入口：Matplotlib 图表示例.

显式 ``import tkinter`` 触发 fspack 打包 Tcl/Tk 运行时（embed python 默认
缺失 tkinter，fspack 依 AST 检测 ``import tkinter`` 决定是否补充 Tcl/Tk 资源）。
配合 ``matplotlib.use("TkAgg")`` 强制使用 TkAgg 交互后端，否则 matplotlib
在 embed python 下回退到 Agg 非交互后端，``plt.show`` 会抛
``FigureCanvasAgg is non-interactive`` 错误。
"""

import tkinter  # noqa: F401  触发 fspack 打包 Tcl/Tk，使 TkAgg 后端可用

import matplotlib

matplotlib.use("TkAgg")  # 显式选择 TkAgg 交互后端，避免回退到 Agg
import matplotlib.pyplot as plt
import numpy as np


def main() -> None:
    """绘制正弦波图表并显示."""
    x = np.linspace(0, 2 * np.pi, 200)
    y = np.sin(x)

    plt.figure(figsize=(8, 5))
    plt.plot(x, y, label="sin(x)", color="steelblue")
    plt.title("$project_name")
    plt.xlabel("x")
    plt.ylabel("sin(x)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
