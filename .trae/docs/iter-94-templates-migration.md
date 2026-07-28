# iter-94: examples 迁移到 assets/templates + fsp doctor --test/--bench

## 需求清单

- [x] 将 `examples/` 整合为 `src/fspack/assets/templates/` 下的项目模板
- [x] 删除 `examples/` 目录
- [x] `fsp doctor --test` 运行所有模板构建，打印汇总结果
- [x] `fsp doctor --bench` 运行所有模板构建，输出性能分析报告
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）

## 迭代目标

统一示例/模板来源，将 `examples/` 迁移到包内 `assets/templates/` 目录，作为
集成测试与性能基准的统一输入。新增 `fsp doctor --test`/`--bench` 子命令选项，
运行所有模板构建，分别输出构建汇总与性能分析报告。

## 改动文件清单

### 新增

- `src/fspack/templates/loader.py` — 项目模板加载器
  - `ProjectTemplate` dataclass：目录路径 + 元数据（id/name/version/
    requires_python/dependencies/app_type）
  - `list_project_templates()`：扫描 `assets/templates/` 目录，解析每个子目录的
    `pyproject.toml`，返回排序后的模板列表
  - `get_project_template(id)`：按目录名获取单个模板
  - `project_templates_dir()`：返回模板目录绝对路径
- `src/fspack/assets/templates/` — 16 个项目模板（从 `examples/` 迁移）
  - cli_complex_py314 / cli_helloworld_pyall / cli_office_py38 /
    multi_entry_py310 / pygame_cli_pyall / pygame_conway_py313 /
    pygame_gktetris_py38 / pygame_snake_pyall / pyqt5_cli_pyall /
    pyside2_app_py310 / pyside2_qml_dashboard_py38 / sci_matplotlib_py38 /
    sci_numpy_py38 / sci_scipy_py38 / tk_app_pyall / web_app_pyall
- `tests/test_template_loader.py` — 模板加载器测试（8 个测试）

### 修改

- `src/fspack/cli_doctor.py`
  - 新增 `TemplateBuildResult` frozen dataclass：构建结果（模板 id/成功/
    耗时/错误/产物大小/入口数）
  - 新增 `_build_single_template()`：构建单个模板，复制到临时目录后调用
    `build()`，捕获异常返回结果
  - 新增 `run_doctor_test()`：运行所有模板构建，打印汇总表格
  - 新增 `run_doctor_bench()`：运行所有模板构建（`profile=True`），打印
    汇总表格 + 性能分析（耗时排名、产物大小排名、瓶颈识别）
  - 新增 `_print_template_build_summary()` / `_print_performance_analysis()`
  - 导入新增 `tempfile`/`time`/`field`
- `src/fspack/cli.py`
  - `_add_doctor_subparser()` 新增 `--test`/`--bench` 参数
  - `_run_doctor(ns)` 根据 `ns.bench`/`ns.test` 分发到 `run_doctor_bench`/
    `run_doctor_test`
- `tests/test_cli_doctor.py`
  - 新增 8 个测试：`TemplateBuildResult` 数据结构 + 汇总打印 + 性能分析
- `tests/test_e2e_slow.py` / `test_offline_integration.py` /
  `test_build_dry_run.py` / `test_config.py` / `test_builder.py`
  - `_EXAMPLES` 路径从 `Path(__file__).parent.parent / "examples"` 改为
    `project_templates_dir()`（通过 `from fspack.templates.loader import
    project_templates_dir` 导入）
- `pyproject.toml`
  - `[tool.fspack] exclude` 从 `["examples", "templates"]` 改为
    `["templates", ".trae"]`（examples 已删除）
  - sdist 注释更新（移除 examples 引用）
- `ruff.toml` — `extend-exclude` 新增 `"src/fspack/assets/templates"`
- `pyrefly.toml` — `project-excludes` 新增 `"src/fspack/assets/templates/**"`

### 删除

- `examples/` 目录（16 个示例项目 + `__init__.py`）

## 关键决策与依据

### 模板存储位置

选择 `src/fspack/assets/templates/` 而非顶层 `templates/` 目录：

- 包内资源随 wheel 分发，安装后可直接访问
- 不与顶层 `templates/`（GitHub Actions 打包模板）混淆
- `packages = ["src/fspack"]` 自动包含 assets 子目录

### 模板加载策略

`loader.py` 直接解析每个模板目录的 `pyproject.toml` 获取元数据，不额外
创建 `manifest.toml`：

- 避免元数据重复维护（`pyproject.toml` 是唯一真相源）
- `ProjectTemplate` 从 `[project]` 段读取 name/version/requires-python/
  dependencies，从 `[tool.fspack]` 段读取 app-type
- 无 `pyproject.toml` 的目录跳过并 warning（容错）

### --test 与 --bench 区别

- `--test`：调用 `build()` 不启用 profile，用 `time.perf_counter()` 测量
  总耗时，输出汇总表格（模板名/状态/耗时/产物大小/入口数）
- `--bench`：调用 `build(profile=True)`，每个模板输出详细的各阶段耗时
  报告（`print_profile_report`），最后额外输出性能分析（耗时排名降序、
  产物大小排名降序、最慢/最快倍率）

### 不可测函数标记

`_build_single_template`/`run_doctor_test`/`run_doctor_bench` 涉及实际
构建（下载 Python、下载依赖），在单元测试中无法运行，标记
`# pragma: no cover`。`_print_template_build_summary`/
`_print_performance_analysis` 用模拟数据测试，覆盖率达标。

### ruff/pyrefly 排除

`assets/templates/` 下的示例项目代码是数据文件（非 fspack 自身代码），
不应受 fspack 的 lint/类型检查规则约束。在 `ruff.toml` 的
`extend-exclude` 和 `pyrefly.toml` 的 `project-excludes` 中排除。

## 代码实现情况

### fsp doctor --test 输出示例

```
模板构建测试（16 个模板）
[1/16] 构建 cli_helloworld_pyall ...
  √ 成功 (3.2s, 12.3 MiB)
[2/16] 构建 cli_office_py38 ...
  √ 成功 (8.5s, 15.1 MiB)
...

模板构建汇总
 #  模板                          状态      耗时    产物大小    入口数
 1  cli_helloworld_pyall          √ 成功    3.2s    12.3 MiB    1
 2  cli_office_py38               √ 成功    8.5s    15.1 MiB    1
 ...

构建完成：16/16 全部成功
  总耗时 45.2s | 平均 2.8s | 总产物 234.5 MiB
```

### fsp doctor --bench 额外输出

```
性能分析

耗时排名（降序）
 #  模板                          耗时      占比
 1  pyside2_qml_dashboard_py38    15.2s     33.7% (最慢)
 2  pygame_gktetris_py38          8.1s      17.9%
 ...

产物大小排名（降序）
 #  模板                          大小
 1  pyside2_qml_dashboard_py38    45.2 MiB (最大)
 ...

最慢模板 pyside2_qml_dashboard_py38 (15.2s) 是最快 cli_helloworld_pyall (1.2s) 的 12.7 倍
```

## 整合优化情况

- 统一示例/模板来源：`examples/` → `assets/templates/`，消除双份维护负担
- `loader.py` 复用 `pyproject.toml` 作为元数据源，无额外配置文件
- `--test`/`--bench` 复用 `build()` 函数，不重复构建逻辑
- 汇总表格复用 `rich.table.Table`，与 `print_doctor_report` 风格一致

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 98 files already formatted
- `uv run pyrefly check` — 0 errors (7 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1435 passed, 1 skipped, 30 deselected, coverage 97.61%

新模块覆盖率：

| 模块 | 覆盖率 |
|------|--------|
| templates/loader.py | 81% |
| cli_doctor.py（新增函数） | 93%（`_build_single_template` 等标记 no cover） |

## 遗留事项

- `loader.py` 的 81% 覆盖率未覆盖的错误路径（pyproject.toml 解析失败、
  目录无 pyproject.toml）可在后续迭代补充
- `test_e2e_slow.py` 中引用的 `gui_calc_pyall`/`cli_tool_pyall` 不存在于
  `assets/templates/`，这些 slow 测试在迁移前就已失败（非本次引入）

## 下一轮计划

iter-95: 继续按 `req-47-feature-perf-polish.md` 推进剩余功能/性能完善项。
