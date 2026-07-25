import sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from PySide2.QtGui import QGuiApplication
from PySide2.QtQml import QQmlApplicationEngine, qmlRegisterType
from PySide2.QtQuickControls2 import QQuickStyle
from PySide2.QtCore import QObject, Signal, Slot, Property

# ========== 主题控制器（供 QML 双向绑定） ==========
class ThemeController(QObject):
    themeChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dark = False

    @Property(bool, notify=themeChanged)
    def isDark(self):
        return self._dark

    @Slot(bool)
    def setDark(self, value):
        if self._dark != value:
            self._dark = value
            self.themeChanged.emit()

if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    QQuickStyle.setStyle("Fusion")

    theme = ThemeController()

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("Theme", theme)
    engine.load("Main.qml")

    if not engine.rootObjects():
        sys.exit(-1)
    sys.exit(app.exec_())