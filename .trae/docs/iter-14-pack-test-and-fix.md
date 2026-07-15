# 迭代 14：全量打包测试与问题修复

## 迭代目标

对 `examples/` 下全部 11 个示例项目执行 `fspack b` 打包 + `fspack r` 运行验证,
修复测试过程中暴露的问题,确保所有项目可打包、可运行。

## 改动文件清单

### 源码修复

- `src/fspack/console.py` — `step`/`success`/`error` 的 Unicode 符号替换为 GBK 兼容符号
  - `▶`(U+25B6)→`>`、`✓`(U+2713)→`√`(U+221A)、`✗`(U+2717)→`×`(U+00D7)
  - 根因:Windows GBK 控制台无法编码 U+2713/U+2717/U+25B6,`rich` legacy_windows_render
    抛 `UnicodeEncodeError`,导致构建完成后输出成功消息时崩溃
- `src/fspack/cli.py` — `p_run` 的 `rest` 参数从 `nargs=argparse.REMAINDER` 改为 `nargs="*"`
  - 根因:REMAINDER 会贪婪捕获 project 之后的 `--debug`,导致 `fspack r <project> --debug`
    把 `--debug` 当作透传参数传给 exe,而非 fspack 的调试标志
  - 改用 `nargs="*"` 后,`--debug` 正确解析为 fspack 选项;透传参数仍可用 `--` 分隔

### 示例修复

- `examples/tk_basic/pyproject.toml` — `dependencies` 从 `[]` 改为 `["PyYAML"]`
  - 根因:代码 `import yaml`(导入名),PyPI 包名是 `PyYAML`,未声明导致 `pip download yaml` 失败
- `examples/pyside2_app/pyside2app.py` — 添加 `import PySide2.QtGui`
- `examples/gui_calc/guicalc.py` — 添加 `import PySide6.QtCore` 和 `import PySide6.QtGui`
- `examples/pyqt5_app/pyqt5app.py` — 添加 `import PyQt5.QtCore` 和 `import PyQt5.QtGui`
  - 根因:Qt 的 `QtWidgets.pyd` 在 C 层依赖 `QtGui.pyd`,`QtGui.pyd` 又依赖 `QtCore.pyd`;
    精简打包按 AST 子模块分析剥离未用 `.pyd`,显式 import 声明 C 层依赖以保留对应 `.pyd`

### 测试更新

- `tests/test_console.py` — 断言符号同步更新(`▶`→`>`、`✓`→`√`、`✗`→`×`)
- `tests/test_cli.py` — 新增 `test_run_debug_flag_after_project` 回归测试,
  验证 `fspack r <project> --debug` 正确解析 debug 标志

## 关键决策与依据

### 1. console 符号用 GBK 兼容字符而非动态检测

考虑过"UTF-8 终端用 Unicode,GBK 终端用 ASCII"的动态检测方案,但:
- `console.capture()`(测试用)不走 stdout,检测 `sys.stdout.encoding` 在测试环境不稳定
- 统一用 GBK 兼容符号(`√`/`×`/`>`)跨平台一致,测试稳定,语义清晰
- `√`(U+221A)和 `×`(U+00D7)在 GBK 和 UTF-8 终端均可正常显示

### 2. REMAINDER 改 nargs="*" 而非 interspersed

`argparse.REMAINDER` 会捕获位置参数后的所有 token(含 `-` 开头),导致 `--debug` 被吞。
改用 `nargs="*"` 后,`-` 开头的 token 会被 argparse 识别为选项;透传参数用 `--` 分隔。
这是 argparse 推荐的"选项 vs 位置参数"分离方式,接口更清晰。

### 3. Qt 子模块依赖用显式 import 声明而非自动检测

Qt 的 `QtWidgets.pyd` → `QtGui.pyd` → `QtCore.pyd` 是 C 层链接依赖,AST 分析无法发现。
选择在示例代码中显式 `import X.QtGui`/`import X.QtCore` 声明依赖,而非在 fspack 中硬编码
Qt 特殊处理。理由:
- "代码即声明"原则,依赖关系由代码显式表达
- 避免在 fspack 中引入 Qt 特定逻辑(通用性)
- `--keep-module` CLI 选项仍可作为用户兜底手段

## 验证结果

### 打包测试(11/11 成功)

| 示例 | 类型 | 依赖 | 结果 |
|------|------|------|------|
| cli_helloworld | cli | 无 | √ |
| cli_complex | cli | ordered-set, lxml | √ (req-14 验证) |
| cli_office | cli | pypdf | √ |
| cli_tool | cli | requests | √ |
| tk_basic | gui | PyYAML | √ (修复声明) |
| web_app | cli | flask | √ |
| pygame_demo | cli | pygame | √ |
| pygame_snake | cli | pygame | √ |
| gui_calc | gui | PySide6 | √ (修复 QtGui/QtCore) |
| pyqt5_app | gui | PyQt5 | √ (修复 QtGui/QtCore) |
| pyside2_app | gui | PySide2 | √ (修复 QtGui) |

### 运行验证(10/11 成功)

- cli_helloworld: `hello, world` √
- cli_complex: `loaded ordered_set 4.1.0` + `6.1.1`(lxml)√
- cli_office: 生成 example.pdf √
- cli_tool: `requests 2.34.2` √
- web_app: `hello from flask` √
- pygame_demo: `pygame 2.6.1` √
- pygame_snake: `snake ready` / `game over, score: 0` √
- gui_calc --debug: `hello from PySide6` √
- pyqt5_app --debug: `hello from PyQt5` √
- pyside2_app --debug: `hello from PySide2` √
- tk_basic --debug: 失败(embed python 不含 tkinter)

### 门禁

- `ruff check`: All checks passed
- `ruff format --check`: 45 files already formatted
- `pyrefly check`: 0 errors
- `pytest -m "not slow" --cov=fspack`: 298 passed, 覆盖率 98.42%

## 遗留事项

1. **tk_basic 的 tkinter 限制**:Windows embed python 不含 tkinter(`Lib/tkinter/` 和
   `_tkinter.pyd` 缺失)。打包成功但运行时 `ModuleNotFoundError: No module named 'tkinter'`。
   这是 embed python 的固有限制,需用 python-build-standalone 或手动补 tcl/tk 才能解决,
   超出本次迭代范围。

2. **`missing` 误报**:tk_basic 声明 `PyYAML` 后,日志仍提示 `AST 发现未声明依赖: yaml`。
   原因:`DependencyReport.missing` 比较归一化包名(`pyyaml`),不知道导入名(`yaml`)→
   包名(`PyYAML`)映射。不影响功能,但日志有误导性。修复需维护导入名→包名映射表,
   属于增强项,不在本次范围。
