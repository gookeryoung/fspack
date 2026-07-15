# 迭代 15：tk 打包限制文档化

## 迭代目标

针对 `examples/tk_basic` 在 Windows 打包后运行失败（`ModuleNotFoundError: No module named 'tkinter'`）
的问题，评估解决方向并制定策略。用户选定方向 D（文档化限制），不改运行时架构，
在 README 中明确说明 embed python 不含 tkinter 的限制与替代方案。

## 问题根因

迭代 14 全量打包测试发现 tk_basic 打包成功但运行失败：

```
ModuleNotFoundError: No module named 'tkinter'
```

根因：Windows embeddable python（python.org 官方精简版，约 10MB）为控制体积裁剪了
tkinter 相关文件：

- `Lib/tkinter/`（标准库纯 Python 模块）
- `_tkinter.pyd`（C 扩展模块）
- `tcl/`、`tk/`（Tcl/Tk 运行时库）

fspack 当前 Windows 运行时走 embed，Linux 走 python-build-standalone（含完整标准库，
含 tkinter）。

## 策略评估

评估三种可行方向：

| 方向 | 做法 | 优点 | 代价 |
|------|------|------|------|
| A. 全局切换 standalone | Windows 也用 python-build-standalone，弃用 embed | 架构统一、标准库完整、与 uv 对齐 | 体积 +20~50MB/项目；改动大 |
| B. 条件切换（仅 tkinter） | 检测 import tkinter 时用 standalone | 保持精简策略 | 双路径维护、检测脆弱 |
| D. 文档化限制 | 明示不支持 tkinter，建议用 Qt | 零代码改动 | tk_basic 无法运行 |

**用户决策**：选 D。理由：保留 embed 精简策略（体积约 10MB），避免所有 Windows 应用
体积增加 20-50MB。tkinter 用户可改用 Qt 框架（已验证支持）或在 Linux 打包。

## 改动文件清单

### 文档更新（纯文档迭代，无源码改动）

- `examples/tk_basic/README.md` — 从空文件改为完整说明：
  - 限制根因（embed python 裁剪 tkinter 相关文件）
  - 替代方案表（PySide6/PySide2/PyQt5 示例链接）
  - Linux 打包提示（standalone 含 tkinter）
- `README.md`（项目根）— 三处更新：
  - 第 22 行：Unicode 符号同步 iter-14 console.py 改动（`▶`/`✓` → `>`/`√`）
  - 第 149 行：tk_basic 说明标注「Windows 打包受限（见已知限制）」
  - 新增「已知限制」章节（平台支持后）：tkinter 限制 + `missing` 误报说明
- `.trae/req/req-15-tk-pack-limitation-doc.md` — 需求文档
- `.trae/docs/iter-15-tk-pack-limitation-doc.md` — 本迭代文档

### 记忆更新

- `project_memory.md`「已知限制」章节：补充 iter-15 策略决策记录

## 关键决策与依据

### 1. 选 D 而非 A（全局切换 standalone）

主要顾虑是体积：python-build-standalone Windows install_only 约 30-40MB，embed 约 10MB。
全局切换会让所有 Windows 应用体积增加 20-50MB，与 fspack 的精简打包目标冲突。
tkinter 是标准库 GUI 但非主流选择（Qt 系列更常用），为它牺牲所有应用的体积不划算。

### 2. 保留 tk_basic 示例而非删除

tk_basic 仍有价值：
- 验证 `detect_entry` 的 GUI 类型识别（`_GUI_HINTS` 含 `tkinter`）
- 作为限制说明的样本，向用户展示 fspack 的边界
- Linux 打包仍可运行（standalone 含 tkinter）

### 3. 同时文档化 `missing` 误报

iter-14 遗留的 `missing` 误报问题（导入名≠包名）一并写入 README「已知限制」章节，
避免用户被日志误导。该问题不影响功能（declared 优先下载），仅日志提示有误导性。

## 验证结果

### 门禁（确认无回归）

纯文档迭代，不涉及代码改动，但仍跑全套门禁确认无回归：

- `ruff check src tests`：All checks passed
- `ruff format --check src tests`：already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack`：全部通过，覆盖率 ≥ 95%

### 验收标准对照

- [x] `examples/tk_basic/README.md` 不再为空，清晰说明限制与替代方案
- [x] 项目根 `README.md` 含「已知限制」章节
- [x] `project_memory.md` 已知限制记录完整（含策略决策）
- [x] 不修改源码、不调整示例代码、不新增测试
- [x] 全套门禁通过，无回归

## 遗留事项

无新增遗留事项。iter-14 的两条遗留（tkinter 限制、missing 误报）均已通过文档化处理。
后续若用户需求变化，可重新评估方案 A（全局切换 standalone）或方案 B（条件切换）。
