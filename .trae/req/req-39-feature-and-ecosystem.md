# 功能增强与生态建设（10 项迭代 iter-71 ~ iter-80）

## 背景

req-37 完成性能基线与 5 大文件拆分（iter-51~60），req-38 规划代码质量、
类型安全、覆盖率补强与文档同步（iter-61~70）。iter-61~70 收尾后，项目结构、
类型安全、基础覆盖率与文档同步均达稳态。本需求转向**用户功能增强**、
**深度性能优化**、**跨平台可靠性**与**生态建设**，提升 fspack 的易用性、
性能上限与社区可用性。

### 现状基线（2026-07-27，iter-60 完成后）

**剩余大文件（>500 行，iter-61/62 拆分 installer/loader 后仍存在）**：

| 模块 | 行数 | 拆分方向 |
|------|------|---------|
| `packaging/nuitka_compile.py` | 639 | 编译流程 / 产物剥离验证 / stamp 缓存 |
| `packaging/pipeline.py` | 564 | BuildContext + 阶段函数 / 公共辅助 |
| `packaging/nuitka_env.py` | 540 | 环境检查 / standalone python / ccache |
| `packaging/wheel_pip.py` | 524 | pip 调用 / uv 调用 / sdist 回退 |
| `analyzer.py` | 445 | AST 扫描 / 指纹计算 / stdlib 分类 |
| `slim/qt.py` | 443 | 依赖闭包 / classify_entry / 辅助判断 |

**用户功能缺口**：

- 无 `--dry-run` 预览模式（用户无法在打包前查看计划与依赖列表）
- 无打包产物大小报告（构建后无法快速定位体积热点）
- 无 `fsp doctor` 环境诊断命令（缺工具时仅打包失败报错，无前置体检）
- 错误信息仅描述问题，无修复建议链接与自动诊断

**性能优化空间**：

- `NuitkaCompile._compile_files` 串行调 nuitka `--module` 逐文件编译，
  大项目（50+ 模块）耗时显著；Nuitka 单进程内 `--jobs=N` 仅并行 C 编译，
  Python 层串行编译仍是瓶颈
- `source_fingerprint` 用 mtime_ns 判断文件变更，部分文件系统（如 FAT32、
  网络挂载）mtime 精度为秒级，导致增量构建误判；缺少内容 hash 回退机制
- 缓存命中率无可见报告，用户无法判断缓存是否生效

**跨平台可靠性缺口**：

- CI 仅在 ubuntu-latest 跑 Python 3.8/3.14 矩阵，无 Windows 平台测试
  （Windows 路径、mingw 交叉编译、NSIS 流程未在 CI 覆盖）
- slow 端到端测试（21 个）仅手动触发，无 cron 定时运行，回归风险高
- `pytest-benchmark` 基线已建立（iter-51），但未纳入 CI 回归门禁，
  性能退化无法自动发现
- Linux 平台测试覆盖薄弱（多数测试针对 Windows 路径与 embed python）

**生态建设缺口**：

- `docs/` 仅含 api.rst（手写）+ changelog.rst + integration.md，无自动
  API 参考（sphinx-autodoc）、贡献指南、故障排查指南
- 无架构决策记录（ADR），重要决策散落在 project_memory 与迭代记录中
- README 架构图与模块索引未反映 iter-56~62 拆分后的新模块结构

## 10 项迭代任务

### 结构优化收尾（iter-71 ~ iter-72）

- [ ] **iter-71 nuitka_compile.py 拆分**：639 行 → `nuitka_compile.py`
  （compile_src/compile_packages 编译入口 + stamp 缓存）/
  `nuitka_strip.py`（产物剥离与 .pyd 可加载性验证），`nuitka.py` facade
  不变。基类 `NuitkaCompile` 保留，按职责拆 mixin
- [ ] **iter-72 pipeline.py 拆分**：564 行 → `pipeline.py`（BuildContext +
  build/clean_dist/resolve_project_info 入口 + 公共辅助）/
  `pipeline_stages.py`（_prepare_runtime/_analyze_dependencies/
  _download_dependencies/_compile_user_sources/_build_entry_loaders 阶段函数），
  `builder.py` facade 不变

### 用户功能增强（iter-73 ~ iter-75）

- [ ] **iter-73 `--dry-run` 预览模式**：`fsp b --dry-run` 解析 pyproject.toml
  + 自动解析 Python 版本 + AST 扫描依赖 + 显示打包计划（目标平台、Python 版本、
  依赖列表、预估 wheel 数、runtime 来源、loader 编译器）不执行下载与编译。
  帮助用户在打包前确认配置正确，避免长耗时构建后才发现配置错误
- [ ] **iter-74 打包产物大小报告**：`fsp b` 完成后输出 dist 体积报告，
  按 runtime/src/site-packages 三大类统计，site-packages 按 Top 10 包占比
  排序，标注精简节省空间。复用 BuildTracker 表格渲染，支持 `--no-size-report`
  关闭。帮助用户定位体积热点优化打包
- [ ] **iter-75 `fsp doctor` 环境诊断命令**：新增 `fsp doctor` 子命令，
  检查 mingw-w64/gcc/NSIS/wine/pip/uv/Pillow 等工具可用性与版本，
  显示 Python 版本、平台、镜像源配置、缓存目录大小。输出绿/黄/红三色
  诊断结果与修复建议（如"mingw-w64 未安装，运行 choco install mingw"）。
  帮助用户前置发现环境问题

### 深度性能优化（iter-76 ~ iter-77）

- [ ] **iter-76 Nuitka 编译并行化**：`_compile_files` 串行调 nuitka
  `--module` 改为 `ProcessPoolExecutor` 并行（CPU 密集 nuitka 编译），
  每个进程编译一批 .py 文件。注意：Nuitka 单进程内 `--jobs=N` 已并行
  C 编译，Python 层并行需控制总进程数避免 CPU 过载（建议
  `max_workers=min(cpu_count, 4)`）。基线对比验证大项目（50+ 模块）
  提速 ≥30%
- [ ] **iter-77 增量构建指纹优化**：`source_fingerprint` 增加内容 hash
  回退机制——mtime_ns 相同但内容变更（罕见但 FAT32/网络挂载可能发生）
  时用 sha256 内容 hash 二次校验。`--fingerprint-content-hash` 强制启用
  内容 hash（牺牲速度换准确性）。新增缓存命中率报告（build 结束输出
  embed/wheel/nuitka/pyc 各阶段缓存命中数与未命中数）

### 跨平台与 CI 增强（iter-78 ~ iter-79）

- [ ] **iter-78 CI 增强**：(1) test job 添加 windows-latest 矩阵，
  覆盖 Windows 路径/mingw/NSIS 流程；(2) 新增 `slow-e2e` job，每周六
  04:00 UTC cron 定时运行 slow 端到端测试（21 个），PR 不触发避免阻塞；
  (3) 新增 `benchmark` job，每次 push 到 main 跑 pytest-benchmark，
  与基线对比退化 >10% 失败。三 job 独立并行，避免拉长 PR 反馈时间
- [ ] **iter-79 Linux 平台测试覆盖补强**：审查 tests/ 下所有测试，
  识别 Windows 专属路径（embed python/`python3X._pth`/mingw 交叉编译/
  `python.exe`）并补充 Linux 对等测试（standalone python/`libpython.so`/
  gcc 原生编译/`python3`）。目标：Linux 平台测试覆盖率从当前 ~40% 提升
  至 ≥80%，缩小与 Windows 测试覆盖差距

### 生态建设（iter-80）

- [ ] **iter-80 文档体系完善**：(1) 启用 sphinx-autodoc 自动生成 API
  参考（从 docstring 提取，替代手写 api.rst）；(2) 新增 `CONTRIBUTING.md`
  贡献指南（开发环境搭建、提交规范、测试要求、PR 流程）；(3) 新增
  `docs/troubleshooting.md` 故障排查指南（常见错误码、环境问题、缓存清理、
  跨平台注意事项）；(4) 新增 `docs/adr/` 架构决策记录目录，迁移
  project_memory 中重要决策（Nuitka 版本锁定、entry wrapper 用 runpy、
  mixin 拆分策略等）为编号 ADR 文档；(5) 更新 README 架构图反映
  iter-56~72 拆分后的模块结构

## 验收标准

- 每次迭代全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- 结构拆分保持公开 API 不变（`__all__` 与 import 路径兼容），所有现有
  测试不破坏
- 功能增强迭代须配套测试（`--dry-run`/size report/`fsp doctor` 各自
  有单元测试覆盖核心逻辑）
- 性能优化迭代须对比基线，退化 > 10% 失败；iter-76 需大项目实测提速 ≥30%
- CI 增强后 Windows 矩阵测试通过，slow cron 与 benchmark 门禁正常触发
- iter-79 Linux 覆盖率提升至 ≥80%（用 `--cov` 报告对比）
- iter-80 文档构建通过（`sphinx-build` 无 warning），ADR 编号连续

## 实施顺序

1. iter-71~72（结构优化收尾，先减剩余大文件降低后续迭代阅读成本）
2. iter-73~75（用户功能增强，在结构稳定后新增功能避免重复重构）
3. iter-76~77（深度性能优化，在功能稳定后做基线对比）
4. iter-78~79（跨平台与 CI 增强，在功能与性能稳定后扩大测试覆盖）
5. iter-80（生态建设收尾，文档同步全部新功能与结构变更）

## 依赖关系

- iter-71 依赖 iter-66（pyrefly 清理）完成，避免拆分时引入新类型抑制
- iter-76 依赖 iter-71（nuitka_compile 拆分）完成，避免并行化重构与
  拆分冲突
- iter-77 依赖 iter-54（scandir 优化）基线，对比内容 hash 回退的开销
- iter-80 依赖 iter-70（架构文档同步）与 iter-71~72（最终模块结构）
  完成，避免文档反复更新

## 风险与缓解

- **iter-76 Nuitka 并行化风险**：Nuitka 单进程内已 `--jobs=N` 并行 C 编译，
  Python 层再加进程池可能导致 CPU 过载。缓解：`max_workers` 限制为
  `min(cpu_count, 4)`，基准测试对比串行/并行总耗时，退化则回退
- **iter-78 Windows CI 风险**：Windows runner 慢于 Linux，可能拉长 CI
  反馈时间。缓解：Windows 矩阵仅跑非 slow 测试，slow 走 cron job
- **iter-80 sphinx-autodoc 风险**：fspack 模块导入有副作用（如
  `from fspack.config import MIRRORS` 触发配置加载）。缓解：用
  `autodoc_mock_imports` 或显式 `autoclass`/`autofunction` 指定模块
