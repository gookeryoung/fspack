# 迭代 25：matplotlib ft2font 剥离修复

## 迭代目标

修复 `sci_matplotlib` 打包后运行失败的问题。经排查发现两层问题：

1. **ft2font C 扩展被误剥离**（核心 bug，已修复）：`MatplotlibSlimSpec` 复用
   `_default_classify`，将顶层 `.pyd`（如 `ft2font.cp311-win_amd64.pyd`）归为
   submodule 按需保留。但 `ft2font` 是 `matplotlib.__init__._check_versions()`
   的硬依赖（`from . import ft2font`），用户只 `import matplotlib.pyplot` 时
   `ft2font` 不在保留集合，被剥离，导致 `ImportError: cannot import name
   'ft2font'`。
2. **cp312 wheel 误选**（首次打包偶发，根因已定位）：`_parse_pip_download_wheels`
   解析 pip stdout 失败时回退到目录扫描，返回缓存中所有 wheel（含不匹配版本
   的 cp312 numpy），导致 cp311 运行时加载 cp312 wheel 失败。

## 需求清单

- [x] 定位 matplotlib 打包运行失败根因（ft2font 被剥离）
- [x] 扩展 `_default_classify` 新增 `top_ext_always_shared` 参数
- [x] `MatplotlibSlimSpec` 传 `top_ext_always_shared=True`，顶层 C 扩展始终保留
- [x] 新增 ft2font 场景与 `top_ext_always_shared` 行为测试
- [x] 重新打包验证 matplotlib 运行成功（输出 `matplotlib demo ok`）
- [x] 定位首次打包 cp312 误选根因（目录扫描回退）
- [x] 门禁通过（538 passed，覆盖率 98.37%）

## 改动文件清单

- `src/fspack/slim/base.py`：`_default_classify` 新增 `top_ext_always_shared`
  参数，顶层 `.pyd`/`.pyi`/`..so` 在该参数为 True 时归 shared（不归 submodule）；
  加 `# noqa: PLR0913`（参数数 6 > 5）
- `src/fspack/slim/libs.py`：`MatplotlibSlimSpec.classify_entry` 传
  `top_ext_always_shared=True`；docstring 补充 ft2font 硬依赖说明
- `tests/test_slim.py`：`TestMatplotlibSlimSpec` 新增
  `test_classify_top_pyd_always_shared`、`test_classify_top_pyi_always_shared`；
  新增 `TestTopExtAlwaysSharedBehavior`（4 个测试覆盖默认/submodule/shared/
  子目录行为）

## 关键决策与依据

### 1. top_ext_always_shared 设计

matplotlib 顶层有 6 个 `.pyd`：5 个以 `_` 开头（`_image`/`_path`/`_qhull`/
`_tri`/`_c_internal_utils`，本就归 shared），唯独 `ft2font` 不以 `_` 开头，被
`_default_classify` 归 submodule 按需剥离。

`top_ext_always_shared=True` 让顶层 `SUBMODULE_EXTS`（.pyd/.pyi/.so）全归 shared，
不做子模块选择性剥离。仅影响顶层 C 扩展，不影响子目录（子目录本就归 shared），
不影响 `.py` 文件（.py 本就归 shared）。

默认 False，向后兼容：`NumpySlimSpec`/`LxmlSlimSpec`/`ScipySlimSpec` 不传此参数，
行为不变。numpy 的硬依赖 C 扩展在 `_core/` 子目录（归 shared）或以 `_` 开头
（归 shared），不需要此参数。

### 2. cp312 误选根因（不修复，记录遗留）

验证结论：
- `pip download --no-index --abi cp311` **严格拒绝** cp312 wheel（移除 cp311
  numpy 后报 "No matching distribution"）
- `uv pip compile --python-version 3.11` **正确解析** cp311 版本（numpy 2.4.6）
- 缓存 key 含 py_version，3.11.9 与 3.12.0 的 key 不同，不跨版本命中

首次打包 cp312 误选的最可能路径：`_parse_pip_download_wheels(result.stdout)`
解析 pip stdout 失败（返回空列表）时，回退到目录扫描
`sorted(f.name for f in cache_dir.glob("*.whl"))`，返回缓存中**所有** wheel
（含 cp312 的 numpy-2.5.1、cp38 的 numpy-1.24.4），slim_unpack 解压了 cp312
wheel，运行时 cp311 加载 cp312 失败。

第二次打包（删除 dist 后）pip stdout 解析成功，正确返回 11 个 cp311 wheel。

## 测试验证结果

- `ruff check`：All checks passed
- `ruff format --check`：51 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack`：538 passed，覆盖率 98.37%
  （slim/base.py 100%、slim/libs.py 100%）
- 手动打包验证：`sci_matplotlib` 打包后运行输出
  `matplotlib 3.11.1` + `matplotlib demo ok: saved histogram.png size=9933`

## 遗留事项

- **目录扫描回退 bug**：`_parse_pip_download_wheels` 解析失败时回退到
  `cache_dir.glob("*.whl")` 返回所有 wheel（含不匹配 Python 版本/平台的），
  可能导致跨版本 wheel 污染。建议修复：回退时用 `WheelInfo` 按目标
  py_version/platform_tags 过滤，或直接报错而非回退。
- slow 测试（`test_build_and_run_sci_matplotlib`）依赖 wine+mingw，Windows
  本地 skip，需 Linux CI 实跑验证。

## 下一轮计划

无。本迭代修复完成，门禁通过。
