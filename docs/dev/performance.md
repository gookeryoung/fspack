# 性能基线对比与更新指南

fspack 性能基线体系用 `pytest-benchmark` 建立可量化基线，配合 `fsp doctor --bench-compare`
按基线类别分组对比，检测性能退化。CI 在 `push to main` 时自动运行基线测试并与
历史最佳基准对比，退化超阈值则阻断 CI。

## 健壮性与打包速度改进记录（iter-126~145）

req-49 驱动的 20 轮迭代分 4 个阶段交付：阶段 1 健壮性基础（iter-126~130）、
阶段 2 打包速度优化（iter-131~135）、阶段 3 深度健壮性（iter-136~140）、
阶段 4 性能基线守护（iter-141~145）。以下汇总各阶段关键改进。

### 健壮性改进

#### 网络与下载可靠性

- **iter-126 下载重试与完整性校验**：`Downloader.download` 引入指数退避
  重试（3 次，1s/2s/4s），区分可重试错误（连接超时、503）与不可重试（404）；
  `--require-hashes` 模式下校验下载归档 sha256（重试最初基于 tenacity，
  后以标准库手写循环替换以精简第三方依赖）
- **iter-132 wheel 下载 uv 加速**：`_download_online` 在 uv 可用时改用
  `uv pip download`（比 pip 快 2-5x），保留 pip 回退；uv 路径检测在解析与
  下载阶段共享

#### subprocess 超时与死锁防护

- **iter-127**：`_stream_compile` 增加 600s 超时，超时 kill 进程；修复
  `process.wait()` 顺序——先 join drain 线程再 wait，避免 PIPE 缓冲区满死锁；
  `_parse_parallel` 单 chunk 60s 超时；`_precompile_pyc` compileall 300s 超时

#### 缓存健壮性

- **iter-128**：`_load_deps_cache` 损坏时删除缓存文件避免反复尝试；
  `_precompile_pyc` 编译失败不写 stamp 下次重试；Nuitka stamp 用
  `tempfile + rename` 原子化；`fsp doctor --check-cache` 检测损坏缓存
- **iter-129 内容 hash 回退**：`_site_packages_fingerprint` 增加 sha256 内容
  hash 选项；`source_fingerprint` 在 mtime_ns + size 相同但显式启用内容 hash
  时二次校验；覆盖 FAT32 场景（mock mtime 秒级精度）
- **iter-139 缓存目录健康检查**：`CacheHealthReport` 统一封装扫描结果；
  `_scan_cache_health` 作为唯一扫描入口，消除三处重复扫描逻辑；
  `fsp cache status`/`fsp cache clean` 子命令；孤儿 wheel 检测

#### 错误恢复与自愈

- **iter-130**：构建开始检测 `dist/` 半成品（有 runtime/ 无 exe）提示清理；
  wheel 下载失败清理部分 `.whl`；Nuitka 编译失败清理 `.build/`；runtime 解压
  失败删除损坏归档
- **iter-140 构建中断恢复**：`fsp b` 检测 `dist/` 半成品（含 `.build_failed`
  标记），`--auto-clean` 自动清理或交互确认；构建异常写入 `dist/.build_failed`
  JSON 记录失败阶段；`fsp c` 保留诊断文件

#### 安全加固

- **iter-136 tarball 安全 extract**：`_validate_tar_member` 实现 PEP 706 data
  filter 等价（绝对路径/路径穿越/符号链接/硬链接/设备文件）；
  `_validate_zip_member` 校验 zip 条目；10 个恶意归档测试

#### 编译与解析容错

- **iter-137 编译产物验证增强**：`_individual_import_test` 并发化
  （ThreadPoolExecutor，4 worker），批量测试崩溃后逐个定位损坏 .pyd；
  Nuitka 编译失败记录到 `.nuitka_failed_files.json`，下次跳过
- **iter-138 依赖分析异常容错**：`_parse_file_worker` ast.parse 失败记录到
  `ast_errors` 字段不静默跳过；`_parse_parallel` 从 `pool.map` 改为
  `submit + as_completed`，单 worker 卡死不阻塞其他；QML 解析失败不影响主流程

### 打包速度改进

#### Nuitka 编译并行化

- **iter-131**：`_compile_files` 用 `ThreadPoolExecutor`（subprocess 释放
  GIL，线程足够）并行编译多个 `.py`，`max_workers=min(cpu_count, 4)`；保留
  心跳线程但改为全局心跳
- **实测提速 3.82x**（50 文件串行 514ms → 并行 135ms）
- **iter-133 多入口 loader 并行编译**：`_build_entry_loaders` 用
  `ThreadPoolExecutor` 并行编译多个 entry loader；共享
  `tempfile.TemporaryDirectory` 工作目录

#### wheel 下载加速

- **iter-132**：`_download_online` 在 uv 可用时改用 `uv pip download`
  （比 pip 快 2-5x）；保留 pip 回退
- **实测提速 2.93x**（50 wheel pip 213ms → uv 73ms）

#### AST 并行解析调优

- **iter-134**：`_parse_parallel` chunksize 自适应算法优化（按文件大小加权，
  避免大文件扎堆）；`ProcessPoolExecutor` 改用 `initializer` 预加载
  `_STDLIB` 集合，减少 worker 启动开销

#### 冷启动 import 终极惰性化

- **iter-135**：
  - `pipeline/__init__.py` 顶部 `fspack.ui.console` 移至函数内（解决 ~17ms）
  - `stages.py` 顶部 `BuildTracker` 类型注解改用字符串前向引用，`progress`
    导入移至 `build()` 内（解决 ~8ms）
  - `wheels/downloader.py` 顶部 `threading` 移至方法内
  - 守护测试扩展防止回退
  - `import fspack.builder` 从 88.6ms 降至 ~61ms

### 关键量化成果

| 指标 | 优化前 | 优化后 | 改善 |
|------|--------|--------|------|
| Nuitka 50 文件编译 | 514ms（串行） | 135ms（并行） | 3.82x |
| wheel 50 包下载 | 213ms（pip） | 73ms（uv） | 2.93x |
| `import fspack.builder` | 88.6ms | ~61ms | -31% |
| lazy-import 启动收益 | — | 51ms | numpy `__init__.py` 延迟 |
| ccache 加速比 | — | 8.08x | 命中 vs 未命中 |
| 性能基线测试数 | 10 | 26 | +16 |
| CI 退化检测精度 | 单一 25% | 6 类别 10-25% | 误报↓ 灵敏度↑ |

### 多入口 loader 性能门禁

`tests/perf/test_loader_perf_baseline.py` 补充多入口 C loader 的确定性门禁与真实
MinGW 基线。普通门禁（非 slow）用假工具链断言：1/5/20 入口下 C 对象恒编译
1 次、Windows 资源与链接各按入口数增长、工具链探测次数与入口数无关；
预热构建零工具调用；持久化工具链身份命中时只做有限 `stat`/`which`。
`slow + e2e` 基线用真实 MinGW 测量单入口冷构建、5 入口冷构建与热构建，
供 `fsp doctor --bench-compare` 按 `loader_build` 类别（25% 阈值、5ms 噪声
下限）检测退化；缺少 MinGW/windres 时自动跳过。

## 基线测试清单

共 39 个基线测试，按 11 个类别分组，各类别有不同的 StdDev 特性与退化阈值（CI 的
ubuntu job 上 `loader_build` 3 项因缺少 MinGW 跳过，实际参与对比 36 项）：

### ast_scheduling_small / ast_scheduling_large（6 + 4 个，阈值 100%）

`tests/perf/test_perf_baseline.py` 的 `test_small_files[...]` / `test_large_files[...]`，
测量多文件 AST 解析在串行/默认并行调度下的整目录耗时。测试含进程池启动与文件
I/O，跨 runner 世代波动极大：同一份代码在 2026-11-23 前后 CI run 实测
small `project0-serial` 为 16.59/21.95/24.37/36.25ms（2.2x），large
`project0-serial` 为 1.171/1.578/1.691/2.227s（1.9x）。测试文件本身已声明
"不对跨机器耗时比值作硬断言"，因此这两个类别阈值设为 100%（仅捕获 ≥2x 灾难性
退化），并分别设 8ms / 600ms 绝对噪声下限。

### ast_analysis（2 个，阈值 50%）

`tests/perf/test_perf_baseline.py` 的 AST import 收集与依赖分析基线，CPU/文件系统
敏感，跨 runner 实测 4.28~9.27ms（2.2x）。阈值 50% + 2ms 绝对下限吸收抖动，
仍能捕获 ≥2x 的调度退化。

### core（7 个，阈值 10%）

`tests/perf/test_perf_baseline.py`，核心场景基线，确定性高 StdDev <1%。

| 测试名 | 场景 |
|--------|------|
| `test_classify_entry_baseline` | 入口类型分类 |
| `test_slim_unpack_baseline` | wheel 精简解包 |
| `test_project_info_from_dir_baseline` | ProjectInfo 冷解析 |
| `test_project_info_from_dir_cached_baseline` | ProjectInfo 缓存命中 |
| `test_generate_wrapper_source_baseline` | EntryWrapper 生成 |
| `test_ensure_env_cache_hit_baseline` | Nuitka 环境缓存命中 |
| `test_wheel_download_cache_hit_baseline` | wheel 依赖缓存命中 |

### fingerprint（1 个，阈值 10%）

`tests/perf/test_perf_baseline.py` 的 `test_source_fingerprint_baseline`，stat 密集
µs 级微基准。同一份代码跨 runner 绝对抖动可达 ~100µs，因此单独成类并设
120µs 绝对噪声下限（`noise_floor`）；真实退化（数倍变慢）绝对差远超下限仍会捕获。

### build_perf（4 个，阈值 25%）

`tests/perf/test_build_perf_baseline.py`，端到端编排基线，含 AST 扫描与文件 I/O
抖动 StdDev 5-27%。mock 掉 7 个阶段函数（runtime 下载、wheel 下载、源码编译等），
仅测量阶段编排 + BuildTracker + ProjectInfo 解析 + console 渲染开销。该类别基线
median 仅 3.3~4.4ms，2026-11-23 前后连续 5 次 CI run 实测绝对差 +2.23~+5.93ms
（同一份代码跨 runner 世代差异），因此设 4ms 绝对噪声下限（约等于"2x 才告警"），
更大规模的退化仍会被捕获；配合系统性退化判定吸收 run 级波动。

| 测试名 | 场景 |
|--------|------|
| `test_small_project_cold_cache_baseline` | 小项目（1 入口 3 依赖）冷缓存 |
| `test_small_project_warm_cache_baseline` | 小项目热缓存 |
| `test_medium_project_cold_cache_baseline` | 中项目（10 入口 20 依赖）冷缓存 |
| `test_medium_project_warm_cache_baseline` | 中项目热缓存 |

### nuitka_compile（4 个，阈值 10%）

`tests/perf/test_nuitka_compile_baseline.py`，Nuitka 编译基线，mock `time.sleep`
模拟编译耗时，确定性高 StdDev <1%。

| 测试名 | 场景 |
|--------|------|
| `test_serial_compile_baseline` | 串行编译 50 文件 |
| `test_parallel_compile_baseline` | 并行编译 50 文件（iter-131） |
| `test_ccache_hit_baseline` | ccache 命中模拟（2ms/文件） |
| `test_ccache_miss_baseline` | ccache 未命中模拟（20ms/文件） |

### wheel_download（3 个，阈值 10%）

`tests/perf/test_wheel_download_baseline.py`，wheel 下载基线，mock `time.sleep`
模拟下载耗时，确定性高 StdDev <1%。与 runner 负载无关，作为系统性抖动判定的
"无抖动基准"：这些测试一旦退化即视为真实代码回归，不做抖动豁免。

| 测试名 | 场景 |
|--------|------|
| `test_pip_parallel_download_baseline` | pip 并行下载 50 包（30ms/包） |
| `test_uv_parallel_download_baseline` | uv 并行下载 50 包（10ms/包，iter-132） |
| `test_cold_download_baseline` | 冷下载完整编排 |

### wheel_cache（1 个，阈值 10%）

`test_cache_hit_baseline`：deps_cache 命中分支含 50 次 `is_file()`，文件系统/调度
抖动为 µs 级（2026-11-23 run 实测 +160.6µs / +22.4%），故单独成类并设 300µs
绝对噪声下限；真实退化（检查逻辑变复杂、批量 stat 变慢）远超该量级仍会捕获。

### entry_startup（4 个，阈值 15%）

`tests/perf/test_entry_startup_baseline.py`，启动时间基线，真实 `subprocess.run`
启动 Python 解释器，subprocess 抖动 StdDev 5-8%。

| 测试名 | 场景 |
|--------|------|
| `test_default_startup_baseline` | 默认启动（numpy `__init__.py` 全量执行） |
| `test_lazy_import_startup_baseline` | lazy-import 启用（延迟 `__init__.py`） |
| `test_no_site_startup_baseline` | `python -S` 模拟 `--no-site` |
| `test_no_site_lazy_combined_baseline` | `python -S` + lazy 双重优化 |

### loader_build（3 个，阈值 25%）

`tests/perf/test_loader_perf_baseline.py`，真实 MinGW 编译/链接基线，冷构建
StdDev 5-20%，热构建只做校验与复制。缺少 MinGW/windres 时自动跳过。

| 测试名 | 场景 |
|--------|------|
| `test_loader_single_entry_cold_baseline` | 单入口冷缓存（C 1 次、资源 1 次、链接 1 次） |
| `test_loader_multi_entry_cold_baseline` | 5 入口冷缓存（C 仍 1 次，资源/链接各 5 次） |
| `test_loader_multi_entry_warm_baseline` | 5 入口热缓存（零工具调用） |

## 运行方式

### 本地运行基线测试

```bash
# 运行全部基线测试（slow marker，默认门禁不执行）
uv run pytest tests/perf/test_perf_baseline.py tests/perf/test_build_perf_baseline.py \
  tests/perf/test_nuitka_compile_baseline.py tests/perf/test_wheel_download_baseline.py \
  tests/perf/test_entry_startup_baseline.py -m slow --benchmark-only

# 运行单个类别的基线测试
uv run pytest tests/perf/test_nuitka_compile_baseline.py -m slow --benchmark-only

# 查看基线统计（median/min/mean/stddev）
uv run pytest tests/perf/test_perf_baseline.py -m slow --benchmark-only \
  --benchmark-columns=median,min,mean,stddev
```

### 保存基线快照

```bash
# 保存当前运行为基线快照（命名遵循 iter<N> 或 main 约定）
uv run pytest tests/perf/test_perf_baseline.py -m slow --benchmark-only \
  --benchmark-save=iter145 --benchmark-min-rounds=20 --benchmark-warmup=on

# 快照保存到 .benchmarks/<platform>/<name>_<datetime>.json
```

### CI 自动运行

CI benchmark job（`.github/workflows/ci.yml`）仅在 `push to main` 时触发：

1. 运行 6 个基线测试文件（39 个测试，ubuntu 上跳过 `loader_build` 3 项），
   `--benchmark-min-rounds=20 --benchmark-warmup=on`
2. 保存结果到 `.benchmarks/`（按平台 + Python 版本缓存）
3. 调用 `fsp doctor --bench-compare --bench-threshold 25` 与历史最佳基准对比
4. 退化超阈值则 exit 1 阻断 CI；系统性退化（机器抖动）不阻断

## 对比工具用法

`fsp doctor --bench-compare` 扫描 `.benchmarks/` 下所有历史 JSON，按测试名
找最小 median 作为最佳基准，当前运行与最佳对比。

### 基本用法

```bash
# 默认：按类别阈值对比，未匹配类别用全局 25%
uv run fsp doctor --bench-compare

# 自定义全局阈值（用于未匹配类别的测试）
uv run fsp doctor --bench-compare --bench-threshold 20

# 指定基线 JSON 存储目录（默认 .benchmarks/）
uv run fsp doctor --bench-compare --bench-dir .benchmarks/

# 列出基线类别与阈值
uv run fsp doctor --bench-compare --bench-list-categories

# 禁用类别分组，仅用全局阈值
uv run fsp doctor --bench-compare --bench-no-categories --bench-threshold 25
```

### 输出格式

```
当前运行: 0126_iter145-final_Windows-CPython-3.11-64bit_20260804T120000.json
类别阈值: ast_scheduling_small=100%, ast_scheduling_large=100%, ast_analysis=50%, build_perf=25%, nuitka_compile=10%, wheel_download=10%, wheel_cache=10%, entry_startup=15%, loader_build=25%, fingerprint=10%, core=10% | 全局阈值: 25%

测试名                       当前 median    最佳 median          Δ   阈值        类别      最佳来源     状态
...
汇总: 39 项 | 退化 0 | 提升 0 | 首次 39 | 全局阈值 25%
```

### 退出码

- `0`：无退化、无历史基线、或系统性退化（机器抖动不阻断）
- `1`：有退化超过类别阈值（或全局阈值，对未匹配类别的测试）

## 各类别阈值与依据

阈值基于 iter-141~144 实测 StdDev 设定，兼顾检测灵敏度与误报控制：

| 类别 | 阈值 | StdDev 依据 | 说明 |
|------|------|------------|------|
| ast_scheduling_small | 100% | 2.2x 跨 runner | 多文件 AST 解析 + 进程池 + I/O，8ms 绝对下限，仅捕获 ≥2x 退化 |
| ast_scheduling_large | 100% | 1.9x 跨 runner | 大样本 AST 解析，600ms 绝对下限，仅捕获 ≥2x 退化 |
| ast_analysis | 50% | 2.2x 跨 runner | AST 收集/依赖分析，2ms 绝对下限 |
| core | 10% | <1% of median | 确定性高（JSON/内存操作），10% 足以检测真实退化 |
| build_perf | 25% | 5-27% of median + 加性 2-6ms | 含 AST 扫描与文件 I/O 抖动，4ms 绝对下限 |
| nuitka_compile | 10% | <1% of median | mock `time.sleep` 确定性高，作为无抖动基准 |
| wheel_download | 10% | <1% of median | mock `time.sleep` 确定性高，作为无抖动基准 |
| wheel_cache | 10% | µs 级 stat 抖动 | 含 50 次 `is_file()`，300µs 绝对下限 |
| entry_startup | 15% | 5-8% of median | 真实 subprocess 启动抖动，15% 平衡灵敏度与误报 |
| fingerprint | 10% | ~100µs 绝对抖动 | stat 密集 µs 级微基准，另设 120µs `noise_floor` 吸收跨 runner 绝对差 |

### 系统性退化检测

当多个不相关测试同步中等幅度退化时，判定为机器负载波动而非代码问题：

- 可比测试数 ≥ 5
- 无确定性类别（`nuitka_compile`、`wheel_download` 等 mock `time.sleep` 类别）
  测试退化——这些测试耗时与 runner 负载无关，一旦退化即视为真实代码信号，
  不再做抖动豁免
- 抖动敏感测试的退化率 ≥ 40%（mock sleep 测试不计入分母）
- 退化测试的平均退化幅度 ≥ 20%

2026-11-23 的 run 整体 14/36（38.9%，差 0.4 个百分点未触发旧门槛），但 7 个确定性
测试全部正常、14/29 抖动敏感测试退化（48.3%）、平均 46.4%，且退化为加性偏移
（build_perf 绝对差集中 +2.90~+3.17ms），确认是 runner 抖动。因此退化率改按
"抖动敏感"测试计算，避免确定性测试稀释分母导致漏判。

满足以上条件时输出警告但不阻断 CI（exit 0），建议人工审查 artifact 中的 JSON
数据确认无真实退化。

## 退化排查指南

### 1. 确认退化是否真实

CI benchmark job 失败时，先下载 `benchmark-results` artifact 检查 JSON 数据：

```bash
# 查看当前运行的 median 与历史最佳的对比
uv run fsp doctor --bench-compare --bench-dir .benchmarks/
```

如果多个不相关测试同步大幅退化（如 AST 分析 + wheel 下载 + 启动时间同时退化
30%+），很可能是机器负载波动。对比工具会自动检测并输出系统性退化警告，不阻断 CI。

### 2. 定位退化代码

如果只有特定类别的测试退化：

- **core 退化**：检查 `analyzer.py`、`slim.py`、`config.py`、`packaging/wheels/cache.py`
  的近期改动
- **build_perf 退化**：检查 `packaging/pipeline/` 下的阶段函数与 `BuildTracker`
- **nuitka_compile 退化**：检查 `packaging/nuitka/compile.py` 的并行化逻辑
- **wheel_download 退化**：检查 `packaging/wheels/downloader.py` 的并行下载逻辑
- **entry_startup 退化**：检查 `packaging/entry_wrapper.py` 与 lazy-import 注入

### 3. 更新基线

如果退化是预期的（如功能扩展导致合理变慢），更新基线：

```bash
# 重新运行基线测试保存新快照
uv run pytest tests/perf/test_perf_baseline.py tests/perf/test_build_perf_baseline.py \
  tests/perf/test_nuitka_compile_baseline.py tests/perf/test_wheel_download_baseline.py \
  tests/perf/test_entry_startup_baseline.py -m slow --benchmark-only \
  --benchmark-save=main --benchmark-min-rounds=20 --benchmark-warmup=on

# 提交新快照到 .benchmarks/（可选，CI 缓存会自动更新）
```

## 历史基线快照

`.benchmarks/` 目录存放历史基线 JSON 文件，按平台与 Python 版本分子目录：

```
.benchmarks/
├── Windows-CPython-3.11-64bit/          # 本地 Windows 基线
│   ├── main_20260804T120000.json        # CI main 分支基线
│   └── iter145_20260804T130000.json     # iter-145 专项基线
└── Windows-CPython-3.11-64bit-doctor/   # doctor 测试结果（非 pytest-benchmark）
    └── 20260802T231903.json
```

### iter-145 最终基线快照

`0126_iter145-final.json` 是 req-49 阶段 4 收尾时保存的全量回归基线快照，
涵盖 iter-126~145 全部 26 个基线测试，作为后续性能优化的对照基准。

```bash
# 生成 iter-145 最终基线快照
uv run pytest tests/perf/test_perf_baseline.py tests/perf/test_build_perf_baseline.py \
  tests/perf/test_nuitka_compile_baseline.py tests/perf/test_wheel_download_baseline.py \
  tests/perf/test_entry_startup_baseline.py -m slow --benchmark-only \
  --benchmark-save=0126_iter145-final --benchmark-min-rounds=20 --benchmark-warmup=on \
  --benchmark-columns=median,min,mean,stddev
```

## 扩展基线

新增基线测试时：

1. 在对应测试文件中添加 `test_<场景>_baseline` 测试函数，标注 `@pytest.mark.slow`
2. 若测试名符合现有类别模式（如 `test_*_compile_baseline` 匹配 nuitka_compile），
   自动归入该类别，无需改对比配置
3. 若属于新类别，在 `fspack/doctor/benchmark_compare.py` 的 `DEFAULT_CATEGORIES`
   中添加新类别条目，设定合理阈值
4. 阈值依据：先运行 10+ 轮测量 StdDev，阈值设为 `max(2 * StdDev%, 10%)`
5. 同步更新本文档的基线测试清单与类别阈值表
