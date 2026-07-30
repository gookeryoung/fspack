# iter-111：NuitkaCompiler Protocol 类型声明（iter-85）

## 需求清单

- [x] iter-85 mixin Protocol 类型声明：消除 `# type: ignore[attr-defined]` 抑制
- [x] pyrefly 抑制警告数从 14 降至 ≤10
- [x] 全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- [x] 全量测试不破坏（1835 passed）

## 迭代目标

核实 req-40 iter-85~90 完成状态，推进未完成项。iter-85 为类型安全深化，
独立无依赖，验收明确（pyrefly ≤10），优先推进。

## iter-85~90 核实结果

| iter | 状态 | 详情 |
|------|------|------|
| iter-85 | 本轮完成 | NuitkaCompiler Protocol 类型声明，消除 attr-defined |
| iter-86 | 已完成 | parsing.py lru_cache + mtime 失效 + clear_project_cache |
| iter-87 | 部分完成 | source_fingerprint 已用生成器+os.scandir；collect_imports_and_submodules 仍用 list+dict 双结构 |
| iter-88 | 已完成 | tests/conftest.py mirror fixture + tests/_stubs.py 辅助桩 |
| iter-89 | 部分完成 | 8 个基线测试达标但缺 test_nuitka_ensure_env_baseline 与 test_wheel_download_cache_hit_baseline |
| iter-90 | 部分完成 | CI benchmark job 已有；基线快照 0001_iter80-baseline.json 未提交；文档缺性能基线对比指南 |

pyrefly 核实前：14 suppressed, 6 warnings。

## 改动文件清单

新增：
- `src/fspack/packaging/nuitka_protocol.py`（266 行）：NuitkaCompilerProtocol
  接口契约，声明 6 个 mixin（Env/Standalone/Ccache/Strip/Compile/Verify）
  全部跨类调用方法签名。Protocol 仅类型检查期生效，运行时无开销。

修改：
- `src/fspack/packaging/nuitka_compile.py`：
  - 删除顶部 10 个 stub 方法（`_runtime_python`/`_is_nuitka_cached`/
    `_ensure_ccache`/`_build_compile_env`/`_resolve_jobs`/`ensure_env`/
    `_ensure_build_python`/`_nuitka_cache_dir`/`_strip_compiled_sources`/
    `_cleanup_build_dirs`，均 `raise NotImplementedError`）
  - 5 个 classmethod（compile_src/compile_packages/_resolve_compile_python/
    _compile_files/compile_with_stamp）cls 注解从裸 `cls` 改为
    `cls: type[NuitkaCompilerProtocol]`
  - 添加 `if TYPE_CHECKING: from fspack.packaging.nuitka_protocol import NuitkaCompilerProtocol`
  - 更新 docstring 移除"stub 在类顶部"描述，改为"Protocol 类型契约声明"
- `src/fspack/packaging/nuitka_strip.py`：
  - `_strip_compiled_sources` cls 注解改为 `type[NuitkaCompilerProtocol]`
  - 删除 L80 `# type: ignore[attr-defined]`（NuitkaVerify mixin 跨类调用）
  - 更新 docstring 描述从"无法 stub 占位"改为"Protocol 类型契约"
- `src/fspack/doctor_templates.py`：
  - L545-559 启动耗时排名逻辑改用 walrus 操作符 + tuple 解构收窄类型：
    `run_pairs: list[tuple[TemplateBuildResult, TemplateRunResult]]` 替代
    `run_results`，消除 3 处 `# type: ignore[union-attr]`
- `tests/test_cli_doctor.py`：
  - L998 `_FakeProc.communicate` 加返回类型注解 `-> tuple[str, str]`，
    消除 `# type: ignore[no-untyped-def]`

## 关键决策与依据

1. **Protocol 完整声明所有 facade 方法**：初版 Protocol 只声明跨 mixin 方法，
   pyrefly 报 `NuitkaCompilerProtocol has no class attribute compile_src`
   等 4 处 missing-attribute。原因：`cls: type[NuitkaCompilerProtocol]` 让
   pyrefly 把 cls 当作 Protocol 类型，cls.compile_src（NuitkaCompile 自身方法）
   也需在 Protocol 声明。补全 NuitkaCompile 全部 11 个方法后通过。

2. **为何不用 stub 方法占位**：NuitkaStrip 调 `cls._verify_compiled_modules`
   （NuitkaVerify 提供）不能 stub——NuitkaStrip 在 MRO 中位于 NuitkaVerify
   前面，stub 会覆盖 NuitkaVerify 的真实实现破坏运行时。Protocol 方案统一
   所有 mixin 的类型声明，无需 stub，且消除 NuitkaCompile 顶部 10 个 stub
   方法（原 `# pragma: no cover` 占位代码）。

3. **walrus + tuple 解构收窄 union-attr**：doctor_templates.py 的
   `run_results = [r for r in ok_results if r.run_result and ...]` 过滤了
   None，但 pyrefly 无法通过列表推导收窄类型。改用
   `(r, rr) for r in ok_results if (rr := r.run_result) is not None`，
   tuple 解构让 `rr` 成为非 None 的 TemplateRunResult，消除 3 处 union-attr。

4. **测试桩 communicate 加返回类型**：`_FakeProc.communicate` 缺返回类型注解
   触发 no-untyped-def。加 `-> tuple[str, str]` 与 subprocess.Popen.communicate
   签名一致，消除 1 处抑制，达 ≤10 目标。

## 代码实现情况

### Protocol 设计（nuitka_protocol.py）

```python
class NuitkaCompilerProtocol(Protocol):
    """各 mixin 的 classmethod 用 cls: type[NuitkaCompilerProtocol] 注解，
    pyrefly 据此解析 cls.<method>() 跨 mixin 与同 mixin 内调用。"""
    # ==== NuitkaEnv 提供 ====
    @staticmethod
    def _nuitka_cache_dir(cache_root: Path, py_version: str) -> Path: ...
    # ... 6 个 staticmethod + 5 个 classmethod（NuitkaEnv/Standalone/Ccache/Strip/Verify）
    # ==== NuitkaCompile 提供 ====
    @classmethod
    def compile_src(cls, src_dir: Path, ...) -> None: ...
    # ... 11 个方法（含 staticmethod/classmethod）
```

### NuitkaCompile cls 注解改造

```python
class NuitkaCompile:
    # 删除顶部 10 个 stub 方法（raise NotImplementedError）

    @classmethod
    def compile_src(  # noqa: PLR0913
        cls: type[NuitkaCompilerProtocol],  # 替代裸 cls
        src_dir: Path,
        ...
    ) -> None:
        ...
        ccache_exe = cls._ensure_ccache(...)  # 不再需要 type: ignore
```

### NuitkaStrip 跨 mixin 调用

```python
class NuitkaStrip:
    @classmethod
    def _strip_compiled_sources(
        cls: type[NuitkaCompilerProtocol],  # 替代裸 cls
        ...
    ) -> int:
        ...
        # 删除 # type: ignore[attr-defined]
        verified_files, unverified_artifacts = cls._verify_compiled_modules(
            verify_py_exe, compiled_files
        )
```

## 测试验证结果

- ruff check：All checks passed
- ruff format --check：117 files already formatted
- pyrefly check：0 errors (10 suppressed, 6 warnings not shown)
  - 从 14 suppressed 降至 10，达 iter-85 验收 ≤10 目标
  - 减少：1 处 attr-defined（nuitka_strip.py L80）+ 3 处 union-attr
    （doctor_templates.py L547/553/555）+ 1 处 no-untyped-def
    （test_cli_doctor.py L998）+ 1 处 attr-defined（nuitka_compile.py
    stub 删除后 docstring 文字提及不再计入）
- pytest：1835 passed, 12 skipped, 8 deselected in 15.14s
- coverage：95.22% ≥ 95%

## 遗留事项

- iter-87：collect_imports_and_submodules 仍用 list+dict 双结构，未改生成器
  （单文件 AST 操作，500 文件场景由 analyze_dependencies 调度，改造收益
  不明显且可能破坏 API）
- iter-89：缺 test_nuitka_ensure_env_baseline 与 test_wheel_download_cache_hit_baseline
  2 个基线（8 个基线总数达标）
- iter-90：基线快照 0001_iter80-baseline.json 未提交（.benchmarks 下只有
  doctor 相关 json）；docs 缺性能基线对比指南
- iter-86/88 已完成

## 下一轮计划

- iter-112：推进 iter-89 性能基线矩阵扩展（补 nuitka/wheel 2 个基线）
  或 iter-87 collect_imports_and_submodules 生成器改造
- iter-90 依赖 iter-89 完成，最后推进基线快照提交 + 文档指南
