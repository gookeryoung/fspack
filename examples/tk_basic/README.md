# tk-basic

tkinter 标准库 GUI 示例：输入框、文本框、按钮交互。

## 已知限制：Windows 打包不支持 tkinter

`fspack b` 在 Windows 打包本示例可成功生成 `dist/tk-basic.exe`，但运行会失败：

```
ModuleNotFoundError: No module named 'tkinter'
```

### 根因

fspack Windows 运行时使用 [python.org 官方 embeddable package](https://www.python.org/downloads/windows/)
（精简版，约 10MB），该发行版为控制体积裁剪了以下 tkinter 相关文件：

- `Lib/tkinter/`（标准库纯 Python 模块）
- `_tkinter.pyd`（C 扩展模块）
- `tcl/`、`tk/`（Tcl/Tk 运行时库）

embed python 的设计目标是嵌入到应用中运行 Python 脚本，非完整发行版，
因此 tkinter、pip、ensurepip 等模块默认缺失。

### 替代方案

如需在 Windows 打包 GUI 应用，建议改用以下框架（fspack 已验证支持）：

| 框架 | 示例 | 说明 |
|------|------|------|
| PySide6 | `examples/gui_calc` | Qt6 官方绑定，LGPL |
| PySide2 | `examples/pyside2_app` | Qt5 官方绑定，LGPL，需 Python < 3.11 |
| PyQt5 | `examples/pyqt5_app` | Qt5 第三方绑定，GPL/商业双授权 |

或在 Linux 打包本示例（fspack Linux 运行时用 python-build-standalone，含完整标准库）：

```bash
fspack b --target linux
```
