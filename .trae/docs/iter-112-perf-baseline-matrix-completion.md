# iter-112：性能基线矩阵扩展收尾（iter-89）

## 需求清单

- [x] 补全 iter-89 缺失的 2 个基线测试：test_nuitka_ensure_env_baseline
  与 test_wheel_download_cache_hit_baseline
- [x] 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- [x] 全量测试不破坏（1835 passed）
- [x] 新增基线不破坏现有 8 个基线

## 迭代目标

推进 req-40 iter-89 性能基线矩阵扩展收尾。iter-89 原计划新增 3 个基线
（ProjectInfo/Nuitka/wheel 缓存），iter-94 已完成 ProjectInfo 2 个、iter-102
已完成 EntryWrapper 1 个，本轮补齐最后 2 个（Nuitka ensure_env 缓存命中、
wheel 依赖解析缓存命中），达 iter-89 验收「基线测试数 ≥ 8，覆盖 AST/指纹/
wheel/slim/配置/Nuitka/缓存全场景」。基线总数 10 个（原 5 + ProjectInfo 2
+ EntryWrapper 1 + 本轮 2）。

## 改动文件清单

修改：
- `tests/test_perf_baseline.py`：
  - 顶部 import 新增 `_deps_cache_key`/`_load_deps_cache`/`_save_deps_cache`
    （来自 `fspack.packaging.wheel_cache`）
  - 末尾新增 2 个基线测试类（88 行）：
    - `TestNuitkaEnsureEnvBaseline`：单测 `ensure_env` 缓存命中分支耗时
    - `TestWheelDownloadCacheBaseline`：单测 `_load_deps_cache` 缓存命中耗时

文档：
- `.trae/req/req-40-deep-refactor-baseline-guard.md`：iter-89 勾选完成
- `.trae/docs/iter-112-perf-baseline-matrix-completion.md`：本迭代记录

清理：
- 删除 `.trae/docs/iter-107-startup-time-optimization.md`（保留最新 5 条：
  iter-108~112）

## 关键决策与依据

1. **Nuitka ensure_env 基线 mock 策略**：复用 `test_nuitka.py::test_ensure_env_cache_hit_skips_download`
   的 mock 模式——`monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)`
   让 `_check_c_compiler` 通过，预创建 `cache_dir/nuitka/__init__.py` 让
   `_is_nuitka_cached` 返回 True 走缓存命中分支。这样 ensure_env 仅执行
   mingw 检查 + 一次 `is_file()` + `StageRecorder.hit_cache/set_detail`，无
   subprocess 开销，测量纯缓存命中分支耗时作为后续优化参考。

2. **wheel 缓存基线用 50 个 wheel 文件**：与 AST 基线 50 个 .py 文件对齐，
   模拟中等规模项目依赖。`_load_deps_cache` 命中分支 = JSON 解析 + 50 次
   `is_file()` 校验，测量缓存查找开销。预创建 wheel 文件 + `_save_deps_cache`
   写缓存 JSON，benchmark 直接调 `_load_deps_cache(cache, key)`。

3. **StageRecorder 每轮新建**：`ensure_env` 缓存命中分支会调
   `stage.hit_cache()` 累加 `_hits`。若复用同一 StageRecorder，多轮 benchmark
   会累积 hits 不影响功能但语义不清晰。每轮新建避免状态泄漏，与
   `test_project_info_from_dir_baseline` 用 `pedantic + setup` 每轮清空缓存
   的设计哲学一致。

4. **基线测试标 `@pytest.mark.slow`**：与现有 8 个基线一致，默认门禁
   (`make check` = `pytest -m "not slow"`) 跳过，仅 `--benchmark-only` 显式
   运行。基线测试用于开发期对比，不阻断 CI（与 req-40 设计一致）。

5. **mirror fixture 复用 conftest**：`TestNuitkaEnsureEnvBaseline` 用
   `conftest.py` 的 `mirror` fixture（`MirrorConfig(name="t", ...)`），不
   重复定义。ensure_env 缓存命中分支不用 mirror.pypi_index，但签名需要
   MirrorConfig 参数，传 fixture 即可。

## 代码实现情况

### TestNuitkaEnsureEnvBaseline

```python
@pytest.mark.slow
class TestNuitkaEnsureEnvBaseline:
    """Nuitka 环境就绪性能基线（iter-89 缓存命中场景）."""

    def test_ensure_env_cache_hit_baseline(
        self, benchmark, tmp_path, monkeypatch, mirror,
    ) -> None:
        from fspack.packaging.nuitka import NuitkaCompiler
        from fspack.platform import Platform
        from fspack.progress import StageRecorder

        monkeypatch.setattr("fspack.packaging.loader.mingw_available", lambda: True)
        cache_root = tmp_path / "nuitka_cache"
        cache_dir = NuitkaCompiler._nuitka_cache_dir(cache_root, "3.11.9")
        nuitka_pkg = cache_dir / "nuitka"
        nuitka_pkg.mkdir(parents=True, exist_ok=True)
        (nuitka_pkg / "__init__.py").write_text("", encoding="utf-8")

        def _run() -> str:
            st = StageRecorder("Nuitka 环境")
            return NuitkaCompiler.ensure_env(
                cache_root, "3.11.9", Platform.WINDOWS, mirror, stage=st
            )

        result = benchmark(_run)
        assert result == "4.1.3"
```

### TestWheelDownloadCacheBaseline

```python
@pytest.mark.slow
class TestWheelDownloadCacheBaseline:
    _WHEEL_COUNT = 50  # 与 AST 基线 50 个 .py 文件对齐

    def test_wheel_download_cache_hit_baseline(self, benchmark, tmp_path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        wheels = []
        for i in range(self._WHEEL_COUNT):
            whl = cache / f"pkg_{i:03d}-1.0.0-py3-none-any.whl"
            whl.write_bytes(b"x" * 1024)
            wheels.append(whl)
        key = _deps_cache_key(("pkg_000",), "3.11.9", ("win_amd64",))
        _save_deps_cache(cache, key, wheels)

        result = benchmark(_load_deps_cache, cache, key)
        assert result is not None
        assert len(result) == self._WHEEL_COUNT
        assert {p.name for p in result} == {w.name for w in wheels}
```

## 测试验证结果

- 新基线功能验证（`--benchmark-disable` 退化为普通测试）：
  `2 passed in 0.10s`，功能正确性断言通过
- ruff check：All checks passed
- ruff format --check：118 files already formatted
- pyrefly check：0 errors（10 suppressed, 6 warnings not shown）
- pytest：1835 passed, 12 skipped, 10 deselected in 12.75s
  - 10 deselected = 10 个 slow 基线测试（默认门禁跳过，符合规则）
- coverage：95.22% ≥ 95%

## 基线矩阵最终状态（iter-89 验收）

| # | 基线测试 | 类 | 配套迭代 | 状态 |
|---|---------|-----|---------|------|
| 1 | test_collect_imports_and_submodules_baseline | TestAstBaseline | iter-52 | ✅ |
| 2 | test_analyze_dependencies_baseline | TestAstBaseline | iter-52 | ✅ |
| 3 | test_classify_entry_baseline | TestSlimBaseline | - | ✅ |
| 4 | test_slim_unpack_baseline | TestSlimBaseline | iter-53 | ✅ |
| 5 | test_source_fingerprint_baseline | TestFingerprintBaseline | iter-54 | ✅ |
| 6 | test_project_info_from_dir_baseline | TestProjectInfoBaseline | iter-94 | ✅ |
| 7 | test_project_info_from_dir_cached_baseline | TestProjectInfoBaseline | iter-94 | ✅ |
| 8 | test_generate_wrapper_source_baseline | TestEntryWrapperBaseline | iter-102 | ✅ |
| 9 | test_ensure_env_cache_hit_baseline | TestNuitkaEnsureEnvBaseline | iter-112 | ✅ 新增 |
| 10 | test_wheel_download_cache_hit_baseline | TestWheelDownloadCacheBaseline | iter-112 | ✅ 新增 |

iter-89 验收「基线测试数 ≥ 8，覆盖 AST/指纹/wheel/slim/配置/Nuitka/缓存
全场景」达成：10 个基线覆盖 7 大场景（AST 2 + 指纹 1 + wheel 缓存 1 +
slim 2 + 配置 2 + Nuitka 1 + 入口包装器 1）。

## 遗留事项

- iter-87：collect_imports_and_submodules 仍用 list+dict 双结构，未改生成器
  （单文件 AST 操作，500 文件场景由 analyze_dependencies 调度，改造收益
  不明显且可能破坏 API）
- iter-88：测试 fixture 共享化状态待核实（req-40 仍标 `[ ]`，但 iter-111
  记录显示 conftest.py 已有 mirror fixture + tests/_stubs.py 辅助桩）
- iter-90：基线快照 0001_iter80-baseline.json 未提交（.benchmarks 下只有
  doctor 相关 json）；docs 缺性能基线对比指南；CI benchmark job 已有
  （.github/workflows/ci.yml L81-133）

## 下一轮计划

- iter-113：推进 iter-90 性能基线 CI 门禁固化（提交基线快照 + 文档指南）
  或 iter-87 collect_imports_and_submodules 生成器改造
- iter-88 状态核实：若 conftest.py + _stubs.py 已满足 fixture 共享化验收，
  勾选 req-40 iter-88 完成
