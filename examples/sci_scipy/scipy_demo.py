"""scipy 科学计算示例：线性代数与优化求解。

验证 scipy 在 embed python 下打包可用，覆盖 linalg 线性代数、
optimize 优化求解、sparse 稀疏矩阵等核心子模块。打包后无 GUI 依赖，
stdout 可见。
"""

from __future__ import annotations


def main() -> None:
    """运行 scipy 线性代数与优化求解并打印结果."""
    import numpy as np
    import scipy
    from scipy import linalg, optimize, sparse

    # 线性方程组求解 Ax = b
    a = np.array([[3.0, 2.0], [1.0, 4.0]])
    b_vec = np.array([7.0, 6.0])
    x = linalg.solve(a, b_vec)
    residual = float(np.linalg.norm(a @ x - b_vec))

    # 优化求解：Rosenbrock 函数最小值
    result = optimize.minimize(
        lambda v: (1.0 - v[0]) ** 2 + 100.0 * (v[1] - v[0] ** 2) ** 2,
        x0=np.array([-1.0, 1.0]),
        method="Nelder-Mead",
    )
    xmin = result.x

    # 稀疏矩阵
    csr = sparse.eye(3, format="csr")
    nnz = csr.nnz

    print(f"scipy {scipy.__version__}")
    print(
        f"scipy demo ok: x=({x[0]:.3f},{x[1]:.3f}) residual={residual:.2e} min=({xmin[0]:.3f},{xmin[1]:.3f}) nnz={nnz}"
    )


if __name__ == "__main__":
    main()
