# iter-95: ProjectInfo.from_dir 解析缓存（lru_cache + mtime 失效）

## 需求清单

- [x] `ProjectInfo.from_dir` 增加 `lru_cache` 按 `(project_dir, py_version, mtime)`
      缓存解析结果，避免 `fsp b`/`fsp p` 流程内多次调用重复解析
- [x] 缓存键含 `pyproject.toml` 的 `mtime_ns`，文件修改后自动失效
- [x] 新增 `clear_project_cache()` 公开 API 供测试隔离与显式刷新
- [x] 新增 `test_project_info_from_dir_baseline` 基线测量冷解析耗时
- [x] 新增 `test_project_info_from_dir_cached_baseline` 基线测量缓存命中耗时
- [x] 全套门禁通过（ruff / pyrefly / pytest / coverage ≥ 95%）
- [x] 既有基线测试无退化（AST / fingerprint / slim）

## 迭代目标

对应 req-47 阶段 2「性能优化与代码质量」的 iter-94 配置加载缓存项。
`parsing.py` 的 `ProjectInfo.from_dir`/`parse_project` 每次解析 pyproject.toml
+ AST 扫描入口 + 推断 app_type，构建流程内被多次调用：

- `fsp b`：`cli.py:378`（合并 build_defaults）+ `pipeline.py:221`（`_execute_build`
  内 `resolve_project_info`）= **2 次解析**
- `fsp p`：`installer.py:167`（`_prepare_dist` 内 `resolve_project_info`）+
  `pipeline.py:221`（dist 未就绪时 `build()` 内 `resolve_project_info`）= **2 次解析**
- `fsp r`：`runner.py:39` = **1 次解析**

同一项目同一 pyproject.toml 状态下重复解析是浪费。本迭代引入 `lru_cache`
按 `(project_dir, py_version, pyproject_mtime_ns)` 缓存 `ProjectInfo` 实例，
同一项目在 pyproject.toml 未修改时复用缓存，缓存命中比冷解析快 **8.8-13 倍**。

## 改动文件清单

### 修改

- `src/fspack/config/parsing.py`
  - 模块顶部新增 `from functools import lru_cache` 与
    `_PROJECT_CACHE_MAXSIZE = 64` 常量
  - 模块 docstring 补充「解析缓存」段落说明缓存策略与失效条件
  - `__all__` 新增 `"clear_project_cache"`
  - `parse_project()` 重构为「入口校验 + mtime 收集 + 缓存查询」三层：
    - 校验 `pyproject.toml` 存在
    - `pp.stat().st_mtime_ns` 收集 mtime（纳秒分辨率，覆盖秒级与亚秒级修改）
    - 委托 `_parse_project_cached(project_dir, py_version, mtime_ns)` 查询缓存
  - 新增 `_parse_project_cached()` 私有函数，`@lru_cache(maxsize=64)` 装饰，
    含原 `parse_project` 的实际解析逻辑（tomllib 解析 + AST 扫描 + 入口识别）
  - 新增 `clear_project_cache()` 公开函数，调用
    `_parse_project_cached.cache_clear()` 清空缓存
- `src/fspack/config/__init__.py`
  - `from fspack.config.parsing import` 列表新增 `clear_project_cache`
  - `__all__` 新增 `"clear_project_cache"`
  - 模块 docstring「项目解析」段补充 `clear_project_cache`
- `tests/test_perf_baseline.py`
  - 新增 `sample_pyproject_project` fixture：构造带完整 `[project]` +
    `[tool.fspack]` 配置 + 入口脚本的样本项目
  - 新增 `TestProjectInfoBaseline` 类含两个基线测试：
    - `test_project_info_from_dir_baseline`：冷解析基线，`benchmark.pedantic`
      + `setup=clear_project_cache` 每轮清空，测量实际解析耗时
    - `test_project_info_from_dir_cached_baseline`：缓存命中基线，预热后
      `benchmark` 多次调用，测量缓存查找开销
- `tests/test_config.py`
  - 顶部新增 `import os`、`import time`（用于 mtime 失效测试）
  - `from fspack.config import` 列表新增 `clear_project_cache`
  - 文件末尾新增「解析缓存（lru_cache）测试」段含 8 个测试：
    - `test_parse_project_cache_hit_returns_same_object`：缓存命中返回同一对象
    - `test_parse_project_cache_hit_via_from_dir`：`from_dir` 同样命中缓存
    - `test_parse_project_cache_invalidates_on_mtime_change`：mtime 变化触发失效
    - `test_clear_project_cache_empties_cache`：显式清空后重新解析
    - `test_clear_project_cache_idempotent`：多次清空不报错
    - `test_parse_project_cache_separates_different_py_version`：不同参数分别缓存
    - `test_parse_project_cache_separates_different_project_dirs`：不同目录分别缓存
    - `test_parse_project_cache_error_not_cached`：异常不被缓存
  - 新增 `_make_minimal_project` 辅助函数构造最小可解析项目

## 关键决策与依据

### 缓存键设计：`(project_dir, py_version, pyproject_mtime_ns)`

- `project_dir`：已 resolve 的绝对路径，唯一标识项目位置
- `py_version`：`parse_project` 参数，影响 `ProjectInfo.py_version` 字段；
  不同 py_version 应分别缓存
- `pyproject_mtime_ns`：`pyproject.toml` 的 `st_mtime_ns`，纳秒分辨率
  - 文件修改后 mtime 变化，触发新缓存条目（旧条目由 LRU 淘汰）
  - `st_mtime_ns` 比 `st_mtime` 分辨率更高（纳秒 vs 秒），覆盖亚秒级修改
  - 文件被 `touch` 但内容未改也会过度失效，但缓存重建成本低（< 1ms）可接受

### lru_cache 而非手动字典缓存

- `functools.lru_cache` 是标准库，无新依赖
- 自动 LRU 淘汰，无需手动管理缓存大小
- `cache_clear()` / `cache_info()` 提供清空与统计接口
- `maxsize=64` 覆盖多数项目场景（同时处理 ≤ 64 个不同项目/版本组合）

### mtime_ns 在函数内不读取

`_parse_project_cached` 的 `pyproject_mtime_ns` 参数仅作缓存键，函数内不读取
（避免重复 `stat`）。用 `# noqa: ARG001` 抑制 ruff 未使用参数警告，docstring
说明「仅作缓存键」。

### 缓存命中返回同一对象（identity 相等）

`lru_cache` 命中时返回缓存的 `ProjectInfo` 实例，`is` 比较相等。
`ProjectInfo` 是 `frozen dataclass`，不可变，共享实例安全（无并发修改风险）。
测试 `test_parse_project_cache_hit_returns_same_object` 验证此特性。

### 异常不被缓存

`lru_cache` 仅缓存成功返回值，异常会传播给调用方且不缓存。修复 pyproject.toml
后再次调用能成功解析。测试 `test_parse_project_cache_error_not_cached` 验证。

### 测试隔离：每个测试用 tmp_path 天然隔离

不同测试用不同 `tmp_path`，`project_dir` 不同，缓存键不同，天然隔离。
同一测试内修改 pyproject.toml 后强制重解析需 `os.utime` 推进 mtime
（部分平台 mtime 分辨率不足）。

## 代码实现情况

### parse_project 重构

```python
def parse_project(project_dir: Path, py_version: str | None = None) -> ProjectInfo:
    """解析 pyproject.toml 并识别入口，返回项目元信息。"""
    project_dir = Path(project_dir).resolve()
    pp = project_dir / "pyproject.toml"
    if not pp.is_file():
        raise ProjectError(f"未找到 pyproject.toml: {pp}")
    mtime_ns = pp.stat().st_mtime_ns
    return _parse_project_cached(project_dir, py_version, mtime_ns)


@lru_cache(maxsize=_PROJECT_CACHE_MAXSIZE)
def _parse_project_cached(
    project_dir: Path,
    py_version: str | None,
    pyproject_mtime_ns: int,  # noqa: ARG001 — 仅作缓存键
) -> ProjectInfo:
    """缓存版项目解析：实际读取 pyproject.toml + AST 识别入口."""
    # ... 原 parse_project 的实际解析逻辑 ...


def clear_project_cache() -> None:
    """清空 parse_project 的解析缓存."""
    _parse_project_cached.cache_clear()
```

### 基线测试方法

冷解析基线用 `benchmark.pedantic(setup=clear_project_cache, rounds=10, iterations=1)`：
- `setup` 每轮清空缓存，确保每轮都是冷解析
- `iterations=1` 避免 inner loop 命中缓存
- `rounds=10` 取统计（min/median/mean/stddev）

缓存命中基线用 `benchmark()` 默认行为：预热一次填充缓存后多次循环，测量
缓存查找开销。

## 整合优化情况

- 缓存逻辑集中在 `parsing.py`，`ProjectInfo.from_dir`/`parse_project`/
  `resolve_project_info` 自动受益，无需调用方改动
- `clear_project_cache` 作为公开 API 供测试与特殊场景使用，不污染主流程
- 基线测试与既有 `TestAstBaseline`/`TestSlimBaseline`/`TestFingerprintBaseline`
  风格一致，纳入同一文件便于对比

## 测试验证结果

- `uv run ruff check src tests` — All checks passed
- `uv run ruff format --check src tests` — 98 files already formatted
- `uv run pyrefly check` — 0 errors (7 suppressed, 7 warnings)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95` —
  1443 passed, 1 skipped, 32 deselected, coverage 97.61%

新模块覆盖率：

| 模块 | 覆盖率 | 说明 |
|------|--------|------|
| config/parsing.py | 95% | 新增缓存逻辑全覆盖（`_parse_project_cached`/`clear_project_cache`） |

### 基线对比

| 测试 | Min (us) | Mean (us) | 备注 |
|------|----------|-----------|------|
| test_project_info_from_dir_baseline（冷解析） | 706.5 | 2039.6 | 新增 |
| test_project_info_from_dir_cached_baseline（缓存命中） | 80.6 | 157.4 | 新增 |
| test_collect_imports_and_submodules_baseline | 30.3 | 33.7 | 既有，无退化 |
| test_analyze_dependencies_baseline | 5239.7 | 5411.9 | 既有，无退化 |
| test_classify_entry_baseline | 3.4 | 3.9 | 既有，无退化 |
| test_slim_unpack_baseline | 4388.5 | 4763.0 | 既有，无退化 |
| test_source_fingerprint_baseline | 374.9 | 465.2 | 既有，无退化 |

**缓存收益**：缓存命中（Min 80.6us）比冷解析（Min 706.5us）快 **8.8 倍**；
按 Mean（157.4 vs 2039.6us）快 **13 倍**。

`fsp b`/`fsp p` 流程内 2 次调用场景：原 2 × 706.5us = 1.41ms → 缓存后
706.5 + 80.6 = 787.1us，**节省 44%** 解析耗时。

## 遗留事项

- `req-47` 阶段 2 剩余项：iter-93 mixin Protocol 类型声明（当前 pyrefly
  7 suppressed 已满足 ≤10 验收，Protocol 边际收益低，可推迟）
- `req-47` 阶段 2 剩余项：iter-95 AST 分析内存优化（`collect_imports_and_submodules`
  双结构改生成器，`source_fingerprint` 递归改 `yield`）
- 缓存上限 64 个条目，长期运行进程若处理大量不同项目可能频繁淘汰，
  可考虑加 `cache_info()` 监控（暂不必要）

## 下一轮计划

iter-96：按 req-47 阶段 3「CI 与跨平台」推进，优先 iter-96 CI 三 job 增强
（Windows 矩阵 + slow-e2e cron + benchmark 门禁），依赖 iter-95 性能基线
稳定后再加 CI 门禁。
