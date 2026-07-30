# iter-113：packaging 模块按职责拆分子包

## 需求清单

- [x] 将 packaging/ 下 37 个平铺文件按职责拆分为 5 个子包 + 顶层模块
- [x] 公开 API 完全兼容（facade 路径 `fspack.packaging.<subpkg>.X` 不变）
- [x] 子包内部模块路径更新（如 `nuitka_compile` → `nuitka.compile`）
- [x] 100+ 处测试 monkeypatch 路径更新
- [x] 全套门禁通过（ruff/pyrefly/pytest 1835 passed/coverage ≥ 95%）

## 迭代目标

packaging/ 目录原 37 文件平铺，nuitka/wheels/installer/loader/pipeline 各由
6-8 个扁平模块组成（如 `nuitka.py`/`nuitka_env.py`/`nuitka_compile.py`/...），
文件数多、命名冗长、职责边界模糊。本轮按职责拆分为 5 个子包，每个子包通过
`__init__.py` facade 暴露公开 API，内部模块按单一职责拆分，提升可维护性。

**核心约束**：公开 API 路径不变（`from fspack.packaging.nuitka import NuitkaCompiler`、
`from fspack.packaging.wheels import download_wheels` 等），仅子包内部模块路径变化
（`fspack.packaging.nuitka_compile._HEARTBEAT_INTERVAL` →
`fspack.packaging.nuitka.compile._HEARTBEAT_INTERVAL`）。

## 改动文件清单

### 子包创建（git mv + 新建 __init__.py）

**nuitka/（8 文件 + __init__.py）：**
- `nuitka.py` → `nuitka/compiler.py`（NuitkaCompiler 类定义）
- `nuitka_env.py` → `nuitka/env.py`
- `nuitka_standalone.py` → `nuitka/standalone.py`
- `nuitka_ccache.py` → `nuitka/ccache.py`
- `nuitka_strip.py` → `nuitka/strip.py`
- `nuitka_compile.py` → `nuitka/compile.py`
- `nuitka_verify.py` → `nuitka/verify.py`
- `nuitka_protocol.py` → `nuitka/protocol.py`
- 新建 `nuitka/__init__.py`（facade：`import shutil/subprocess/sys` 供 monkeypatch +
  `from .compiler import NuitkaCompiler`）

**wheels/（6 文件，__init__.py = facade）：**
- `wheels.py` → `wheels/__init__.py`（facade：`import os/re/shutil/subprocess/sys/Path`
  供 monkeypatch + re-export 全部公开 API 与私有辅助）
- `wheel_pip.py` → `wheels/downloader.py`
- `wheel_resolver.py` → `wheels/resolver.py`
- `wheel_sdist.py` → `wheels/sdist.py`
- `wheel_cache.py` → `wheels/cache.py`
- `wheel_markers.py` → `wheels/markers.py`

**installer/（5 文件 + __init__.py）：**
- `installer.py` → `installer/base.py`
- `installer_linux.py` → `installer/linux.py`
- `installer_macos.py` → `installer/macos.py`
- `installer_nsis.py` → `installer/nsis.py`
- `installer_zip.py` → `installer/zip.py`
- 新建 `installer/__init__.py`（facade：`import subprocess` 供 monkeypatch +
  re-export + `_facade` 模式解决函数级 monkeypatch）

**loader/（3 文件，__init__.py = facade）：**
- `loader.py` → `loader/__init__.py`（facade：`import shutil/subprocess` 供 monkeypatch）
- `loader_compile.py` → `loader/compile.py`
- `loader_source.py` → `loader/source.py`

**pipeline/（2 文件，__init__.py = facade）：**
- `pipeline.py` → `pipeline/__init__.py`（facade：显式 import 运行时依赖供 monkeypatch）
- `pipeline_stages.py` → `pipeline/stages.py`

### 顶层保留模块（不变）

runtime/entry/pyc/dep_analyzer/size_report/sbom/builtin/profile/sync/icon/log_file/net.py
——net 是跨子包基础设施（runtime/builtin/nuitka 均依赖），其余为独立职责模块。

### import/monkeypatch/docstring 更新

- nuitka/ 内 8 文件：`from fspack.packaging.nuitka_protocol` → `.protocol`；
  docstring 中 `:mod:`fspack.packaging.nuitka_X`` → `:mod:`fspack.packaging.nuitka.X``
- wheels/ 内 6 文件：`from fspack.packaging.wheel_X` → `wheels.X`；docstring 更新
- installer/ 内 5 文件：`from fspack.packaging.installer` → `.base`（避免循环依赖）；
  docstring 更新；`_facade` 模式保留函数级 monkeypatch 兼容
- loader/ 内 3 文件：`from fspack.packaging.loader_compile/source` → `.compile/.source`
- pipeline/ 内 2 文件：`from fspack.packaging.pipeline_stages` → `.stages`；
  修复 `_DEFAULT_ICON` 路径深度（`parent.parent` → `parent.parent.parent`）

### 测试更新

- `test_nuitka.py`：4 处 monkeypatch 路径（`nuitka_compile.X` → `nuitka.compile.X`、
  `nuitka_ccache.CCACHE_URLS` → `nuitka.ccache.CCACHE_URLS`）
- `test_wheels.py`：~40 处 monkeypatch 路径（`wheel_pip.X` → `wheels.downloader.X`、
  `wheel_resolver.X` → `wheels.resolver.X`）
- `test_offline_mode.py`：4 处 monkeypatch + docstring 更新
- `test_perf_baseline.py`：`from wheel_cache import` → `from wheels.cache import`
- `test_installer.py`/`test_linux_installer.py`/`test_macos_installer.py`：
  monkeypatch + import 路径更新
- `test_builder.py`/`test_build_dry_run.py`/`test_dep_analyzer.py`/`test_extras.py`/
  `test_offline_integration.py`：`pipeline_stages.X` → `pipeline.stages.X`

### 文档更新

- `packaging/__init__.py`：模块概览重写，按子包 + 顶层模块分组
- `docs/api.rst`：automodule 指令更新（nuitka/wheels/installer/loader/pipeline 子模块路径）
- `docs/architecture.rst`：离线模式引用路径 + 模块结构表格更新

## 关键决策与依据

1. **facade 在 `__init__.py`**：nuitka/wheels/loader/pipeline 的原 facade 文件
   （`nuitka.py`/`wheels.py`/`loader.py`/`pipeline.py`）直接 git mv 为
   `__init__.py`（wheels/loader/pipeline）或拆分为 `__init__.py` + `compiler.py`
   （nuitka，因 `nuitka.py` 含 NuitkaCompiler 类定义）。避免冗余的 facade.py 中转。
   installer 因 base.py 含 `Installer` 基类 + 大量业务逻辑，新建 `__init__.py` facade。

2. **monkeypatch 兼容**：`fspack.packaging.<subpkg>.<stdlib>.<attr>` 路径
   （如 `fspack.packaging.nuitka.subprocess.run`）通过 `__init__.py` 显式
   `import subprocess` 解析。子包内部模块路径（`nuitka_compile.X`）更新为
   `nuitka.compile.X`。installer 的函数级 monkeypatch（`fspack.packaging.installer.build`）
   用 `_facade` 模式：base.py 底部 `import fspack.packaging.installer as _facade`，
   内部调用改走 `_facade.<fn>` 运行时动态查找。

3. **`_DEFAULT_ICON` 路径修复**：pipeline/stages.py 从 `packaging/` 移到
   `packaging/pipeline/` 后深一层，`Path(__file__).parent.parent`（原指向
   `src/fspack/`）会错误指向 `src/fspack/packaging/`。修复为 `parent.parent.parent`。

4. **net.py 保留顶层**：runtime/builtin/nuitka.ccache/nuitka.standalone 均依赖
   `fspack.packaging.net.Downloader`，是跨子包基础设施，不归入任何子包。

## 测试验证结果

- ruff check：All checks passed
- ruff format --check：114 files already formatted
- pyrefly check：0 errors（10 suppressed, 6 warnings not shown）
- pytest：1835 passed, 12 skipped, 10 deselected in 11.70s
- coverage：95.23% ≥ 95%
- 全仓 grep 确认 `fspack.packaging.(nuitka_|wheel_|installer_|loader_|pipeline_stages)`
  旧路径零残留（src/tests/docs 均无匹配，仅 .trae/docs/ 历史记录保留）

## 遗留事项

- 无

## 下一轮计划

- 待用户分配新需求
