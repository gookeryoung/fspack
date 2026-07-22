"""有库 GUI 示例（基本依赖）：PyQt5 创建 QApplication 不显示窗口。

验证 Python 3.12 + PyQt5 组合下 embed python 打包可用。

- 仅依赖 PyQt5 一个库，演示「基本依赖」场景
- 移除显式 ``import PyQt5.QtCore``/``import PyQt5.QtGui`` 的 C 层依赖声明，
  依赖 fspack iter-19 引入的 Qt 模块依赖闭包机制自动保留对应 ``.pyd`` 与
  ``Qt5*.dll``——用户代码只需 ``import`` 实际使用的子模块即可
"""

import contextlib
import os
from pathlib import Path


def main() -> None:
    """创建 QApplication 验证 PyQt5 可用。."""
    import PyQt5

    # Windows 默认不搜索 .pyd 所在目录的依赖 DLL，需注册 Qt5*.dll 所在目录
    qt_dir = str(Path(PyQt5.__file__).parent)
    with contextlib.suppress(OSError):
        os.add_dll_directory(qt_dir)

    # 仅 import 实际使用的子模块，QtWidgets 的 C 层依赖（QtGui/QtCore）由
    # fspack 闭包机制自动保留对应 .pyd 与 Qt5*.dll，无需显式声明
    from PyQt5.QtWidgets import QApplication, QLabel

    app = QApplication([])
    label = QLabel("hello from PyQt5")
    print(label.text())
    app.quit()


if __name__ == "__main__":
    main()
