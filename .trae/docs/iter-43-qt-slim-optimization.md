# iter-43 Qt 库精简规则增强

## 需求清单

- [x] 扩展 `_QT_EXCLUDE_SUBDIRS` 加入 `metatypes`/`QtAsyncio`，新增 `lib/cmake/` 三级子目录剥离
- [x] 扩展 .debug 资源剥离到 `.debug.bin`（统一 `.debug.*` 子串匹配）
- [x] 新增 FFmpeg 系列 DLL 按 Multimedia 闭包选择性保留
- [x] 新增 `pyside6qml.abi3.dll`/`pyside2qml.abi3.dll` 按 Qml 闭包选择性保留
- [x] 新增 `opengl32sw.dll` 按 OpenGL 相关模块闭包智能保留
- [x] 为新增优化点添加测试用例，覆盖率不下降
- [x] 全套门禁通过（ruff/pyrefly/pytest）

详见 [req-32](../req/req-32-qt-slim-optimization.md)。

## 迭代目标

分析 RimSort 项目打包后 PySide6 体积达 634 MB，发现 [qt.py](../../src/fspack/slim/qt.py) 当前精简规则遗漏了若干可剥离的体积。本次迭代实施 5 项优化，使 RimSort 场景（闭包 = {Widgets, Gui, Core, WebEngineWidgets, WebEngineCore, Network, Positioning, WebChannel}）的 PySide6 体积从约 338 MB 降至约 280 MB。

## 改动文件清单

- `src/fspack/slim/qt.py`：核心优化实现
- `tests/test_slim.py`：新增测试用例（17 个单元测试 + 2 个端到端集成测试）
- `.trae/req/req-32-qt-slim-optimization.md`：需求记录（新建）
- `.trae/docs/iter-43-qt-slim-optimization.md`：本次迭代记录（新建）

## 关键决策与依据

### 1. lib/ 目录不能整体剥离

最初设计将 `lib/` 加入 `_QT_EXCLUDE_SUBDIRS`，但原有测试 `test_qt_other_subdir_shared` 期望 `PySide2/lib/fonts/times.ttf` 归 shared 保留。检查发现：
- PySide6 的 `lib/` 仅含 `cmake/`（构建系统用，可剥离）
- PySide2 的 `lib/` 含 `fonts/`（Qt 内嵌字体，运行时需要）

决策：`lib/` 不加入 `_QT_EXCLUDE_SUBDIRS`，新增 `_QT_LIB_EXCLUDE_SUBDIRS = {"cmake"}`，在 `classify_entry` 中专门处理 `lib/cmake/` 三级子目录剥离。

### 2. FFmpeg/QML ABI DLL 返回 submodule 类而非 exclude

`classify_entry` 的契约：返回 `("submodule", "Multimedia")` 表示按子模块选择性保留，实际剥离由 `_slim_extract` 在 `keep_subs` 非空且不含目标子模块时执行。这与 .pyd/Qt6*.dll 的处理方式一致，保持分类与剥离的职责分离。

`keep_subs` 为空时（全量解压模式），所有 submodule 类保留——这是向后兼容设计，避免纯顶层 import 场景下误剥离。

### 3. opengl32sw.dll 智能保留策略

opengl32sw.dll 是 Mesa 软件 OpenGL 后备，系统无 GPU 驱动时 Qt 加载此 DLL。决策：仅当闭包内含 `_QT_OPENGL_DEPS` 任一模块（OpenGL/OpenGLWidgets/Quick/Quick3D/QuickShapes/QuickWidgets/Multimedia/Graphs/DataVisualization/DataVisualizationQml）时保留。

WebEngineCore 不在 `_QT_OPENGL_DEPS` 中——Chromium 自带 GPU 加速与软件渲染后备，不依赖 opengl32sw.dll。RimSort 闭包含 WebEngineCore 但无 OpenGL 相关模块，opengl32sw.dll 被剥离。

### 4. .debug 资源剥离扩展

原规则 `entry.endswith(".debug.pak")` 遗漏 `v8_context_snapshot.debug.bin`（约 2.3MB）。决策：扩展为 `".debug." in parts[-1].lower()` 子串匹配，覆盖所有 DevTools 调试资源文件（`.debug.pak`/`.debug.bin` 及未来可能新增的 `.debug.*` 后缀）。

### 5. opengl32sw.dll 返回 shared 而非 submodule

opengl32sw.dll 的保留条件是"任一 OpenGL 相关模块在闭包内"（与 `_QT_OPENGL_DEPS` 的交集非空），而非精确匹配某个子模块。这与 submodule 类的"必须精确匹配"语义不同。决策：直接在 `classify_entry` 中判断，保留时返回 `("shared", None)`，剥离时返回 `("exclude", None)`。

## 代码实现情况

### qt.py 新增常量

```python
_QT_LIB_EXCLUDE_SUBDIRS = frozenset({"cmake"})
_QT_FFMPEG_DLL_PREFIXES = frozenset({"avcodec", "avformat", "avutil", "swscale", "swresample"})
_QT_QML_ABI_DLL_NAMES = frozenset({"pyside6qml.abi3.dll", "pyside2qml.abi3.dll"})
_QT_OPENGL_SW_DLL_NAMES = frozenset({"opengl32sw.dll"})
_QT_OPENGL_DEPS = frozenset({
    "OpenGL", "OpenGLWidgets", "Quick", "Quick3D", "QuickShapes", "QuickWidgets",
    "Multimedia", "Graphs", "DataVisualization", "DataVisualizationQml",
})
```

### qt.py 新增辅助函数

- `_is_ffmpeg_dll(filename)`：识别 FFmpeg 系列 DLL（前缀 + 版本号 + .dll）
- `_is_qml_abi_dll(filename)`：识别 QML 绑定层 ABI DLL
- `_is_opengl_sw_dll(filename)`：识别 opengl32sw.dll

### qt.py classify_entry 新增分支

```python
if suffix == ".dll":
    qt_sub = _qt_dll_submodule(stem)
    if qt_sub is not None:
        if is_abi_pkg and qt_sub in _QT_ABI_DLL_DEPS:
            return ("shared", None)
        return ("submodule", qt_sub)
    # FFmpeg → Multimedia 子模块
    if _is_ffmpeg_dll(filename):
        return ("submodule", "Multimedia")
    # QML ABI → Qml 子模块
    if _is_qml_abi_dll(filename):
        return ("submodule", "Qml")
    # opengl32sw → OpenGL 相关模块闭包智能保留
    if _is_opengl_sw_dll(filename):
        if _QT_OPENGL_DEPS & keep_subs:
            return ("shared", None)
        return ("exclude", None)
    return ("shared", None)
```

### lib/cmake/ 三级子目录剥离

```python
if subdir == "lib" and len(parts) >= 4 and parts[2] in _QT_LIB_EXCLUDE_SUBDIRS:
    return ("exclude", None)
```

### .debug 资源剥离扩展

```python
# 原: if entry.endswith(".debug.pak"):
if ".debug." in parts[-1].lower():
    return ("exclude", None)
```

## 整合优化情况

- 模块顶部 docstring 同步更新，列出所有新增优化点
- `classify_entry` docstring 详细说明每类条目的处理规则
- `__all__` 导出新增辅助函数，便于其他模块复用
- 测试用例覆盖单元测试（参数化）+ 端到端集成测试（slim_unpack）

## 测试验证结果

```
938 passed, 21 deselected in 5.88s
TOTAL coverage: 97.03%
slim/qt.py coverage: 99% (1 行未覆盖：版本守卫 fallback)
```

新增测试：
- `TestClassifyEntry`：8 个新测试（metatypes/lib_cmake/lib_fonts/QtAsyncio/FFmpeg/QML ABI/opengl32sw/.debug.bin）
- `TestQtAuxiliaryDllIdentifiers`：3 个参数化测试（_is_ffmpeg_dll/_is_qml_abi_dll/_is_opengl_sw_dll，共 23 个用例）
- `TestSlimUnpack`：2 个端到端集成测试（auxiliary_dll_with_deps_kept/auxiliary_dll_without_deps_excluded）

参数化测试 `test_opengl32sw_with_opengl_dep_kept` 覆盖 8 个 OpenGL 相关模块。

## 遗留事项

无。

## 下一轮计划

无（本次迭代需求全部完成）。

## 体积优化效果（RimSort 场景估算）

| 优化项 | 节省体积 |
|---|---|
| metatypes/ 目录剥离 | 14.45 MB |
| lib/cmake/ 三级子目录剥离 | 0.01 MB |
| QtAsyncio/ 目录剥离 | 0.10 MB |
| FFmpeg 系列 DLL 剥离 | 17.90 MB |
| pyside6qml.abi3.dll 剥离 | 0.08 MB |
| opengl32sw.dll 剥离 | 19.68 MB |
| v8_context_snapshot.debug.bin 剥离 | 2.33 MB |
| **合计** | **≈ 54.55 MB** |

精简后预期体积：338 MB → 约 283 MB
