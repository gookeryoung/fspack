---
name: "python-gui-pyside"
description: "PySide2/PySide6 桌面 GUI 开发技能：提供设计令牌、四区布局、信号槽、QThread、QSS 样式等可复用代码模板与最佳实践。当需要创建或修改 Qt 桌面应用界面、编写 PySide 代码、生成 GUI 项目或搭建主窗口/侧边栏/对话框时调用。"
---

# PySide2/PySide6 桌面 GUI 开发技能

自包含的 Qt 桌面应用开发指南：设计系统 + 代码模板 + 参考示例。模板按 Python 版本自动区分绑定 `PySide2`（≤3.10）/ `PySide6`（≥3.11），代码须双兼容。

## 何时调用

- 用户要求创建/修改 PySide（Qt）桌面 GUI 应用
- 生成的项目 `project_type=gui`，需搭建主窗口、侧边栏、对话框、表单等界面
- 需要 QThread 后台任务、QSS 样式、信号槽跨线程通信等 Qt 专项模式
- 用户提到 PySide2 / PySide6 / Qt 桌面应用 / QWidget / 主窗口 / 侧边栏导航

## 双兼容（关键约束）

- 模板按 Python 版本自动区分绑定：`PySide2`（≤3.10）/ `PySide6`（≥3.11），代码须双兼容。
- 导入一律 `try: PySide2 except ImportError: PySide6`；事件循环 `app.exec if hasattr(app, "exec") else app.exec_`。
- 枚举跨版本用短名（如 `Qt.AlignCenter`，两代均支持）；仅 PySide6 用全路径。
- 依赖声明由模板自动生成，勿手改：

```toml
dependencies = [
    "PySide2>=5.15.2.1; python_version <= '3.10'",
    "PySide6>=6.5.0; python_version >= '3.11'",
]
```

## 项目骨架（GUI 项目文件布局）

```
src/fspack/
├── __init__.py
├── main.py            # 入口：QApplication + MainWindow + 事件循环
├── theme.py           # 设计令牌（色彩/排版/间距/尺寸）+ QSS_TOKENS
├── style.qss          # QSS 样式表（${TOKEN} 占位符）
├── windows/
│   └── main_window.py # 主窗口（四区结构）
├── widgets/
│   ├── sidebar.py     # 侧边栏导航
│   └── header.py      # 头部条
├── workers/
│   └── task_worker.py # QThread Worker 模式
├── dialogs/
│   └── settings_dialog.py
└── core/
    └── services.py    # 纯 Python 业务逻辑（不 import PySide，便于单测）
tests/
├── test_services.py   # 纯逻辑测试
└── test_gui.py        # pytest-qt 界面测试（@pytest.mark.gui）
```

要点：业务逻辑放 `core/` 纯 Python 模块，GUI 层只做信号槽连接与状态展示；重型部件（QWebEngineView 等）惰性导入以加快启动。

## 设计系统（Design Tokens）

所有 QSS 与代码须引用令牌常量，禁止散落硬编码颜色/尺寸。令牌集中定义在 `theme.py`，QSS 通过 `string.Template` 占位符引用。

### 色彩系统

| 令牌 | 色值 | 用途 |
|------|------|------|
| `COLOR_PRIMARY` | `#0887A0` | 主色：头部条、侧边栏背景、主操作按钮、选中态 |
| `COLOR_PRIMARY_DARK` | `#00829E` | 主色按下态、分割线 |
| `COLOR_ACCENT` | `#87C6BB` | 强调色：成功状态、辅助高亮 |
| `COLOR_TEXT_ON_PRIMARY` | `#FFFFFF` | 主色背景上的文字/图标 |
| `COLOR_TEXT_PRIMARY` | `#2C3E50` | 主文字（深色，白底内容区） |
| `COLOR_TEXT_SECONDARY` | `#518394` | 次级文字、说明、禁用态 |
| `COLOR_BG_APP` | `#FFFFFF` | 应用底色、内容区背景 |
| `COLOR_BG_MUTED` | `#E5EDE0` | 浅底：卡片间隙、分组背景 |
| `COLOR_BORDER` | `#D1DDE2` | 边框、分割线 |
| `COLOR_DANGER` | `#E74C3C` | 错误/危险操作 |
| `COLOR_WARNING` | `#F39C12` | 警告 |
| `COLOR_SUCCESS` | `#27AE60` | 成功 |

### 排版

| 令牌 | 字号 | 字重 | 用途 |
|------|------|------|------|
| `FONT_TITLE` | 18px | Bold | 窗口标题、页面标题 |
| `FONT_HEADING` | 15px | Bold | 区块标题、分组标题 |
| `FONT_BODY` | 13px | Regular | 正文、表单标签 |
| `FONT_CAPTION` | 11px | Regular | 说明文字、状态栏、表头 |

字体族：`"PingFang SC", "Microsoft YaHei", "Segoe UI", "Helvetica Neue", Arial, sans-serif`（macOS/Windows/Linux 顺序回退）。

### 间距尺度

8px 基准网格，所有间距须为 8 的倍数：

| 令牌 | 值 | 用途 |
|------|-----|------|
| `SPACING_XS` | 4px | 图标与文字间隙 |
| `SPACING_SM` | 8px | 控件内边距、紧凑间隙 |
| `SPACING_MD` | 16px | 控件间间隙、表单字段间距 |
| `SPACING_LG` | 24px | 区块内边距 |
| `SPACING_XL` | 32px | 区块间间隙 |

### 圆角与尺寸

| 令牌 | 值 | 用途 |
|------|-----|------|
| `RADIUS_SM` | 4px | 按钮、输入框 |
| `RADIUS_MD` | 6px | 卡片、面板 |
| `CONTROL_HEIGHT` | 32px | 按钮/输入框标准高度 |
| `CONTROL_HEIGHT_SM` | 26px | 紧凑控件 |
| `SIDEBAR_WIDTH` | 220px | 侧边栏宽度 |
| `HEADER_HEIGHT` | 40px | 头部条高度 |
| `TOOLBAR_HEIGHT` | 44px | 工具栏高度 |
| `STATUSBAR_HEIGHT` | 28px | 状态栏高度 |

令牌的使用场景、配色比例、组件设计、交互模式与状态规范见 `UI-DESIGN.md`；四区结构（Header/Sidebar/Content/Status）、阶段联动与 Splitter 规则见 `LAYOUT.md`；表单验证、数据模型、会话状态、快捷键、进度对话框、消息框、拖放、动画、错误处理、上下文菜单等实现模式见 `PATTERNS.md`。

## 最佳实践

1. **双兼容优先**：导入一律 `try: PySide2 except ImportError: PySide6`；事件循环用 `app.exec if hasattr(app, "exec") else app.exec_`；枚举跨版本用短名 `Qt.AlignCenter`（两代均支持）。
2. **令牌驱动样式**：颜色/尺寸/间距只来自 `theme.py`，QSS 用 `${TOKEN}` 占位符，禁止内联硬编码。
3. **四区布局**：头部 + 侧边栏 + 内容区 + 状态栏，用 Layout 嵌套 + `QSplitter`，禁用 `setGeometry`。
4. **逻辑与界面分离**：业务逻辑放 `core/` 纯 Python 模块（可单测），GUI 层只连信号槽与展示状态。
5. **主线程不阻塞**：长任务用 QThread Worker 模式；非主线程禁止操作 GUI 部件，只通过信号回主线程。
6. **新式信号槽**：信号定义为类属性 `Signal(int)`，连接用方法引用，禁用 `SIGNAL/SLOT` 字符串语法。
7. **入口可测**：`main()` 加 `# pragma: no cover`，拆出 `create_main_window()` 等可测函数。
8. **响应式**：宽 < 1000px 时侧边栏折叠为图标条（56px）；内容区卡片用 `QGridLayout` 在 `resizeEvent` 重算列数。

## 代码模板

以下模板组成一个完整的参考应用，按需取用。

### 1. theme.py — 设计令牌

```python
"""设计令牌：色彩、排版、间距、尺寸。QSS 与代码统一引用此处常量。"""

from __future__ import annotations

__all__ = [
    "COLOR_PRIMARY", "COLOR_PRIMARY_DARK", "COLOR_ACCENT",
    "COLOR_TEXT_ON_PRIMARY", "COLOR_TEXT_PRIMARY", "COLOR_TEXT_SECONDARY",
    "COLOR_BG_APP", "COLOR_BG_MUTED", "COLOR_BORDER",
    "COLOR_DANGER", "COLOR_WARNING", "COLOR_SUCCESS",
    "FONT_TITLE", "FONT_HEADING", "FONT_BODY", "FONT_CAPTION", "FONT_FAMILY",
    "SPACING_XS", "SPACING_SM", "SPACING_MD", "SPACING_LG", "SPACING_XL",
    "RADIUS_SM", "RADIUS_MD",
    "CONTROL_HEIGHT", "CONTROL_HEIGHT_SM",
    "SIDEBAR_WIDTH", "HEADER_HEIGHT", "TOOLBAR_HEIGHT", "STATUSBAR_HEIGHT",
    "QSS_TOKENS",
]

# 色彩
COLOR_PRIMARY = "#0887A0"
COLOR_PRIMARY_DARK = "#00829E"
COLOR_ACCENT = "#87C6BB"
COLOR_TEXT_ON_PRIMARY = "#FFFFFF"
COLOR_TEXT_PRIMARY = "#2C3E50"
COLOR_TEXT_SECONDARY = "#518394"
COLOR_BG_APP = "#FFFFFF"
COLOR_BG_MUTED = "#E5EDE0"
COLOR_BORDER = "#D1DDE2"
COLOR_DANGER = "#E74C3C"
COLOR_WARNING = "#F39C12"
COLOR_SUCCESS = "#27AE60"

# 排版（px 数值，QSS 用字符串带 px 后缀）
FONT_FAMILY = '"PingFang SC", "Microsoft YaHei", "Segoe UI", "Helvetica Neue", Arial, sans-serif'
FONT_TITLE = "18px"
FONT_HEADING = "15px"
FONT_BODY = "13px"
FONT_CAPTION = "11px"

# 间距
SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 16
SPACING_LG = 24
SPACING_XL = 32

# 圆角与尺寸
RADIUS_SM = "4px"
RADIUS_MD = "6px"
CONTROL_HEIGHT = "32px"
CONTROL_HEIGHT_SM = "26px"
SIDEBAR_WIDTH = 220
HEADER_HEIGHT = 40
TOOLBAR_HEIGHT = 44
STATUSBAR_HEIGHT = 28

# QSS 占位符映射（string.Template.substitute 入参）
QSS_TOKENS = {
    "COLOR_PRIMARY": COLOR_PRIMARY,
    "COLOR_PRIMARY_DARK": COLOR_PRIMARY_DARK,
    "COLOR_ACCENT": COLOR_ACCENT,
    "COLOR_TEXT_ON_PRIMARY": COLOR_TEXT_ON_PRIMARY,
    "COLOR_TEXT_PRIMARY": COLOR_TEXT_PRIMARY,
    "COLOR_TEXT_SECONDARY": COLOR_TEXT_SECONDARY,
    "COLOR_BG_APP": COLOR_BG_APP,
    "COLOR_BG_MUTED": COLOR_BG_MUTED,
    "COLOR_BORDER": COLOR_BORDER,
    "COLOR_DANGER": COLOR_DANGER,
    "COLOR_WARNING": COLOR_WARNING,
    "COLOR_SUCCESS": COLOR_SUCCESS,
    "FONT_FAMILY": FONT_FAMILY,
    "FONT_TITLE": FONT_TITLE,
    "FONT_HEADING": FONT_HEADING,
    "FONT_BODY": FONT_BODY,
    "FONT_CAPTION": FONT_CAPTION,
    "RADIUS_SM": RADIUS_SM,
    "RADIUS_MD": RADIUS_MD,
    "CONTROL_HEIGHT": CONTROL_HEIGHT,
    "CONTROL_HEIGHT_SM": CONTROL_HEIGHT_SM,
}
```

### 2. style.qss — 参考样式表

```css
/* 全局基础 */
QWidget {
    color: ${COLOR_TEXT_PRIMARY};
    font-family: ${FONT_FAMILY};
    font-size: ${FONT_BODY};
}
QMainWindow, QDialog { background-color: ${COLOR_BG_APP}; }

/* 头部条 */
QFrame#headerBar {
    background-color: ${COLOR_PRIMARY};
    min-height: 40px;
    max-height: 40px;
}
QLabel#headerTitle {
    color: ${COLOR_TEXT_ON_PRIMARY};
    font-size: ${FONT_TITLE};
    font-weight: bold;
}
QPushButton#headerBtn {
    background: transparent;
    color: ${COLOR_TEXT_ON_PRIMARY};
    border: none;
    padding: 4px 12px;
    border-radius: ${RADIUS_SM};
}
QPushButton#headerBtn:hover { background-color: ${COLOR_PRIMARY_DARK}; }

/* 侧边栏 */
QListWidget#sidebar {
    background-color: ${COLOR_PRIMARY};
    color: ${COLOR_TEXT_ON_PRIMARY};
    border: none;
    font-size: ${FONT_BODY};
    outline: none;
    padding: 8px 0;
}
QListWidget#sidebar::item {
    height: 40px;
    padding-left: 16px;
}
QListWidget#sidebar::item:selected {
    background-color: ${COLOR_PRIMARY_DARK};
    border-left: 3px solid ${COLOR_ACCENT};
}
QListWidget#sidebar::item:hover {
    background-color: ${COLOR_PRIMARY_DARK};
}

/* 内容区卡片 */
QFrame#card {
    background-color: ${COLOR_BG_APP};
    border: 1px solid ${COLOR_BORDER};
    border-radius: ${RADIUS_MD};
}

/* 按钮 */
QPushButton {
    background-color: ${COLOR_PRIMARY};
    color: ${COLOR_TEXT_ON_PRIMARY};
    border: none;
    border-radius: ${RADIUS_SM};
    padding: 6px 16px;
    min-height: ${CONTROL_HEIGHT};
}
QPushButton:hover { background-color: ${COLOR_PRIMARY_DARK}; }
QPushButton:pressed { background-color: ${COLOR_PRIMARY_DARK}; }
QPushButton:disabled { background-color: ${COLOR_BORDER}; color: ${COLOR_TEXT_SECONDARY}; }
QPushButton#dangerBtn { background-color: ${COLOR_DANGER}; }
QPushButton#dangerBtn:hover { background-color: #C0392B; }

/* 输入框 */
QLineEdit, QComboBox, QSpinBox, QTextEdit {
    border: 1px solid ${COLOR_BORDER};
    border-radius: ${RADIUS_SM};
    padding: 4px 8px;
    min-height: ${CONTROL_HEIGHT};
    background-color: ${COLOR_BG_APP};
}
QLineEdit:focus { border: 1px solid ${COLOR_PRIMARY}; }

/* 状态栏 */
QStatusBar {
    background-color: ${COLOR_BG_MUTED};
    color: ${COLOR_TEXT_SECONDARY};
    font-size: ${FONT_CAPTION};
    min-height: 28px;
    max-height: 28px;
}

/* 选项卡 */
QTabWidget::pane { border: 1px solid ${COLOR_BORDER}; border-radius: ${RADIUS_MD}; }
QTabBar::tab {
    padding: 6px 16px;
    min-height: 36px;
    font-size: ${FONT_BODY};
    border: none;
}
QTabBar::tab:selected { border-bottom: 2px solid ${COLOR_PRIMARY}; }

/* 滚动条 */
QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }
QScrollBar::handle:vertical {
    background: ${COLOR_BORDER};
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: ${COLOR_TEXT_SECONDARY}; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
```

### 3. main.py — 入口与四区主窗口

```python
"""fspack GUI 入口（PySide2/PySide6 双兼容）。"""

from __future__ import annotations

import sys
from pathlib import Path
from string import Template

try:
    from PySide2.QtWidgets import QApplication
except ImportError:
    from PySide6.QtWidgets import QApplication

from fspack import theme
from fspack.windows.main_window import MainWindow

__all__ = ["create_main_window", "load_stylesheet", "main"]


def load_stylesheet() -> str:
    """加载 QSS 并替换设计令牌占位符。"""
    qss = Path(__file__).parent / "style.qss"
    return Template(qss.read_text("utf-8")).substitute(theme.QSS_TOKENS)


def create_main_window() -> MainWindow:
    """构建主窗口（可测函数，拆离事件循环）。"""
    window = MainWindow()
    return window


def main() -> int:  # pragma: no cover
    """启动 GUI 应用（事件循环阻塞，需图形环境手动测试）。"""
    app = QApplication(sys.argv)
    app.setStyleSheet(load_stylesheet())
    window = create_main_window()
    window.show()
    # PySide2 用 exec_()，PySide6 推荐 exec()（exec_ 已弃用）
    run = app.exec if hasattr(app, "exec") else app.exec_
    return run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

### 4. windows/main_window.py — 四区主窗口

```python
"""主窗口：头部 + 侧边栏 + 内容区（QStackedWidget）+ 状态栏。"""

from __future__ import annotations

try:
    from PySide2.QtCore import Qt
    from PySide2.QtWidgets import (
        QFrame, QHBoxLayout, QLabel, QListWidget, QMainWindow,
        QSplitter, QStackedWidget, QVBoxLayout, QWidget,
    )
except ImportError:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QFrame, QHBoxLayout, QLabel, QListWidget, QMainWindow,
        QSplitter, QStackedWidget, QVBoxLayout, QWidget,
    )

from fspack import theme
from fspack.widgets.sidebar import Sidebar

__all__ = ["MainWindow"]

_NAV_ITEMS = ["仪表盘", "数据管理", "设置"]


class MainWindow(QMainWindow):
    """四区结构主窗口。"""

    def __init__(self) -> None:
        """初始化主窗口并组装四区布局。"""
        super().__init__()
        self.setWindowTitle("fspack")
        self.setMinimumSize(800, 600)
        self.resize(1280, 800)
        self._build_ui()

    def _build_ui(self) -> None:
        """组装头部 + (侧边栏 | 内容区) + 状态栏。"""
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 头部条
        root.addWidget(self._build_header())

        # 中区：侧边栏 + 内容区，QSplitter 可拖拽
        splitter = QSplitter(Qt.Horizontal if hasattr(Qt, "Horizontal") else Qt.Orientation.Horizontal)
        self.sidebar = Sidebar(items=_NAV_ITEMS)
        self.stack = QStackedWidget()
        for label in _NAV_ITEMS:
            page = self._build_placeholder_page(label)
            self.stack.addWidget(page)
        self.sidebar.current_row_changed.connect(self.stack.setCurrentIndex)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([theme.SIDEBAR_WIDTH, 1060])
        root.addWidget(splitter, stretch=1)

        # 状态栏
        status = self.statusBar()
        status.showMessage("就绪")

        self.setCentralWidget(central)

    def _build_header(self) -> QFrame:
        """构建头部条：左侧标题，右侧占位。"""
        bar = QFrame(objectName="headerBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(theme.SPACING_MD, 0, theme.SPACING_MD, 0)
        title = QLabel("fspack", objectName="headerTitle")
        layout.addWidget(title)
        layout.addStretch()
        return bar

    def _build_placeholder_page(self, label: str) -> QWidget:
        """构建内容占位页（实际项目替换为业务页面）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG)
        layout.addWidget(QLabel(f"{label} 页面", style="font-size: 18px; font-weight: bold;"))
        layout.addStretch()
        return page

    def resizeEvent(self, event):  # noqa: N802
        """窗口宽度 < 1000px 时折叠侧边栏为图标条。"""
        super().resizeEvent(event)
        if self.width() < 1000:
            self.sidebar.set_folded(True)
        else:
            self.sidebar.set_folded(False)
```

### 5. widgets/header.py — 头部条

```python
"""头部条：QFrame + QButtonGroup 互斥 Tab + 右侧辅助按钮。"""

from __future__ import annotations

from typing import List

try:
    from PySide2.QtCore import Signal
    from PySide2.QtWidgets import (
        QButtonGroup, QFrame, QHBoxLayout, QPushButton, QSpacerItem,
        QSizePolicy, QWidget,
    )
except ImportError:
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import (
        QButtonGroup, QFrame, QHBoxLayout, QPushButton, QSpacerItem,
        QSizePolicy, QWidget,
    )

from fspack import theme

__all__ = ["HeaderBar"]


class HeaderBar(QFrame):
    """头部条：左侧 Tab 按钮组（互斥）+ 右侧辅助按钮。

    tab_changed 信号在用户点击 Tab 时发出，携带 tab 索引。
    """

    tab_changed = Signal(int)

    def __init__(
        self,
        tabs: List[str],
        actions: List[tuple[str, object]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """初始化头部条。

        Args:
            tabs: Tab 按钮文本列表
            actions: 右侧辅助按钮 [(文本, 回调), ...]
            parent: 父部件
        """
        super().__init__(objectName="headerBar", parent=parent)
        self.setFixedHeight(theme.HEADER_HEIGHT)
        self._build_ui(tabs, actions or [])

    def _build_ui(self, tabs: List[str], actions: list[tuple[str, object]]) -> None:
        """组装头部条布局。"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACING_SM, theme.SPACING_XS, theme.SPACING_SM, theme.SPACING_XS)
        layout.setSpacing(theme.SPACING_XS)

        # 左侧 Tab 按钮组（互斥）
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        for i, text in enumerate(tabs):
            btn = QPushButton(text, objectName="headerBtn")
            btn.setCheckable(True)
            self._tab_group.addButton(btn, id=i)
            layout.addWidget(btn)
        if self._tab_group.buttons():
            self._tab_group.buttons()[0].setChecked(True)
        self._tab_group.idClicked.connect(self.tab_changed.emit)

        # 弹簧填充
        layout.addItem(QSpacerItem(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum))

        # 右侧辅助按钮
        for text, callback in actions:
            btn = QPushButton(text, objectName="headerBtn")
            if callback:
                btn.clicked.connect(callback)
            layout.addWidget(btn)

    def set_current_tab(self, index: int) -> None:
        """程序化切换 Tab（blockSignals 避免循环触发）。"""
        btn = self._tab_group.button(index)
        if btn:
            self._tab_group.blockSignals(True)
            btn.setChecked(True)
            self._tab_group.blockSignals(False)
```

### 6. widgets/sidebar.py — 侧边栏导航

```python
"""侧边栏导航：QListWidget，选中态高亮 + 左侧强调竖条。"""

from __future__ import annotations

from typing import List

try:
    from PySide2.QtCore import Signal
    from PySide2.QtWidgets import QListWidget, QListWidgetItem
except ImportError:
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QListWidget, QListWidgetItem

from fspack import theme

__all__ = ["Sidebar"]

_ICON_BAR_WIDTH = 56


class Sidebar(QListWidget):
    """侧边栏一级导航。currentRowChanged → stack.setCurrentIndex。"""

    current_row_changed = Signal(int)

    def __init__(self, items: List[str]) -> None:
        """初始化侧边栏并填充导航项。"""
        super().__init__(objectName="sidebar")
        self._folded = False
        for text in items:
            QListWidgetItem(text, self)
        self.setCurrentRow(0)
        self.currentRowChanged.connect(self.current_row_changed.emit)
        self.setFixedWidth(theme.SIDEBAR_WIDTH)

    def set_folded(self, folded: bool) -> None:
        """折叠/展开侧边栏；折叠时收窄为图标条宽度。"""
        self._folded = folded
        self.setFixedWidth(_ICON_BAR_WIDTH if folded else theme.SIDEBAR_WIDTH)
```

### 7. workers/task_worker.py — QThread Worker 模式

```python
"""QThread Worker 模式：QObject 子类 + moveToThread，信号跨线程回主线程。"""

from __future__ import annotations

import time

try:
    from PySide2.QtCore import QObject, QThread, Signal, Slot
except ImportError:
    from PySide6.QtCore import QObject, QThread, Signal, Slot

__all__ = ["TaskWorker", "WorkerController"]


class TaskWorker(QObject):
    """后台任务执行器。禁止在此操作 GUI 部件，只发信号。"""

    progress = Signal(int)       # 进度 0-100
    finished_ok = Signal(str)    # 成功结果
    failed = Signal(str)         # 错误信息

    def __init__(self, payload: str) -> None:
        """初始化并保存任务入参。"""
        super().__init__()
        self._payload = payload

    @Slot()
    def run(self) -> None:
        """执行长任务，通过信号回报进度与结果。"""
        try:
            for step in range(1, 101):
                if QThread.currentThread().isInterruptionRequested():
                    return
                time.sleep(0.02)  # 模拟耗时
                self.progress.emit(step)
            self.finished_ok.emit(f"完成: {self._payload}")
        except (OSError, ValueError) as exc:
            self.failed.emit(str(exc))


class WorkerController(QObject):
    """管理 Worker 生命周期：建线程 → moveToThread → 启动 → 清理。"""

    def __init__(self, payload: str) -> None:
        """初始化控制器并装配 worker 与 thread。"""
        super().__init__()
        self._thread = QThread()
        self._worker = TaskWorker(payload)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_finished)
        # finished → deleteLater，避免线程对象泄漏
        self._worker.finished_ok.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)

    def start(self) -> None:
        """启动后台线程。"""
        self._thread.start()

    def stop(self) -> None:
        """请求中断并等待线程退出。"""
        self._thread.requestInterruption()
        self._thread.quit()
        self._thread.wait(3000)

    def _on_finished(self, _msg: str) -> None:
        """任务结束回调（子类可重写或外接信号）。"""
```

### 8. dialogs/settings_dialog.py — 对话框模式

```python
"""设置对话框：QDialog + QFormLayout，模态执行。"""

from __future__ import annotations

try:
    from PySide2.QtWidgets import (
        QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
        QSpinBox, QVBoxLayout, QWidget,
    )
except ImportError:
    from PySide6.QtWidgets import (
        QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
        QSpinBox, QVBoxLayout, QWidget,
    )

from fspack import theme

__all__ = ["SettingsDialog"]


class SettingsDialog(QDialog):
    """设置对话框，返回用户填写的配置。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化对话框并构建表单。"""
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(400)
        self._build_ui()

    def _build_ui(self) -> None:
        """构建表单字段 + 确认/取消按钮。"""
        root = QVBoxLayout(self)
        root.setContentsMargins(
            theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG, theme.SPACING_LG
        )

        form = QFormLayout()
        form.setSpacing(theme.SPACING_MD)
        self.name_edit = QLineEdit(placeholderText="名称")
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 3600)
        self.timeout_spin.setValue(30)
        form.addRow("名称", self.name_edit)
        form.addRow("超时(秒)", self.timeout_spin)
        root.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
            if hasattr(QDialogButtonBox, "Ok")
            else QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def values(self) -> dict:
        """返回表单填写值。"""
        return {"name": self.name_edit.text(), "timeout": self.timeout_spin.value()}
```

## 兼容性速查

### 双兼容导入模式

```python
try:
    from PySide2.QtWidgets import QApplication, QWidget
    from PySide2.QtCore import Qt, Signal, Slot, QObject, QThread, QTimer
except ImportError:
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtCore import Qt, Signal, Slot, QObject, QThread, QTimer
```

事件循环兼容写法：

```python
run = app.exec if hasattr(app, "exec") else app.exec_
return run()
```

### API 差异速查

| 场景 | PySide2 (Qt5) | PySide6 (Qt6) |
|------|---------------|---------------|
| 事件循环 | `app.exec_()` | `app.exec()`（`exec_` 已弃用） |
| 对齐枚举 | `Qt.AlignCenter` | `Qt.AlignmentFlag.AlignCenter` |
| 方向枚举 | `Qt.Horizontal` | `Qt.Orientation.Horizontal` |
| 键盘修饰 | `Qt.ControlModifier` | `Qt.KeyboardModifier.ControlModifier` |
| 窗口标志 | `Qt.Window` | `Qt.WindowType.Window` |
| 鼠标按钮 | `Qt.LeftButton` | `Qt.MouseButton.LeftButton` |
| itemDataRole | `Qt.DisplayRole` | `Qt.ItemDataRole.DisplayRole` |
| 拖放动作 | `Qt.CopyAction` | `Qt.DropAction.CopyAction` |

枚举策略：跨版本运行用短名 `Qt.AlignCenter`（两代均支持，PySide6 仅发 DeprecationWarning）；枚举值判断用 `hasattr` 守卫，例如 `Qt.Horizontal if hasattr(Qt, "Horizontal") else Qt.Orientation.Horizontal`。

### 版本与依赖

- PySide2 仅支持 Python 3.6-3.10，PyPI 最新 wheel `5.15.2.1`，无 3.11+ wheel。
- PySide6 支持 Python 3.8+，3.11+ 环境必选。
- 依赖声明（模板自动生成）：

```toml
dependencies = [
    "PySide2>=5.15.2.1; python_version <= '3.10'",
    "PySide6>=6.5.0; python_version >= '3.11'",
]
```

pip/uv 按 Python 版本只安装其一。

## 信号槽要点

- 新式信号槽，禁用 `SIGNAL/SLOT` 字符串语法。
- 信号定义为类属性：`value_changed = Signal(int)`。
- 连接用方法引用：`button.clicked.connect(self._on_clicked)`；避免重复连接（多次 connect 多次触发）。
- disconnect 用 `try/except RuntimeError`（连接已断开会抛错）。
- 跨线程 worker → UI 用 `Qt.QueuedConnection`（信号第五参数或默认跨线程自动队列）。

## 事件循环与定时器

- `QApplication` 全局单例，传入 `sys.argv`（解析 `-style` 等命令行参数）。
- 长任务禁止主线程执行（冻结 UI），用 QThread 或 QThreadPool + QRunnable。
- 定时器用 `QTimer.singleShot(ms, callback)` 或 `QTimer(timeout=cb).start(ms)`，不用 `time.sleep`。
- 事件过滤器 `installEventFilter` + 重写 `eventFilter()`，优先于子类化。

## 资源系统

- `.qrc` 管理图标/样式表，编译为 `_rc.py`（`pyside2-rcc`/`pyside6-rcc`），引用用 `:/` 前缀：`QIcon(":/icons/app.png")`。
- `.ui` 用 `pyside2-uic`/`pyside6-uic` 编译为 `.py`（比运行时 `QUiLoader` 快，推荐）。
- 资源变更后须重新编译 `_rc.py` 并纳入版本控制（构建环境可能缺 rcc 工具链）。
- 大文件（视频/字体）用磁盘路径加载，不进 `.qrc`。

## 测试（pytest-qt）

- `qapp` fixture 提供单例 `QApplication`（自动管理生命周期，兼容 PySide2/PySide6）。
- `qtbot` 模拟交互：`mouseClick`/`keyClicks`/`addWidget`。
- 等待信号 `qtbot.waitSignal(widget.signal, timeout=1000)`，断言 `blocker.args`。
- GUI 测试加 `@pytest.mark.gui`（或 `slow`），CI 默认 `-m "not slow"` 跳过。
- 无头环境设 `QT_QPA_PLATFORM=offscreen`（CI 用 xvfb 或 offscreen 平台插件）。

GUI 测试示例：

```python
import pytest

from fspack.widgets.sidebar import Sidebar


@pytest.mark.gui
def test_sidebar_switch_page(qtbot):  # qtbot 由 pytest-qt 提供
    """侧边栏切换行时发出 current_row_changed 信号。"""
    sidebar = Sidebar(items=["A", "B", "C"])
    qtbot.addWidget(sidebar)

    with qtbot.waitSignal(sidebar.current_row_changed, timeout=1000) as blocker:
        sidebar.setCurrentRow(2)

    assert blocker.args == [2]
    assert sidebar.currentRow() == 2
```

## 常见陷阱

1. **主线程阻塞**：在槽函数里做耗时 I/O 会冻结 UI。改用 QThread Worker，通过信号回主线程更新界面。
2. **非主线程操作 GUI**：worker 里直接 `label.setText()` 会导致崩溃或未定义行为。只发信号，由主线程槽更新。
3. **重复 connect**：多次 `connect` 同一信号槽会多次触发。初始化时连接一次，或 disconnect 前检查。
4. **枚举全路径在 PySide2 失效**：`Qt.AlignmentFlag.AlignCenter` 在 PySide2 不存在。跨版本用短名或 `hasattr` 守卫。
5. **硬编码颜色**：QSS 内联 `color: #0887A0` 散落难维护。统一走 `theme.py` + `${TOKEN}` 占位符。
6. **绝对定位 `setGeometry`**：窗口缩放后布局错乱。一律用 Layout，弹窗固定尺寸除外。
7. **`exec_` 弃用**：PySide6 中 `exec_` 发警告。用 `app.exec if hasattr(app, "exec") else app.exec_` 兼容。
8. **线程对象泄漏**：QThread 退出后未 `deleteLater` 会残留。`finished` 信号连 `deleteLater`。
9. **裸 `except` 吞异常**：worker 的 `run` 里只捕获预期异常（如 `(OSError, ValueError)`），其余抛出由上层处理。
10. **macOS 风格冲突**：QSS 在 macOS 默认风格下可能失效。`app.setStyle("Fusion")` 统一。
