# 修复 fspack r 调试体验并新增多版本多库示例

## Context

用户跑 `fspack r` 运行打包好的 guicalc.exe，控制台只打印 "INFO 运行: ...guicalc.exe" 后回到提示符，既无程序输出（期望 "hello from PySide6"）也无报错。

根因在 [loader.py:198-199](file:///f:/Dev/fspack/src/fspack/loader.py#L198-L199)：GUI 应用编译时加 `-mwindows`（Windows subsystem），该子系统下 `print()` 写到 invalid handle 被静默丢弃。这是 GUI 应用预期行为，但调试体验差——用户无法看到输出和错误。

同时用户希望覆盖更多 Python 版本 × 典型库组合（py3.8+pyside2、py3.12+pyqt5、pygame 贪吃蛇），现有 examples 只有 helloworld/guicalc/clitool/pygamedemo/webapp 五个。

目标：1) `fspack r` 增加 `--debug` 选项，用 embed python.exe 直跑入口脚本绕过 GUI subsystem，输出可见；2) 新增 3 个示例项目 + 对应 e2e slow 测试。

## 方案

### 1. fspack r --debug 实现

#### 1.1 cli.py 加 --debug 选项

**文件**：[src/fspack/cli.py](file:///f:/Dev/fspack/src/fspack/cli.py)

`p_run` 子解析器加：
```python
p_run.add_argument("--debug", action="store_true", help="用 embed python 直跑入口脚本（绕过 GUI loader，输出可见）")
```

`main` 里 `run` 分支改为：
```python
run_cmd.run(project, rest_args=_drop_separator(ns.rest), debug=ns.debug)
```

#### 1.2 commands/run.py 实现 debug 路径

**文件**：[src/fspack/commands/run.py](file:///f:/Dev/fspack/src/fspack/commands/run.py)

- `run` 函数签名加 `debug: bool = False` 参数
- 新增 `_build_debug_cmd(project: Path, info: ProjectInfo) -> list[str]`：
  - Windows：`[dist/runtime/python.exe, dist/src/<entry_rel>]`
  - Linux：`[dist/runtime/python/bin/python3.X, dist/src/<entry_rel>]`（用 `glob("python3.*")` 找，不依赖 info.py_version，因为 fspack r 时 py_version 可能与构建时不同）
  - 入口脚本不存在 → `FspackError("未找到入口脚本: ... （请先执行 fsp b）")`
  - python.exe 不存在 → `FspackError("未找到 embed python: ... （请先执行 fsp b）")`
- `run` 内逻辑：
  - `debug=True`：`cmd = _build_debug_cmd(project, info) + rest_args`；构造 `env = {**os.environ, "PYTHONUNBUFFERED": "1"}`；Linux 额外 `env["PYTHONHOME"] = str(dist/runtime/python)`（standalone python 需要）；`subprocess.run(cmd, check=False, env=env)`
  - `debug=False`：原逻辑 `_find_exe` + `_build_cmd`，`subprocess.run(cmd, check=False)`
  - 退出码非零时：若 `info.app_type is AppType.GUI and not debug`，`_logger.warning("GUI 应用输出被 Windows subsystem 吞掉，如需查看输出请用 fspack r --debug")`；然后 `raise FspackError("程序退出码非零: ...")`

**原理**：embed python 包内的 `python.exe` 是 console 子系统，`print` 输出可见；`_pth` 文件控制 sys.path（含 `..\src` 和 `Lib\site-packages`），所以能找到用户源码和第三方依赖。Linux standalone python 需 `PYTHONHOME` 指向 `runtime/python`。

#### 1.3 test_commands.py 加 debug 测试

**文件**：[tests/test_commands.py](file:///f:/Dev/fspack/tests/test_commands.py)

新增 5 个测试：
- `test_run_run_debug_windows`：mock `platform.system` 返回 "Windows"，构造 dist/runtime/python.exe + dist/src/app.py，验证 cmd = `[python.exe, entry.py]` 且 env 含 `PYTHONUNBUFFERED=1`
- `test_run_run_debug_linux`：mock `platform.system` 返回 "Linux"，构造 dist/runtime/python/bin/python3.11 + 入口，验证 cmd 正确且 env 含 `PYTHONHOME`
- `test_run_run_debug_missing_python`：python.exe 不存在 → `FspackError` match "未找到 embed python"
- `test_run_run_debug_missing_entry`：入口脚本不存在 → `FspackError` match "未找到入口脚本"
- `test_run_run_gui_nonzero_hints_debug`：GUI 应用非零退出码时验证 warning 日志（用 `caplog`）

现有 `test_run_run_success` / `test_run_run_nonzero_exit` 保持不变（默认 debug=False 路径）。

### 2. 新增 3 个示例项目

#### 2.1 tests/examples/pyside2app/

**pyproject.toml**：
```toml
[project]
name = "pyside2app"
version = "0.1.0"
dependencies = ["PySide2"]
```

**pyside2app.py**：仿 [guicalc.py](file:///f:/Dev/fspack/tests/examples/guicalc/guicalc.py) 结构——`import PySide2` + `os.add_dll_directory` + `QApplication([])` + `QLabel("hello from PySide2")` + `print(label.text())` + `app.quit()`。`PySide2` 在 `_GUI_HINTS` 中，自动识别为 GUI 应用。

#### 2.2 tests/examples/pyqt5app/

**pyproject.toml**：
```toml
[project]
name = "pyqt5app"
version = "0.1.0"
dependencies = ["PyQt5"]
```

**pyqt5app.py**：同结构，`import PyQt5` + `QApplication` + `QLabel("hello from PyQt5")` + print。`PyQt5` 在 `_GUI_HINTS` 中。

#### 2.3 tests/examples/snake/

**pyproject.toml**：
```toml
[project]
name = "snake"
version = "0.1.0"
dependencies = ["pygame"]
```

**snake.py**：简化验证版——`os.environ.setdefault("SDL_VIDEODRIVER", "dummy")` + `os.environ.setdefault("SDL_AUDIODRIVER", "dummy")` + `pygame.init()` + `set_mode((200,200))` + `draw.rect` 画蛇头 + `display.flip()` + `print("snake ready")` + `pygame.quit()`。pygame 不在 `_GUI_HINTS`，识别为 CLI（console subsystem），print 可见。

### 3. test_e2e_slow.py 加 3 个 slow 测试

**文件**：[tests/test_e2e_slow.py](file:///f:/Dev/fspack/tests/test_e2e_slow.py)

仿现有 `test_build_and_run_guicalc` 模式（[line 75-107](file:///f:/Dev/fspack/tests/test_e2e_slow.py#L75-L107)）：

- `test_build_and_run_pyside2app`：`build(proj, mirror, "3.8.10", target=WINDOWS)`；验证 exe + `python38.dll` + `python38._pth` + `PySide2` 解包；wine 运行设 `QT_QPA_PLATFORM=offscreen`；DLL 失败则 skip（仿 guicalc）
- `test_build_and_run_pyqt5app`：`build(proj, mirror, "3.12.0", target=WINDOWS)`；验证 `python312.dll`/`_pth` + `PyQt5` 解包；wine 运行同上
- `test_build_and_run_snake`：用 `_build_and_run("snake", "snake ready", tmp_path, extra_env={"SDL_VIDEODRIVER":"dummy","SDL_AUDIODRIVER":"dummy"})`；验证 `pygame` 解包

测试头注释更新：覆盖 8 类典型项目（原 5 类 + pyside2app/pyqt5app/snake）。

## 不改动的部分

- `loader.py` 的 `-mwindows` 编译选项保留：GUI 应用发布时应无控制台窗口，这是正确行为
- `_pth` 文件格式不动：已正确包含 `..\src` 和 `Lib\site-packages`
- 现有 e2e 测试不动
- `--debug` 不影响默认 `fspack r` 行为（默认仍跑 loader exe）

## 验证

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyrefly check
uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95
```

手动端到端验证：
```bash
# guicalc 已构建，直接验证 --debug
fspack r --debug
# 期望打印 "hello from PySide6"

# 新示例构建验证（可选，slow）
cd tests/examples/pyside2app && fspack b --py-version 3.8.10
cd tests/examples/pyqt5app && fspack b --py-version 3.12.0
cd tests/examples/snake && fspack b
```

预期：
- ruff/pyrefly 全过
- pytest 非 slow 全绿，覆盖率 ≥ 95%
- `fspack r --debug` 在 guicalc 打印 "hello from PySide6"
- 新示例 pyproject.toml 能被 `parse_project` 正确解析（app_type 推断正确）
