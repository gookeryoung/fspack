"""科学计算综合示例：NumPy 生成数据 + SciPy 拟合求解 + Matplotlib 绘图.

验证科学计算三件套在 embed python（free-threaded 3.14t）下打包可用，
单入口串起典型数据分析流水线：

1. numpy：随机数生成与统计聚合，生成带噪声的阻尼振荡观测数据
2. scipy.optimize：curve_fit 非线性最小二乘拟合，恢复模型参数
3. numexpr：多线程表达式引擎批量计算拟合值
4. matplotlib：Agg 非交互后端绘制「观测散点 + 拟合曲线」对比图，
   覆盖 mpl-data 字体/样式资源与 pyplot 接口

使用 Agg 非交互后端，无需 GUI 即可生成图片，适合打包后无显示环境
运行验证。打包后 stdout 可见。
"""

from __future__ import annotations

from pathlib import Path


def main() -> None:
    """运行科学计算流水线：生成数据 → 拟合参数 → 计算拟合值 → 绘图保存 PNG."""
    import matplotlib

    matplotlib.use("Agg")  # 非交互后端，无需 GUI
    import matplotlib.pyplot as plt
    import numexpr as ne
    import numpy as np
    import scipy
    from scipy import optimize

    # ---- 1. numpy：生成带噪声的阻尼振荡观测数据 ----
    rng = np.random.default_rng(42)
    x = np.linspace(0.0, 4.0, 200)
    # 真实参数：振幅 2.0 / 角频率 5.0 / 衰减 0.8 / 初相 0.5
    y = 2.0 * np.sin(5.0 * x + 0.5) * np.exp(-0.8 * x) + rng.normal(0.0, 0.08, x.size)

    def damped(xv: object, amp: object, freq: object, decay: object, phase: object) -> object:
        """阻尼振荡模型：amp·sin(freq·x + phase)·e^(-decay·x)."""
        return amp * np.sin(freq * xv + phase) * np.exp(-decay * xv)

    # ---- 2. scipy：非线性最小二乘拟合恢复模型参数 ----
    popt, _ = optimize.curve_fit(damped, x, y, p0=(1.0, 4.0, 0.5, 0.0))
    amp, freq, decay, phase = popt

    # ---- 3. numexpr：表达式引擎批量计算拟合值（多线程加速）----
    fitted = ne.evaluate("amp*sin(freq*x+phase)*exp(-decay*x)")
    rms = float(np.sqrt(np.mean((y - fitted) ** 2)))

    # ---- 4. matplotlib：观测散点 + 拟合曲线对比图 ----
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.scatter(x, y, s=8, alpha=0.6, label="observed")
    ax.plot(x, fitted, color="crimson", label="fitted")
    ax.set_title("Damped Oscillation Fit")
    ax.set_xlabel("t")
    ax.set_ylabel("y")
    ax.legend()

    out_file = Path(__file__).parent / "fit_result.png"
    fig.savefig(out_file, dpi=80)
    plt.close(fig)

    print(f"numpy {np.__version__} / scipy {scipy.__version__} / numexpr {ne.__version__}")
    print(f"matplotlib {matplotlib.__version__}")
    print(
        f"sci demo ok: amp={amp:.3f} freq={freq:.3f} decay={decay:.3f} "
        f"phase={phase:.3f} rms={rms:.4f} saved {out_file.name}"
    )


if __name__ == "__main__":
    main()
