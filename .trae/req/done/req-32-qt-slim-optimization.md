# Qt 库精简规则增强

## 背景

分析 RimSort 项目打包后 PySide6 体积达 634 MB，发现 [qt.py](../../src/fspack/slim/qt.py) 当前精简规则遗漏了若干可剥离的体积：

- `metatypes/`（14.45 MB）：Qt 元类型 JSON，编译期用，运行时不需要
- `lib/`（0.01 MB）：cmake 文件
- `QtAsyncio/`（0.10 MB）：QtAsyncio 模块，不用 asyncio 不需要
- `opengl32sw.dll`（19.68 MB）：软件 OpenGL 后备，仅 OpenGL 相关模块闭包内需要
- FFmpeg 系列 DLL（17.90 MB）：`avcodec-61.dll` 等，仅 Multimedia 闭包内需要
- `pyside6qml.abi3.dll`（0.08 MB）：QML 绑定层，仅 Qml 闭包内需要
- `v8_context_snapshot.debug.bin`（2.33 MB）：DevTools 调试资源，运行时不需要

## 需求清单

- [x] 扩展 `_QT_EXCLUDE_SUBDIRS` 加入 `metatypes`/`lib`/`QtAsyncio`
- [x] 扩展 .debug 资源剥离到 `.debug.bin`（统一 `.debug.*` 子串匹配）
- [x] 新增 FFmpeg 系列 DLL（`avcodec-*`/`avformat-*`/`avutil-*`/`swscale-*`/`swresample-*`）按 Multimedia 闭包选择性保留
- [x] 新增 `pyside6qml.abi3.dll`/`pyside2qml.abi3.dll` 按 Qml 闭包选择性保留
- [x] 新增 `opengl32sw.dll` 按 OpenGL 相关模块闭包智能保留（仅当闭包内有 OpenGL/Quick/Multimedia/Graphs/DataVisualization 等模块时保留）
- [x] 为新增优化点添加测试用例，覆盖率不下降
- [x] 全套门禁通过（ruff/pyrefly/pytest）

## 验收标准

- RimSort 项目（闭包 = {Widgets, Gui, Core, WebEngineWidgets, WebEngineCore, Network, Positioning, WebChannel}）精简后预期从约 338 MB 降至约 280 MB
- 不影响 WebEngine 应用：Qt6WebEngineCore.dll/Qt6WebEngineWidgets.dll/resources/icudtl.dat 等仍保留
- 不影响 Multimedia/Quick/OpenGL 应用：相应模块闭包内 FFmpeg/opengl32sw 仍保留
- 全套测试通过，覆盖率 ≥ 95%
