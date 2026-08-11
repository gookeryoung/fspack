"""$project_name 入口：PySide6 + QML 示例."""

import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine


def main() -> None:
    """加载 main.qml 并启动 PySide6 应用."""
    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    qml_path = Path(__file__).parent / "main.qml"
    engine.load(str(qml_path))
    if not engine.rootObjects():
        sys.exit(1)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
