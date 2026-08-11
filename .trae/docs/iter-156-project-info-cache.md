# iter-156: 配置加载缓存（ProjectInfo.from_dir 按 mtime lru_cache）

## 需求清单
- [x] req-40: 深度重构与性能基线守护 → iter-86 配置加载缓存（ProjectInfo.from_dir 按 mtime 失效）
- [x] req-47: 功能性能完善 → iter-94 配置加载缓存（lru_cache + mtime 失效键）

## 迭代目标
在 `ProjectInfo.from_dir` 中引入基于 `pyproject.toml` mtime 的 LRU 缓存，构建流程内多次调用同一项目时避免重复 TOML 解析 + AST 扫描。

## 改动文件清单
- `src/fspack/config/models.py`：新增 `_project_info_from_dir_cached`（`@functools.lru_cache(maxsize=64)`）、`_clear_project_info_cache`、改造 `ProjectInfo.from_dir` 取 mtime 作缓存键
- `src/fspack/pipeline/runtime_stage.py`：补 `from pathlib import Path` 修复 F821
- `src/fspack/pipeline/deps_stage.py`：移除未用 `Platform` 导入、补 `TYPE_CHECKING` 导入 `StageRecorder`、TkinterBundler dispatch 加 `attr-defined` 抑制
- `src/fspack/pipeline/dist_helpers.py`：`if False` 伪引用改 `TYPE_CHECKING` 导入 `BuildTracker`
- `src/fspack/pipeline/__init__.py`：补 `E402`/`F401` noqa、`__all__` 排序与补齐
- `src/fspack/dep_analyzer/__init__.py`：`__all__` 补齐 `_collect_loader_entries` / `_read_ascii_string`
- `src/fspack/dep_analyzer/common.py`：`_parse_deps_parallel.parse_fn` 补 `Callable` 类型注解

## 关键决策与依据
1. **缓存键选择**：`(resolved_dir_str, py_version, mtime_ns)`。`mtime_ns` 由 `pyproject.toml.stat().st_mtime_ns` 获取，文件变动后缓存自动失效；文件不存在时 `mtime_ns=0`，交给 `parse_project` 抛 `ProjectError`（lru_cache 不缓存异常，不污染缓存）。
2. **maxsize=64**：覆盖 doctor/bench 模式下多模板项目批量构建场景；容量过大会导致旧项目长时间占用（项目配置对象可达数 MB 级）。
3. **`mtime_ns` ARG001 抑制**：参数仅作为 lru_cache 键参与哈希，函数体内无需访问（键本身就是失效语义），因此加 `# noqa: ARG001` 明确意图。
4. **显式缓存清除函数**：`_clear_project_info_cache` 供测试隔离、调试、运行时修改 `pyproject.toml` 后手动失效使用。

## 代码实现情况
核心缓存实现：

```python
@functools.lru_cache(maxsize=64)
def _project_info_from_dir_cached(
    resolved_dir_str: str,
    py_version: str | None,
    mtime_ns: int,  # noqa: ARG001 - 作为 lru_cache 失效键
) -> ProjectInfo:
    from fspack.config.parsing import parse_project
    return parse_project(Path(resolved_dir_str), py_version)

@classmethod
def from_dir(cls, project_dir: Path, py_version: str | None = None) -> ProjectInfo:
    resolved = Path(project_dir).resolve()
    try:
        mtime_ns = (resolved / "pyproject.toml").stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return _project_info_from_dir_cached(str(resolved), py_version, mtime_ns)
```

## 测试验证结果
- **门禁**：ruff 0 errors、pyrefly 0 errors、pytest 2229 passed / 12 skipped、覆盖率 95.13% ≥ 95%
- **专项测试**：`tests/test_profile.py` + `test_log_file.py` + `test_nuitka.py` + `test_site_packages.py` + `test_build_perf_baseline.py` 共 246 passed
- **基线测试**：`test_perf_baseline.py` 中 `TestProjectInfoBaseline` 覆盖冷解析 + 缓存命中两条基线，缓存命中时从毫秒级降到微秒级

## 遗留事项
无

## 下一轮计划
iter-157：AST 分析内存优化——`collect_imports_and_submodules` 改用生成器 + 单次 dict 合并（当前 list+set 双结构在 500+ 文件项目内存占用高），`source_fingerprint` 的 `os.scandir` 递归改用 yield 生成器避免全量路径列表。
