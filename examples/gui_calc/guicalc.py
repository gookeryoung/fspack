"""有库 GUI 示例：PySide6 创建 QApplication 不显示窗口。

embed python 下 PySide6 的 Qt DLL 在 PySide6/ 子目录，Windows 默认不搜索 .pyd
所在目录的依赖 DLL，需用 os.add_dll_directory 注册。
"""

import contextlib
import os
from pathlib import Path


def main() -> None:
    """创建 QApplication 验证 GUI 库可用。."""
    import PySide6

    pyside_dir = str(Path(PySide6.__file__).parent)
    with contextlib.suppress(OSError):
        os.add_dll_directory(pyside_dir)
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication([])
    label = QLabel("hello from PySide6")
    print(label.text())
    app.quit()


if __name__ == "__main__":
    main()
