"""GUI 入口：PySide2 创建并显示一个带文字的窗口."""

import contextlib
import os
from pathlib import Path


def main() -> None:
    """创建 QApplication 并显示窗口，进入事件循环等待用户关闭."""
    import PySide2

    pyside_dir = str(Path(PySide2.__file__).parent)
    with contextlib.suppress(OSError):
        os.add_dll_directory(pyside_dir)
    import PySide2.QtGui  # QtWidgets 在 C 层依赖 QtGui,显式导入以保留 .pyd
    from PySide2.QtCore import QTimer
    from PySide2.QtWidgets import QApplication, QLabel

    app = QApplication([])
    label = QLabel("hello from multi_entry gui")
    label.setWindowTitle("Multi-entry GUI 示例")
    label.resize(300, 150)
    label.show()
    print(label.text())

    # offscreen 模式（CI/wine）无窗口系统，定时自动退出避免挂起
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        QTimer.singleShot(1000, app.quit)

    app.exec_()


if __name__ == "__main__":
    main()
