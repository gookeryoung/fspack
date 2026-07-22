# 迭代 20：PySide2 abi3.dll 隐式依赖修复与示例完善

## 迭代目标

完善 `examples/pyqt5_cli`（基本依赖）与 `examples/pyside2_app`（带 QtMultimedia
非基本依赖）两个示例，验证 iter-19 引入的白名单制精简打包与 Qt 模块依赖闭包
机制在两种场景下均可正确打包运行。

## 需求清单

- [x] pyqt5_cli 作为基本依赖示例（仅 PyQt5），验证闭包机制对 PyQt5 有效
- [x] pyside2_app 作为带其他依赖示例（PySide2 + QtMultimedia），验证 slim
      对非基本 Qt 模块的保留正确性
- [x] 两示例移除显式 C 层 import 声明，验证 iter-19 闭包自动推导
- [x] 修复 PySide2 abi3.dll 隐式依赖 Qt5Qml.dll/Qt5Network.dll 的剥离 bug
- [x] 门禁通过（ruff/pyrefly/pytest，覆盖率 98.10%）

## 改动文件清单

### 示例代码

- `examples/pyqt5_cli/pyqt5_cli.py`：移除显式 `import PyQt5.QtCore`/
  `import PyQt5.QtGui`，仅保留 `from PyQt5.QtWidgets import QApplication,
  QLabel`。验证闭包机制对 PyQt5 有效（QtWidgets 自动加入 Gui/Core）。
- `examples/pyside2_app/pyside2app.py`：引入
  `from PySide2.QtMultimedia import QMediaPlayer`，移除显式
  `import PySide2.QtGui`。验证 slim 对非基本 Qt 模块的保留与闭包传递
  （Multimedia → Gui/Core/Network）。

### 核心实现

- `src/fspack/slim.py`：
  - 新增 `_QT_ABI_DLL_PACKAGES`（`pyside2`/`pyside6`）与
    `_QT_ABI_DLL_DEPS`（`Qml`/`Network`）常量
  - 修改 `classify_entry`：PySide2/PySide6 的 `Qt5Qml.dll`/`Qt5Network.dll`
    （abi3.dll 隐式依赖的 C 层 DLL）归 shared 始终保留，不通过子模块保留
    集合处理——避免误保留 `qml/` 资源目录（仅运行 QML 应用时才需要）
  - 对应 `.pyd`（`QtQml.pyd`/`QtNetwork.pyd`）仍按子模块选择性保留，
    仅用户显式 import 时保留
  - 修改 `slim_unpack` 闭包应用逻辑：移除 abi3 基础依赖注入（改为在
    `classify_entry` 中处理 DLL），避免 `qml/` 目录被误保留

### 测试

- `tests/test_slim.py`：修改 `test_selective_unpack`，新增 `Qt5Qml.dll`/
  `Qt5Sql.dll` 验证：abi3 依赖的 Qml/Network DLL 归 shared 保留，非 abi3
  依赖且未 import 的 Qt5Sql.dll 剥离

## 关键决策与依据

### PySide2 abi3.dll 隐式依赖的发现

pyside2_app 构建后运行报
`ImportError: DLL load failed while importing QtCore: 找不到指定的模块。`。
用 `pefile` 分析 PE 依赖链：

```
QtCore.pyd → pyside2.abi3.dll → Qt5Qml.dll → Qt5Network.dll, Qt5Core.dll
```

`pyside2.abi3.dll`（绑定层，归 shared 始终保留）C 层依赖 `Qt5Qml.dll`，
而 iter-19 的白名单制把 `Qt5Qml.dll` 当作 Qml 子模块选择性保留——用户未
import QtQml 时被剥离，导致 abi3.dll 加载失败。

PyQt5 不存在此问题：其绑定层（sip）不依赖 Qml/Network，pyqt5_cli 运行成功
证明。

### 修复方案选择：classify_entry 中归 shared vs 注入子模块集合

两种方案：

1. **注入子模块集合**（初版修复）：把 Qml/Network 加入 `keep_subs`，
   闭包传递加入 Core。问题：`qml/` 资源目录的保留判断
   `_QT_QML_DEPS & subs` 会命中 Qml，导致 `qml/` 目录（约 21MB）被误
   保留——但 abi3.dll 只依赖 Qt5Qml.dll（DLL），不需要 qml/ 资源
2. **classify_entry 中归 shared**（最终方案）：对 `_QT_ABI_DLL_PACKAGES`
   的包，`Qt5Qml.dll`/`Qt5Network.dll` 归 shared 始终保留，`.pyd` 仍按
   子模块选择性保留。`qml/` 目录不受影响

方案 2 更精确：区分了 C 层 DLL 依赖（始终保留）与 Python 绑定（按需保留），
避免误保留 qml/ 资源目录。参考 fspacker 的 `PySide2Packer.PATTERNS` 基础
依赖含 Network/Qml 的 DLL 但不含 qml/ 目录。

### .pyd 与 .dll 区别对待

- `Qt5Qml.dll`/`Qt5Network.dll`（C 层 DLL）：abi3.dll 的 C 层依赖，
  归 shared 始终保留
- `QtQml.pyd`/`QtNetwork.pyd`（Python 绑定）：仅用户显式 import 时保留

abi3.dll 是 C 层 DLL，只依赖 C 层 DLL，不依赖 Python `.pyd`。用户不 import
QtQml 时不需要 QtQml.pyd，但 abi3.dll 仍需 Qt5Qml.dll。

## 代码实现情况

### classify_entry 的 abi3 DLL 处理

```python
is_abi_pkg = normalize_name(top_pkg) in _QT_ABI_DLL_PACKAGES
# ...
if is_qt and suffix == ".dll":
    qt_sub = _qt_dll_submodule(stem)
    if qt_sub is not None:
        # PySide2/PySide6 的 abi3.dll 隐式依赖 Qml/Network 的 DLL → 归 shared
        # 始终保留（AST 无法发现此 C 层依赖）；.pyd 仍按子模块选择性保留
        if is_abi_pkg and qt_sub in _QT_ABI_DLL_DEPS:
            return ("shared", None)
        return ("submodule", qt_sub)
    return ("shared", None)
```

## 整合优化情况

- slim_unpack 闭包应用循环回归简洁：仅处理 Qt 模块依赖闭包，abi3 DLL 依赖
  在 classify_entry 中处理，职责分离
- 测试新增 Qt5Sql.dll 验证非 abi3 依赖的未 import 子模块仍被正确剥离

## 测试验证结果

### pyqt5_cli（基本依赖）

- 构建：保留子模块 Core, Gui, Widgets，跳过 62 个未用子模块文件
- 运行：输出 "hello from PyQt5"

### pyside2_app（带 QtMultimedia）

- 构建：保留子模块 Core, Gui, Multimedia, Network, Widgets，跳过 2544 个
  未用子模块文件（含 qml/ 目录资源，正确剥离）
- 运行：输出 "hello from PySide2 + QtMultimedia (player state: 0)"
- Qt5Qml.dll/Qt5Network.dll 归 shared 保留，QtQml.pyd/QtNetwork.pyd 按需剥离

### 门禁

- ruff check/format：通过
- pyrefly check：0 errors
- pytest：461 passed，覆盖率 98.10%

## 遗留事项

- QFontDatabase 警告 "Cannot find font directory .../PySide2/lib/fonts"：
  Qt 自身行为（不再自带字体），不影响功能，可忽略或后续部署字体文件
- 若用户 import QtQuick 相关模块，qml/ 目录会因 Quick 在 `_QT_QML_DEPS`
  中而保留，符合预期

## 下一轮计划

无。两示例验证完成，slim 精简策略对基本依赖与非基本依赖场景均有效。
