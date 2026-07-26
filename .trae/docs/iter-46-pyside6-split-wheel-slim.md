# iter-46 PySide6 拆分 wheel 精简修复

## 需求清单

- [x] 诊断 PySide6 全量解压根因（拆分 wheel 顶层目录不匹配 whl_pkg）
- [x] `_detect_top_pkg` 增加回退匹配，识别拆分 wheel 的主包顶层目录
- [x] `_unpack_one_wheel` 用 `top_pkg` 查找 keep_subs，使拆分 wheel 共享主包保留集合
- [x] `SlimSpec` 新增 `is_fallback` 类属性，避免回退匹配误识别辅助目录
- [x] 新增 5 个测试覆盖拆分 wheel 场景（含 numpy.libs 回归）
- [x] 全套门禁通过

## 迭代目标

修复 PySide6 6.6+ 拆分 wheel（`pyside6`/`pyside6_essentials`/`pyside6_addons`）打包时全量解压的问题。三个 wheel 内顶层目录均为 `PySide6`，但 `pyside6_essentials`/`pyside6_addons` 的文件名归一化包名（`pyside6-essentials`/`pyside6-addons`）与顶层目录（`pyside6`）不匹配，原 `_detect_top_pkg` 严格匹配失败返回 None，触发兜底全量解压。实际大体积 DLL/plugins/resources 全在 essentials/addons 中（合计 3780 文件，占 95%+ 体积），导致 PySide6 整体表现为全量解压。

## 改动文件清单

- [src/fspack/slim/base.py](../../src/fspack/slim/base.py)
  - `SlimSpec` 新增 `is_fallback: bool = False` 类属性
  - `_detect_top_pkg`：whl_pkg 严格匹配失败时，回退到第一个能匹配**非兜底** spec 的顶层目录
  - `_unpack_one_wheel`：签名改为接收 `merged` 字典，用 `normalize_name(top_pkg)` 查找 keep_subs（替代原来的 `whl_pkg` 查找）
  - `slim_unpack` 主循环：传递 `merged` 给 `_unpack_one_wheel`，不再在循环外查找 keep_subs
- [src/fspack/slim/default.py](../../src/fspack/slim/default.py)
  - `DefaultSlimSpec.is_fallback = True`（覆盖基类 False）
- [tests/test_slim.py](../../tests/test_slim.py)：新增 5 个测试
  - `test_split_wheel_essentials_shared_keep_subs`：essentials wheel 共享主包 keep_subs
  - `test_split_wheel_addons_shared_keep_subs`：addons wheel 共享主包 keep_subs
  - `test_split_wheel_no_keep_subs_still_excludes`：keep_subs 为空时仍应用剥离规则
  - `test_split_wheel_multi_wheel_share_keep_subs`：三个 wheel 同时精简
  - `test_split_wheel_numpy_libs_not_matched`：numpy.libs 辅助目录不被回退匹配（回归）

## 关键决策与依据

### 1. 回退匹配仅识别非兜底 spec

`_detect_top_pkg` 回退匹配时检查 `spec.is_fallback`，仅记录非兜底 spec 匹配的顶层目录。依据：numpy wheel 的 `numpy.libs/` 辅助目录归一化为 `numpy-libs`，仅匹配 `DefaultSlimSpec`（兜底），不应被回退识别为 top_pkg。若回退匹配也接受兜底 spec，则 `numpy.libs` 会被误识别为 top_pkg，导致 numpy 主包走错 spec（DefaultSlimSpec 而非 NumpySlimSpec，虽然当前两者行为一致，但破坏了 spec 路由的语义）。

### 2. 用 is_fallback 类属性而非导入 DefaultSlimSpec

`base.py` 不能导入 `default.py`（`default.py` 已导入 `base.py`，会循环导入）。在 `SlimSpec` 基类定义 `is_fallback` 类属性（默认 False），`DefaultSlimSpec` 覆盖为 True，避免循环导入且语义清晰。

### 3. keep_subs 查找从 slim_unpack 移到 _unpack_one_wheel

原设计在 `slim_unpack` 主循环中用 `whl_pkg`（wheel 文件名归一化包名）查找 keep_subs，对拆分 wheel 失效。改为在 `_unpack_one_wheel` 内部用 `normalize_name(top_pkg)`（实际顶层目录归一化名）查找，使 `pyside6_essentials` wheel 检测到 top_pkg=`PySide6` 后，用 `pyside6` 查找 `merged["pyside6"]`，正确共享主包保留集合。

## 代码实现情况

### `_detect_top_pkg` 回退匹配逻辑

```python
def _detect_top_pkg(zf, whl_pkg):
    fallback: str | None = None
    for name in zf.namelist():
        top = name.split("/")[0]
        if top.endswith(".dist-info"):
            continue
        if normalize_name(top) == whl_pkg:
            return top  # 严格匹配优先
        if fallback is None:
            spec = get_spec(normalize_name(top))
            if not spec.is_fallback:
                fallback = top  # 回退：非兜底 spec 匹配
    return fallback
```

### `_unpack_one_wheel` 用 top_pkg 查找 keep_subs

```python
def _unpack_one_wheel(whl, dest, whl_pkg, merged):
    with zipfile.ZipFile(whl) as zf:
        top_pkg = _detect_top_pkg(zf, whl_pkg)
        if top_pkg is None:
            zf.extractall(dest)
            return
        keep_subs = merged.get(normalize_name(top_pkg), set())  # 用 top_pkg 而非 whl_pkg
        _slim_extract(zf, dest, top_pkg, keep_subs)
```

## 整合优化情况

- 无重复代码新增
- `_unpack_one_wheel` 签名变更（`keep_subs` → `merged`），同步更新 `slim_unpack` 调用方
- 测试覆盖严格匹配优先级、回退匹配、辅助目录排除、空 keep_subs、多 wheel 共享 5 个场景

## 测试验证结果

- ruff check：All checks passed
- ruff format --check：47 files already formatted
- pyrefly check：0 errors
- pytest（非 slow）：956 passed, 21 deselected
- 覆盖率：97.10%（≥ 95%），slim/base.py 100%，slim/default.py 100%，slim/qt.py 100%

## RimSort 预期精简效果

修复后 RimSort 打包 PySide6 应保留闭包 `{Core, Gui, Widgets, Network, Positioning, WebChannel, WebEngineCore, WebEngineWidgets}`：
- 保留 8 个 Qt6*.dll + 8 个 .pyi + `icudtl.dat` + `QtWebEngineProcess.exe` + 基础 plugins
- 剥离 translations/include/metatypes/glue/support/scripts/doc/QtAsyncio/qml/lib/cmake 等目录
- 剥离 80+ 个未用 Qt6*.dll、60+ 个 .pyi、12 个 .exe 工具、FFmpeg DLL、opengl32sw.dll、pyside6qml.abi3.dll
- 预计体积从 ~350MB 降到 ~80MB

## 遗留事项

- 待用户实际重新打包 RimSort 验证精简效果（需清除 `dist/runtime/Lib/site-packages/PySide6/` 后重新构建）

## 下一轮计划

无。本次修复闭环完成，待用户验证。
