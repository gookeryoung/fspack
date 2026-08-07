# 需求：QML 项目自动绑定 QtSvg 支持

## 背景

QML 项目打包后运行时 SVG 图像加载失败，定位为缺少 QtSvg 支持。根因是 fspack
的 Qt 精简规则存在两处缺陷：

1. `plugins/imageformats` 目录"始终保留"（`_QT_PLUGIN_DEPS["imageformats"] =
   frozenset()`），其中 `qsvg.dll`（SVG 图片格式插件）被保留，但它的 C 层依赖
   `Qt5Svg.dll`/`Qt6Svg.dll` 按 `Svg` 子模块选择性保留。用户未 `import QtSvg`
   时 `Svg` 不在闭包 → `Qt5Svg.dll` 被剥离 → `qsvg.dll` 运行时加载失败。

2. QML 无 `import QtSvg` 语法（QtSvg 是 C++ 模块），`Image { source: "*.svg" }`
   通过图片格式插件系统加载 SVG，AST 扫描永远发现不了 SVG 依赖，`Svg` 永远
   不进闭包。用户即使想显式声明也无从下手。

## 需求清单

- [ ] QML 项目（`Qml` 在闭包中）自动把 `Svg` 加入依赖闭包，使 `Qt5Svg.dll`/
      `Qt6Svg.dll` 随之保留，无需用户显式 `import QtSvg`
- [ ] `plugins/imageformats/qsvg*.dll` 按 `Svg` 子模块选择性保留，消除"插件在
      但依赖 DLL 不在"的矛盾；非 SVG 项目剥离 `qsvg.dll` 省体积
- [ ] 其他 imageformats 插件（qjpeg/qgif/qico 等）仍始终保留，基础图片格式
      支持不受影响
- [ ] 全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）

## 验收标准

- `_qt_module_closure({"Qml"})` 结果含 `Svg`
- `QtSlimSpec.expand_closure({"Qml"})` 结果含 `Svg`
- `classify_entry("PySide2/plugins/imageformats/qsvg.dll", "PySide2", {"Svg"})`
  返回 `("shared", None)`
- `classify_entry("PySide2/plugins/imageformats/qsvg.dll", "PySide2")`（无 Svg）
  返回 `("exclude", None)`
- `classify_entry("PySide2/plugins/imageformats/qjpeg.dll", "PySide2")`（非 svg）
  仍返回 `("shared", None)`
- 既有 QML 项目测试（`test_qt_qml_dynamic_expansion` 等）不回归
