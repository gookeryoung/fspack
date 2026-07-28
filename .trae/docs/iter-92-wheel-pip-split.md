# iter-92: wheel_pip.py 与 pipeline.py 职责拆分

## 需求清单

- [x] 拆分 `wheel_pip.py`：依赖解析与 sdist 回退职责拆到独立模块
- [x] 拆分 `pipeline.py`：阶段函数实现职责拆到独立模块
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

将两个过大的构建流水线模块按职责拆分为多个单一职责模块，降低单文件复杂度
便于维护与测试，同时保持 `wheels.py`/`builder.py` facade 公开 API 完全不变。

## 改动文件清单

### 新增

- `src/fspack/packaging/wheel_resolver.py` — 在线依赖解析与并行下载
  - 从 `wheel_pip.py` 迁入 `_find_uv` / `_resolve_with_uv` / `_run_pip_download` /
    `_download_online` / `_download_one_resolved` / `_download_resolved_parallel` /
    `_merge_parallel_results` 及模块级常量 `_UV_RESOLVED_LINE_RE` /
    `_PARALLEL_DOWNLOAD_WORKERS`
- `src/fspack/packaging/wheel_sdist.py` — sdist 回退构建
  - 从 `wheel_pip.py` 迁入 `_parse_missing_packages` / `_handle_sdist_fallback` /
    `_build_sdist_wheels` 及模块级常量 `_MISSING_PKG_RE`
- `src/fspack/packaging/pipeline_stages.py` — 构建阶段函数实现
  - 从 `pipeline.py` 迁入 `BuildContext` / `_prepare_runtime` /
    `_prepare_linux_runtime` / `_prepare_windows_runtime` / `_analyze_dependencies` /
    `_download_dependencies` / `_compile_user_sources` / `_build_entry_loaders` /
    `_resolve_project_icon` / 依赖缓存（`_dep_cache_load`/`_dep_cache_save`/
    `_dep_cache_path`）/ `_site_packages_has_deps` / `_normalize_pkg_name` /
    `_strip_version_specifier` / `unpack_wheels` / `default_icon_path` /
    `fspack_wheel_cache_dir` 及常量 `_DEFAULT_ICON`

### 修改

- `src/fspack/packaging/wheel_pip.py`
  - 删除已迁移到 `wheel_resolver.py` / `wheel_sdist.py` 的方法与常量
  - 从 `wheel_resolver` / `wheel_sdist` re-export 必要函数（`_find_uv`/
    `_resolve_with_uv`/`_run_pip_download`/`_download_online`/
    `_download_one_resolved`/`_download_resolved_parallel`/
    `_merge_parallel_results`/`_handle_sdist_fallback`/`_build_sdist_wheels`/
    `_parse_missing_packages`/`_UV_RESOLVED_LINE_RE`/`_MISSING_PKG_RE`），
    保持 `wheels.py` facade re-export 链与测试 monkeypatch 路径兼容
  - 保留 `download_wheels` 入口、`_prefilter_by_python_version`/
    `_build_pip_download_args`/`_parse_wheel_names`/`_record_wheel_stage`/
    `_deps_cache_key`/`_load_deps_cache`/`_save_deps_cache`/
    `_find_pip_python`/`_stream_subprocess`/`_run_pip` 等入口辅助函数
- `src/fspack/packaging/pipeline.py`
  - 删除已迁移到 `pipeline_stages.py` 的阶段函数与 `BuildContext`
  - 从 `pipeline_stages` re-export 阶段函数与 `BuildContext`，保持
    `fspack.packaging.pipeline.<fn>` patch 路径兼容（测试通过本模块 patch 阶段函数）
  - 保留 `build` / `_execute_build` / `resolve_project_info` / `clean_dist` /
    `_print_build_plan` 入口与编排，`_KEEP_NSI` 常量
  - 顶部新增 `DependencyReport` 导入（`_print_build_plan` 类型注解需要）
- `tests/test_wheels.py`
  - `monkeypatch.setattr("fspack.packaging.wheel_pip._find_uv", ...)` →
    `fspack.packaging.wheel_resolver._find_uv`
  - `monkeypatch.setattr("fspack.packaging.wheel_pip._resolve_with_uv", ...)` →
    `fspack.packaging.wheel_resolver._resolve_with_uv`
- `tests/test_offline_mode.py`
  - `monkeypatch.setattr("fspack.packaging.wheel_pip._find_uv", ...)` →
    `fspack.packaging.wheel_resolver._find_uv`
- `tests/test_builder.py`
  - 批量更新 monkeypatch 路径：被 `pipeline_stages.py` 阶段函数调用的函数
    从 `fspack.packaging.pipeline.<fn>` 改为 `fspack.packaging.pipeline_stages.<fn>`
    （`download_embed`/`extract_embed`/`download_standalone`/`extract_standalone`/
    `download_wheels`/`unpack_wheels`/`compile_loader`/`TkinterBundler`/`detect_platform`）
  - 保留 `fspack.packaging.pipeline.write_pth` 与 `fspack.packaging.pipeline.copy_source`
    路径不变（这两个函数在 `_execute_build` 中直接调用，patch 路径仍为 `pipeline`）
- `tests/test_build_dry_run.py`
  - 同 `test_builder.py`：批量更新阶段函数调用的 patch 路径到 `pipeline_stages`
  - 循环 patch 拆分：`copy_source` 单独 patch 到 `pipeline`，其余 patch 到 `pipeline_stages`
- `tests/test_offline_integration.py`
  - `fspack.packaging.pipeline.extract_embed` →
    `fspack.packaging.pipeline_stages.extract_embed`

## 关键决策与依据

### 拆分边界

按"职责单一 + 边界清晰"原则拆分：

**wheel_pip.py 拆分**：

- `wheel_pip.py`：wheel 下载入口与缓存调度（`download_wheels` + 依赖解析缓存 +
  pip 命令构造 + 入口辅助函数）
- `wheel_resolver.py`：在线依赖解析与并行下载（uv 解析 + pip download 串行/并行
  + sdist 回退触发）
- `wheel_sdist.py`：sdist 回退构建（解析缺失包名 + `pip wheel --no-deps` 构建）

**pipeline.py 拆分**：

- `pipeline.py`：构建流水线入口与编排（`build`/`_execute_build`/
  `resolve_project_info`/`clean_dist`/`_print_build_plan`）
- `pipeline_stages.py`：阶段函数实现（runtime 准备 → 依赖分析 → 依赖下载 →
  源码编译 → loader 生成）+ `BuildContext` + 依赖缓存 + icon 解析 + wheel 解压

### 循环依赖处理

`wheel_pip.py` 顶层导入 `wheel_resolver` 与 `wheel_sdist`；`wheel_resolver.py`
的 `_download_resolved_parallel` 调用 `wheel_sdist._handle_sdist_fallback`；
`wheel_sdist.py` 的 `_build_sdist_wheels` 调用 `wheel_pip._stream_subprocess`。

解决方案：`wheel_sdist.py` 在函数体内惰性导入 `_stream_subprocess`，避免顶层
循环依赖。`wheel_resolver.py` 顶层导入 `wheel_sdist`（单向依赖，无循环）。

`pipeline.py` 顶层导入 `pipeline_stages`；`pipeline_stages.py` 不导入 `pipeline`，
通过 `BuildContext` 聚合参数避免反向依赖。

### 测试兼容性维护

通过 facade 模块（`wheel_pip.py`/`pipeline.py`）re-export 拆分后的函数，保持
`wheels.py`/`builder.py` 公开 API 不变。但阶段函数内部调用其他函数时，patch
的是阶段函数所在模块（`pipeline_stages`）的全局名字，因此测试中 patch 阶段函数
调用的下游函数时，路径必须更新为 `fspack.packaging.pipeline_stages.<fn>`。

`_execute_build` 中直接调用的函数（`write_pth`/`copy_source`）仍 patch 到
`fspack.packaging.pipeline.<fn>`，保持不变。

### 显式导入与 noqa

`pipeline.py` 顶部显式导入运行时依赖（`download_embed`/`extract_embed`/
`download_standalone`/`extract_standalone`/`download_wheels`/`compile_loader`/
`copy_source`/`write_pth`）并通过 `# noqa: F401` 抑制未使用警告，目的是兼容
历史测试 `monkeypatch.setattr("fspack.packaging.pipeline.<func>", ...)` 路径
解析。但实际阶段函数调用这些函数时，名字解析走的是 `pipeline_stages` 模块的
全局名字，因此这些显式导入仅保留给 `write_pth`/`copy_source` 等少数在
`_execute_build` 中直接调用的函数。其他函数的 patch 路径已更新到
`pipeline_stages`，但保留导入以维持兼容性（未来清理测试后再删除）。

## 代码实现情况

### 模块规模变化

| 模块 | 拆分前 | 拆分后 | 变化 |
|------|--------|--------|------|
| wheel_pip.py | ~325 stmts | 152 stmts | -53% |
| wheel_resolver.py | - | 128 stmts | 新增 |
| wheel_sdist.py | - | 43 stmts | 新增 |
| pipeline.py | ~410 stmts | 179 stmts | -56% |
| pipeline_stages.py | - | 236 stmts | 新增 |

### 公开 API 不变

- `wheels.py` facade 通过 re-export 链保持所有公开函数访问路径不变
- `builder.py` facade 通过 re-export 链保持所有公开函数访问路径不变
- `download_wheels` / `build` / `resolve_project_info` / `clean_dist` 等入口
  函数签名与行为完全不变

## 整合优化情况

- 删除 `pipeline.py` 中已迁移函数的重复定义
- 删除 `wheel_pip.py` 中已迁移函数的重复定义
- 拆分后每个模块职责单一，便于独立测试与复用
- `pipeline_stages.py` 通过 `BuildContext` 聚合参数，避免阶段函数传递 6-8 个参数

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 96 files already formatted
- `uv run pyrefly check` — 0 errors (5 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1418 passed, 1 skipped, 30 deselected, coverage 97.84%

新模块覆盖率：

| 模块 | 覆盖率 |
|------|--------|
| wheel_pip.py | 100% |
| wheel_resolver.py | 100% |
| wheel_sdist.py | 100% |
| pipeline.py | 93% |
| pipeline_stages.py | 98% |

## 遗留事项

- `pipeline.py` 顶部显式导入的运行时依赖（`download_embed`/`extract_embed` 等）
  中，部分函数实际调用已迁移到 `pipeline_stages.py`，这些导入仅为历史测试
  patch 路径兼容保留。未来清理测试 patch 路径后可删除冗余导入。

## 下一轮计划

iter-93: 继续按 `req-47-feature-perf-polish.md` 推进剩余功能/性能完善项。
