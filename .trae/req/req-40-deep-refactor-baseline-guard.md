# 深度重构与性能基线守护（10 项迭代 iter-81 ~ iter-90）

## 背景

req-37 完成性能基线建立与 5 大文件拆分（iter-51~60），req-38 规划代码质量与
可靠性（iter-61~70），req-39 规划功能增强与生态建设（iter-71~80）。本需求
在 iter-71~80 完成后，对剩余大文件进行**深度重构**，并建立**性能基线守护
机制**——每个重构步骤都通过 `pytest-benchmark` 基线对比验证不退化，确保
重构不引入性能回归。

### 现状基线（2026-07-27，iter-80 完成后预期状态）

**性能基线**（已保存为 `.benchmarks/.../0001_iter80-baseline.json`）：

| 测试场景 | Median | Mean | StdDev | 适用迭代 |
|---------|--------|------|--------|---------|
| `test_classify_entry_baseline` | 3.6 μs | 3.7 μs | 4.5 μs | iter-84 |
| `test_collect_imports_and_submodules_baseline` | 33.5 μs | 34.3 μs | 4.0 μs | iter-83 |
| `test_source_fingerprint_baseline` | 428.4 μs | 439.9 μs | 60.2 μs | iter-83/86 |
| `test_slim_unpack_baseline` | 5.2 ms | 5.4 ms | 931.8 μs | iter-84 |
| `test_analyze_dependencies_baseline` | 6.8 ms | 7.0 ms | 459.9 μs | iter-83/86 |

**门禁规则**：重构后基线测试退化 > 10% 失败（`--benchmark-compare` 自动比较）。

**剩余大文件（>400 行，iter-71~72 拆分 nuitka_compile/pipeline 后仍存在）**：

| 模块 | 行数 | 拆分方向 | 影响基线 |
|------|------|---------|---------|
| `packaging/nuitka_env.py` | 540 | env_check / standalone_python / ccache | 无（不在基线） |
| `packaging/wheel_pip.py` | 524 | pip_caller / uv_caller / sdist_fallback | slim_unpack（间接） |
| `analyzer.py` | 445 | ast_scanner / fingerprint / stdlib_classify | analyze_dependencies + fingerprint |
| `slim/qt.py` | 443 | qt_closure / qt_classify / qt_helpers | classify_entry + slim_unpack |
| `packaging/runtime.py` | 340 | runtime_download / runtime_extract | 无（不在基线） |
| `slim/spec.py` | 325 | spec_base / registry | classify_entry（间接） |

**类型安全缺口**：NuitkaCompiler 三 mixin（NuitkaEnv/NuitkaCompile/NuitkaVerify）
跨类调用用 `# type: ignore[attr-defined]` 抑制，iter-66 计划清理至 ≤40，
本需求进一步用 Protocol 类型声明彻底消除 mixin 跨类调用抑制。

## 10 项迭代任务

### 大文件拆分（iter-81 ~ iter-84）

- [x] **iter-81 nuitka_env.py 拆分**：540 行 → `nuitka_env.py`（NuitkaEnv
  mixin 入口 + C 编译器检查 + ensure_env 编排）/
  `nuitka_standalone.py`（standalone python 下载与缓存：_ensure_build_python/
  _build_python_cache_dir/_build_python_exe）/
  `nuitka_ccache.py`（ccache 下载与 PATH 查找：_ensure_ccache/CCACHE_URLS），
  `nuitka.py` facade 不变。**基线对比**：Nuitka 不在性能基线，仅验证功能
  测试不破坏
- [x] **iter-82 wheel_pip.py 拆分**：524 行 → `wheel_pip.py`（download_wheels
  入口 + 缓存调度）/
  `wheel_resolver.py`（_run_pip_download/_download_online/uv pip compile
  解析）/
  `wheel_sdist.py`（sdist 回退：pip wheel --no-deps 构建纯 Python wheel），
  `wheels.py` facade 不变。**基线对比**：`test_slim_unpack_baseline`
  (5.2ms) 不退化（slim_unpack 依赖 download_wheels 产物，但下载不在基线
  测量范围，仅验证间接影响）
- [x] **iter-83 analyzer.py 拆分**：560 行 → `analyzer.py`（analyze_dependencies
  入口 + ProcessPoolExecutor 并行调度 + _local_packages 本地包识别）/
  `analyzer_ast.py`（collect_imports/collect_imports_and_submodules/
  collect_submodule_imports + STDLIB_FALLBACK + parse_qml_imports + QML 映射表）/
  `analyzer_fingerprint.py`（source_fingerprint + _iter_py_entries +
  _EXCLUDED_DIRS + _is_excluded），`analyzer.py` facade 不变。**基线对比**：
  iter-109 实施完成，median 对比 iter-80 基线：
  `test_analyze_dependencies_baseline` -13%（提速）、
  `test_source_fingerprint_baseline` -0.9%（提速）、
  `test_collect_imports_and_submodules_baseline` 0%，均不退化
- [x] **iter-84 slim/qt.py 拆分**：439 行 → `qt.py`（QtSlimSpec 入口 +
  classify_entry + facade re-export）/
  `qt_closure.py`（_qt_module_closure + 依赖映射表 _QT_MODULE_DEPS/
  _QT_PLUGIN_DEPS/_QT_RESOURCE_DEPS/_QT_QML_DEPS/_QT_ABI_DLL_DEPS/
  _QT_OPENGL_DEPS/_QT_WEBENGINE_TOP_FILES）/
  `qt_helpers.py`（_is_ffmpeg_dll/_is_opengl_sw_dll/_is_qml_abi_dll/
  _normalize_qt_sub/_qt_dll_submodule + 常量 _QT_EXCLUDE_SUBDIRS/
  _QT_LIB_EXCLUDE_SUBDIRS/_QT_FFMPEG_DLL_PREFIXES/_QT_QML_ABI_DLL_NAMES/
  _QT_OPENGL_SW_DLL_NAMES），`slim/__init__.py` re-export 不变。
  **基线对比**：iter-109 实施完成，median 对比 iter-80 基线：
  `test_classify_entry_baseline` 0%（3.6μs）、
  `test_slim_unpack_baseline` -0.6%（提速），均不退化

### 类型安全深化（iter-85）

- [x] **iter-85 mixin Protocol 类型声明**：NuitkaCompiler 三 mixin
  （NuitkaEnv/NuitkaCompile/NuitkaVerify）跨类调用改用 `typing.Protocol`
  声明接口契约，替代 `# type: ignore[attr-defined]` 抑制。定义
  `NuitkaCompilerProtocol` 描述 mixin 间依赖的方法签名，各 mixin 用
  `cls: NuitkaCompilerProtocol` 类型注解替代裸 `cls`。**基线对比**：
  全量基线不退化（Protocol 仅类型检查期生效，运行时无开销）；
  pyrefly 抑制警告数从 iter-66 后的 ≤40 进一步降至 ≤10

### 性能敏感路径优化（iter-86 ~ iter-87）

- [ ] **iter-86 配置加载缓存**：`parsing.py` 的 `ProjectInfo.from_dir`
  每次解析 pyproject.toml，构建流程内多次调用（build/resolve_project_info/
  installer）重复读取。增加模块级 `lru_cache` 按 `(project_dir, mtime)`
  缓存解析结果，mtime 变化时失效。**基线对比**：
  `test_analyze_dependencies_baseline` (6.8ms) 不退化（analyze_dependencies
  内部不调 from_dir，但 build 流程提速）；新增
  `test_project_info_from_dir_baseline` 基线测量配置解析耗时
- [ ] **iter-87 AST 分析内存优化**：`collect_imports_and_submodules`
  当前用 `list` + `set` 双结构收集导入，大项目（500+ 文件）内存占用高。
  改用生成器 + 单次 `dict` 合并，减少中间结构。`source_fingerprint`
  的 `os.scandir` 递归改用 `yield` 生成器避免全量路径列表。**基线对比**：
  `test_collect_imports_and_submodules_baseline` (33.5μs) 提速 ≥10% 或
  不退化；`test_source_fingerprint_baseline` (428μs) 提速 ≥10% 或不退化

### 测试基础设施（iter-88 ~ iter-89）

- [ ] **iter-88 测试 fixture 共享化**：审查 tests/ 下 23 个测试文件，
  识别重复的 fixture（tmp_path 包装、mock subprocess、样本项目构造等）
  提取到 `tests/conftest.py`。减少测试代码重复，提升新测试编写效率。
  **基线对比**：基线测试本身不退化（fixture 重构不影响测量逻辑）
- [ ] **iter-89 性能基线矩阵扩展**：扩展 `test_perf_baseline.py`，
  新增 (1) `test_project_info_from_dir_baseline`（配置解析基线，配套
  iter-86）；(2) `test_nuitka_ensure_env_baseline`（Nuitka 环境检查基线，
  mock 网络下载）；(3) `test_wheel_download_cache_hit_baseline`（wheel
  缓存命中基线，mock pip download）。形成 8 个基线测试覆盖全部核心场景。
  **基线对比**：新增基线不破坏现有 5 个基线

### 基线固化与 CI 门禁（iter-90）

- [ ] **iter-90 性能基线 CI 门禁固化**：(1) 将 `0001_iter80-baseline.json`
  提交到仓库作为基线快照；(2) CI 新增 `perf-regression` job，每次 push
  到 main 跑 `pytest-benchmark --benchmark-compare=0001_iter80-baseline`，
  退化 > 10% 失败；(3) 文档补充性能基线对比指南（如何运行、如何解读、
  如何更新基线）；(4) 全量回归：iter-81~89 累计重构后基线总退化 < 5%。
  **验收**：CI perf-regression job 正常运行，基线快照与文档同步

## 验收标准

- 每次迭代全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- **性能基线守护**：iter-83/84/86/87 重构后对应基线测试退化 ≤ 10%
  （`--benchmark-compare` 自动验证）
- 结构拆分保持公开 API 不变（`__all__` 与 import 路径兼容），所有现有
  测试不破坏
- iter-85 后 pyrefly 抑制警告数 ≤ 10
- iter-89 后性能基线测试数 ≥ 8（覆盖 AST/指纹/wheel/slim/配置/Nuitka/
  缓存全场景）
- iter-90 后 CI perf-regression job 正常运行，iter-81~89 累计基线退化 < 5%

## 实施顺序

1. iter-81~84（大文件拆分，按基线影响范围升序：nuitka_env 无基线 → wheel_pip
   间接 → analyzer 直接 → qt 直接，先易后难积累信心）
2. iter-85（类型安全深化，在拆分完成后统一处理 mixin Protocol）
3. iter-86~87（性能敏感路径优化，在结构稳定后做精细优化）
4. iter-88~89（测试基础设施，在功能稳定后提升测试效率）
5. iter-90（基线固化与 CI 门禁，收尾守护机制）

## 基线对比方法

### 重构后验证流程

```bash
# 1. 重构前保存当前基线（仅首次需要，基线已保存为 0001_iter80-baseline.json）
uv run pytest tests/test_perf_baseline.py --benchmark-only --benchmark-save=iter80-baseline

# 2. 重构后对比基线
uv run pytest tests/test_perf_baseline.py --benchmark-only \
  --benchmark-compare=0001_iter80-baseline \
  --benchmark-compare-fail=mean:10%

# 3. 查看详细对比（含百分比变化）
uv run pytest tests/test_perf_baseline.py --benchmark-only \
  --benchmark-compare=0001_iter80-baseline \
  --benchmark-columns=median,min,mean,stddev
```

### 退化判定规则

- `mean` 退化 > 10%：失败（主指标）
- `median` 退化 > 15%：警告（次要指标，受异常值影响）
- `min` 退化 > 20%：警告（最佳值，受系统噪声影响）
- 标准差 `StdDev` > `mean` 的 50%：警告（测量不稳定）

### 基线更新条件

仅以下情况允许更新基线快照：
1. 功能变更导致基线测试样本变化（如 iter-89 新增基线测试）
2. 性能优化迭代（iter-86/87）验证提速后固化新基线
3. Python 版本升级导致基线数值整体偏移

更新基线须在迭代记录中说明原因，并重新保存为 `0002_<reason>.json`。

## 依赖关系

- iter-83 依赖 iter-52（AST 并行化）已完成，避免拆分与并行化冲突
- iter-84 依赖 iter-60（slim/base.py 拆分）已完成，qt.py 拆分基于
  spec.py 已分离的前提
- iter-85 依赖 iter-66（pyrefly 清理）完成，避免 Protocol 重构与
  抑制清理冲突
- iter-86 依赖 iter-58（config.py 拆分）已完成，parsing.py 已独立
- iter-90 依赖 iter-78（CI 增强）完成，复用 benchmark job 基础设施

## 风险与缓解

- **iter-83 analyzer.py 拆分风险**：`source_fingerprint` 与
  `analyze_dependencies` 是性能基线核心，拆分可能引入函数调用开销。
  缓解：拆分后立即跑基线对比，退化 > 5% 回退拆分
- **iter-87 AST 内存优化风险**：生成器改写可能因惰性求值改变时序，
  影响 ProcessPoolExecutor 并行调度。缓解：保持 `analyze_dependencies`
  入口签名不变，仅优化内部实现
- **iter-90 CI 门禁风险**：CI runner 性能波动可能导致基线误报。
  缓解：用 `--benchmark-compare-fail=mean:10%` 容忍 10% 波动，
  连续 3 次退化才标记失败
