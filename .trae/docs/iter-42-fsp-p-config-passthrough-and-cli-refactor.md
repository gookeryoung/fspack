# 迭代 42 - fsp p 配置透传修复与 cli build 复用

## 需求清单

- [x] `fsp p` 在配置了打包 nuitka 选项时没有生效，依然只打包了 py
- [x] 重构 `cli.py` build 子命令复用 `build_options_from_defaults`，消除配置合并逻辑重复

## 迭代目标

1. 修复 `fsp p`（`build_release`）内部调用 `build()` 时未透传 `[tool.fspack]` 配置的构建默认值（`nuitka`/`pyc_strip`/`no_site`/`pyc_optimize` 等），导致安装包打包阶段始终用默认 `BuildOptions`（`nuitka=False`）
2. 提取 `build_options_from_defaults(defaults)` 公共函数，将 `[tool.fspack]` 配置层 `BuildDefaults` 转为运行层 `BuildOptions`，供 `installer._prepare_dist` 与 `cli.main` 复用
3. 重构 `cli.py` build 子命令：用 `build_options_from_defaults` + `dataclasses.replace()` 替代原内联合并逻辑，消除 "config or 默认值" 重复判断
4. 不改变外部行为：CLI 标志合并语义（布尔 `cli or config`、`pyc_optimize` CLI > config > 默认 2）保持一致
5. 全套门禁通过，覆盖率不低于上一轮（97.49%）

## 改动文件清单

### 主代码

- `src/fspack/config.py`：
  - 新增 `build_options_from_defaults(defaults: BuildDefaults) -> BuildOptions` 函数
  - `BuildDefaults` 中 `None` 的字段使用 `BuildOptions` 默认值，非 `None` 覆盖
  - 加入 `__all__` 导出
- `src/fspack/packaging/installer.py`：
  - `_prepare_dist` 在 `no_build=False` 分支调用 `build()` 时，通过 `build_options_from_defaults(info.build_defaults)` 生成 `options` 并透传
  - 调整 `no_build=True` 分支顺序：先校验 dist 目录存在，再 `resolve_project_info`（避免无 `pyproject.toml` 时报错路径变化）
- `src/fspack/cli.py`：
  - build 子命令分支用 `build_options_from_defaults` 构造配置层 base，再用 `replace()` 应用 CLI 覆盖
  - 移除 `BuildOptions` 直接构造，消除 "config or 默认值" 重复判断
  - 合并语义保持：布尔 `cli or base`、`pyc_optimize` `cli if cli is not None else base`

### 测试

- `tests/test_installer.py`：
  - 新增 `test_prepare_dist_passes_build_defaults_to_build`：验证 `[tool.fspack] nuitka=true` 等配置透传到 `build()` 的 `options` 参数
  - 新增 `test_prepare_dist_no_config_uses_default_options`：无配置时 `options` 为默认 `BuildOptions`
  - 新增 `test_build_options_from_defaults_translation`：单元测试 `build_options_from_defaults` 转换逻辑
  - `fake_build` mock 签名添加 `options: BuildOptions | None = None` 关键字参数
- `tests/test_linux_installer.py`：
  - `fake_build` mock 签名添加 `options: BuildOptions | None = None` 关键字参数
  - 导入 `BuildOptions`

## 关键决策与依据

### 1. `build_options_from_defaults` 职责边界

函数仅做 "配置层 → 运行层" 的类型转换，不处理 CLI 标志覆盖。CLI 合并逻辑由调用方（`cli.main`）通过 `replace()` 实现。这样：

- `installer._prepare_dist`（无 CLI 标志场景）直接用 `build_options_from_defaults` 即可
- `cli.main`（有 CLI 标志场景）用 `build_options_from_defaults` 构造 base，再 `replace()` 覆盖

避免在 `build_options_from_defaults` 内塞入 CLI 合并逻辑导致 `installer` 路径无法复用。

### 2. `_prepare_dist` 的 `no_build=True` 分支顺序调整

原重构版本先 `resolve_project_info` 再判断 `no_build`，导致 `no_build=True` 且无 `pyproject.toml` 时报 `ProjectError` 而非 `InstallerError("未找到 dist")`。调整为先校验 dist 存在，再 `resolve_project_info`，恢复原行为。

### 3. CLI 合并用 `replace()` 而非重新构造 `BuildOptions`

原代码直接构造 `BuildOptions(...)`，需要重复 `defaults.xxx if defaults.xxx is not None else 默认值` 判断。重构后 `base = build_options_from_defaults(defaults)` 已完成这层判断，`replace()` 只需处理 CLI 覆盖，逻辑更清晰。

## 代码实现情况

- `build_options_from_defaults`：15 行，纯函数，无副作用
- `_prepare_dist`：`no_build=True` 分支 3 行，`no_build=False` 分支 3 行
- `cli.main` build 分支：从 30 行降到 28 行，消除重复判断

## 整合优化情况

- `cli.main` 与 `installer._prepare_dist` 共享 `build_options_from_defaults`，消除 "config or 默认值" 重复逻辑
- `BuildDefaults` → `BuildOptions` 转换逻辑单点维护，未来新增配置字段只需改一处

## 测试验证结果

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：47 files already formatted
- `uv run pyrefly check`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：810 passed, coverage 97.52%

CLI 合并语义验证（`tests/test_cli.py` 已有测试全通过）：
- `test_build_config_defaults_used_when_cli_silent`：配置 `nuitka=true` 等，CLI 静默 → `options.nuitka is True`
- `test_build_cli_flag_overrides_config_pyc_optimize`：CLI `--pyc-optimize 0` > 配置 `pyc_optimize=1` → `options.pyc_optimize == 0`
- `test_build_cli_nuitka_flag_with_config`：配置 `nuitka=true` + CLI `--nuitka` → `options.nuitka is True`
- `test_build_cli_nuitka_flag_with_no_config`：无配置 + CLI `--nuitka` → `options.nuitka is True`
- `test_build_config_pyc_optimize_default_when_both_silent`：均未指定 → `options.pyc_optimize == 2`

## 遗留事项

无。

## 下一轮计划

无明确下一轮计划。当前 `[tool.fspack]` 配置透传链路已闭环（`fsp b` CLI 合并 + `fsp p` 自动透传），`BuildOptions`/`BuildDefaults` 抽象稳定。
