"""有库 GUI 示例（带有其他依赖）：PySide2 GUI + QtMultimedia 非基本 Qt 模块。

验证 Python 3.8 + PySide2 组合下 embed python 打包可用，并验证 fspack slim
精简策略对非基本 Qt 子模块的保留正确性。

- 在 PySide2 基础上引入 QtMultimedia 子模块（非基本 Qt 模块），演示「带有其他
  依赖」场景
- 验证 slim 闭包机制：``import QtWidgets + QtMultimedia`` → 闭包自动加入
  ``Gui``/``Core``/``Network``，对应 ``.pyd`` 与 ``Qt5*.dll`` 均保留；
  未用模块（如 ``QtSql``/``QtWebEngine`` 等）的 ``.pyd``/``Qt5*.dll`` 被剥离
- 移除显式 ``import PySide2.QtGui`` 的 C 层依赖声明，依赖闭包机制自动保留
"""

import contextlib
import os
from pathlib import Path


def main() -> None:
    """创建 QApplication 显示窗口，并验证 QtMultimedia 可用。."""
    import PySide2

    # Windows 默认不搜索 .pyd 所在目录的依赖 DLL，需注册 Qt5*.dll 所在目录
    pyside_dir = str(Path(PySide2.__file__).parent)
    with contextlib.suppress(OSError):
        os.add_dll_directory(pyside_dir)

    from PySide2.QtCore import QTimer

    # 引入非基本 Qt 模块 QtMultimedia，验证 slim 闭包机制保留对应 .pyd 与 Qt5*.dll
    # 闭包：Multimedia → Gui/Core/Network，对应 .pyd 与 Qt5*.dll 均保留
    from PySide2.QtMultimedia import QMediaPlayer
    from PySide2.QtWidgets import QApplication, QLabel

    app = QApplication([])
    player = QMediaPlayer()
    # state: 0=Stopped, 1=Playing, 2=Paused
    state = int(player.state())
    label = QLabel(f"hello from PySide2 + QtMultimedia (player state: {state})")
    label.setWindowTitle("PySide2 + QtMultimedia 示例")
    label.resize(400, 150)
    label.show()
    print(label.text())

    # offscreen 模式（CI/wine）无窗口系统，定时自动退出避免挂起
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        QTimer.singleShot(1000, app.quit)

    app.exec_()


if __name__ == "__main__":
    main()
