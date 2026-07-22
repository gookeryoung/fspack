# 迭代 24：科学库精简规则与示例

## 迭代目标

为 numpy/matplotlib/scipy 等科学库补充 slim 精简规则与示例项目，验证
科学库在 embed python 下打包可用，并进一步压缩产物体积。

- 扩展 `_default_classify` 支持「任意层级嵌套剥离」（含跨包），用于处理
  scipy 各子模块下的 `tests/`、matplotlib 跨包 `mpl_toolkits/tests/`
- 新增 `MatplotlibSlimSpec`/`ScipySlimSpec`，在 `slim/__init__.py` 注册
- 新增 3 个示例项目（`sci_numpy`/`sci_matplotlib`/`sci_scipy`）与 slow 测试

## 需求清单

参见 `req-23-sci-slim.md`。

## 改动文件清单

### 核心实现

- `src/fspack/slim/base.py`：`_default_classify` 新增 `nested_excludes`
  参数，在 metadata 检查后、跨包 shared 检查前插入「任意层级剥离」逻辑；
  不再调用 `_classify_top_or_meta`（内联 metadata + 跨包检查，QtSlimSpec
  仍用 `_classify_top_or_meta`）；加 `# noqa: PLR0911`（return 数超限，
  与 `QtSlimSpec.classify_entry` 同风格）
- `src/fspack/slim/libs.py`：新增 `MatplotlibSlimSpec`（剥离 `sphinxext`
  + 跨包/嵌套 `tests`）、`ScipySlimSpec`（剥离嵌套 `tests`）；提取
  `_NESTED_TEST_DIRS` 常量供两个 spec 共享
- `src/fspack/slim/__init__.py`：注册 `MatplotlibSlimSpec`/`ScipySlimSpec`
  （在 `LxmlSlimSpec` 前、`DefaultSlimSpec` 前），导出到公共 API

### 示例

- `examples/sci_numpy/numpy_demo.py`（新增）：numpy 数组运算（广播/矩阵乘/
  统计聚合/linalg.det），打印 `numpy demo ok`
- `examples/sci_numpy/pyproject.toml`（新增）：声明 numpy 依赖
- `examples/sci_matplotlib/matplotlib_demo.py`（新增）：Agg 后端绘制直方图
  并保存 PNG（无需 GUI），打印 `matplotlib demo ok`
- `examples/sci_matplotlib/pyproject.toml`（新增）：声明 matplotlib + numpy
- `examples/sci_scipy/scipy_demo.py`（新增）：scipy linalg.solve +
  optimize.minimize（Rosenbrock）+ sparse.eye，打印 `scipy demo ok`
- `examples/sci_scipy/pyproject.toml`（新增）：声明 numpy + scipy

### 测试

- `tests/test_slim.py`：新增 `TestMatplotlibSlimSpec`（12 个）、
  `TestScipySlimSpec`（9 个）、`TestNestedExcludesBehavior`（4 个）；
  `TestSlimSpecRegistry` 新增 `test_get_spec_sci_libs`、
  `test_classify_entry_dispatches_to_matplotlib`/`_scipy`
- `tests/test_e2e_slow.py`：新增 3 个 slow 测试
  （`test_build_and_run_sci_numpy`/`_matplotlib`/`_scipy`），断言输出
  子串 + 验证精简剥离生效（f2py/sphinxext/嵌套 tests 不解包）

## 关键决策与依据

### 1. nested_excludes 设计：任意层级 + 含跨包

原有 `COMMON_EXCLUDE_SUBDIRS` 仅检查 `parts[1]`（二级目录），无法处理：

- `scipy/linalg/tests/`（parts[1]=linalg 非 tests，三级嵌套）
- `mpl_toolkits/tests/`（parts[0]=mpl_toolkits ≠ top_pkg=matplotlib，
  跨包提前返回 shared）

`nested_excludes` 在 metadata 检查后、跨包 shared 检查前遍历 `parts[1:]`，
任意层级命中即剥离。刻意跳过 `parts[0]`（顶层包名），避免误伤名为 `tests`
的顶层包（极端情况，由 `test_nested_excludes_not_affect_top_pkg_name`
覆盖）。

`nested_excludes` 默认空集，向后兼容：现有 `NumpySlimSpec`/`LxmlSlimSpec`
不传此参数，行为不变（由 `test_nested_excludes_empty_no_strip` 验证）。

### 2. matplotlib 专属剥离：sphinxext + 嵌套 tests

matplotlib wheel 含跨包目录 `mpl_toolkits/`（独立顶层包）、
`matplotlib.libs/`（共享 DLL）、`pylab.py`（顶层模块）。运行时保留：
`mpl-data/`（字体/样式）、`backends/`、`matplotlib.libs/`、
`mpl_toolkits/`（非 tests）、`pylab.py`。剥离：

- `sphinxext`：Sphinx 文档构建扩展（运行时不需要）
- `tests`（嵌套）：`mpl_toolkits/tests/`、`mpl_toolkits/mplot3d/tests/`、
  `matplotlib/tests/`（后者与 `COMMON_EXCLUDE_SUBDIRS` 冗余但无害）

### 3. scipy 专属剥离：嵌套 tests

scipy 各子模块（`linalg`/`fft`/`optimize`/`stats`/...）下均含 `tests/`，
约占 scipy 总体积 10-15%。仅靠 `nested_excludes={"tests"}` 即可剥离
`scipy/<sub>/tests/` 与 `scipy/<sub>/<deep>/tests/`（如
`scipy/fft/_pocketfft/tests/`）。运行时保留 `scipy/_lib/`（内部库）、
`scipy/<sub>/`（非 tests 部分）、`scipy.libs/`（共享 DLL）。

### 4. _default_classify 内联 metadata/跨包检查

原 `_default_classify` 调用 `_classify_top_or_meta` 处理 metadata 与跨包
shared。新增 `nested_excludes` 需在这两类检查之间插入，无法复用
`_classify_top_or_meta`（它会一次性返回 shared）。改为内联 metadata +
nested + 跨包检查，`_classify_top_or_meta` 仍供 `QtSlimSpec.classify_entry`
使用（Qt 不需要 nested 剥离）。

### 5. matplotlib Agg 后端（非 GUI）

`sci_matplotlib` 用 `matplotlib.use("Agg")` 非交互后端，无需 GUI 即可
`savefig` 生成 PNG。打包后无显示环境（wine/CI）可运行，避免 PySide2/PyQt5
在 wine 上缺系统 DLL 的问题。numpy 随 matplotlib 一起打包（依赖）。

### 6. _NESTED_TEST_DIRS 常量提取

matplotlib 与 scipy 都需剥离嵌套 `tests`，提取为 `_NESTED_TEST_DIRS`
模块级常量。两处相似即提取，符合 rule-01「三处相似才考虑提取」的最低
阈值（两处共享且语义明确）。

### 7. slow 测试 timeout 加大

numpy/scipy wheel 体积大（scipy 约 30MB+），下载与解压耗时。timeout
从默认 240 秒提升至 600（numpy/matplotlib）/900（scipy）秒。

## 代码实现情况

### _default_classify 扩展（base.py）

```python
@classmethod
def _default_classify(  # noqa: PLR0911
    cls, entry, top_pkg, keep_subs,
    extra_excludes=frozenset(),
    nested_excludes=frozenset(),
) -> tuple[str, str | None]:
    parts = entry.split("/")
    if parts[0].endswith(".dist-info"):
        return ("metadata", None)
    if nested_excludes:
        for part in parts[1:]:
            if part in nested_excludes:
                return ("exclude", None)
    if parts[0] != top_pkg:
        return ("shared", None)
    # 顶层文件 / 子目录分类（同原逻辑）
    ...
```

### MatplotlibSlimSpec / ScipySlimSpec（libs.py）

```python
_NESTED_TEST_DIRS = frozenset({"tests"})

class MatplotlibSlimSpec(SlimSpec):
    _EXTRA_EXCLUDES = frozenset({"sphinxext"})
    # classify_entry 委托 _default_classify(extra_excludes, _NESTED_TEST_DIRS)

class ScipySlimSpec(SlimSpec):
    # classify_entry 委托 _default_classify(frozenset(), _NESTED_TEST_DIRS)
```

### slim/__init__.py 注册顺序

```python
register_spec(QtSlimSpec)
register_spec(NumpySlimSpec)
register_spec(MatplotlibSlimSpec)
register_spec(ScipySlimSpec)
register_spec(LxmlSlimSpec)
register_spec(DefaultSlimSpec)  # 兜底，最后注册
```

## 整合优化情况

- **OCP 扩展**：新增 matplotlib/scipy 精简规则只新增 spec 子类 + 注册，
  不改分发逻辑（沿用 iter-23 设计）
- **nested_excludes 通用增强**：基类方法扩展，未来 pandas/astropy 等含
  嵌套 tests 的科学库可直接复用
- **_NESTED_TEST_DIRS 共享**：matplotlib/scipy 共用常量，避免重复字面量
- **测试结构对齐**：`TestMatplotlibSlimSpec`/`TestScipySlimSpec` 与既有
  `TestNumpySlimSpec`/`TestLxmlSlimSpec` 风格一致

## 测试验证结果

### 门禁

- `ruff check src tests`：All checks passed
- `ruff format --check src tests`：51 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack`：532 passed, 20 deselected，
  覆盖率 98.37%（slim/base.py 100%、slim/libs.py 100%、slim/__init__.py 100%）

### 新增测试

- `tests/test_slim.py`：
  - `TestMatplotlibSlimSpec`（12 个）：match/normalize/expand_closure/
    dist-info/runtime-subdir/sphinxext/matplotlib-tests/mpl_toolkits-tests/
    mpl_toolkits-runtime/matplotlib-libs/pylab/common/init-private
  - `TestScipySlimSpec`（9 个）：match/normalize/expand_closure/dist-info/
    nested-tests/runtime-subdir/scipy-libs/top-tests/common/init-private
  - `TestNestedExcludesBehavior`（4 个）：cross-pkg/deep-level/empty-no-strip/
    not-affect-top-pkg-name
  - `TestSlimSpecRegistry` 新增 3 个：get_spec_sci_libs、
    classify_entry_dispatches_to_matplotlib/_scipy
- `tests/test_e2e_slow.py`：新增 3 个 slow 测试，断言运行输出 +
  验证精简剥离生效（f2py/sphinxext/嵌套 tests 不解包，mpl_toolkits/_lib 保留）

## 遗留事项

- slow 测试依赖 wine + mingw，Windows 本地不执行（skip），需在 Linux CI 实跑
  验证 numpy/matplotlib/scipy wheel 真实下载与运行
- 未新增 pandas 专属 spec——pandas 走 `DefaultSlimSpec` 兜底，如未来需要
  剥离 pandas 嵌套 tests，可复用 `_NESTED_TEST_DIRS` 新增 `PandasSlimSpec`

## 下一轮计划

无。本迭代需求清单全部完成，门禁通过。
