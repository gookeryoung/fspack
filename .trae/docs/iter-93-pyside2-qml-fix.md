# iter-93: PySide2 模板 Python 版本约束与 QML 插件 DLL 搜索路径修复

## 需求清单

- [x] PySide2 模板（pyside2/pyside2-qml）添加 `requires-python = ">=3.8,<3.11"` 约束
- [x] 修复打包后 QML 应用运行错误：`qtquick2plugin.dll` 加载失败找不到 Qt5*.dll
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

修复用户反馈的两个问题：

1. PySide2 不支持 Python 3.11+，但 fsp init 创建的 PySide2 模板未约束 Python
   版本，导致 fspack 可能选择 3.11+ 版本，PySide2 安装失败或运行异常。
2. 打包后 QML 应用运行时 `qtquick2plugin.dll` 加载失败，因为 QML 插件依赖的
   Qt5Core.dll/Qt5Quick.dll 等在 `site-packages/PySide2/` 目录下，不在 Windows
   默认 DLL 搜索路径中。

## 改动文件清单

### 修改

- `src/fspack/templates/registry.py`
  - `_pyproject()` 函数新增 `requires_python` 参数（默认 `">=3.8"`），支持
    自定义 `requires-python` 约束
  - `_PYPROJECT_NO_DEPS` / `_PYPROJECT_WITH_DEPS` 模板用 `$requires_python`
    占位符替换硬编码的 `>=3.8`
  - pyside2 与 pyside2-qml 模板调用 `_pyproject(("PySide2",), requires_python=">=3.8,<3.11")`
- `src/fspack/packaging/entry.py`
  - `_WRAPPER_TEMPLATE` 的 Qt 插件路径设置段新增 `os.add_dll_directory` 调用
  - 将 Qt 包根目录（`site-packages/PySide2/` 等）添加到 DLL 搜索路径，使 QML
    插件能找到 Qt5Core.dll/Qt5Quick.dll 等 C 层依赖
  - 重构循环：`_qt_root` 变量复用，`_qt_plugins` 从 `_qt_root` 派生
- `tests/test_init_templates.py`
  - `test_init_project_pyside2` 新增断言：pyproject.toml 含
    `requires-python = ">=3.8,<3.11"`
  - `test_init_project_pyside2_qml` 新增同样的 requires-python 断言
- `tests/test_entry.py`
  - 新增 `test_generate_wrapper_source_qt_dll_directory`：验证 wrapper 源码含
    `os.add_dll_directory` 调用与 `_qt_root` 变量

## 关键决策与依据

### PySide2 版本约束

PySide2 官方支持 Python 3.6-3.10，不支持 3.11+。fspack 的 `resolve_py_version`
已支持根据 `requires-python` 自动选择最高兼容版本，但模板未声明约束时默认选择
3.11.9（Windows）/ 3.10.20（Linux），导致 PySide2 在 3.11 上安装失败。

选择 `">=3.8,<3.11"` 而非 `">=3.8,<=3.10"`：PEP 440 上界 `<3.11` 更清晰，
且 fspack 已知版本映射中 3.10.20（Linux）/ 3.10.11（Windows）满足约束。

### QML 插件 DLL 加载失败根因

Windows DLL 加载器搜索顺序：

1. 应用目录（`dist/runtime/`，python.exe 所在目录）
2. 系统目录（System32）
3. PATH 环境变量

PySide2 的 Qt DLL（Qt5Core.dll/Qt5Gui.dll/Qt5Quick.dll 等）在
`site-packages/PySide2/` 目录下，不在上述搜索路径中。QML 引擎加载
`site-packages/PySide2/qml/QtQuick.2/qtquick2plugin.dll` 时，该 DLL 的 C 层
依赖（Qt5Quick.dll 等）无法被找到，导致 "plugin cannot be loaded for module
QtQuick" 错误。

### os.add_dll_directory vs PATH

Python 3.8+ 引入 `os.add_dll_directory` 作为推荐的 DLL 搜索路径添加方式，
取代修改 PATH 环境变量。优势：

- 仅影响当前进程的 DLL 搜索，不污染子进程环境
- 不受 PATH 长度限制（Windows PATH 上限 2048 字符）
- 与 `os.environ["PATH"]` 修改不冲突，可并存

wrapper 已在 Qt 插件路径循环中检测到 Qt 包目录，复用同一循环添加
`add_dll_directory`，避免重复扫描。

### 异常处理

`os.add_dll_directory` 可能抛 `OSError`/`FileNotFoundError`（目录权限问题或
路径过长），用 `try/except` 静默处理——DLL 目录设置失败不应阻止应用启动，
最坏情况是 QML 插件加载失败（与修复前行为一致）。

## 代码实现情况

### wrapper Qt DLL 目录设置

```python
for _qt_pkg in ("PySide2", "PySide6", "PyQt5", "PyQt6"):
    _qt_root = os.path.join(_SITE_PACKAGES, _qt_pkg)
    _qt_plugins = os.path.join(_qt_root, "plugins")
    if os.path.isdir(_qt_plugins):
        os.environ.setdefault("QT_PLUGIN_PATH", _qt_plugins)
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", _qt_plugins)
        if os.path.isdir(_qt_root):
            try:
                os.add_dll_directory(_qt_root)
            except (OSError, FileNotFoundError):
                pass
        break
```

### _pyproject 函数签名

```python
def _pyproject(
    dependencies: tuple[str, ...] = (),
    requires_python: str = ">=3.8",
) -> str:
```

## 整合优化情况

- `_pyproject` 函数签名向后兼容：`requires_python` 有默认值，现有模板调用
  无需修改
- wrapper 的 Qt 插件路径与 DLL 目录设置合并到同一循环，避免重复扫描
- PySide6/PyQt5/PyQt6 模板无需版本约束（PySide6 支持 3.11+，PyQt5/6 同理）

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 96 files already formatted
- `uv run pyrefly check` — 0 errors (5 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1419 passed, 1 skipped, 30 deselected, coverage 97.84%

## 遗留事项

无。

## 下一轮计划

iter-94: 继续按 `req-47-feature-perf-polish.md` 推进剩余功能/性能完善项。
