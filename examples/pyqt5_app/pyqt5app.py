"""有库 GUI 示例：PyQt5 创建 QApplication 不显示窗口。

验证 Python 3.12 + PyQt5 组合下 embed python 打包可用。
"""

import contextlib
import os
from pathlib import Path


def main() -> None:
    """创建 QApplication 验证 PyQt5 可用。."""
    import PyQt5

    qt_dir = str(Path(PyQt5.__file__).parent)
    with contextlib.suppress(OSError):
        os.add_dll_directory(qt_dir)
    from PyQt5.QtWidgets import QApplication, QLabel

    app = QApplication([])
    label = QLabel("hello from PyQt5")
    print(label.text())
    app.quit()


if __name__ == "__main__":
    main()
