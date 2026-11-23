"""$project_name 入口：PySide2 主窗口示例."""

import sys

from PySide2.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget


class MainWindow(QMainWindow):
    """主窗口：点击按钮计数."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("$project_name")
        self.resize(400, 200)

        self._count = 0
        central = QWidget(self)
        layout = QVBoxLayout(central)

        self.label = QLabel("点击按钮 0 次", central)
        self.button = QPushButton("点我", central)
        self.button.clicked.connect(self._on_click)

        layout.addWidget(self.label)
        layout.addWidget(self.button)
        self.setCentralWidget(central)

    def _on_click(self) -> None:
        self._count += 1
        self.label.setText(f"点击按钮 {self._count} 次")


def main() -> None:
    """启动 PySide2 应用."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
