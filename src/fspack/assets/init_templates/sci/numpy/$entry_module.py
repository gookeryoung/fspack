"""$project_name 入口：NumPy 数值计算示例."""

import numpy as np


def main() -> None:
    """演示 NumPy 数组运算与统计."""
    a = np.array([[1, 2, 3], [4, 5, 6]])
    b = np.array([[1, 0], [0, 1], [2, 3]])

    print("$project_name: NumPy 数值计算")
    print(f"数组 a:\n{a}")
    print(f"数组 b:\n{b}")
    print(f"矩阵乘法 a @ b:\n{a @ b}")
    print(f"a 的均值: {a.mean():.2f}")
    print(f"a 的标准差: {a.std():.2f}")
    print(f"a 的转置:\n{a.T}")


if __name__ == "__main__":
    main()
