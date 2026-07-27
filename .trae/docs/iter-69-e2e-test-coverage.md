# iter-69 examples 端到端集成测试完善

## 需求清单

- [x] 补充端到端测试覆盖：PySide2 QML 应用、Nuitka 编译（Windows + Linux）、
  用户自定义 slim-include 规则覆盖 spec 剥离

## 迭代目标

扩展现有 slow 端到端测试套件，覆盖此前未测试的关键场景：
PySide2 QML 应用打包（资源文件 + QtQml 模块依赖）、Nuitka 编译端到端
（Windows mingw + Linux gcc，验证 .pyd/.so 产物与 .py 剥离）、用户自定义
slim-include 规则覆盖 spec 默认剥离（numpy/distutils 强制保留）。

## 改动文件清单

- `tests/test_e2e_slow.py`：
  - 文件 docstring 补充 iter-69 扩展说明
  - 新增 `test_build_and_run_pyside2_qml_dashboard_py38`（PySide2 QML 应用）
  - 新增 `test_build_with_nuitka_compilation`（Windows Nuitka 编译）
  - 新增 `test_build_linux_with_nuitka`（Linux Nuitka 编译）
  - 新增 `test_build_with_slim_include_rule`（slim-include 规则端到端）

## 关键决策与依据

1. **PySide2 QML 测试用 keep_modules 显式保留 QtQml/QtQuickControls2**：
   AST 分析 QML 应用无法检出 QtQml 模块依赖（QML 在运行时通过 QmlApplicationEngine
   动态加载），需通过 `BuildOptions(keep_modules={...})` 显式保留，与
   `pyside2_app_py310` 同策略

2. **Nuitka 测试仅在原生平台运行**：Nuitka 无法交叉编译（`target` 必须等于
   `detect_platform()`），故 Windows Nuitka 测试用 mingw + wine，Linux Nuitka
   测试用 gcc 原生运行。交叉构建时 `pipeline._compile_user_sources` 自动跳过
   Nuitka 编译

3. **Nuitka 测试配合 pyc_strip=True**：验证 .py 源码被剥离（仅入口 helloworld.py
   保留，runpy.run_path 需要 .py 文件）。Nuitka 编译跳过入口文件（entry_rels
   传入 compile_with_stamp），故入口 .py 必须存在

4. **slim-include 测试用 numpy/distutils 作验证目标**：NumpySlimSpec 默认剥离
   `distutils`（已弃用构建工具），通过 `[tool.fspack] slim-include = ["numpy/distutils/*"]`
   强制保留。保留 distutils 不影响 numpy 运行（distutils 仅在 import numpy.distutils
   时加载，numpy 顶层 API 不依赖），测试同时验证应用仍能正常运行

5. **slim-include 测试跨平台兼容**：用 `detect_platform()` 分支选择 Windows
   (mingw + wine) 或 Linux (gcc + 原生运行) 路径，单测试覆盖双平台

## 代码实现情况

- `test_e2e_slow.py` 测试数：21 → 25（+4 新增）
- 全部标 `@pytest.mark.slow`，默认门禁不执行
- 总测试数：1047（非 slow）+ 25（slow）= 1072
- 总覆盖率：98.58%（非 slow，未变化）

## 测试验证结果

- ruff check：通过
- ruff format --check：通过（自动格式化 1 处）
- pyrefly check：0 errors
- pytest（非 slow）：1047 passed，覆盖率 98.58%（≥95%）
- pytest --collect-only：25 slow 测试全部 collect 成功

## 遗留事项

- Nuitka e2e 测试首次运行需下载 Nuitka + clang（~150MB），耗时 >5 分钟，
  仅在手动执行 `pytest -m slow` 时触发
- PySide2 QML 测试在 wine 下可能因系统 DLL 缺失跳过运行断言，仅验证构建产物
  （与现有 PySide2/PyQt5 测试同条件）
- slim-include 测试依赖 numpy wheel 下载（~30MB），首次执行耗时较长

## 下一轮计划

iter-70：架构文档与模块索引同步（更新 README 架构图、补充模块职责索引、
更新开发文档中的导入路径示例，含 iter-56~62 拆分后的新模块）
