# 需求：tkinter 等内置库打包

## 背景

req-15 文档化了 embed python 不含 tkinter 的限制（策略 D：仅文档化）。
本次迭代改为彻底解决：从 python-build-standalone Windows 构建提取 tkinter
组件补充到 embed python runtime，使 Windows 打包的 tkinter 应用可直接运行。

embed python 缺失三类 tkinter 文件：
- `Lib/tkinter/`（纯 Python 包）
- `_tkinter.pyd`（C 扩展）
- `tcl/tcl8.6/` + `tcl/tk8.6/`（Tcl/Tk 运行时脚本）

Linux standalone 已含完整 stdlib（含 tkinter），无需补充。

## 需求

- [x] 1. 新增 `packaging/builtin.py`，实现 `TkinterBundler`：从 standalone Windows
      tarball 提取 tkinter 组件，按版本缓存 zip（3 层缓存：runtime→cache zip→下载）
- [x] 2. 新增 `BuiltinError` 异常类
- [x] 3. `EntryWrapper.generate_wrapper_source` 新增 `has_tkinter` 参数，模板注入
      `TCL_LIBRARY`/`TK_LIBRARY` 环境变量设置（embed python 缺失 Tcl/Tk 脚本路径）
- [x] 4. `builder.py` 新增"补充内置库"阶段：AST 检出 tkinter 且目标 Windows 时触发
      `TkinterBundler.ensure`，传 `has_tkinter` 给 wrapper 生成
- [x] 5. `packaging/__init__.py` 导出 `TkinterBundler`
- [x] 6. 测试覆盖：URL/名称生成、需求检测、三层缓存、zip 提取、builder 集成
- [x] 7. 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- Windows 目标打包 tkinter 应用后可直接运行，不再报 `ModuleNotFoundError: No module named 'tkinter'`
- 缓存机制：首次下载 ~40MB tarball → 提取 ~3-5MB zip 缓存 → 后续构建秒级解压
- Linux 目标不触发 tkinter 补充（standalone 已含）
- 非 tkinter 项目不触发补充，wrapper 注入 `if False:` 跳过环境变量设置
- 取代 req-15（限制已消除，无需文档化）
