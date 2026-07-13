# 验证 `--debug` 调试选项与多版本示例测试

## 摘要

本次迭代已在前一轮对话中完成实现（context 丢失前），现需走完「验证 → 提交」收尾闭环。
两大功能均已落地：

1. **`fspack r --debug`**：绕过 GUI loader exe（`-mwindows` 子系统吞 stdout），用 embed `python.exe` 直跑入口脚本，使 `print`/stderr 可见。GUI 应用非零退出时提示用户用 `--debug`。
2. **多版本 + 典型库示例与慢测试**：新增 pyside2app（3.8.10+PySide2）、pyqt5app（3.12.0+PyQt5）、snake（3.11.9+pygame 贪吃蛇）三个示例及对应 `@pytest.mark.slow` 端到端测试。

## 当前状态分析（已通过 Read 验证）

### 已完成

| 文件 | 状态 | 关键内容 |
|------|------|---------|
| [src/fspack/cli.py](file:///f:/Dev/fspack/src/fspack/cli.py) | ✓ | L40 `--debug` 参数；L69 `debug=ns.debug` 透传 |
| [src/fspack/commands/run.py](file:///f:/Dev/fspack/src/fspack/commands/run.py) | ✓ | `run(debug=False)` + `_build_debug_cmd`；GUI 非零退出提示 `--debug` |
| [tests/test_commands.py](file:///f:/Dev/fspack/tests/test_commands.py) | ✓ | 5 个 debug 测试（L110-215）：windows/linux/missing_python/missing_entry/gui_nonzero_hints |
| [tests/test_e2e_slow.py](file:///f:/Dev/fspack/tests/test_e2e_slow.py) | ✓ | L132 pyside2app@3.8.10、L166 pyqt5app@3.12.0、L200 snake@3.11.9 |
| tests/examples/pyside2app/ | ✓ | pyproject.toml + pyside2app.py（QApplication + QLabel 打印 hello） |
| tests/examples/pyqt5app/ | ✓ | pyproject.toml + pyqt5app.py（同构） |
| tests/examples/snake/ | ✓ | pyproject.toml + snake.py（pygame dummy 驱动画蛇头，打印 snake ready） |

### 待完成

- 全套门禁验证（ruff/pyrefly/pytest/coverage）——上一轮刚修 SIM117，需确认全绿
- 手动验证 `fspack r --debug` 在 guicalc 项目上打印 `hello from PySide6`
- git commit + push（遵循 rule-09 中文提交信息风格）
- 更新 project_memory.md 记录 `--debug` 决策

## 提议变更（仅验证与收尾，无代码改动）

### 步骤 1：运行全套门禁

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyrefly check
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95
```

**预期**：全绿，覆盖率 ≥95%（上一轮基线 99.90%，新增测试不应下降）。
**失败处理**：定位根因修复，不放宽断言或加 `# pragma: no cover`。

### 步骤 2：手动验证 `--debug` 实际效果

在 guicalc 项目（PySide6 GUI 应用）上验证：

```bash
# 先确保已构建（若 dist/ 不存在则先 build）
F:\Dev\fspack\.venv\Scripts\fspack.exe b f:\Dev\fspack\tests\examples\guicalc

# 验证 --debug 能看到输出
F:\Dev\fspack\.venv\Scripts\fspack.exe r --debug f:\Dev\fspack\tests\examples\guicalc
```

**预期**：stdout 打印 `hello from PySide6`，退出码 0。
**注意**：用项目 venv 的 `fspack.exe` 直跑，不用 `uv run fspack`（避免在示例目录创建新 venv 污染，见 project_memory.md）。

### 步骤 3：验证普通 `fspack r`（无 --debug）的提示

```bash
F:\Dev\fspack\.venv\Scripts\fspack.exe r f:\Dev\fspack\tests\examples\guicalc
```

**预期**：GUI 应用若非零退出，日志含 `GUI 应用输出被 Windows subsystem 吞掉，如需查看输出请用 \`fspack r --debug\``。
（若 guicalc 正常退出 0，则无警告——这是预期行为，`--debug` 是按需调试工具。）

### 步骤 4：git commit + push

按文件名暂存（不用 `git add -A`），中文提交信息：

```
feat: 新增 fspack r --debug 调试选项与多版本示例测试

--debug 用 embed python 直跑入口脚本，绕过 GUI loader 的 Windows subsystem
使 stdout/stderr 可见；新增 pyside2app(3.8)、pyqt5app(3.12)、snake(pygame) 示例
及对应 slow 端到端测试。
```

暂存文件清单（按实际变更）：
- src/fspack/cli.py
- src/fspack/commands/run.py
- tests/test_commands.py
- tests/test_e2e_slow.py
- tests/examples/pyside2app/pyproject.toml
- tests/examples/pyside2app/pyside2app.py
- tests/examples/pyqt5app/pyproject.toml
- tests/examples/pyqt5app/pyqt5app.py
- tests/examples/snake/pyproject.toml
- tests/examples/snake/snake.py

分支已跟踪远程 → 自动 push。

### 步骤 5：更新 project_memory.md

在 `## Windows 兼容性约定` 或新增 `## 调试` 章节追加：
- `--debug` 机制：embed python.exe（console 子系统）直跑入口，绕过 `-mwindows` GUI loader
- GUI 应用非零退出时自动提示 `--debug`
- 多版本慢测试矩阵：3.8.10/3.11.9/3.12.0

## 假设与决策

1. **不改实现代码**：上轮已实现且测试覆盖充分，本轮仅验证收尾。若门禁失败则按根因修复（属自决范围）。
2. **不跑 slow 测试**：门禁用 `-m "not slow"`（slow 需 mingw+wine，Windows 本地无环境）。新示例的 slow 测试在 CI/Linux 验证。
3. **guicalc 验证用项目 venv**：遵循 project_memory.md 约定，避免 `uv run` 在示例目录污染 venv。
4. **提交信息风格**：遵循 rule-09，中文 + 变更类型，单段落。

## 验证清单

- [ ] `ruff check src tests` 无错误
- [ ] `ruff format --check src tests` 无差异
- [ ] `pyrefly check` 无错误
- [ ] `pytest -m "not slow" --cov=fspack --cov-fail-under=95` 全绿，覆盖率 ≥95%
- [ ] `fspack r --debug` on guicalc 打印 `hello from PySide6`
- [ ] git commit 成功并 push 到 origin
- [ ] project_memory.md 更新
