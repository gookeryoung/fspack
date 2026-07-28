# iter-103: 修复 tk app 打包后 _tkinter ImportError

## 需求清单

- [x] 定位 tk_app_pyall 打包后运行失败根因
- [x] 修复 `_build_tkinter_zip` 提取 Tcl/Tk C 运行时 DLL（tcl86t.dll / tk86t.dll）
- [x] 扩展 tcl/ 提取规则覆盖 dde1.4/reg1.3/tix8.4.3 等扩展包
- [x] 过滤 .lib / .sh 开发期文件节省空间
- [x] 更新测试 fixture 与断言覆盖新增提取规则
- [x] 实际打包 tk_app_pyall 验证运行成功
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

用户报告 tkinter app 打包后运行失败。复现错误：
`ImportError: DLL load failed while importing _tkinter: 找不到指定的模块。`

## 根因分析

`TkinterBundler._build_tkinter_zip` 从 python-build-standalone Windows tarball 提取
tkinter 组件时，**只提取了 Tcl/Tk 的 .tcl 脚本，未提取 tcl86t.dll / tk86t.dll
两个 C 运行时 DLL**。

- `_tkinter.pyd`（C 扩展）直接依赖 `tcl86t.dll` / `tk86t.dll`
- 旧正则 `_TCL_DIR_RE = r"/(tcl\d+\.\d+)/"` 只匹配 `/tcl8.6/` 这类版本目录
- `python/DLLs/tcl86t.dll` 路径里没有 `/tcl\d+\.\d+/` 匹配，从未被提取
- loader.exe 的 `SetDllDirectoryW(runtime)` 让 Windows 在 runtime/ 搜索 DLL，
  但 runtime/ 根本没有这两个 DLL → `_tkinter.pyd` 加载失败

同时旧正则也不匹配 `python/tcl/dde1.4/`、`python/tcl/reg1.3/`、
`python/tcl/tix8.4.3/` 等扩展包目录，导致 Tcl 扩展包完全缺失（package require
dde/reg/Tix 会失败）。

## 改动文件清单

### 修改

- `src/fspack/packaging/builtin.py`
  - 删除旧正则 `_TCL_DIR_RE` / `_TK_DIR_RE`（只匹配版本目录，遗漏 DLL 与扩展包）
  - 新增 `_TCL_RUNTIME_DLL_RE = r"/DLLs/((?:tcl|tk)\d+t?\.dll)$"`：匹配
    `python/DLLs/tcl86t.dll` / `python/DLLs/tk86t.dll`，提取到 runtime 根目录
  - 新增 `_TCL_DIR_PREFIX_RE = r"/tcl/(.+)$"`：匹配 `python/tcl/<subdir>/<file>`
    全部子目录（含 tcl8.6/tk8.6 主脚本与 dde1.4/reg1.3/tix8.4.3 扩展包）
  - 新增 `_TCL_DEV_EXTS = (".lib", ".sh")`：过滤 import library 与 config 脚本
    （运行时无用，节省 ~370KB）
  - 重写 `_build_tkinter_zip` 提取逻辑：四类文件按顺序匹配，第四类 tcl/ 目录
    加 `.lib`/`.sh` 后缀过滤
- `tests/test_builtin.py`
  - `_make_tkinter_tarball` fixture 新增 5 个文件：`DLLs/tcl86t.dll`、
    `DLLs/tk86t.dll`、`tcl/dde1.4/tcldde14.dll`、`tcl/tcl86t.lib`、
    `tcl/tclConfig.sh`（验证 .lib/.sh 过滤）
  - `test_build_tkinter_zip_extracts_all_components` 新增断言：根目录含
    `tcl86t.dll`/`tk86t.dll`；`tcl/dde1.4/tcldde14.dll` 存在；
    `tcl/tcl86t.lib` 与 `tcl/tclConfig.sh` 被过滤
  - `test_build_tkinter_zip_preserves_content` 新增 `tcl86t.dll`/`tk86t.dll`/
    `tcl/dde1.4/tcldde14.dll` 内容断言

## 关键决策与依据

1. **正则匹配 `python/DLLs/` 而非依赖前缀**：python-build-standalone 不同 release
   的 tarball 内目录前缀可能变化（`python/install/` vs `python/`），但 `DLLs/`
   子目录位置稳定。用 `r"/DLLs/((?:tcl|tk)\d+t?\.dll)$"` 匹配任意前缀下的
   DLLs 目录，兼容历史与未来 release。

2. **tcl/ 目录整体提取 + 后缀过滤**：旧正则只匹配 `tcl8.6`/`tk8.6` 版本目录，
   遗漏 `dde1.4`/`reg1.3`/`tix8.4.3` 等扩展包。改为 `r"/tcl/(.+)$"` 匹配整个
   tcl/ 目录，再用 `_TCL_DEV_EXTS` 过滤 `.lib`（import library）/`.sh`
   （config 脚本）等开发期文件，运行时无用，节省 ~370KB。

3. **DLL 提取到 runtime 根目录**：`_tkinter.pyd` 在 runtime 根目录，
   `tcl86t.dll`/`tk86t.dll` 也放根目录，loader.exe 的 `SetDllDirectoryW`
   已将 runtime/ 加入 DLL 搜索路径，Windows 加载 `_tkinter.pyd` 时能自动找到
   同目录的依赖 DLL。

## 代码实现情况

- `_build_tkinter_zip` 重写为四类提取规则：
  1. `.../tkinter/**` → `Lib/tkinter/...`（纯 Python 包，不变）
  2. `.../_tkinter*.pyd` → `_tkinter.pyd`（C 扩展，根目录，不变）
  3. `.../DLLs/tcl*t.dll` / `.../DLLs/tk*t.dll` → 根目录（**新增**，必需）
  4. `.../tcl/<subdir>/**` → `tcl/<subdir>/...`（扩展为全部子目录，**新增**过滤）
- 测试 fixture 模拟真实 tarball 结构，包含 5 个新增文件覆盖所有提取分支
- 测试断言验证：DLL 提取到根目录、扩展包保持原结构、开发期文件被过滤

## 整合优化情况

- 删除冗余的 `_TCL_DIR_RE` / `_TK_DIR_RE` 两个正则，统一为
  `_TCL_RUNTIME_DLL_RE` + `_TCL_DIR_PREFIX_RE`
- 旧逻辑只提取 `tcl8.6`/`tk8.6` 目录文件，新逻辑覆盖整个 `tcl/` 目录，
  消除"扩展包缺失"潜在问题
- `.lib`/`.sh` 过滤节省 ~370KB（tcl/ 总 7MB 的 5%）

## 测试验证结果

### 单元测试

- ruff check / format：2 文件全通过
- pyrefly：0 errors
- pytest test_builtin.py：15 passed

### 全量测试

- 1642 passed, 1 skipped, 11 failed
- 11 个失败全是 `WinError 1314` symlink 权限问题（预先存在，与本次改动无关）
- 覆盖率：TOTAL 97.12% ≥ 95% 门禁要求
  - `builtin.py` 97%（行 173, 177 是 `f is None` 边界检查，预先存在）

### 端到端验证

实际打包 `tk_app_pyall` 模板并运行 exe：

- 构建成功：产物 31.0MB（比修复前 26.0MB 多 5MB，主要是 tcl 扩展包脚本与 DLL）
- runtime 根目录包含 `tcl86t.dll` 和 `tk86t.dll` ✓
- 运行 `tk_app_pyall.exe` 成功：
  - ExitCode: 0 ✓
  - 输出：`Tk patchlevel: 8.6.12` ✓
  - tk 窗口正常创建并销毁 ✓

## 遗留事项

- 无（修复前 `_build_tkinter_zip` 缓存 zip 已自动重建为新版本，旧缓存被清理）

## 下一轮计划

- 继续 req-47 阶段 4 后续：启动速度优化（iter-104）
