"""numpy 科学计算示例：数组运算与统计。

验证 numpy 在 embed python 下打包可用，覆盖数组创建、广播运算、
线性代数与统计聚合等核心 API。打包后无 GUI 依赖，stdout 可见。
"""

from __future__ import annotations


def main() -> None:
    """运行 numpy 数组运算并打印结果摘要."""
    import numpy as np

    # 创建数组与广播运算
    a = np.arange(12).reshape(3, 4)
    b = np.array([1, 2, 3, 4])
    product = a * b  # 广播

    # 线性代数：矩阵乘法
    mat = a @ a.T  # (3,4) @ (4,3) -> (3,3)

    # 统计聚合
    mean = float(product.mean())
    total = int(product.sum())
    det = float(np.linalg.det(mat))

    print(f"numpy {np.__version__}")
    print(f"numpy demo ok: mean={mean:.2f} sum={total} det={det:.2f}")


if __name__ == "__main__":
    main()
