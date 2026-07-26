"""matplotlib 绘图示例：Agg 后端保存图片到文件。

验证 matplotlib 在 embed python 下打包可用，含 mpl-data 字体/样式资源、
backends 后端模块、pyplot 接口。使用 Agg 非交互后端，无需 GUI 即可生成图片，
适合打包后无显示环境运行。打包后 stdout 可见。
"""

from __future__ import annotations

from pathlib import Path


def main() -> None:
    """用 matplotlib Agg 后端绘制直方图并保存到 PNG 文件."""
    import matplotlib

    matplotlib.use("Agg")  # 非交互后端，无需 GUI
    import matplotlib.pyplot as plt
    import numpy as np

    rng = np.random.default_rng(42)
    data = rng.standard_normal(1000)

    fig, ax = plt.subplots()
    ax.hist(data, bins=30, color="steelblue", edgecolor="white")
    ax.set_title("Simple Histogram")
    ax.set_xlabel("Value")
    ax.set_ylabel("Frequency")

    out_file = Path(__file__).parent / "histogram.png"
    fig.savefig(out_file, dpi=80)
    plt.close(fig)

    print(f"matplotlib {matplotlib.__version__}")
    print(f"matplotlib demo ok: saved {out_file.name} size={out_file.stat().st_size}")


if __name__ == "__main__":
    main()
