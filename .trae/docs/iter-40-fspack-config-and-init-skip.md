# 迭代 40 - fspack 自身打包配置与 Nuitka __init__.py 跳过

## 需求清单

- [x] 为自身打包设计忽略目录 EXAMPLES，并始终启用 nuitka 等优化措施进行打包，在 fspack.toml 中启用，增加该配置的支持
- [x] __init__.py 文件是否应当 Nuitka 编译 PYD，如不应当请优化

## 迭代目标

1. 在 `[tool.fspack]` 配置中支持 `exclude` 排除目录与 `nuitka`/`pyc_strip`/`no_site`/`pyc_optimize`/`no_pyc`/`no_stdlib_trim` 构建默认值
2. CLI 标志与 `[tool.fspack]` 配置合并：布尔开关用 `any([cli, config])`，整数 `pyc_optimize` 用 `CLI > config > 默认值 2`
3. fspack 自身 `pyproject.toml` 启用 `nuitka+pyc_strip+no_site`，排除 `examples` 目录
4. Nuitka 编译跳过 `__init__.py`（无收益的 subprocess 开销）

## 改动文件清单

### 主代码
- `src/fspack/config.py`：新增 `BuildDefaults` dataclass（位置在 `ProjectInfo` 之前避免 F821），`ProjectInfo` 新增 `exclude_dirs` 与 `build_defaults` 字段；新增 `_parse_build_defaults` 解析函数与 `_BUILD_DEFAULT_KEYS` 映射
- `src/fspack/builder.py`：`copy_source` 新增 `extra_excludes` 参数，新增 `_merge_excludes` 合并内置 `_EXCLUDE` 与配置额外排除
- `src/fspack/cli.py`：build 子命令在 dispatch 前调用 `ProjectInfo.from_dir` 读取 `build_defaults`，与 CLI 标志合并
- `src/fspack/packaging/nuitka.py`：`compile_src` 收集 `.py` 文件时跳过 `__init__.py`
- `pyproject.toml`：新增 `[tool.fspack]` 配置（exclude/nuitka/pyc_strip/no_site）

### 测试
- `tests/test_config.py`：新增 `exclude`/`build_defaults` 解析与错误处理测试
- `tests/test_builder.py`：新增 `extra_excludes` 测试
- `tests/test_cli.py`：新增 `_make_minimal_project` 辅助（cli.main 现在需要可解析的 pyproject.toml）；新增配置合并测试
- `tests/test_nuitka.py`：更新 `test_compile_src_records_stage_metrics` 期望（`__init__.py` 不再计入编译计数）

## 关键决策与依据

### 1. BuildDefaults 字段全部为 `bool | None`/`int | None`
- `None` 表示配置未指定，使用 `BuildOptions` 默认值
- 非 `None` 时作为 CLI 未显式指定该标志时的回退默认值
- 这样可以在 `cli.py` 用 `any([cli, config])` 简洁合并布尔开关

### 2. 合并策略
- **布尔开关**（`nuitka`/`pyc_strip`/`no_site` 等）：`any([cli, config])`，CLI 或配置任一启用 → 启用
- **整数开关**（`pyc_optimize`）：`cli if cli is not None else config if config is not None else 2`，CLI 显式指定 > 配置指定 > 默认值 2

### 3. `BuildDefaults` 位置在 `ProjectInfo` 之前
- `ProjectInfo` 用 `build_defaults: BuildDefaults = field(default_factory=BuildDefaults)`，运行时需要 `BuildDefaults` 类已定义
- `from __future__ import annotations` 只延迟注解求值，不延迟 `default_factory` 求值

### 4. Nuitka 跳过 `__init__.py`
- `__init__.py` 通常为空或仅含 import，编译为 `.pyd` 无收益
- 保留 `.py` 维持包标识（PEP 420），`.pyc` 预编译提供字节码优化
- 跳过后 `compiled_files` 不含 `__init__.py`，删除循环天然跳过，无需额外检查

### 5. fspack 自身打包配置
```toml
[tool.fspack]
exclude = ["examples"]
nuitka = true
pyc_strip = true
no_site = true
```

## 代码实现情况

- 全套门禁通过：ruff check / ruff format --check / pyrefly check (0 errors) / pytest 807 passed
- 覆盖率 97.44%（≥95% 要求）
- cli.py 覆盖率从 88% 提升到 97%（新增配置合并测试覆盖了 `build_defaults` 合并分支）

## 整合优化情况

- 无重复代码，配置合并逻辑集中在 `cli.py` 的 build 子命令分支
- `BuildDefaults` 与 `BuildOptions` 分离：前者是配置层（`None` 表示未指定），后者是运行层（具体值）
- `_parse_build_defaults` 与 `_parse_exclude` 风格一致，类型不匹配时报错

## 测试验证结果

```
807 passed, 21 deselected in 5.27s
TOTAL 2926 stmts, 50 miss, 822 branch, 32 brpart, 97% cover
```

## 遗留事项

- 无

## 下一轮计划

- 无（任务完成）

## 提交记录

- `4fc07d7` feat: 新增 [tool.fspack] 配置支持，可声明 exclude 排除目录与 nuitka/pyc_strip/no_site/pyc_optimize 等构建默认值
- `ae647a8` perf: Nuitka 编译跳过 __init__.py，避免无收益的 subprocess 开销
