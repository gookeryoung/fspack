# iter-146: QML 项目自动绑定 QtSvg + QtWidgets 始终保留

## 需求清单

- [x] QML 项目（`Qml` 在闭包中）自动把 `Svg` 加入依赖闭包，使 `Qt5Svg.dll`/
      `Qt6Svg.dll` 随之保留，无需用户显式 `import QtSvg`
- [x] `plugins/imageformats/qsvg*.dll` 按 `Svg` 子模块选择性保留，消除"插件在
      但依赖 DLL 不在"的矛盾；非 SVG 项目剥离 `qsvg.dll` 省体积
- [x] 其他 imageformats 插件（qjpeg/qgif/qico 等）仍始终保留，基础图片格式
      支持不受影响
- [x] QtWidgets 始终保留不再按需裁剪：任何 Qt 模块在闭包中时自动加入 `Widgets`，
      避免 QML 的 Controls 1.x/Dialogs 插件因 `Qt5Widgets.dll` 缺失加载失败
- [x] 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）

## 迭代目标

修复 QML 项目打包后运行失败的两个问题：
1. SVG 图像加载失败：`Image { source: "*.svg" }` 通过 imageformats 插件加载 SVG，
   但 QML 无 `import QtSvg` 语法，`Qt5Svg.dll`/`Qt6Svg.dll` 被剥离。
2. `qtquickcontrolsplugin.dll` 加载失败：QtQuick.Controls 1.x/Dialogs 插件 C 层
   依赖 `Qt5Widgets.dll`，但 `Widgets` 不在闭包被剥离，导致
   `plugin cannot be loaded for module QtQuick.Controls` 错误。

## 改动文件清单

- `src/fspack/slim/qt.py`（修改）：
  - `QtSlimSpec.expand_closure`：
    - 任何 Qt 模块在闭包中时自动加入 `Widgets`（QtWidgets 始终保留）
    - `Qml` 在闭包中时自动加入 `Svg`（QML 项目 SVG 支持）
  - `QtSlimSpec.classify_entry`：`plugins/imageformats/qsvg*.dll` 按 `Svg` 子模块
    选择性保留，仅当 `Svg` 在 `keep_subs` 时归 `shared`，否则归 `exclude`。
    其余 imageformats 插件仍始终保留。
  - 模块顶部 docstring 与 `classify_entry` docstring 同步更新。
- `tests/test_slim.py`（修改）：
  - `test_subdir_imageformats` 改为测 `qjpeg.dll`（非 svg 始终保留）
  - 新增 `test_subdir_imageformats_qsvg_without_dep_excluded`：qsvg.dll 无 Svg 依赖时剥离
  - 新增 `test_subdir_imageformats_qsvg_with_dep_kept`：qsvg.dll 有 Svg 依赖时保留
  - 新增 `test_subdir_imageformats_qsvg6_with_dep_kept`：Qt6 命名 qsvg6.dll 同样处理
  - 新增 `test_qml_closure_does_not_include_svg`：`_qt_module_closure` 是纯 C 层依赖，Qml 不含 Svg
  - 新增 `test_expand_closure_qml_includes_svg`：`expand_closure({"Qml"})` 含 Svg 及其闭包
  - 新增 `test_expand_closure_widgets_excludes_svg`：非 QML 项目不自动加 Svg
  - 新增 `test_qt_qml_svg_auto_binding`：端到端，QML 项目 qsvg.dll + Qt5Svg.dll 都保留
  - 新增 `test_qt_widgets_no_svg_strips_qsvg`：端到端，Widgets 项目两者都剥离
- `docs/changelog.rst`（修改）：新增 fix(slim) 条目说明 QML 自动绑定 QtSvg
- `.trae/req/req-50-qml-svg-auto-binding.md`（新增）：需求记录

## 关键决策与依据

### 决策 1：Qml → Svg 放在 `expand_closure` 而非 `_qt_module_closure`

`_qt_module_closure` 是纯 C 层 DLL 链接依赖闭包（基于 dumpbin 验证的 DLL 导入表）。
`Qt5Qml.dll` 不链接 `Qt5Svg.dll`，所以不应放在 `_QT_MODULE_DEPS`。`Qml → Svg`
是运行时策略：`imageformats/qsvg.dll` 始终保留需 `Qt5Svg.dll` 配套，而 QML 无
`import QtSvg` 语法无法通过 AST 发现。放在 `expand_closure`（QtSlimSpec 方法层）
保持职责分层：`_qt_module_closure` 处理 C 层硬依赖，`expand_closure` 处理运行时策略。

### 决策 2：qsvg.dll 按 Svg 选择性保留而非始终保留

原逻辑 `imageformats` 始终保留（`frozenset()`），导致 `qsvg.dll` 保留但
`Qt5Svg.dll` 被剥离的矛盾。改为 `qsvg.dll` 按 `Svg` 子模块选择性保留后：
- QML 项目：`Qml` → 自动加入 `Svg` → qsvg.dll + Qt5Svg.dll 都保留 ✓
- Widgets 不用 SVG：`Svg` 不在闭包 → qsvg.dll + Qt5Svg.dll 都剥离 ✓（省体积，无矛盾）
- Widgets 用 SVG 显式 import：`Svg` 在闭包 → 两者都保留 ✓

### 决策 3：非 QML 项目不自动加 Svg

QML 项目无法显式声明 SVG 依赖（无 `import QtSvg` 语法），必须自动处理。
Widgets 项目可以显式 `import QtSvg` 或 `--keep-module PySide2:Svg`，不自动加
Svg 可避免所有 Qt 项目都带上 SVG 模块（约 200-300KB）。

### 决策 4：QtWidgets 始终保留不再按需裁剪

用户反馈 QML 项目打包后 `qtquickcontrolsplugin.dll`（QtQuick.Controls 1.x 插件）
加载失败，根因是该插件 C 层依赖 `Qt5Widgets.dll`，但 `Widgets` 不在闭包被剥离。
QtQuick.Dialogs（FileDialog/ColorDialog 等）内部 import Controls 1.x，是 Qt5 QML
常用模块。用户要求"QtWidgets 是最基本的依赖，一律不剥离"。

实现：`expand_closure` 中 `if subs: subs.add("Widgets")`，任何 Qt 模块在闭包中时
自动加入 `Widgets`。体积代价 `Qt5Widgets.dll` ~5MB，QML 项目本身较大可接受。
空闭包（无 Qt 模块在用）不加 Widgets，不影响纯非 Qt 项目。

## 代码实现情况

### `expand_closure` 修改

```python
if "Qml" in subs:
    subs.add("Svg")
closure = _qt_module_closure(subs)
subs.update(closure)
return subs
```

在调用 `_qt_module_closure` 前注入 `Svg`，使后续闭包计算包含 Svg 的传递依赖
（Gui/Core）。

### `classify_entry` 修改

在 `plugins` 分支的 `imageformats` 子目录处理中，对 `qsvg*.dll` 做特殊判断：

```python
if plugin_type == "imageformats":
    filename = parts[-1].lower()
    if filename.startswith("qsvg"):
        return ("shared", None) if "Svg" in keep_subs else ("exclude", None)
```

在 `if not deps:`（空依赖始终保留）之前拦截，使 qsvg.dll 走 Svg 选择性保留路径，
其他 imageformats 插件仍走原逻辑。

## 测试验证结果

- 26 个针对性测试全部通过（imageformats/qml/svg/expand_closure 相关）
- 全套门禁：2159 passed、12 skipped、coverage 95.69%、ruff format/check 0 errors、
  pyrefly 0 errors
- 既有 QML 测试（`test_qt_qml_dynamic_expansion` 等）无回归

## 遗留事项

无。

## 下一轮计划

无（需求 req-50 已全部完成）。等待用户下一步指示。
