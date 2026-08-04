# 需求：提高健壮性与打包速度（20 项迭代 iter-126 ~ iter-145）

## 背景

iter-117~125 完成结构优化与懒加载主题（`import fspack` 从 ~55ms 降至 18.2ms，
`fsp --help` 冷启动 ~100ms → 61ms）。项目结构与冷启动已达稳态，本需求转向
**健壮性**（下载/编译/缓存的可靠性）与**打包速度**（并行化与工具链升级），
并建立性能基线守护机制确保优化不退化。

## 现状基线（iter-125 完成后）

### 已建立的优化基础设施

- 缓存：wheel 依赖解析缓存（`.deps-<key>.json`）、Nuitka stamp、pyc stamp、
  ProjectInfo `lru_cache`、源码指纹缓存
- 速度：ccache、compileall 多目录合并、`-j 0` 并行、`os.scandir` 优化、
  lazy-import、`--no-site`、path_importer_cache 预填充
- 健壮性：Nuitka 环境失败回退 .pyc、sdist 回退、win7 兼容 DLL 注入、
  `filter="data"` (3.12+)
- 性能基线：10 个 pytest-benchmark 测试 + CI benchmark gate

### 已识别的健壮性隐患

1. `packaging/net.py` 下载无重试、无 hash 校验，网络抖动直接失败
2. `packaging/nuitka/compile.py` `_stream_compile` 无超时，nuitka 卡死会无限等待；
   `process.wait()` 在 drain 线程前调用存在 PIPE 缓冲区满死锁风险
3. `packaging/pyc.py` `_precompile_pyc` 编译失败仍写 stamp，下次跳过导致问题被掩盖
4. `packaging/wheels/cache.py` 缓存损坏仅 warning 不清理，下次仍尝试解析
5. `packaging/runtime.py` `extract_standalone` 3.11 及以下无 `data` filter，
   存在路径穿越风险（虽来自可信源 astral-sh）
6. `packaging/sync.py` `mtime_ns + size` 在 FAT32/网络挂载精度不足，误判未变更
7. `analyzer.py` `_parse_parallel` 无超时，单个文件 ast.parse 卡死阻塞全流程

### 已识别的速度优化空间

1. `packaging/nuitka/compile.py` `_compile_files` 串行编译 `.py` 为 `.pyd`，
   无 Python 层并行（req-39 iter-76 规划未实施）
2. wheel 下载仍走 pip，uv 仅在解析阶段用（`_resolve_with_uv`），下载阶段未用 uv
3. `packaging/pipeline/stages.py` `_build_entry_loaders` 多入口串行编译 loader
4. project_memory 已记录：stages.py 顶部 BuildTracker 加载 rich.progress（~8ms）、
   pipeline/__init__.py 顶部 fspack.console（~17ms）受约束无法延迟

## 20 项迭代任务

### 阶段 1：健壮性基础修复（iter-126 ~ iter-130）

低风险，修复现有可靠性隐患，避免长时间构建后失败。

- [ ] **iter-126 下载文件完整性校验与重试**：(1) `Downloader.download` 增加指数退避
  重试（3 次，1s/2s/4s），用 tenacity 库实现，区分可重试错误（连接超时、503）
  与不可重试（404）；(2) `RuntimeDownloader` 支持 `--require-hashes` 模式下校验
  下载归档 sha256；(3) `download_wheels` 的 `require_hashes=True` 透传给 pip
  （已有参数，补测试覆盖）；(4) 新增 `tests/test_net_retry.py` 覆盖重试逻辑。
  **依赖**：引入 tenacity（更新 pyproject.toml test 依赖）
- [ ] **iter-127 subprocess 超时与死锁防护**：(1) `_stream_compile` 增加 `timeout`
  参数（默认 600s），超时 kill 进程；(2) 修复 `process.wait()` 顺序——先 join
  drain 线程再 wait（或用 `communicate()`）；(3) `_parse_parallel` 增加 `chunksize`
  超时（单个 chunk 60s）；(4) `_precompile_pyc` compileall 增加 300s 超时
- [ ] **iter-128 缓存健壮性**：(1) `_load_deps_cache` 损坏时删除缓存文件
  （避免反复尝试）；(2) `_precompile_pyc` 编译失败（returncode != 0）时不写 stamp，
  下次重试；(3) Nuitka stamp 写入用 `tempfile + rename` 原子化；(4) `fsp doctor`
  增加 `--check-cache` 检测损坏缓存
- [ ] **iter-129 增量构建内容 hash 回退**：(1) `_site_packages_fingerprint` 增加
  sha256 内容 hash 选项（`--fingerprint-content-hash`）；(2) `source_fingerprint`
  在 mtime_ns + size 相同但用户显式启用内容 hash 时二次校验；(3) 测试覆盖 FAT32
  场景（mock mtime 精度秒级）
- [ ] **iter-130 错误恢复与自愈**：(1) 构建开始检测 `dist/` 半成品（无 stamp 但有
  部分产物），提示 `fsp c` 清理；(2) wheel 下载失败时清理部分下载的 `.whl` 文件；
  (3) Nuitka 编译失败时清理 `.build/` 残留（已有，补测试）；(4) runtime 解压失败时
  删除损坏的归档缓存

### 阶段 2：打包速度优化（iter-131 ~ iter-135）

中风险，需基线守护，先做 Nuitka 并行化再做工具链替换。

- [x] **iter-131 Nuitka 编译并行化**：(1) `_compile_files` 用
  `concurrent.futures.ThreadPoolExecutor`（subprocess 释放 GIL，线程足够）并行编译
  多个 `.py`，`max_workers=min(cpu_count, 4)`；(2) 保留心跳线程但改为全局心跳
  （不是每文件一个）；(3) 基线对比：50 文件场景提速 ≥30%（req-39 iter-76 目标，留 iter-142）
- [x] **iter-132 wheel 下载 uv 加速**：(1) `_download_online` 在 uv 可用时改用
  `uv pip download`（比 pip 快 2-5x）；(2) 保留 pip 回退（uv 不支持的场景）；
  (3) `_resolve_with_uv` 与下载阶段共享 uv 路径检测；(4) 基线对比：50 wheel 场景
  提速 ≥40%（留 iter-142）
- [x] **iter-133 多入口 loader 并行编译**：(1) `_build_entry_loaders` 用
  `ThreadPoolExecutor` 并行编译多个 entry loader（每个 loader 独立 mingw/gcc 子进程）；
  (2) 共享 `tempfile.TemporaryDirectory` 工作目录，避免并发创建；(3) 测试覆盖多入口
  场景（4+ 入口）
- [x] **iter-134 AST 并行解析调优**：(1) `_parse_parallel` chunksize 自适应算法优化
  （按文件大小加权，避免大文件扎堆）；(2) `ProcessPoolExecutor` 改用 `initializer`
  预加载 `_STDLIB` 集合，减少 worker 启动开销；(3) 基线对比：500 文件场景提速 ≥15%
  （留 iter-142）
- [x] **iter-135 冷启动 import 终极惰性化**：(1) `pipeline/__init__.py` 顶部
  `fspack.console` 移至函数内（解决 project_memory 遗留 ~17ms）；(2) `stages.py`
  顶部 `BuildTracker` 类型注解改用字符串前向引用，`progress` 导入移至 `build()` 内
  （解决 ~8ms）；(3) `wheels/downloader.py` 顶部 `threading` 移至方法内；
  (4) 守护测试扩展：`test_pipeline_no_top_level_console_import`

### 阶段 3：深度健壮性（iter-136 ~ iter-140）

中高风险，安全加固与异常容错。

- [x] **iter-136 tarball 安全 extract 完整化**：(1) `extract_standalone` 3.11 及以下
  用 `tarfile.open` + 手动 `data` filter（参考 PEP 706 backport）；(2) `extract_embed`
  校验 zip 条目路径无 `..` 与绝对路径；(3) 测试覆盖恶意 tarball（路径穿越、符号链接攻击）
- [x] **iter-137 编译产物验证增强**：(1) `_strip_compiled_sources` 批量验证 .pyd 可加载性
  （已有，扩展为并发验证）；(2) 损坏 .pyd 自动删除并回退到 .py（已有，补测试覆盖损坏
  场景）；(3) Nuitka 编译失败时记录失败文件列表到 stamp，下次跳过这些文件避免反复尝试
- [x] **iter-138 依赖分析异常容错**：(1) `_parse_file_worker` 单文件 ast.parse 失败记录到
  报告（`ast_errors` 字段），不静默跳过；(2) `_parse_parallel` 单个 worker 超时不阻塞
  其他 worker（`as_completed` + timeout）；(3) QML 解析失败不影响主流程（已有，补测试）
- [x] **iter-139 缓存目录健康检查**：(1) `fsp doctor` 扩展 `--check-cache` 检测损坏缓存
  （`.deps-*.json` 损坏、wheel 文件缺失、stamp 不一致）；(2) 检测孤儿文件（cache 目录中
  不属于任何项目的 wheel）；(3) 输出清理建议（`fsp cache clean` 子命令）
- [ ] **iter-140 构建中断恢复**：(1) `fsp b` 开始时检测 `dist/` 半成品（有 runtime/ 无 exe），
  交互式确认或 `--auto-clean` 自动清理；(2) 构建异常时保存失败阶段到 `dist/.build_failed`，
  下次 `fsp b` 检测并提示；(3) `fsp c` 保留 `installer.nsi` 逻辑扩展到保留失败诊断文件

### 阶段 4：性能基线与守护固化（iter-141 ~ iter-145）

低风险，建立可量化的性能守护机制。

- [ ] **iter-141 打包速度端到端基线**：新增 `tests/test_build_perf_baseline.py`：
  (1) 小项目（1 入口、3 依赖）冷/热缓存构建耗时基线；(2) 中项目（10 入口、20 依赖）
  基线；(3) 用 `pytest-benchmark` 的 `pedantic` 模式确保可复现
- [ ] **iter-142 Nuitka 编译基线**：(1) 50 文件串行 vs 并行（iter-131）对比基线；
  (2) ccache 命中 vs 未命中对比；(3) 加入 CI benchmark job，退化 >10% 失败
- [ ] **iter-143 wheel 下载基线**：(1) pip vs uv（iter-132）下载 50 wheel 对比基线；
  (2) 缓存命中 vs 冷下载对比；(3) 加入 CI benchmark job
- [ ] **iter-144 启动时间基线**：(1) entry wrapper 启动耗时基线（用 `python -X importtime`
  解析）；(2) lazy-import 启用 vs 关闭对比；(3) `--no-site` 启用 vs 关闭对比
- [ ] **iter-145 CI 性能门禁固化**：(1) `.github/workflows/ci.yml` benchmark job 扩展，
  覆盖 iter-141~144 新基线；(2) `scripts/compare_benchmark.py` 支持按基线类别分组对比；
  (3) 文档 `docs/performance.md` 性能基线对比与更新指南；(4) iter-126~145 全量回归与
  基线快照 `0126_iter145-final.json`

## 验收标准

- 每轮迭代全套门禁通过（ruff/format/pyrefly/pytest/coverage ≥ 95%）
- 阶段 1：下载/编译/缓存的健壮性测试覆盖核心异常路径，`fsp doctor --check-cache` 可用
- 阶段 2：Nuitka 50 文件编译提速 ≥30%，wheel 下载提速 ≥40%，多入口 loader 编译提速
  ≥2x（4 入口场景）
- 阶段 3：恶意 tarball 测试通过，缓存损坏自愈，构建中断可恢复
- 阶段 4：性能基线测试数 ≥ 14（现有 10 + 新增 4），CI 退化 >10% 自动失败
- 累计：冷启动 `import fspack.builder` 从 88.6ms 降至 ≤70ms（iter-135 目标）

## 实施顺序与依赖

1. iter-126~130（阶段 1 健壮性基础，先修复现有 bug 避免新优化放大问题）
2. iter-131~135（阶段 2 速度优化，在稳定基础上做并行化与工具链升级）
3. iter-136~140（阶段 3 深度健壮性，安全加固与异常容错需在功能稳定后做）
4. iter-141~145（阶段 4 基线守护，量化前 15 轮成果并固化）

**关键依赖**：
- iter-127 依赖 iter-126（重试机制复用错误分类）
- iter-131 依赖 iter-127（并行化前必须有超时防护）
- iter-132 依赖 iter-126（uv 下载复用重试逻辑）
- iter-137 依赖 iter-131（验证逻辑扩展到并行编译产物）
- iter-141~144 依赖对应优化迭代完成
- iter-145 依赖 iter-141~144 全部完成

## 关键决策（用户确认）

- **范围**：按计划执行 20 轮（iter-126~145 完整）
- **速度优先级**：iter-131 Nuitka 并行优先（先于 iter-132 uv 下载）
- **依赖策略**：允许引入 tenacity 用于重试逻辑（iter-126）

## 风险与缓解

- **iter-126 重试风险**：重试可能放大服务端压力。缓解：指数退避 + 抖动，仅对幂等 GET 重试
- **iter-127 超时风险**：超时过短可能误杀正常编译。缓解：Nuitka 600s、compileall 300s
  可配置，默认值基于实测 P99
- **iter-131 并行化风险**：subprocess 并发可能触发 Windows 资源限制。缓解：
  `max_workers=min(cpu_count, 4)`，基线对比退化则回退
- **iter-132 uv 风险**：uv 不支持的 wheel（如本地 `--find-links`）需回退 pip。缓解：
  保留 pip 路径，uv 失败自动回退
- **iter-135 延迟导入风险**：`fspack.console` 移至函数内可能破坏测试 patch 路径。缓解：
  先跑全量测试，patch 路径调整后再提交
- **iter-136 安全 filter 风险**：3.11 以下手动 data filter 可能误拦合法条目。缓解：
  白名单可信前缀（`python/`、`Lib/`）

## 20 轮路线图总览

| 轮次 | 阶段 | 主题 | 风险 |
|------|------|------|------|
| iter-126 | 1 | 下载完整性与重试 | 低 |
| iter-127 | 1 | subprocess 超时与死锁防护 | 低 |
| iter-128 | 1 | 缓存健壮性 | 低 |
| iter-129 | 1 | 内容 hash 回退 | 低 |
| iter-130 | 1 | 错误恢复与自愈 | 低 |
| iter-131 | 2 | Nuitka 编译并行化 | 中 |
| iter-132 | 2 | wheel 下载 uv 加速 | 中 |
| iter-133 | 2 | 多入口 loader 并行编译 | 中 |
| iter-134 | 2 | AST 并行解析调优 | 中 |
| iter-135 | 2 | 冷启动 import 终极惰性化 | 中 |
| iter-136 | 3 | tarball 安全 extract | 中高 |
| iter-137 | 3 | 编译产物验证增强 | 中 |
| iter-138 | 3 | 依赖分析异常容错 | 中 |
| iter-139 | 3 | 缓存目录健康检查 | 中 |
| iter-140 | 3 | 构建中断恢复 | 中高 |
| iter-141 | 4 | 打包速度端到端基线 | 低 |
| iter-142 | 4 | Nuitka 编译基线 | 低 |
| iter-143 | 4 | wheel 下载基线 | 低 |
| iter-144 | 4 | 启动时间基线 | 低 |
| iter-145 | 4 | CI 性能门禁固化 | 低 |
