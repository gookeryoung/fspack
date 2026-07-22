# 迭代 19：白名单制精简打包与 Qt 模块依赖闭包

## 迭代目标

将 Qt 库（PySide2/PySide6/PyQt5/PyQt6）的精简打包策略从黑名单（仅剥离
`.pyd`/`.pyi`/`.so`，`Qt5*.dll`/`Qt6*.dll` 全部保留）升级为白名单制：

- 基于项目代码 import 情况自动推导所需 `.pyd` 与 `Qt5/6*.dll`
- 自动计算 Qt 模块的 C 层传递依赖闭包（如 `import QtWidgets` 自动加入
  `Gui`/`Core`），用户无需显式 `import PySide2.QtGui/QtCore` 或使用
  `--keep-module` 声明 C 层依赖
- 非必要目录（examples/translations/include/typesystems/glue/support/scripts/doc）
  始终剥离；plugins/resources/qml 按依赖映射选择性保留

## 需求清单

- [x] 优化 pyside 等常用库的打包精简策略
- [x] 采取白名单制确定最小基本依赖
- [x] 结合项目代码的引入情况，逐步加入所需的相关 pyd 和 dll 文件
- [x] Qt 模块 C 层传递依赖自动推导（闭包计算）
- [x] 用户无需显式声明 C 层依赖或 `--keep-module`
- [x] 非必要目录与开发工具 exe 始终剥离
- [x] plugins/resources/qml 按依赖映射选择性保留
- [x] 测试覆盖闭包计算与白名单分类，门禁通过

## 改动文件清单

### 核心实现

- `src/fspack/slim.py`：
  - 新增 `_QT_MODULE_DEPS` 映射（60+ Qt 子模块的 C 层直接依赖，归一化名），
    覆盖核心三件套、网络、数据格式、多媒体、OpenGL、QML/Quick、3D、可视化、
    Web、设备/位置、脚本等分类
  - 新增 `_normalize_qt_sub(stem)` 函数：统一 `QtCore`/`Qt5Core`/`Qt6Core` →
    `Core`，`Qt3DCore` → `3DCore`，非 Qt 前缀原样返回
  - 新增 `_qt_dll_submodule(stem)` 函数：Qt 原生 DLL 文件名提取子模块名
    （`Qt5Core` → `Core`），非 Qt5/Qt6 前缀返回 None
  - 新增 `_qt_module_closure(submodules)` 函数：迭代计算 Qt 子模块集合的
    传递依赖闭包（如 `{Widgets}` → `{Widgets, Gui, Core}`），未知模块原样
    保留不触发额外依赖推导
  - 修改 `classify_entry`：Qt 库 `Qt5/6*.dll` 从原 shared（全部保留）改为
    submodule（按子模块选择性保留）；非 Qt 库 `.pyd` 按原始文件名归类不归一化
  - 修改 `slim_unpack`：合并 `submodule_usage`（AST）与 `keep_modules`（用户
    显式）后，对 Qt 绑定包应用 `_qt_module_closure` 自动加入 C 层依赖子模块
  - 更新 docstring 反映白名单+闭包机制

### 测试

- `tests/test_slim.py`：
  - 修改 `test_selective_unpack`：新增 `Qt5Network.dll` 验证剥离，QtGui.pyd/
    Qt5Gui.dll 改为闭包自动保留
  - 新增 `TestQtModuleClosure` 类（8 个测试）：覆盖 Core only、Widgets 闭包、
    Quick 传递依赖、3DExtras 传递依赖、未知模块保留、混合已知未知、空集合、
    幂等性
  - 新增 `TestQtDllClassification` 类（7 个测试）：覆盖 `_qt_dll_submodule`
    与 `_normalize_qt_sub` 全分支
- `tests/test_builder.py`：
  - 更新 `test_unpack_wheels_with_submodule_usage` 断言：`Qt5Gui.dll` 与
    `QtGui.pyd` 由"剥离"改为"闭包自动保留"（用户 import QtCore/QtWidgets，
    闭包自动加入 Gui）

## 关键决策与依据

### 1. 白名单制：Qt5/6*.dll 按子模块选择性保留

原黑名单策略仅剥离 `.pyd`/`.pyi`/`.so`，`Qt5*.dll` 全部保留——但 Qt5 原生
DLL 体积可观（PySide2 wheel 含 40+ 个 Qt5*.dll），未用模块的 DLL 应剥离。
改为白名单制：`Qt5Core.dll` ↔ `Core`，仅保留闭包内子模块对应的 DLL。

### 2. Qt 模块依赖闭包：解决 C 层链接依赖无法静态发现

Qt 的 `.pyd`（Python 绑定）与 `Qt5*.dll`（C 原生库）间存在 C 层链接依赖——
`QtWidgets.pyd` 运行时需 `QtGui.pyd`/`QtCore.pyd`，但 AST 分析无法发现这种
依赖。引入 `_QT_MODULE_DEPS` 映射 + `_qt_module_closure` 迭代计算传递依赖
闭包：用户 `import QtWidgets` → 闭包自动加入 `Gui`/`Core`，对应 `.pyd` 与
`Qt5*.dll` 均保留。用户无需在代码中显式 `import PySide2.QtGui/QtCore` 或
使用 `--keep-module`。

### 3. 归一化名统一 Qt5/Qt6/PySide2/PySide6/PyQt5/PyQt6

`QtCore`/`Qt5Core`/`Qt6Core` 统一为 `Core`，`Qt3DCore`/`Qt53DCore` 统一为
`3DCore`。这样同一份 `_QT_MODULE_DEPS` 映射可同时服务 Qt5 与 Qt6 绑定，且
PySide2/PySide6/PyQt5/PyQt6 共享同一闭包计算逻辑。

### 4. 未知模块安全兜底

`_qt_module_closure` 对不在 `_QT_MODULE_DEPS` 映射中的模块名原样保留在闭包
结果中，但不触发额外依赖推导。这保证未来 Qt 新增模块或映射未覆盖场景下，
至少保留用户显式 import 的子模块，避免误剥离导致运行时 `ImportError`。

### 5. 非 Qt 库 .pyd 不归一化

`classify_entry` 中 `.pyd` 归一化仅对 Qt 库（`_QT_PACKAGES` 内）生效，非 Qt
库（如 numpy）按原始文件名归类为 shared 或 submodule。避免误把 numpy 的
`_core/multiarray.pyd` 归一化为 `core/multiarray` 影响分类。

### 6. plugins/resources/qml 按依赖映射选择性保留

- `plugins/platforms`/`imageformats`/`styles` 等基础功能始终保留
- `plugins/mediaservice` 需 `Multimedia`、`plugins/sqldrivers` 需 `Sql` 等
- `resources/` 仅 WebEngine 相关子模块时保留（约 15MB）
- `qml/` 仅 Qml/Quick 相关子模块时保留（约 21MB）
- 未知 `plugins/` 子目录白名单制剥离

## 代码实现情况

### Qt 模块依赖映射（slim.py）

```python
_QT_MODULE_DEPS: dict[str, frozenset[str]] = {
    "Core": frozenset(),
    "Gui": frozenset({"Core"}),
    "Widgets": frozenset({"Gui", "Core"}),
    "Network": frozenset({"Core"}),
    "Multimedia": frozenset({"Gui", "Core", "Network"}),
    "Quick": frozenset({"Qml", "Gui", "Core"}),
    "3DExtras": frozenset({"3DRender", "3DInput", "3DLogic", "3DCore", "Gui", "Core"}),
    # ... 60+ Qt 子模块
}
```

### 闭包计算（slim.py）

```python
def _qt_module_closure(submodules: set[str]) -> set[str]:
    """计算 Qt 子模块集合的传递依赖闭包（归一化名）。"""
    closure = set(submodules)
    changed = True
    while changed:
        changed = False
        for mod in list(closure):
            deps = _QT_MODULE_DEPS.get(mod)
            if not deps:
                continue
            new_deps = deps - closure
            if new_deps:
                closure.update(new_deps)
                changed = True
    return closure
```

### slim_unpack 应用闭包（slim.py）

```python
# Qt 库：按 Qt 模块依赖映射计算传递依赖闭包，自动加入 C 层依赖子模块
# 例如用户 import QtWidgets → 闭包自动加入 Gui/Core，保留对应 .pyd 与 Qt5/6*.dll，
# 用户无需在代码中显式 import PySide2.QtGui/QtCore 或 --keep-module 声明 C 层依赖
for pkg, subs in merged.items():
    if pkg in _QT_PACKAGES:
        subs.update(_qt_module_closure(subs))
```

### classify_entry Qt5/6*.dll 白名单分类（slim.py）

```python
if is_qt and suffix == ".dll":
    # Qt5Xxx.dll/Qt6Xxx.dll 按子模块选择性保留，其他 .dll 始终保留
    qt_sub = _qt_dll_submodule(stem)
    if qt_sub is not None:
        return ("submodule", qt_sub)
    return ("shared", None)
```

## 整合优化情况

- **用户代码简化**：原需在示例代码中显式 `import PySide2.QtGui`/`QtCore` 声明
  C 层依赖（iter-14 引入的约定），现闭包自动推导，用户代码只需 import 实际
  使用的模块
- **`--keep-module` 仍保留**：作为显式覆盖入口，用于 AST 无法发现或闭包映射
  未覆盖的场景（向后兼容）
- **安全兜底**：纯顶层 import（无 `包.子模块` 形式）→ 保留集合为空 → 全量
  解压；wheel 顶层目录与包名不匹配 → 全量解压；wheel 文件名不可解析 →
  全量解压
- **归一化统一**：Qt5/Qt6/PySide2/PySide6/PyQt5/PyQt6 共享同一份
  `_QT_MODULE_DEPS` 映射与闭包计算逻辑，无需为每个 Qt 版本维护独立映射

## 测试验证结果

### 门禁

- `ruff check`：All checks passed
- `ruff format --check`：All files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack`：461 passed，覆盖率 98.09%（slim.py 99%）

### 新增测试

- `tests/test_slim.py`：
  - `TestQtModuleClosure`（8 个）：core_only、widgets_closure、
    quick_transitive、qt3d_extras_transitive、unknown_module_kept、
    mixed_known_unknown、empty_set、idempotent
  - `TestQtDllClassification`（7 个）：qt5core_to_core、qt6widgets_to_widgets、
    qt5_3d_animation、non_qt_dll_returns_none、normalize_qtcore、
    normalize_qt3dcore、normalize_non_qt
- `tests/test_slim.py::TestClassifyEntry`：新增 Qt5/6*.dll 归一化分类测试
- `tests/test_builder.py::test_unpack_wheels_with_submodule_usage`：断言更新

### 手动验证

- pyside2_app 构建后 `PySide2` 目录仅保留闭包内子模块的 `.pyd` 与
  `Qt5*.dll`，未用模块（如 Network/Multimedia）的 `.pyd`/`Qt5*.dll` 被剥离
- `fsp r --debug` 输出 "hello from PySide2" 无 `ImportError: DLL load failed`

## 遗留事项

- Qt 示例代码（pyside2_app/gui_calc/pyqt5_cli）中显式 `import X.QtGui`/
  `import X.QtCore` 声明 C 层依赖的约定（iter-14 引入）现已非必需——闭包
  自动推导。可后续清理这些显式 import，但保留无害且语义清晰，本迭代未清理。
- `_QT_MODULE_DEPS` 映射基于 Qt 5.15/6.x 文档整理，未来 Qt 新增模块需手动
  补充。未知模块安全兜底保证至少保留用户显式 import 的子模块。
- slow 端到端测试未新增白名单策略验证用例（依赖工具链且耗时），本迭代用
  单元测试 + 手动验证覆盖。

## 下一轮计划

无。本迭代需求清单全部完成，门禁通过。
