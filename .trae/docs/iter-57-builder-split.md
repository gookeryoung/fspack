# iter-57：builder.py 拆分（1059 行 → 4 模块）

## 需求清单

- [x] iter-57：builder.py 拆分（1059 行 → `pipeline.py` 阶段函数 /
  `pyc.py` pyc 预编译 / `sync.py` 源码同步，`builder.py` 作 facade）

## 迭代目标

将 1059 行的 `builder.py` 按职责拆分为三个模块 + facade，提升可维护性。
保持公开 API 不变（`build`/`clean_dist`/`copy_source`/`unpack_wheels` 等函数
与所有 import 路径兼容），所有现有测试不破坏。

## 改动文件清单

- `src/fspack/packaging/pipeline.py`（新增，~663 行）：阶段编排函数
  - `build`/`_build_runtime`/`_build_loader`/`_build_installer` 等阶段函数
  - `_inject_win7_compat_dll`/`_needs_win7_compat_dll`/`_trim_stdlib` 等运行时处理
  - `_site_packages_has_deps`/`fspack_wheel_cache_dir` 等辅助
- `src/fspack/packaging/pyc.py`（新增，~261 行）：pyc 预编译
  - `_precompile_pyc` 批量 compileall 调用
  - `_CompileCompleted` 辅助类
- `src/fspack/packaging/sync.py`（新增，~182 行）：源码同步
  - `copy_source`/`_sync_tree` 源码树同步与开发产物剥离
  - `_dir_size` 目录大小计算
  - 依赖缓存 `_dep_cache_load`/`_dep_cache_save`/`_dep_cache_path`
- `src/fspack/builder.py`（重写为 facade，~98 行）：
  - re-export 所有公开 API 与测试所需私有符号
  - 显式 `import subprocess` 兼容测试 `monkeypatch.setattr("fspack.builder.subprocess.run", ...)`

## 关键决策与依据

### 按职责拆分 vs Mixin

**选型**：按职责拆分为独立模块函数（非 mixin），`builder.py` 作 facade re-export。

**理由**：
1. `builder.py` 全是模块级函数，无类继承结构，不适合 mixin
2. 三个职责（pipeline/pyc/sync）相互独立，函数间通过参数传递，无共享状态
3. facade re-export 保持 `from fspack.builder import build` 路径兼容

### 测试 patch 路径兼容

facade `builder.py` 显式 `import subprocess`，让测试中
`monkeypatch.setattr("fspack.builder.subprocess.run", ...)` 路径可解析。
patch 设置的是 `subprocess` 模块对象的属性（单例），全局生效，对
`pipeline.py`/`pyc.py` 内的调用同样有效。

## 代码实现情况

- 三个新模块完整实现，所有函数签名与 docstring 从原 builder.py 原样迁移
- facade builder.py 仅含 re-export 与必要 import，无业务逻辑
- 测试无改动（所有 patch 路径通过 facade 的标准库导入兼容）

## 测试验证结果

- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors
- pytest：全部通过
- coverage：≥95% 门禁

## 下一轮计划

iter-58：config.py 拆分（731 行 → `models.py`/`parsing.py`/`versions.py`，`config/__init__.py` 作 facade）
