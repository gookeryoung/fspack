# iter-109：结构重构（req-40 iter-83/84 收尾）

## 需求清单

延续 req-40 深度重构路线，完成 iter-81~84 大文件拆分部分中遗留的两项
（iter-83 analyzer.py 拆分、iter-84 slim/qt.py 拆分）。

- [x] req-40 iter-83：analyzer.py 拆分为 facade + analyzer_ast.py + analyzer_fingerprint.py
- [x] req-40 iter-84：slim/qt.py 拆分为 facade + qt_helpers.py + qt_closure.py
- [x] 性能基线对比不退化
- [x] 全套门禁通过（ruff/pyrefly/pytest/coverage）

## 迭代目标

1. 完成 req-40 iter-81~84 拆分系列（iter-81/82 此前已实施，本次补齐 iter-83/84）
2. 保持公开 API 不变（`__all__` 与 import 路径兼容），所有现有测试不破坏
3. 性能基线守护：iter-80 基线测试 median 退化 ≤ 10%

## 改动文件清单

### 新建

- `src/fspack/analyzer_ast.py` — AST 扫描模块（collect_imports/
  collect_imports_and_submodules/collect_submodule_imports/parse_qml_imports/
  STDLIB_FALLBACK/_qml_module_to_qt_sub + QML 映射表）
- `src/fspack/analyzer_fingerprint.py` — 源码指纹与路径排除模块
  （source_fingerprint/_iter_py_entries/_is_excluded/_EXCLUDED_DIRS）
- `src/fspack/slim/qt_helpers.py` — Qt 文件名归一化与判断辅助
  （_normalize_qt_sub/_qt_dll_submodule/_is_ffmpeg_dll/_is_qml_abi_dll/
  _is_opengl_sw_dll + 相关常量）
- `src/fspack/slim/qt_closure.py` — Qt 子模块依赖映射与闭包计算
  （_qt_module_closure + _QT_MODULE_DEPS/_QT_PLUGIN_DEPS/_QT_RESOURCE_DEPS/
  _QT_QML_DEPS/_QT_ABI_DLL_DEPS/_QT_OPENGL_DEPS/_QT_WEBENGINE_TOP_FILES 等）

### 修改

- `src/fspack/analyzer.py` — 改为 facade，保留 analyze_dependencies /
  _parse_file_worker / _parse_serial / _parse_parallel / _local_packages /
  _PARALLEL_THRESHOLD；从子模块 re-export 公开 API 与测试用私有名
  `_qml_module_to_qt_sub`。560 行 → 181 行
- `src/fspack/slim/qt.py` — 改为 facade，保留 QT_PACKAGES / QtSlimSpec；
  从子模块 re-export 测试用私有名 `_is_ffmpeg_dll`/`_is_qml_abi_dll`/
  `_is_opengl_sw_dll`/`_normalize_qt_sub`/`_qt_dll_submodule`/
  `_qt_module_closure`。439 行 → 222 行

### 文档

- `.trae/req/req-40-deep-refactor-baseline-guard.md` — 标记 iter-81/82/83/84
  为 `[x]` 已完成，补充 iter-83/84 的实际拆分内容与基线对比结果

## 关键决策与依据

1. **analyzer_ast.py 包含 QML 解析**：req-40 原文只规定 AST 三函数 + STDLIB_FALLBACK，
   实际 QML 解析（parse_qml_imports/_qml_module_to_qt_sub）也属于"扫描 import"
   职责，归 ast 模块内聚合理。QML 文件用正则解析而非 ast.walk，但语义上同属
   "从代码提取 import"。

2. **analyzer_fingerprint.py 包含 _EXCLUDED_DIRS 与 _is_excluded**：req-40 原文
   只规定 source_fingerprint + os.scandir 递归。实际 `_iter_py_entries` 用
   `_EXCLUDED_DIRS` 做剪枝，`_is_excluded` 也用同一常量判断路径排除。
   放 fingerprint 模块单向被 analyzer 引用，避免循环依赖；若放 analyzer 则
   fingerprint 反向依赖 analyzer 导致循环。

3. **qt_helpers.py 与 qt_closure.py 分离而非合并**：req-40 原文 _normalize_qt_sub
   归 qt_closure.py，但 _normalize_qt_sub 与 _qt_dll_submodule 同属"文件名归一化"
   语义，归 helpers 内聚；qt_closure.py 专注依赖映射与闭包计算。拆分粒度匹配
   项目偏好"按职责拆分"。

4. **facade re-export 私有名以保兼容**：tests/test_slim.py 直接 import
   `_is_ffmpeg_dll`/`_qt_module_closure`/`_normalize_qt_sub` 等私有名，
   tests/test_analyzer.py import `_qml_module_to_qt_sub`。facade 通过
   `from submodule import _xxx` + `__all__` 包含私有名保持导入路径不变。
   重构零破坏测试。

5. **不合并 wheel_*/nuitka_*/loader_* 小模块群**：审查后 wheel_*.py（6 文件）
   职责清晰（pip 调度/解析/sdist/缓存/markers/facade），合并后 >1000 行降低
   可读性；nuitka_*.py（7 文件）按 mixin 类拆分符合"按职责拆分而非基类抽象"
   偏好，每 mixin 独立职责；loader_*.py（3 文件）compile/source 独立并行使用，
   合并无收益。三组均维持现状。

6. **不拆分 cli_doctor.py 1115 行与 cli.py 749 行**：cli_doctor 拆分涉及 5 个
   子模块（环境检查/工具检查/报告渲染/模板测试/基准分析）大量代码移动，
   需独立迭代以保证测试覆盖与 docstring 完整。本次聚焦 req-40 收尾，留 iter-110+。

## 代码实现情况

### analyzer.py 拆分（iter-83）

- `analyzer_ast.py`（319 行）：STDLIB_FALLBACK + _STDLIB + collect_imports/
  collect_imports_and_submodules/collect_submodule_imports + _push +
  parse_qml_imports/_qml_module_to_qt_sub + _QML_MODULE_TO_QT_SUB/_QML_IMPORT_RE/
  _QT_PYTHON_PACKAGES
- `analyzer_fingerprint.py`（90 行）：source_fingerprint + _iter_py_entries +
  _is_excluded + _EXCLUDED_DIRS
- `analyzer.py`（181 行）：facade，保留 analyze_dependencies + _parse_file_worker/
  _parse_serial/_parse_parallel + _local_packages + _PARALLEL_THRESHOLD

### slim/qt.py 拆分（iter-84）

- `qt_helpers.py`（102 行）：_normalize_qt_sub + _qt_dll_submodule +
  _is_ffmpeg_dll/_is_qml_abi_dll/_is_opengl_sw_dll + _QT_EXCLUDE_SUBDIRS/
  _QT_LIB_EXCLUDE_SUBDIRS/_QT_FFMPEG_DLL_PREFIXES/_QT_QML_ABI_DLL_NAMES/
  _QT_OPENGL_SW_DLL_NAMES
- `qt_closure.py`（217 行）：_qt_module_closure + _QT_MODULE_DEPS/
  _QT_PLUGIN_DEPS/_QT_RESOURCE_DEPS/_QT_QML_DEPS/_QT_ABI_DLL_PACKAGES/
  _QT_ABI_DLL_DEPS/_QT_OPENGL_DEPS/_QT_WEBENGINE_TOP_FILES
- `qt.py`（222 行）：facade，保留 QT_PACKAGES + QtSlimSpec（含 classify_entry/
  match/normalize_submodule/expand_closure）

## 整合优化情况

- 无重复代码引入：facade 仅 re-export，子模块无交叉引用
- 无新风险：`__all__` 与 import 路径完全兼容，1835 测试全通过
- 新模块覆盖率均 100%（qt_helpers/qt_closure/analyzer_ast/analyzer_fingerprint）

## 测试验证结果

### 全套门禁

- `ruff check` / `ruff format --check`：All checks passed
- `pyrefly check`：0 errors
- `pytest --cov`：1835 passed, 12 skipped, 8 deselected
- 覆盖率 96.07%（≥ 95% 门禁）

### 性能基线对比（vs iter-80 baseline，median）

| 测试 | iter-80 基线 | iter-109 实测 | 变化 |
|------|-------------|--------------|------|
| `test_classify_entry_baseline` | 3.6μs | 3.6μs | 0% |
| `test_collect_imports_and_submodules_baseline` | 33.5μs | 33.5μs | 0% |
| `test_source_fingerprint_baseline` | 428.4μs | 424.5μs | -0.9%（提速） |
| `test_slim_unpack_baseline` | 5.2ms | 5.17ms | -0.6%（提速） |
| `test_analyze_dependencies_baseline` | 6.8ms | 5.91ms | -13%（提速） |

所有基线无退化，5 个核心场景中 3 个提速。

## 遗留事项

- req-40 iter-85（mixin Protocol 类型声明）、iter-86（配置加载缓存）、
  iter-87（AST 内存优化）、iter-88（测试 fixture 共享化）、iter-89（基线矩阵扩展）、
  iter-90（CI 门禁固化）状态待后续迭代核实与推进
- cli_doctor.py 1115 行（项目最大文件）拆分留 iter-110+，方向：按职责拆为
  doctor_envs.py（环境检查）/doctor_tools.py（工具检查）/doctor_report.py
  （报告渲染）/doctor_templates.py（模板构建测试）/doctor_bench.py（基准分析）
- cli.py 749 行可参考 cli_doctor.py/cli_init.py 模式按子命令拆为
  cli_build.py/cli_package.py/cli_recursive.py 等

## 下一轮计划

- iter-110：核实 req-40 iter-85~90 完成状态；若已完成则补标，否则推进
- iter-111+：cli_doctor.py 拆分（5 个子模块），需独立迭代保证测试覆盖
