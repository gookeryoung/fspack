"""有库 GUI 示例：PySide2 创建 QApplication 不显示窗口。

验证 Python 3.8 + PySide2 组合下 embed python 打包可用。
"""

import contextlib
import os
from pathlib import Path


def main() -> None:
    """创建 QApplication 验证 PySide2 可用。."""
    import PySide2

    pyside_dir = str(Path(PySide2.__file__).parent)
    with contextlib.suppress(OSError):
        os.add_dll_directory(pyside_dir)
    from PySide2.QtWidgets import QApplication, QLabel

    app = QApplication([])
    label = QLabel("hello from PySide2")
    print(label.text())
    app.quit()


if __name__ == "__main__":
    main()
