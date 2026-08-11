# iter-151: pyc.py 拆分为 pyc_compile/pyc_stamp/source_strip（+ runtime_trim）

## 需求清单
- [x] pyc.py（609 行）拆分为 < 300 行的内聚子模块
- [x] 保持 pyc.py facade：所有名字 re-export，兼容 monkeypatch
- [x] 409 全量测试通过 + perf baseline 无回归

## 迭代目标
1. 按职责将 pyc.py 拆分为 4 个内聚子模块
2. 保持 `monkeypatch.setattr("fspack.packaging.pyc.<name>", ...)` 全部兼容（尤其是 `subprocess` / `_WIN7_COMPAT_DLL_NAME` / `_COMPILEALL_TIMEOUT` 三大 patch 点）
3. `pipeline/runtime_stage.py` / `pipeline/compile_stage.py` 调用点无需修改

## 改动文件清单

### 新增模块
1. `fspack/packaging/pyc_stamp.py`（~40 行）
   - `_pyc_stamp_path(dist_dir)`：stamp 文件路径
   - `_pyc_stamp_key(src_dir, site_packages, strip_py, optimize, sp_optimize)`：指纹键（src_fp + sp_fp + 参数）

2. `fspack/packaging/pyc_compile.py`（~197 行）
   - 常量 `_COMPILEALL_TIMEOUT = 300.0`
   - `_run_compileall(py_exe, target_dir, optimize, stage)`：单次 compileall + 超时 + 失败处理
   - `_precompile_pyc(...)`：主入口（stamp 命中检查 → src+site-pkgs 分别编译 → 写 stamp → 条件源码剥离）
   - `_P(name, fallback)`：从 pyc facade 延迟 dispatch 名字

3. `fspack/packaging/source_strip.py`（~145 行）
   - `_strip_compiled_py(...)`：src + site-packages 分别调用 `_strip_py_sources`
   - `_strip_py_sources(...)`：rglob 遍历、跳过 __init__/入口/数据目录、PEP 3147 __pycache__ → legacy 迁移、unlink .py
   - `_is_in_data_dirs(path, data_dirs)`：数据资源目录树内判断（3.8 兼容 try/except relative_to）

4. `fspack/packaging/runtime_trim.py`（~340 行）
   - 常量：`_WIN7_COMPAT_DLL_NAME`、`_STDLIB_TRIM_DIRS`、`_STANDALONE_DEV_BIN_FILES`
   - `_needs_win7_compat_dll(py_version)` / `_inject_win7_compat_dll(runtime_dir)`
   - `_trim_stdlib(runtime_dir, py_version, target, stage)`：剥离 stdlib 目录
   - `_trim_standalone_runtime(...)`：strip libpython 符号 / 删 python3.X 二进制、include/share、Tcl/Tk
   - `_strip_elf_symbols(lib_path, platform)`：调用 strip（Linux --strip-all / macOS -x）
   - `_strip_tcl_tk_counted(python_dir)`：Tcl/Tk 动态库 + 脚本运行时剥离
   - `_P(name, fallback)`：从 pyc facade 延迟 dispatch（subprocess / _WIN7_COMPAT_DLL_NAME）

### 修改模块
5. `fspack/packaging/pyc.py`（63 行，原 609 行）—— facade + patch 兼容层
   - 显式 `import subprocess`（测试 patch `pyc.subprocess.run`）
   - 从 4 个子模块 re-export 全部 18 个公开名字，`__all__` 显式列出

## 关键决策与依据

### 决策 1：拆为 4 个而非计划中 3 个模块
计划写 3 个（pyc_compile/pyc_stamp/source_strip），但 Win7 DLL + runtime trim 有 340 行、与字节码编译无直接耦合，单独抽 runtime_trim。
- 依据：runtime_stage.py 仅导入 `_inject_win7_compat_dll` / `_needs_win7_compat_dll` / `_trim_standalone_runtime` / `_trim_stdlib`，这四个和 compile/source_strip 在调用方分属不同阶段，独立后导入链更清晰

### 决策 2：保留 pyc.py 作为 facade 并显式导入 subprocess
- 依据：测试 L2724/L2835/L2848/L2865/L2877（test_builder）通过 `setattr("pyc.subprocess.run", fake_run)` patch；
  若 pyc facade 不显式导入 subprocess 模块属性，patch 后子模块 dispatch 不到该对象

### 决策 3：_COMPILEALL_TIMEOUT 与 _WIN7_COMPAT_DLL_NAME 双存储 + dispatch
常量在各自子模块定义（作为 fallback 默认值），同时 re-export 到 pyc facade；运行时通过 `_P(name, fallback)` 从 facade 取值。
- 依据：测试直接修改 `pyc._COMPILEALL_TIMEOUT` 或 `pyc._WIN7_COMPAT_DLL_NAME` 后，子模块中必须立即看到修改后的值。直接 import 常量子模块会捕获原值，无法感知 patch

## 代码实现情况
- 拆分后规模：pyc_stamp 40 / source_strip 145 / pyc_compile 197 / runtime_trim 340 / pyc 63，全部 < 350 行
- `_precompile_pyc` 从 pyc_compile 直接调用 pyc_stamp 和 source_strip（无需 dispatch，这两个子模块无 patch 点），依赖清晰直接
- `_run_compileall` 中 `subprocess.TimeoutExpired` 回退用 `_default_subprocess.TimeoutExpired`（永远是真实的 subprocess 类，捕获它就足够），符合原逻辑
- `_strip_elf_symbols`、`_inject_win7_compat_dll` 通过 `_P` dispatch 常量与 subprocess

## 测试验证结果
```
专项（strip/win7/compileall/precompile）：60 passed, 3 skipped

全量回归：
  test_builder.py
  test_extras.py
  test_build_dry_run.py
  test_log_file.py
  test_profile.py
  test_nuitka.py
  test_site_packages.py
  test_build_perf_baseline.py
  —— 409 passed, 11 skipped in 5.18s

性能基线：
  warm small  4.16ms（baseline 一致）
  warm medium 4.61ms（baseline 一致）
  cold small  5.12ms（baseline 一致）
  cold medium 8.06ms（baseline 一致）
```

## 遗留事项
- 目前 pyc facade 包含 4 个子模块，但 runtime_trim 与 pyc（字节码）语义上无紧密关系，后续可考虑把 runtime_trim 移到 packaging 顶层或 packaging/runtime 子目录（当前为了最小化调用点修改，保留在 pyc facade 的 re-export 列表中）

## 下一轮计划
1. iter-152：runtime.py（预计 800+ 行）拆分为 download / extract / win7 三模块
2. 保持 runtime.py facade + 导入兼容（`STANDALONE_RELEASE_TAG`、`download_embed`、`extract_standalone` 等常量/函数）
3. 跑全量测试 + baseline 验证
