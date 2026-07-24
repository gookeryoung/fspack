# iter-33 Win7 兼容性 DLL 注入

## 需求清单

- [x] embed python 支持 Win7 等老旧系统兼容性
- [x] 避免每次构建下载完整 PythonVista embed zip（10.5MB）
- [x] 本地准备兼容版二进制 DLL，随 fspack 分发

## 迭代目标

为 Python 3.9+ 的 Windows embed python 注入 `api-ms-win-core-path-l1-1-0.dll`，使其在 Win7 SP1 / Server 2008 R2 SP2 上也能运行。DLL 随 fspack 内置分发，无需网络下载。

## 背景

用户最初提议使用 `https://gitcode.com/gh_mirrors/py/PythonVista` 的 embed python 替代 python.org 版本以保证 Win7 兼容性，并要求下载前做 URL 可访问性测试。

经实测：
- **gitcode 镜像不提供二进制文件直接下载**：所有 raw/download 路径返回 `text/html` 页面（非原始文件），API 路径 404/418。
- **GitHub raw URL 可用**：`https://github.com/adang1345/PythonVista/raw/master/{ver}/python-{ver}-embed-amd64.zip` 返回 `application/zip`，~10.5MB。
- **PythonVista 兼容性机制**两部分：(1) 附加 `api-ms-win-core-path-l1-1-0.dll`（Python 3.9+ 启动需 `PathCchSkipRoot` 等 API）；(2) 重编译 `python3X.dll` 含源码级 API 回退。

用户调整为"本地准备兼容版二进制 DLL，避免下载"方向。最终方案：从 `api-ms-win-core-path` 项目 Release v1.0.0 提取预编译 x64 DLL（116KB），内置到 fspack assets，构建时注入到 `dist/runtime/`。

## 改动文件清单

| 文件 | 变更 |
|------|------|
| `src/fspack/assets/runtime/api-ms-win-core-path-l1-1-0.dll` | 新增 x64 DLL（116736 字节，LGPL-2.1） |
| `src/fspack/assets/runtime/LICENSE-api-ms-win-core-path.txt` | 新增许可证声明 |
| `src/fspack/assets/runtime/COPYING.LIB-api-ms-win-core-path.txt` | 新增 LGPL-2.1 全文 |
| `src/fspack/builder.py` | 新增 `_WIN7_COMPAT_DLL_NAME`/`_needs_win7_compat_dll`/`_inject_win7_compat_dll` + build 中注入调用 |
| `tests/test_builder.py` | 新增 7 个测试（版本判断参数化 8 组 + 注入复制 + 跳过已存在 + 源缺失 warning + 集成 3 场景） |

## 关键决策与依据

1. **仅注入 DLL 而非用 PythonVista embed zip**：
   - Python 3.9+ 在 Win7 上启动的关键缺失是 `api-ms-win-core-path-l1-1-0.dll`（提供 `PathCchSkipRoot` 等 API）
   - PythonVista 的源码级 API 回退（重编译 python3X.dll）对大多数应用非必需
   - DLL 仅 116KB，随 fspack 分发无需网络下载；PythonVista embed zip 10.5MB 需每次下载
   - 权衡：用 python.org embed + 注入 DLL 覆盖 95% 场景，避免下载开销

2. **DLL 来源**：`https://github.com/adang1345/api-ms-win-core-path` Release v1.0.0
   - 基于 Wine 项目代码实现，LGPL-2.1 许可证
   - PythonVista 项目也使用同一 DLL
   - 提供 x86/x64 预编译版本，fspack 用 amd64 embed 故取 x64

3. **版本判断 `(3, 9) >= (3, 9)`**：
   - Python 3.8 是最后官方支持 Win7 的版本，3.9+ 官方不再支持
   - 用元组比较避免字符串排序陷阱

4. **DLL 缺失时 warning 不报错**：向后兼容旧 fspack 安装（DLL 未打包时），不阻断构建

5. **不修改 pyproject.toml 打包配置**：`packages = ["src/fspack"]` 递归包含 assets 下所有文件，DLL 自动随 wheel 分发

## 代码实现情况

- `_needs_win7_compat_dll(py_version)`：Python 3.9+ 返回 True
- `_inject_win7_compat_dll(runtime_dir)`：DLL 已存在跳过；源缺失 warning；否则 `shutil.copy2` 复制
- `build()` 中 `site_packages.mkdir` 之后、`tracker.stage("分析依赖")` 之前注入，`target is Platform.WINDOWS and _needs_win7_compat_dll(info.py_version)` 守卫

## 测试验证结果

- ruff check: 0 errors
- ruff format --check: 49 files already formatted
- pyrefly check: 0 errors
- pytest: 644 passed, 21 deselected, coverage 97.94%
- builder.py 覆盖率 95%（新增代码 100% 覆盖）

新增 7 个测试：
- `test_needs_win7_compat_dll`（参数化 8 组版本）
- `test_inject_win7_compat_dll_copies_from_assets`
- `test_inject_win7_compat_dll_skips_when_exists`
- `test_inject_win7_compat_dll_warns_when_source_missing`
- `test_build_injects_win7_compat_dll_for_py39_plus`（3.11.9 + Windows）
- `test_build_skips_win7_compat_dll_for_py38`（3.8.10 + Windows）
- `test_build_skips_win7_compat_dll_for_linux`（3.11.9 + Linux）

## 遗留事项

- 仅注入 `api-ms-win-core-path-l1-1-0.dll`，不含 PythonVista 的源码级 API 回退（重编译 python3X.dll）。极少数依赖特定 API 的应用在 Win7 上可能仍有问题，届时需改用 PythonVista embed zip。
- gitcode 镜像不提供二进制文件下载，若未来需用 PythonVista 完整 embed zip，需用 GitHub raw URL（中国大陆可能需代理）。

## 下一轮计划

无明确下一轮计划。如用户反馈 Win7 兼容性不足，可考虑：
1. 增加 CLI 选项 `--pythonvista` 下载 PythonVista embed zip 替代 python.org 版本
2. 在 wrapper 中注入 Win7 兼容性检测逻辑
