# iter-61 installer.py 拆分

## 需求清单

- [x] installer.py 拆分（619 行 → facade + nsis + linux + zip）

## 迭代目标

将 `packaging/installer.py`（619 行）按平台职责拆分为 facade + 3 子模块，
保持公开 API 与测试 patch 兼容。

## 改动文件清单

- `src/fspack/packaging/installer.py`：重写为 facade（357 行），保留基类 `Installer`、
  公共辅助（`_run_stage`/`_prepare_dist`/`_check_exe`/`_release_base` 等）、
  调度（`_resolve_formats`/`build_release`）、函数式 API（`build_installer`/
  `build_linux_installer`），末尾 re-export 子模块符号
- `src/fspack/packaging/installer_nsis.py`：新建（~190 行），NSIS 模板、
  `NsisInstaller` 类、`generate_nsis_script`、`compile_installer`
- `src/fspack/packaging/installer_linux.py`：新建（~240 行），`LinuxInstaller` 类、
  `build_tarball`/`build_deb`、`build_tarball_release`/`build_deb_release`
- `src/fspack/packaging/installer_zip.py`：新建（~80 行），`build_zip`/`_make_zip`
- `tests/test_linux_installer.py`：更新 `build_tarball`/`build_deb` 的
  monkeypatch 路径为 `fspack.packaging.installer_linux.*`（函数实际定义在子模块）

## 关键决策与依据

1. **facade 末尾 re-export 避免循环导入**：子模块从 facade 导入 `Installer` 基类
   与公共辅助，facade 末尾从子模块导入 `NsisInstaller`/`LinuxInstaller` 等。
   Python 模块加载机制支持此模式——子模块导入 facade 时，facade 顶部定义已完成

2. **保留 `import subprocess` 在 facade**：测试通过 `monkeypatch.setattr(
   "fspack.packaging.installer.subprocess.run", ...)` patch。`subprocess` 是全局
   模块对象，patch 影响所有子模块的 `subprocess.run`。facade 保留 `import subprocess`
   （加 `# noqa: F401`）使 patch 路径有效

3. **共享 logger 名 `fspack.packaging.installer`**：子模块用
   `logging.getLogger("fspack.packaging.installer")` 而非 `__name__`，
   保持测试 caplog 按 logger 名过滤兼容

4. **`_DIST_INTERMEDIATE_EXCLUDES` 留 facade**：被 nsis（`_NSIS_EXCLUDE_INTERMEDIATE`）
   与 linux（`_LINUX_IGNORE`）共用，放公共位置避免重复定义

5. **测试 patch 路径更新**：`build_tarball`/`build_deb` 实际定义移到
   `installer_linux.py`，`LinuxInstaller.build_package` 调用的是子模块命名空间的
   引用，故 patch 路径从 `fspack.packaging.installer.build_tarball` 改为
   `fspack.packaging.installer_linux.build_tarball`。`subprocess.run`/`build`/
   `NsisInstaller.build_installer` 的 patch 路径不变（全局对象或类属性共享）

## 代码实现情况

- 拆分后行数：facade 357 + nsis ~190 + linux ~240 + zip ~80 = ~867 行
  （原 619 行，增加 ~248 行来自模块 docstring 与 import 重复，符合拆分预期）
- 公开 API（`__all__`）完全不变：`Installer`/`LinuxInstaller`/`NsisInstaller`/
  `build_deb`/`build_deb_release`/`build_installer`/`build_linux_installer`/
  `build_release`/`build_tarball`/`build_tarball_release`/`build_zip`/
  `compile_installer`/`generate_nsis_script`
- 私有符号 `_make_zip` 也通过 facade re-export（测试需要）

## 整合优化情况

- 无额外整合需求，拆分边界清晰

## 测试验证结果

- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors（86 suppressed，与基线一致）
- pytest（非 slow）：1010 passed，覆盖率 97.18%（≥95%）
- `test_installer.py`（50 测试）+ `test_linux_installer.py`（50 测试）全通过

## 遗留事项

- 无

## 下一轮计划

iter-62：loader.py 拆分（584 行 → loader_source.py + loader_compile.py + facade）
