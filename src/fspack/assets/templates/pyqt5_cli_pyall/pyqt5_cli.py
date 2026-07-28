from PyQt5.QtWidgets import QApplication, QLabel

def main() -> None:
    """创建 QApplication 验证 PyQt5 可用."""
    app = QApplication([])
    label = QLabel("hello from PyQt5")
    print(label.text())
    app.quit()


if __name__ == "__main__":
    main()
