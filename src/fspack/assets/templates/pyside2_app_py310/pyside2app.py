from PySide2.QtWidgets import QApplication, QLabel

def main() -> None:
    """创建 QApplication 显示窗口."""
    app = QApplication([])
    label = QLabel(f"hello from PySide2")
    label.setWindowTitle("PySide2 app 示例")
    label.resize(400, 150)
    label.show()
    app.exec_()


if __name__ == "__main__":
    main()
