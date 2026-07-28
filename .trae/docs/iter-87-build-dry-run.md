# iter-87: build --dry-run 预览模式

## 需求清单

- [x] 在 `fsp b` 子命令添加 `--dry-run` 标志
- [x] `pipeline.build()` 支持 `dry_run=True`：仅解析项目 + 分析依赖，打印打包计划
- [x] dry-run 模式不执行任何写操作（不下载/不编译/不复制/不创建 dist 目录）
- [x] 输出结构化打包计划表格（项目信息/依赖分析/构建选项）
- [x] 编写测试覆盖 dry-run 场景
- [x] 全套门禁验证通过 + 文档更新 + git 提交

## 迭代目标

为 `fsp b` 增加 `--dry-run` 预览模式，让用户在执行实际构建前先确认配置正确，
避免无效构建浪费时间。dry-run 模式仅执行项目解析与依赖分析（AST 扫描），
不触发任何下载/解压/复制/编译等写操作，输出结构化打包计划表格。

## 改动文件清单

- `src/fspack/packaging/pipeline.py`：新增 `_print_build_plan` 函数；`build()` 添加 `dry_run` 参数；`_analyze_dependencies` 添加 `save_cache` 参数支持 dry-run 跳过缓存写入
- `src/fspack/cli.py`：`_add_build_subparser` 注册 `--dry-run` 标志；`_run_build` 透传 `dry_run=ns.dry_run` 给 `build()`
- `tests/test_build_dry_run.py`：新增 17 个测试用例覆盖 pipeline 与 CLI 层
- `tests/test_cli.py`：`_capture_build` fake_build 添加 `dry_run` 参数
- `tests/test_cli_recursive.py`：两处 fake_build 添加 `dry_run` 参数
- `README.md`：`fsp build` 命令速查表与章节添加 `--dry-run` 选项说明

## 关键决策与依据

### 1. dry-run 跳过依赖缓存写入

`_analyze_dependencies` 原本会在分析后调用 `_dep_cache_save` 写入
`dist/.dep_cache.json`，导致 dist 目录被创建。dry-run 模式承诺"不执行任何写操作"，
因此添加 `save_cache: bool = True` 关键字参数，dry-run 时传 `save_cache=False`
跳过缓存写入。常规构建路径不受影响（默认 `save_cache=True`）。

### 2. dry-run 仍执行项目解析与依赖分析

dry-run 不只是打印静态配置，还要执行真实的 AST 扫描，让用户能看到
未声明依赖（missing）等动态分析结果。这才有"预览"价值——能发现潜在问题
（如忘了在 pyproject.toml 声明 rich），而不是简单回显 [tool.fspack] 配置。

### 3. CLI 透传 dry_run 到 build()

`_run_build` 在调用 `build()` 时显式传 `dry_run=ns.dry_run`，
未指定时 `ns.dry_run` 默认 False，与 `build()` 默认值一致。
这样 fake_build 测试也必须接受 `dry_run` 参数，更新了 4 处 fake_build。

## 代码实现情况

### pipeline.py

```python
def build(  # noqa: PLR0913
    ...
    dry_run: bool = False,
) -> ProjectInfo:
    ...
    # dry-run 模式：仅解析项目 + 分析依赖，打印计划后返回
    if dry_run:
        report = _analyze_dependencies(ctx, save_cache=False)
        _print_build_plan(ctx, report)
        return info
    ...
```

```python
def _analyze_dependencies(ctx: BuildContext, *, save_cache: bool = True) -> DependencyReport:
    """分析依赖（源码指纹缓存命中则跳过 AST 扫描）.

    ``save_cache=False`` 时跳过缓存写入（用于 ``--dry-run`` 模式，避免创建
    ``dist/.dep_cache.json`` 触发 dist 目录创建）。
    """
    ...
    else:
        report = DependencyReport.from_src(project_dir, ctx.info.name, ctx.info.dependencies)
        if save_cache:
            _dep_cache_save(ctx.cfg.dist_dir, fingerprint, report)
    ...
```

`_print_build_plan` 用 rich.table 渲染 4 张表：

- **项目信息**：名称、版本、入口、目标平台、Python 版本、runtime 来源、loader 编译器、缓存目录、镜像源
- **依赖分析**：声明依赖数、AST 第三方数、未声明 missing 数、AST 标准库/本地模块
- **私有包源**（可选）：extra-index-url / find-links
- **构建选项**：Nuitka/ccache/pyc_strip/no_site 等

### cli.py

```python
p.add_argument(
    "--dry-run",
    action="store_true",
    help="仅预览打包计划，不执行实际构建（不下载/不编译/不复制文件）",
)
```

```python
build(
    project,
    get_mirror(ns.mirror),
    ns.py_version,
    target=_parse_target(ns.target),
    options=options,
    extra_index_urls=tuple(ns.extra_index_urls or ()),
    find_links=tuple(ns.find_links or ()),
    dry_run=ns.dry_run,
)
```

## 测试验证结果

`tests/test_build_dry_run.py` 17 个测试用例：

**pipeline 层（10 个）**：
- `test_build_dry_run_skips_runtime_download`：Windows 目标不调用 download_embed/extract_embed
- `test_build_dry_run_skips_linux_runtime_download`：Linux 目标不调用 download_standalone/extract_standalone
- `test_build_dry_run_returns_project_info`：返回 ProjectInfo 字段正确
- `test_build_dry_run_prints_plan_summary`：输出含打包计划/项目信息/依赖分析/构建选项/dry-run 提示
- `test_build_dry_run_includes_missing_dependencies`：AST 发现的未声明依赖出现在 missing 列
- `test_build_dry_run_no_write_operations`：dist 目录不被创建
- `test_build_dry_run_merges_private_sources`：CLI 私有包源合并到 info 且显示在私有包源表
- `test_print_build_plan_renders_without_error`：渲染基本表格不抛异常
- `test_print_build_plan_linux_target`：Linux 目标显示 python-build-standalone 与 gcc
- `test_print_build_plan_shows_nuitka_when_enabled`：opts.nuitka=True 时显示 Nuitka/ccache/nuitka-packages
- `test_print_build_plan_shows_private_sources`：info 含私有包源时显示私有包源表

**CLI 层（3 个）**：
- `test_cli_build_dry_run_flag_passed_to_build`：`fsp b --dry-run` 透传 dry_run=True
- `test_cli_build_without_dry_run_flag_defaults_false`：未指定时 dry_run=False
- `test_cli_build_dry_run_alias_b`：别名 b 同样透传

**门禁结果**：
- ruff check：All checks passed
- ruff format：84 files already formatted
- pyrefly check：0 errors
- pytest：1335 passed, 1 skipped, 覆盖率 98.25%（>= 95%）

## 整合优化情况

- 更新 4 处 fake_build 添加 `dry_run: bool = False` 参数，保持与 `build()` 签名同步
- `_analyze_dependencies` 添加 `save_cache` 关键字参数，默认 True 保持向后兼容

## 遗留事项

无。dry-run 模式已完整覆盖 pipeline + CLI 两层，测试覆盖率达标。

## 下一轮计划

按 `req-47-feature-perf-polish.md` 继续推进下一项功能/性能优化任务。
