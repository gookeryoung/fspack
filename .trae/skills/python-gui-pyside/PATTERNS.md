# PySide GUI 实现模式

常用 GUI 实现模式速查。所有代码须双兼容 PySide2/PySide6，颜色/尺寸引用 `theme.py` 令牌。令牌定义见 `SKILL.md`，视觉规范见 `UI-DESIGN.md`，四区结构见 `LAYOUT.md`。

## 一、表单验证

### 场景

配置页、对话框等表单提交前须校验字段完整性、格式与范围。

### 实现

字段级校验 + 提交时聚合校验，错误信息显示在字段下方。

```python
"""表单验证模式：字段级实时校验 + 提交时聚合校验。"""

from __future__ import annotations

try:
    from PySide2.QtCore import Qt
    from PySide2.QtWidgets import (
        QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QSpinBox, QVBoxLayout, QWidget,
    )
except ImportError:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QSpinBox, QVBoxLayout, QWidget,
    )

from fspack import theme


class ValidatedField(QWidget):
    """带错误提示的输入字段。"""

    def __init__(self, widget: QLineEdit | QSpinBox, label_text: str) -> None:
        """初始化字段组件。"""
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACING_XS)
        self._widget = widget
        self._error_label = QLabel("")
        self._error_label.setStyleSheet(f"color: {theme.COLOR_DANGER}; font-size: {theme.FONT_CAPTION};")
        self._error_label.setWordWrap(True)
        self._error_label.hide()
        layout.addWidget(widget)
        layout.addWidget(self._error_label)
        widget.textChanged.connect(self._clear_error)

    def set_error(self, message: str) -> None:
        """显示错误信息。"""
        self._error_label.setText(message)
        self._error_label.show()
        self._widget.setStyleSheet(
            f"border: 2px solid {theme.COLOR_DANGER}; border-radius: {theme.RADIUS_SM};"
        )

    def _clear_error(self) -> None:
        """清除错误状态。"""
        self._error_label.hide()
        self._widget.setStyleSheet("")

    @property
    def value(self) -> str:
        """返回当前值。"""
        return self._widget.text()
```

### 使用规则

- **实时校验**：`textChanged` 连接轻量格式检查（如非空、正则），错误即时清除。
- **提交校验**：提交按钮 `clicked` 触发全量校验，任一字段有错则 `set_error` 并阻止提交。
- **错误文案**：说明原因与修正方向，如"名称不能为空""请输入 1-3600 之间的整数"。
- **视觉反馈**：错误字段边框 `COLOR_DANGER`（2px），下方 `FONT_CAPTION` 错误说明；修正后自动恢复。
- **提交守卫**：所有字段校验通过才执行业务逻辑，否则聚焦第一个错误字段。

## 二、数据模型

### 场景

表格/列表展示结构化数据，须支持排序、过滤、编辑。

### QAbstractTableModel 模板

```python
"""表格数据模型：QAbstractTableModel 子类，支持排序与只读展示。"""

from __future__ import annotations

from typing import Any

try:
    from PySide2.QtCore import QAbstractTableModel, QModelIndex, Qt
except ImportError:
    from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt


class TableModel(QAbstractTableModel):
    """通用表格模型，数据存储在内存列表中。"""

    def __init__(self, headers: list[str], rows: list[list[Any]] | None = None) -> None:
        """初始化模型。"""
        super().__init__()
        self._headers = headers
        self._rows = rows or []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        """返回行数。"""
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        """返回列数。"""
        return len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        """返回单元格数据。"""
        if not index.isValid():
            return None
        if role == Qt.DisplayRole or role == Qt.EditRole:
            return str(self._rows[index.row()][index.column()])
        if role == Qt.TextAlignmentRole:
            # 数值列右对齐，文本列左对齐
            col = index.column()
            if isinstance(self._rows[index.row()][col], (int, float)):
                return Qt.AlignRight | Qt.AlignVCenter
            return Qt.AlignLeft | Qt.AlignVCenter
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:  # noqa: B008
        """返回表头数据。"""
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._headers[section]
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:  # noqa: B008
        """按列排序。"""
        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=lambda row: row[column], reverse=(order == Qt.DescendingOrder))
        self.layoutChanged.emit()

    def replace_rows(self, rows: list[list[Any]]) -> None:
        """整体替换数据并通知视图刷新。"""
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()
```

### 使用规则

- **只读模型**：不重写 `flags`/`setData`，视图自动只读。
- **可编辑模型**：`flags` 返回 `Qt.ItemIsEditable`，`setData` 更新 `_rows` 并 `dataChanged.emit`。
- **批量更新**：用 `beginResetModel`/`endResetModel` 包裹整批替换，避免逐行 `beginInsertRows`。
- **排序**：重写 `sort`，排序后 `layoutChanged.emit`；视图 `setSortingEnabled(True)`。
- **代理列**：`data` 中根据 `role` 返回不同数据（如 `Qt.DecorationRole` 返回图标、`Qt.UserRole` 返回原始对象）。
- **行高**：视图 `verticalHeader().setDefaultSectionSize(theme.CONTROL_HEIGHT)` 统一。

## 三、会话状态

### 场景

记住窗口几何、Splitter 比例、最近文件等用户偏好，下次启动恢复。

### 实现

用 `QSettings` 或 JSON 文件持久化，启动时恢复、关闭时保存。

```python
"""会话状态管理：窗口几何与 Splitter 比例的保存与恢复。"""

from __future__ import annotations

import json
from pathlib import Path

try:
    from PySide2.QtCore import QByteArray
    from PySide2.QtWidgets import QMainWindow, QSplitter
except ImportError:
    from PySide6.QtCore import QByteArray
    from PySide6.QtWidgets import QMainWindow, QSplitter


class SessionState:
    """会话状态管理器，用 JSON 文件持久化。"""

    def __init__(self, config_path: Path) -> None:
        """初始化并加载状态。"""
        self._path = config_path
        self._data: dict = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text("utf-8"))

    def save_geometry(self, window: QMainWindow) -> None:
        """保存窗口几何。"""
        self._data["geometry"] = window.saveGeometry().data().hex()

    def restore_geometry(self, window: QMainWindow) -> None:
        """恢复窗口几何。"""
        geo_hex = self._data.get("geometry")
        if geo_hex:
            window.restoreGeometry(QByteArray.fromHex(bytes.fromhex(geo_hex)))

    def save_splitter(self, name: str, splitter: QSplitter) -> None:
        """保存 Splitter 比例。"""
        self._data[f"splitter_{name}"] = splitter.saveState().data().hex()

    def restore_splitter(self, name: str, splitter: QSplitter) -> None:
        """恢复 Splitter 比例。"""
        state_hex = self._data.get(f"splitter_{name}")
        if state_hex:
            splitter.restoreState(QByteArray.fromHex(bytes.fromhex(state_hex)))

    def save_recent(self, key: str, items: list[str], limit: int = 10) -> None:
        """保存最近使用列表。"""
        self._data[f"recent_{key}"] = items[:limit]

    def get_recent(self, key: str) -> list[str]:
        """获取最近使用列表。"""
        return self._data.get(f"recent_{key}", [])

    def persist(self) -> None:
        """写入磁盘。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
```

### 使用规则

- **保存时机**：`closeEvent` 中调用 `persist`，而非 `destroyed`（此时部件可能已销毁）。
- **恢复时机**：`__init__` 末尾恢复，在 `show()` 之前。
- **防御性恢复**：恢复失败（如分辨率变化）不崩溃，`try/except` 包裹后用默认值。
- **配置路径**：`Path.home() / ".config" / "fspack" / "session.json"`（跨平台）。
- **敏感数据**：不存密码、令牌；用 `QSettings` 时选 `IniFormat` 而非 `NativeFormat`（跨平台一致）。

## 四、快捷键与动作

### 场景

菜单项、工具栏按钮、快捷键共用同一 `QAction`，保持行为一致。

### 实现

```python
"""快捷键与动作：QAction 统一菜单、工具栏与快捷键入口。"""

from __future__ import annotations

try:
    from PySide2.QtGui import QAction, QKeySequence
except ImportError:
    from PySide6.QtGui import QAction, QKeySequence
    # PySide6 中 QAction 从 QtGui 导入；PySide2 从 QtWidgets 导入
    try:
        from PySide2.QtWidgets import QAction  # type: ignore[no-redef]
    except ImportError:
        pass


def create_action(
    parent,
    text: str,
    shortcut: str = "",
    triggered=None,
    icon=None,
) -> QAction:
    """创建 QAction 并绑定快捷键与回调。"""
    action = QAction(text, parent)
    if shortcut:
        action.setShortcut(QKeySequence(shortcut))
    if triggered:
        action.triggered.connect(triggered)
    if icon:
        action.setIcon(icon)
    return action


# 使用示例（在 MainWindow 中）
# self.action_open = create_action(self, "打开", "Ctrl+O", self._on_open)
# self.action_save = create_action(self, "保存", "Ctrl+S", self._on_save)
# self.action_quit = create_action(self, "退出", "Ctrl+Q", self.close)
# self.action_settings = create_action(self, "设置", "Ctrl+,", self._open_settings)
# self.action_toggle_sidebar = create_action(self, "折叠侧边栏", "Ctrl+B", self._toggle_sidebar)
# self.action_toggle_sidebar.setCheckable(True)
```

### 使用规则

- **QAction 统一入口**：同一操作只创建一个 `QAction`，同时添加到菜单和工具栏。
- **标准快捷键**：遵循平台惯例（Ctrl+S 保存、Ctrl+Z 撤销、Ctrl+, 设置）。
- **checkable**：切换类操作（折叠侧边栏、切换主题）用 `setCheckable(True)`，`toggled` 信号而非 `triggered`。
- **禁用管理**：通过 `action.setEnabled(False)` 统一禁用菜单项和工具栏按钮。
- **快捷键冲突**：`QKeySequence` 自动处理平台差异（Ctrl→Cmd on macOS）；避免与系统快捷键冲突。

## 五、进度对话框

### 场景

长任务（2-10s）需模态进度对话框，用户可取消。

### 实现

```python
"""进度对话框：QProgressDialog + Worker 信号联动。"""

from __future__ import annotations

try:
    from PySide2.QtCore import Qt
    from PySide2.QtWidgets import QProgressDialog, QWidget
except ImportError:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QProgressDialog, QWidget


def create_progress_dialog(
    parent: QWidget,
    title: str = "处理中",
    cancel_text: str = "取消",
    determinate: bool = True,
) -> QProgressDialog:
    """创建令牌驱动的进度对话框。"""
    dialog = QProgressDialog(title, cancel_text, 0, 100, parent)
    dialog.setWindowModality(Qt.ApplicationModal)
    dialog.setMinimumDuration(0)  # 立即显示
    dialog.setAutoClose(True)
    dialog.setAutoReset(False)
    dialog.setFixedSize(400, dialog.sizeHint().height())
    if not determinate:
        dialog.setRange(0, 0)  # 不确定模式（滚动动画）
    return dialog


# 使用示例
# progress = create_progress_dialog(self, "扫描中")
# worker = WorkerController(payload)
# worker._worker.progress.connect(progress.setValue)
# worker._worker.finished_ok.connect(progress.close)
# worker._worker.failed.connect(progress.close)
# progress.canceled.connect(worker.stop)
# worker.start()
```

### 使用规则

- **阈值显示**：`setMinimumDuration(500)` 让短任务不弹窗；需立即显示时设为 0。
- **不确定模式**：总量未知时 `setRange(0, 0)`；已知总量后切回 `setRange(0, total)`。
- **取消联动**：`progress.canceled` 连接 `worker.stop`，worker 的 `finished`/`failed` 连接 `progress.close`。
- **模态选择**：阻塞式用 `Qt.ApplicationModal`；非阻塞用 `Qt.NonModal` + 状态栏进度。
- **文案更新**：长任务通过 `progress.setLabelText` 更新当前步骤说明。

## 六、消息框

### 场景

确认操作、提示信息、显示错误详情。

### 实现

```python
"""消息框：令牌驱动的 QMessageBox 封装。"""

from __future__ import annotations

try:
    from PySide2.QtWidgets import QMessageBox, QWidget
except ImportError:
    from PySide6.QtWidgets import QMessageBox, QWidget


def confirm_danger(
    parent: QWidget,
    title: str,
    message: str,
    confirm_text: str = "删除",
) -> bool:
    """危险操作确认对话框，确认按钮用危险色。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Warning)
    confirm_btn = box.addButton(confirm_text, QMessageBox.AcceptRole)
    box.addButton("取消", QMessageBox.RejectRole)
    # 确认按钮用危险色样式
    confirm_btn.setStyleSheet(
        f"background-color: {theme.COLOR_DANGER}; color: {theme.COLOR_TEXT_ON_PRIMARY};"
        f"border: none; border-radius: {theme.RADIUS_SM}; padding: 6px 16px;"
    )
    box.exec()
    return box.clickedButton() is confirm_btn


def show_info(parent: QWidget, title: str, message: str) -> None:
    """信息提示对话框。"""
    QMessageBox.information(parent, title, message)


def show_error(parent: QWidget, title: str, message: str, detail: str = "") -> None:
    """错误对话框，可选展示详情。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Critical)
    if detail:
        box.setDetailedText(detail)
    box.exec()
```

### 使用规则

- **危险确认**：确认按钮用 `COLOR_DANGER` 背景 + 操作名（"删除项目"），不用"确定"。
- **错误详情**：`setDetailedText` 存放堆栈/日志，默认折叠；主消息用用户可读语言。
- **不滥用模态**：非阻塞性信息（如"已保存"）用状态栏提示，不用消息框。
- **按钮顺序**：主操作在右，取消在左（Windows/Linux 惯例）；macOS 相反。
- **不嵌套**：消息框中不弹另一个消息框，改为在关闭后链式弹出。

## 七、拖放

### 场景

拖拽文件/文件夹到窗口直接加载，或拖拽列表项重新排序。

### 实现

```python
"""拖放模式：接收外部文件拖入。"""

from __future__ import annotations

from pathlib import Path

try:
    from PySide2.QtCore import Qt, Signal
    from PySide2.QtGui import QDragEnterEvent, QDropEvent
except ImportError:
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtGui import QDragEnterEvent, QDropEvent


class FileDropArea(QWidget):
    """文件拖放接收区域。"""

    files_dropped = Signal(list)  # list[Path]

    def __init__(self, suffixes: list[str] | None = None, parent=None) -> None:
        """初始化并设置接受的文件后缀。"""
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._suffixes = suffixes  # None 表示接受所有文件

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """拖入时校验 MIME 类型。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """放下时提取文件路径并发出信号。"""
        paths = []
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.exists():
                if self._suffixes is None or path.suffix.lower() in self._suffixes:
                    paths.append(path)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()
```

### 使用规则

- **MIME 校验**：`dragEnterEvent` 中只接受 `hasUrls()` 或 `hasText()` 等预期类型。
- **后缀过滤**：`dropEvent` 中按业务需求过滤，不匹配的静默忽略。
- **视觉反馈**：`dragEnterEvent` 时改变背景色（`COLOR_BG_MUTED`），`dragLeaveEvent` 恢复。
- **文件夹处理**：`toLocalFile()` 返回的路径可能是文件夹，按业务需求递归或拒绝。
- **内部拖放**：列表项拖拽排序用 `QListWidget.setDragDropMode(QListWidget.InternalMove)`。

## 八、动画

### 场景

面板展开/折叠、页面切换淡入、状态提示闪现等微交互动画。

### 实现

```python
"""动画模式：QPropertyAnimation 实现淡入淡出与滑动。"""

from __future__ import annotations

try:
    from PySide2.QtCore import QEasingCurve, QPropertyAnimation, QParallelAnimationGroup
    from PySide2.QtWidgets import QGraphicsOpacityEffect, QWidget
except ImportError:
    from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QParallelAnimationGroup
    from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget


def fade_in(widget: QWidget, duration: int = 200) -> QPropertyAnimation:
    """淡入动画。"""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.start()
    return anim


def slide_height(widget: QWidget, target: int, duration: int = 250) -> QPropertyAnimation:
    """高度滑动动画（展开/折叠）。"""
    anim = QPropertyAnimation(widget, b"maximumHeight", widget)
    anim.setDuration(duration)
    anim.setStartValue(widget.height())
    anim.setEndValue(target)
    anim.setEasingCurve(QEasingCurve.OutQuint)
    anim.start()
    return anim
```

### 使用规则

- **时长**：状态切换 150ms，面板展开 250ms；禁用超过 500ms 的动画。
- **缓动**：淡入淡出用 `OutCubic`，滑动用 `OutQuint`；避免 `InOutLine`（机械感）。
- **并行组**：多个属性同时动画用 `QParallelAnimationGroup`。
- **清理**：动画完成后 `deleteLater`，避免 `QPropertyAnimation` 对象残留。
- **性能**：`QGraphicsOpacityEffect` 在大部件上有性能开销，仅用于小面板；全窗口过渡用 `QGraphicsColorizeEffect` 或重绘。
- **可访问性**：检测系统"减少动画"设置（`QApplication.styleHints().colorScheme` 或平台 API），启用时跳过动画。

## 九、错误处理

### 场景

GUI 应用全局异常捕获，防止未处理异常导致崩溃。

### 实现

```python
"""全局错误处理：sys.excepthook + QThread 异常转发。"""

from __future__ import annotations

import sys
import traceback

try:
    from PySide2.QtCore import QObject, Signal
except ImportError:
    from PySide6.QtCore import QObject, Signal


class ExceptionBridge(QObject):
    """全局异常桥接：sys.excepthook 与 worker 异常统一转发到主线程。"""

    error_occurred = Signal(str, str)  # (title, detail)

    def install(self) -> None:
        """安装全局 excepthook。"""
        sys.excepthook = self._excepthook

    def _excepthook(self, exc_type, exc_value, exc_tb) -> None:
        """捕获未处理异常并转发。"""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        self.error_occurred.emit(f"未处理的异常: {exc_type.__name__}", detail)


# 使用示例（在 main.py 中）
# bridge = ExceptionBridge()
# bridge.install()
# bridge.error_occurred.connect(lambda title, detail: show_error(None, title, str(detail), detail))
```

### 使用规则

- **安装时机**：`QApplication` 创建后、主窗口 `show()` 前。
- **Worker 异常**：Worker `run` 中 `except` 捕获预期异常并通过 `failed` 信号发出；未预期异常由 `excepthook` 兜底。
- **不吞异常**：`excepthook` 中至少记录日志 + 通知用户，不静默忽略。
- **日志**：同时写入日志文件（路径放会话状态目录），便于用户反馈。
- **恢复策略**：异常后尝试恢复 UI 到稳定状态（如停止进度动画、重新启用按钮）。

## 十、上下文菜单

### 场景

列表项、表格行、树节点右键弹出操作菜单。

### 实现

```python
"""上下文菜单：右键弹出操作列表。"""

from __future__ import annotations

try:
    from PySide2.QtCore import QPoint, Qt
    from PySide2.QtWidgets import QMenu, QTableView, QWidget
except ImportError:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QMenu, QTableView, QWidget


def show_context_menu(
    parent: QWidget,
    pos: QPoint,
    actions: list[tuple[str, callable | None]],
) -> None:
    """在指定位置弹出上下文菜单。

    Args:
        parent: 父部件（提供坐标系）
        pos: 弹出位置（parent 坐标系）
        actions: [(显示文本, 回调), ...]，回调为 None 时为分隔线
    """
    menu = QMenu(parent)
    for text, callback in actions:
        if callback is None:
            menu.addSeparator()
        else:
            menu.addAction(text, callback)
    menu.exec(parent.mapToGlobal(pos))


# 使用示例（QTableView 子类）
# def contextMenuEvent(self, event):
#     index = self.indexAt(event.pos())
#     if not index.isValid():
#         return
#     show_context_menu(self, event.pos(), [
#         ("打开", lambda: self._open_item(index)),
#         ("复制路径", lambda: self._copy_path(index)),
#         (None, None),  # 分隔线
#         ("删除", lambda: self._delete_item(index)),
#     ])
```

### 使用规则

- **位置校验**：`indexAt(event.pos())` 确认点击在有效项上，空白区域不弹菜单。
- **菜单项状态**：根据当前项权限/状态 `setEnabled(False)` 或不显示菜单项。
- **分隔线**：不同操作类别间用 `addSeparator()` 分隔（查看/编辑 vs 删除）。
- **快捷键**：菜单项设置快捷键（`QAction.setShortcut`），与菜单和快捷键统一。
- **关闭清理**：`QMenu.exec` 是模态阻塞的，返回后菜单自动销毁；不用 `popup`（非阻塞，需手动管理生命周期）。

## 与其他文档的关系

| 文档 | 职责 |
|------|------|
| `SKILL.md` | 设计令牌、代码模板、技术参考 |
| `UI-DESIGN.md` | 视觉规范、组件设计、交互模式 |
| `LAYOUT.md` | 四区结构、阶段联动、Splitter 规则 |
| `PATTERNS.md` | 实现模式（本文档） |

开发时先查 `UI-DESIGN.md` 确定视觉规范，查 `LAYOUT.md` 确定结构规范，查本文档获取实现模式，最后查 `SKILL.md` 获取代码模板与令牌值。
