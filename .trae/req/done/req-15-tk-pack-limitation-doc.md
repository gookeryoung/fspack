# 需求：文档化 embed python 不含 tkinter 的限制

## 背景

迭代 14 全量打包测试发现 `examples/tk_basic` 打包成功但运行失败：

```
ModuleNotFoundError: No module named 'tkinter'
```

根因：Windows embeddable python（python.org 官方精简版）不含 tkinter 相关文件：

- `Lib/tkinter/`（标准库纯 Python 模块）
- `_tkinter.pyd`（C 扩展）
- `tcl/`/`tk/`（Tcl/Tk 运行时库）

embed python 设计目标是嵌入到应用中运行 Python 脚本，为控制体积裁剪了 tkinter、
pip、ensurepip 等模块。fspack 当前 Windows 运行时走 embed，Linux 走
python-build-standalone（含完整标准库，含 tkinter）。

用户选定策略 D：文档化限制，不改运行时架构。保留 tk_basic 示例作为限制说明样本，
不引入 python-build-standalone Windows 版（避免体积增大 20-50MB/项目）。

## 需求

- [ ] 在 `examples/tk_basic/README.md` 说明 embed python 不含 tkinter 的限制，
      指引用户改用 PySide2/PySide6/PyQt5/PyQt6 或在 Linux 打包。
- [ ] 在项目根 `README.md` 新增「已知限制」章节，说明 Windows embed python
      不支持 tkinter，以及 `missing` 误报导入名≠包名（iter-14 遗留）。
- [ ] 确认 `project_memory.md` 已知限制章节记录完整。
- [ ] 不修改源码、不调整示例代码、不新增测试（纯文档迭代）。
- [ ] 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%），确认无回归。

## 验收标准

- `examples/tk_basic/README.md` 不再为空，清晰说明限制与替代方案。
- 项目根 `README.md` 含「已知限制」章节。
- 现有测试全部通过，覆盖率不低于 95%。
