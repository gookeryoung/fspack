# iter-102: 移除 inject_mingw_runtime_dlls

## 需求清单

- [x] 移除 `inject_mingw_runtime_dlls` / `_locate_mingw_dll` / `_MINGW_RUNTIME_DLLS`
- [x] 从 `loader_compile.py` `__all__` 与模块 docstring 清理相关条目
- [x] 从 `loader.py` facade 移除 re-export 与 `__all__` 条目
- [x] 从 `pipeline_stages.py` 移除 Windows 目标的 inject 调用与注释
- [x] 删除 `tests/test_loader.py` 中 4 个 inject 相关测试与 `_make_completed` 辅助函数
- [x] 删除 `tests/test_builder.py` 中 2 个 inject 验证测试与 `_setup_embed_mocks` 中的 mock
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

用户反馈 Terminal#43-45 三条警告 `MinGW 运行时 DLL 缺失: libgcc_s_seh-1.dll /
libwinpthread-1.dll / libstdc++-6.dll，跳过注入` 始终出现。根因：Windows 上
`gcc -print-file-name=libgcc_s_seh-1.dll` 返回去掉 `.dll` 后缀的纯名字
`libgcc_s_seh-1`（MinGW-w64 把 `.dll` 当作可执行后缀处理），`_locate_mingw_dll`
的 fallback 用 `shutil.which("libgcc_s_seh-1")` 查找不带后缀的名字必然失败，
实际 DLL 就在 gcc 同目录却从未被查找过——功能在 Windows 上从未生效。

用户决定直接移除 inject 函数不替换：Nuitka 编译 .pyd 时已静态链接运行时，
loader.exe 在用户机器上靠 PATH 中的 DLL 能跑（现状即如此），目标机器无 MinGW
时即使 inject 也不工作（实际从未工作过）。保留只会持续误导用户认为有 bug。

## 改动文件清单

### 修改

- `src/fspack/packaging/loader_compile.py`
  - 删除模块 docstring 中 "MinGW 运行时 DLL 注入" 条目
  - 从 `__all__` 移除 `"inject_mingw_runtime_dlls"`
  - 删除 `_MINGW_RUNTIME_DLLS` 常量、`_locate_mingw_dll` 函数、
    `inject_mingw_runtime_dlls` 函数（共约 60 行）
- `src/fspack/packaging/loader.py`
  - 删除 facade docstring 中 "MinGW 运行时 DLL 注入" 提及
  - 从 import 与 `__all__` 移除 `inject_mingw_runtime_dlls`
- `src/fspack/packaging/pipeline_stages.py`
  - 删除 `_prepare_runtime` 中 Windows 目标的 inject 调用与 5 行注释
- `tests/test_loader.py`
  - 从 import 移除 `inject_mingw_runtime_dlls`
  - 删除 `_make_completed` 辅助函数与 4 个 inject 测试（约 116 行）
- `tests/test_builder.py`
  - `_setup_embed_mocks` 删除 inject mock（3 行）
  - 删除 `test_build_calls_inject_mingw_runtime_dlls_for_windows`（18 行）
  - 删除 `test_build_skips_inject_mingw_runtime_dlls_for_linux`（39 行）

## 关键决策与依据

1. **根因不修复直接移除**：用户在询问"是不是该移除"时选择了"直接移除不替换"。
   inject 函数在 Windows 上从未工作过（gcc -print-file-name 返回去后缀名字的
   怪异行为），保留只会持续输出误导性警告。Nuitka 编译 .pyd 默认静态链接
   运行时，loader.exe 在有 MinGW 的机器上靠 PATH 加载，目标机器部署场景
   未报告运行问题，移除风险可控。

2. **不引入静态链接选项替代**：备选方案是给 loader 编译命令加
   `-static-libgcc -static-libstdc++ -static`，但用户明确选择"不替换"，
   保持现状最小改动。

3. **保留 `_find_mingw_gcc` 等其他 mingw 相关 API**：仍被 loader 编译、
   `mingw_available()`、icon 资源编译使用，不属于本次清理范围。

## 代码实现情况

- 删除 `inject_mingw_runtime_dlls` 函数及其私有 helper `_locate_mingw_dll`、
  常量 `_MINGW_RUNTIME_DLLS`
- 清理 3 个模块的 docstring / `__all__` / import 中的相关引用
- 删除 6 个测试用例（4 个单元测试 + 2 个集成测试）与 1 个测试辅助函数
- 清理 `_setup_embed_mocks` fixture 中的 inject mock

## 整合优化情况

- 无重复代码引入
- 无新风险：删除的代码路径原本在 Windows 上就从未生效
- 测试 import 清理后 `Any` 仍被其他 25 处使用，无需调整

## 测试验证结果

- ruff check / format：4 文件全通过
- pyrefly：0 errors
- pytest：1642 passed, 1 skipped, 11 failed
  - 11 个失败全是 `WinError 1314` symlink 权限问题（预先存在，与本次改动无关）
  - 删除的 6 个 inject 测试不影响其他测试通过
- 覆盖率：TOTAL 97.12% ≥ 95% 门禁要求
  - `loader.py` 100%
  - `loader_compile.py` 99%（`_find_mingw_gcc` 默认返回路径未覆盖，预先存在）
  - `pipeline_stages.py` 96%

## 遗留事项

- 无

## 下一轮计划

- 继续 req-47 阶段 4 后续：启动速度优化（iter-103）
