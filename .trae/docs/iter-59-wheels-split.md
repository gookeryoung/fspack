# iter-59：wheels.py 拆分（709 行 → 4 模块）

## 需求清单

- [x] iter-59：wheels.py 拆分（709 行 → `wheel_pip.py` pip 调用 /
  `wheel_cache.py` 依赖缓存 / `wheel_markers.py` marker 过滤，
  `wheels.py` 作 facade）

## 迭代目标

将 709 行的 `packaging/wheels.py` 按职责拆分为三个模块 + facade，提升可维护性。
保持公开 API 不变（`download_wheels` 与所有 import 路径兼容），所有现有 98 个
test_wheels.py 测试不破坏。

## 改动文件清单

- `src/fspack/packaging/wheel_markers.py`（新增，~85 行）：``python_version`` 标记预过滤
  - `_MARKER_PY_VER_RE` 正则常量
  - `_filter_by_python_version` 按目标 Python 版本过滤依赖列表
  - `_eval_python_version_marker`/`_eval_single_marker` 标记评估
- `src/fspack/packaging/wheel_cache.py`（新增，~79 行）：依赖解析缓存
  - `_deps_cache_key` 缓存键计算（纳入依赖/版本/平台/私有源）
  - `_load_deps_cache`/`_save_deps_cache` 缓存读写（``.deps-<key>.json``）
- `src/fspack/packaging/wheel_pip.py`（新增，~608 行）：pip/uv 调用与下载流程
  - `download_wheels` 入口函数
  - `_find_pip_python`/`_find_uv`/`_resolve_with_uv` 解释器与 uv 查找
  - `_run_pip_download`/`_download_online`/`_run_pip`/`_stream_subprocess` pip 调用
  - `_handle_sdist_fallback`/`_build_sdist_wheels` sdist 回退
  - `_parse_pip_download_wheels`/`_parse_wheel_names`/`_parse_missing_packages` 输出解析
  - `_build_pip_download_args`/`_prefilter_by_python_version`/`_record_wheel_stage` 辅助
  - 常量：`_PIP_PYTHON_NAMES`/`_UV_RESOLVED_LINE_RE`/`_MISSING_PKG_RE`/`_PIP_WHEEL_LINE_RE`
- `src/fspack/packaging/wheels.py`（重写为 facade，~63 行）：
  - re-export `download_wheels` 与所有测试所需私有符号
  - 显式 `import os/re/shutil/subprocess/sys/Path` 兼容测试 patch 路径

## 关键决策与依据

### 模块级 patch 与函数级 patch 的兼容策略

**问题**：测试中两类 monkeypatch：
1. 模块级属性 patch：`fspack.packaging.wheels.subprocess.run`/`sys.executable`/
   `os.environ`/`os.read`/`Path.resolve`/`shutil.which`
2. 函数级 patch：`fspack.packaging.wheels._find_pip_python`/
   `_stream_subprocess`/`_find_uv`/`_resolve_with_uv`

**策略**：
1. 模块级 patch：facade 显式 `import` 这些标准库模块。因标准库模块为单例，
   patch 设置属性后全局生效，对 `wheel_pip.py` 内的调用同样有效。
2. 函数级 patch：facade re-export 这些函数保持 `from fspack.packaging.wheels import _xxx`
   导入兼容；但函数内部调用走 `wheel_pip.py` 的 globals，需更新测试 patch 路径到
   `fspack.packaging.wheel_pip._xxx`（73 处替换）。

### 拆分依据

- `wheel_markers.py`：纯函数，无外部依赖，独立测试
- `wheel_cache.py`：纯文件 I/O，无 pip 依赖，独立测试
- `wheel_pip.py`：核心下载逻辑，依赖前两个模块

## 代码实现情况

- 三个新模块完整实现，所有函数签名与 docstring 从原 wheels.py 原样迁移
- facade wheels.py 仅含 re-export 与标准库导入，无业务逻辑
- test_wheels.py 更新 4 个函数的 73 处 patch 路径（`fspack.packaging.wheels._xxx` →
  `fspack.packaging.wheel_pip._xxx`）

## 测试验证结果

- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors
- pytest：98 个 test_wheels.py 测试全部通过
- 新模块覆盖率：wheel_pip.py 100% / wheel_cache.py 100% / wheel_markers.py 100% /
  wheels.py(facade) 100%

## 下一轮计划

iter-60：slim/base.py 拆分（526 行 → `spec.py`/`unpack.py`，`base.py` 作 facade）
