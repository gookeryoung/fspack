"""$project_name 入口：SciPy 科学计算示例."""

import numpy as np
from scipy import linalg


def main() -> None:
    """求解线性方程组 Ax = b 并验证."""
    A = np.array([[3, 2, 1], [2, 3, 2], [1, 2, 4]], dtype=float)
    b = np.array([6, 7, 8], dtype=float)

    print("$project_name: SciPy 科学计算")
    print(f"系数矩阵 A:\n{A}")
    print(f"常数向量 b: {b}")

    x = linalg.solve(A, b)
    print(f"解向量 x: {x}")
    print(f"验证 A @ x: {A @ x}")

    det = linalg.det(A)
    print(f"行列式 |A|: {det:.4f}")


if __name__ == "__main__":
    main()
