# 需求：功能性能完善（20 项迭代 iter-86 ~ iter-105）

## 背景

req-46 完成缓存配置 + 离线支持 + init 模板命令（iter-76~85），项目基础能力
稳态。req-39/40/41 早期规划了 iter-71~100 路线图，但实际走向为 req-43/44/45/46
占用 iter-71~85，原规划中**功能增强、深度性能优化、CI 增强、macOS、体积优化、
启动优化、安全加固、可观测性、插件机制、文档体系**等大部分尚未实施。

本需求基于 2026-07-28 实际状态，重新规划 iter-86~105 共 20 轮迭代，聚焦
**功能性能完善**，按 4 阶段递进：用户功能增强 → 性能与代码质量 → CI 与
跨平台 → 体积/启动/安全/文档。

### 现状基线（2026-07-28，iter-85 完成后）

**功能缺口**：

- 无 `fsp doctor` 环境诊断（缺工具时仅打包失败报错，无前置体检）
- 无 `--dry-run` 预览模式（无法打包前查看计划与依赖）
- 无打包产物大小报告（构建后无法定位体积热点）
- 无 `--log-file` 日志持久化（CI 上传与问题排查不便）
- 无 `--profile` 耗时分析（无法识别瓶颈阶段）

**大文件（>400 行，需拆分）**：

| 模块 | 行数 | 拆分方向 |
|------|------|---------|
| `packaging/nuitka_compile.py` | 809 | compile_src/compile_packages + stamp 缓存 + .pyd 验证 |
| `packaging/wheel_pip.py` | 748 | download_wheels 入口 + pip_caller + sdist_fallback |
| `packaging/pipeline.py` | 668 | BuildContext + 阶段函数 + 公共辅助 |
| `packaging/nuitka_env.py` | 666 | env_check + standalone_python + ccache |
| `cli.py` | 563 | main + build_parser + 各子命令参数解析 |
| `analyzer.py` | 513 | analyze_dependencies + ast_scanner + fingerprint |
| `slim/qt.py` | 490 | qt_closure + qt_classify + qt_helpers |
| `packaging/loader_compile.py` | 464 | mingw/gcc 调用 + 缓存键 + 失败回退 |
| `packaging/runtime.py` | 429 | runtime_download + runtime_extract |

**类型安全缺口**：NuitkaCompiler 三 mixin（NuitkaEnv/NuitkaCompile/NuitkaVerify）
跨类调用用 `# type: ignore[attr-defined]` 抑制。

**性能优化空间**：

- `ProjectInfo.from_dir` 每次解析 pyproject.toml，构建流程内多次调用重复读取
- `collect_imports_and_submodules` 用 list+set 双结构收集，大项目内存占用高
- `source_fingerprint` 的 `os.scandir` 递归全量路径列表

**CI 与测试缺口**：

- `.github/workflows/ci.yml` 仅 ubuntu-latest Python 3.8/3.14，无 Windows 矩阵
- slow 端到端测试无 cron 定时运行，回归风险高
- `pytest-benchmark` 基线已建立（5 个基线测试）但未纳入 CI 回归门禁
- `tests/` 30 个测试文件无 `conftest.py`，重复 fixture 散落各文件
- Linux 平台测试覆盖薄弱

**平台扩展缺口**：

- `Platform` 枚举仅 WINDOWS/LINUX，无 MACOS
- 无 macOS runtime/loader/安装包支持

**体积/启动/安全/可观测性缺口**：

- DLL/.so 传递依赖未分析（如 Qt6Core.dll 依赖的 ICU 未用时仍保留）
- 无运行时压缩、无重复文件检测
- sys.path 扫描未优化、重量级模块未延迟导入、site.py 未精简
- 依赖下载无哈希校验、无 SBOM、安装包未签名
- 无 manifest.json 产物清单

**文档缺口**：

- `docs/` 仅 api.rst（手写）+ architecture.rst + changelog.rst + integration.md
- 无 CONTRIBUTING.md / troubleshooting.md / adr/
- api.rst 手写未用 sphinx-autodoc

## 20 项迭代任务

### 阶段 1：用户功能增强（iter-86 ~ iter-90）

低风险、高用户价值，无需新依赖，先解决用户痛点。

- [x] **iter-86 `fsp doctor` 环境诊断命令**：新增 `fsp doctor` 子命令，检查
  mingw-w64/gcc/NSIS/wine/pip/uv/Pillow 等工具可用性与版本，显示 Python
  版本、平台、镜像源配置、缓存目录大小。输出绿/黄/红三色诊断结果与修复
  建议（如"mingw-w64 未安装，运行 choco install mingw"）。帮助用户前置
  发现环境问题，避免打包中途失败
- [x] **iter-87 `--dry-run` 预览模式**：`fsp b --dry-run` 解析 pyproject.toml
  + 自动解析 Python 版本 + AST 扫描依赖 + 显示打包计划（目标平台、Python
  版本、依赖列表、预估 wheel 数、runtime 来源、loader 编译器）不执行下载
  与编译。帮助用户在打包前确认配置正确
- [x] **iter-88 打包产物大小报告**：`fsp b` 完成后输出 dist 体积报告，按
  runtime/src/site-packages 三大类统计，site-packages 按 Top 10 包占比
  排序，标注精简节省空间。复用 BuildTracker 表格渲染，支持 `--no-size-report`
  关闭。帮助用户定位体积热点
- [x] **iter-89 `--log-file` 构建日志持久化**：`--log-file <path>` 选项将
  构建日志写入文件（含时间戳、阶段耗时、缓存命中、错误堆栈），便于 CI
  上传与问题排查。日志格式支持 text（默认）与 json（结构化，便于解析）
- [x] **iter-90 `--profile` 耗时分析报告**：`--profile` 选项输出耗时分析
  报告（各阶段 wall time / CPU time / 内存峰值），识别瓶颈阶段。复用
  BuildTracker.stage() 已收集的耗时数据，扩展内存峰值采集（psutil，新增
  依赖）。报告格式：表格 + 可选 JSON

### 阶段 2：性能优化与代码质量（iter-91 ~ iter-95）

中风险，需基线守护，先拆分大文件降低后续迭代阅读成本，再做精细优化。

- [x] **iter-91 nuitka_compile.py + nuitka_env.py 拆分**：(1) `nuitka_compile.py`
  809 行 → `nuitka_compile.py`（compile_src/compile_packages 编译入口 + stamp
  缓存）+ `nuitka_strip.py`（产物剥离与 .pyd 可加载性验证），`nuitka.py`
  facade 不变；(2) `nuitka_env.py` 666 行 → `nuitka_env.py`（NuitkaEnv
  mixin 入口 + C 编译器检查 + ensure_env 编排）+ `nuitka_standalone.py`
  （standalone python 下载与缓存）+ `nuitka_ccache.py`（ccache 下载与 PATH
  查找）。**基线对比**：Nuitka 不在性能基线，仅验证功能测试不破坏
- [ ] **iter-92 wheel_pip.py + pipeline.py 拆分**：(1) `wheel_pip.py` 748 行 →
  `wheel_pip.py`（download_wheels 入口 + 缓存调度）+ `wheel_resolver.py`
  （_run_pip_download/_download_online/uv pip compile 解析）+ `wheel_sdist.py`
  （sdist 回退），`wheels.py` facade 不变；(2) `pipeline.py` 668 行 →
  `pipeline.py`（BuildContext + build/clean_dist/resolve_project_info 入口 +
  公共辅助）+ `pipeline_stages.py`（_prepare_runtime/_analyze_dependencies/
  _download_dependencies/_compile_user_sources/_build_entry_loaders 阶段函数），
  `builder.py` facade 不变。**基线对比**：`test_slim_unpack_baseline` 不退化
- [ ] **iter-93 mixin Protocol 类型声明**：NuitkaCompiler 三 mixin
  （NuitkaEnv/NuitkaCompile/NuitkaVerify）跨类调用改用 `typing.Protocol`
  声明接口契约，替代 `# type: ignore[attr-defined]` 抑制。定义
  `NuitkaCompilerProtocol` 描述 mixin 间依赖的方法签名，各 mixin 用
  `cls: NuitkaCompilerProtocol` 类型注解替代裸 `cls`。**基线对比**：全量基线
  不退化（Protocol 仅类型检查期生效）；pyrefly 抑制警告数大幅降低
- [ ] **iter-94 配置加载缓存**：`parsing.py` 的 `ProjectInfo.from_dir` 每次
  解析 pyproject.toml，构建流程内多次调用（build/resolve_project_info/
  installer）重复读取。增加模块级 `lru_cache` 按 `(project_dir, mtime)`
  缓存解析结果，mtime 变化时失效。**基线对比**：`test_analyze_dependencies_baseline`
  不退化；新增 `test_project_info_from_dir_baseline` 基线测量配置解析耗时
- [ ] **iter-95 AST 分析内存优化**：`collect_imports_and_submodules` 当前用
  `list` + `set` 双结构收集导入，大项目（500+ 文件）内存占用高。改用生成器
  + 单次 `dict` 合并，减少中间结构。`source_fingerprint` 的 `os.scandir`
  递归改用 `yield` 生成器避免全量路径列表。**基线对比**：
  `test_collect_imports_and_submodules_baseline` 提速 ≥10% 或不退化；
  `test_source_fingerprint_baseline` 提速 ≥10% 或不退化

### 阶段 3：CI 与跨平台（iter-96 ~ iter-100）

中高风险，扩大测试覆盖与平台支持。

- [ ] **iter-96 CI 三 job 增强**：(1) test job 添加 windows-latest 矩阵，
  覆盖 Windows 路径/mingw/NSIS 流程；(2) 新增 `slow-e2e` job，每周六
  04:00 UTC cron 定时运行 slow 端到端测试，PR 不触发避免阻塞；(3) 新增
  `benchmark` job，每次 push 到 main 跑 pytest-benchmark，与基线对比退化
  >10% 失败。三 job 独立并行，避免拉长 PR 反馈时间
- [ ] **iter-97 Linux 平台测试覆盖补强**：审查 tests/ 下所有测试，识别
  Windows 专属路径（embed python/`python3X._pth`/mingw 交叉编译/`python.exe`）
  并补充 Linux 对等测试（standalone python/`libpython.so`/gcc 原生编译/
  `python3`）。目标：Linux 平台测试覆盖率从当前 ~40% 提升至 ≥80%
- [ ] **iter-98 测试 fixture 共享化**：审查 tests/ 下 30 个测试文件，识别
  重复的 fixture（tmp_path 包装、mock subprocess、样本项目构造等）提取到
  `tests/conftest.py`。减少测试代码重复，提升新测试编写效率
- [ ] **iter-99 macOS runtime + loader**：(1) `Platform` 枚举新增 `MACOS`；
  `detect_platform` 识别 Darwin；(2) `StandaloneRuntime` 扩展支持 macOS
  （python-build-standalone 提供 macOS x86_64 + arm64 tarball）；(3) 新增
  `MacLoader`（clang 编译，dlopen libpython3.X.dylib，Mach-O 格式，
  `@executable_path` 解析 runtime 路径）；(4) `wheel_platform_tags` 新增
  macOS 标签（macosx_11_0_x86_64 / macosx_11_0_arm64）；(5) 测试：mock
  clang 编译，验证 C 源码生成与缓存键
- [ ] **iter-100 macOS 安装包与里程碑收尾**：(1) 新增 `MacInstaller`（.pkg
  通过 pkgbuild + productbuild，.dmg 通过 hdiutil）；(2) `build_release`
  的 `auto` 格式在 macOS 平台默认 .pkg + .dmg；(3) 代码签名基础：`--codesign`
  选项调 codesign 签名 .app 与 .pkg（ad-hoc 签名）；(4) iter-86~100 全量
  回归与基线快照更新

### 阶段 4：体积/启动/安全/文档（iter-101 ~ iter-105）

高风险，部分需新依赖，在功能与平台稳定后做深度优化。

- [ ] **iter-101 DLL/so 传递依赖分析**：(1) 新增 `dep_analyzer.py` 模块，
  用 `objdump`（Linux/macOS）或 `pefile` 库（Windows）解析 .dll/.so/.dylib
  的依赖树；(2) Qt 闭包外但被保留的 DLL 若无依赖引用则剥离；(3) `--analyze-deps`
  选项启用深度依赖分析（默认关闭，耗时）；(4) 体积报告新增"依赖分析节省"行
- [ ] **iter-102 启动时间优化**：(1) entry wrapper 注入 `sys.path_hooks`
  优先匹配 site-packages；(2) 重量级模块延迟导入钩子：`--lazy-import numpy,pandas`
  首次 import 时才执行模块初始化；(3) `.pth` 文件优化：site-packages 下 .pth
  仅在 `--no-site` 关闭时处理，默认跳过；(4) 测量启动时间基线，验证提速 ≥10ms
- [ ] **iter-103 安全加固：依赖哈希校验 + SBOM**：(1) `--require-hashes`
  选项透传给 pip download，强制依赖哈希校验；(2) 生成 SBOM（软件物料清单）
  `dist/release/<name>-<ver>-sbom.json`，含所有依赖名称/版本/哈希/许可证
  （SPDX 格式）；(3) 安装包签名：Windows 用 sigstore 签名 .exe（`--sign-exe`），
  Linux 用 GPG 签名 .deb（`--sign-deb`）
- [ ] **iter-104 构建产物清单 manifest.json**：生成 `dist/manifest.json`
  产物清单（含所有文件路径、大小、sha256、来源 wheel），便于审计与对比。
  `--manifest` 选项控制生成（默认开启）。支持 `fsp manifest` 子命令对比
  两次构建产物的差异（新增/删除/修改文件）
- [ ] **iter-105 文档体系完善**：(1) 启用 sphinx-autodoc 自动生成 API 参考
  （从 docstring 提取，替代手写 api.rst）；(2) 新增 `CONTRIBUTING.md`
  贡献指南（开发环境搭建、提交规范、测试要求、PR 流程）；(3) 新增
  `docs/troubleshooting.md` 故障排查指南；(4) 新增 `docs/adr/` 架构决策
  记录目录，迁移 project_memory 重要决策为编号 ADR 文档；(5) changelog
  汇总 iter-86~105 变更

## 验收标准

- 每次迭代全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- 阶段 1 功能增强迭代须配套测试（doctor/dry-run/size-report/log-file/profile
  各自有单元测试覆盖核心逻辑）
- 阶段 2 结构拆分保持公开 API 不变（`__all__` 与 import 路径兼容），所有现有
  测试不破坏；iter-93 后 pyrefly 抑制警告数 ≤ 10；iter-94/95 基线退化 ≤ 10%
- 阶段 3 CI 增强后 Windows 矩阵测试通过，slow cron 与 benchmark 门禁正常
  触发；iter-97 Linux 覆盖率提升至 ≥80%；iter-99/100 macOS 支持可构建
- 阶段 4 体积优化（iter-101）典型项目（PySide6）体积减少 ≥10%；启动优化
  （iter-102）启动时间减少 ≥20ms；安全加固（iter-103）SBOM 格式合规；
  产物清单（iter-104）manifest.json 完整；文档（iter-105）sphinx 构建
  无 warning，ADR 编号连续

## 实施顺序

1. iter-86~90（阶段 1 用户功能增强，先解决用户痛点，低风险快速见效）
2. iter-91~95（阶段 2 性能与代码质量，在功能稳定后做拆分与优化）
3. iter-96~100（阶段 3 CI 与跨平台，在功能与性能稳定后扩大测试覆盖与平台）
4. iter-101~105（阶段 4 体积/启动/安全/文档，在核心稳定后做深度优化收尾）

## 依赖关系

- iter-87 依赖 iter-86（doctor 复用环境检查逻辑）
- iter-88 依赖 iter-74（如已实现 BuildTracker 表格渲染，否则在 iter-88 内实现）
- iter-90 依赖 iter-89（profile 复用 log-file 基础设施）
- iter-91 依赖 iter-66（pyrefly 清理，避免拆分引入新类型抑制）
- iter-93 依赖 iter-91（mixin 拆分完成后再统一 Protocol 重构）
- iter-94 依赖 iter-58（config.py 拆分，parsing.py 已独立）
- iter-96 依赖 iter-95（性能基线稳定后再加 CI 门禁）
- iter-99 依赖 iter-58（Platform 枚举在 config 模块）
- iter-100 依赖 iter-99（MacLoader）与 iter-61（installer.py 拆分）
- iter-101 依赖 iter-92（wheel_pip 拆分）与 iter-84（qt.py 拆分）
- iter-102 依赖 iter-87（启动测量复用 dry-run 配置解析）
- iter-103 依赖 iter-88（SBOM 复用体积统计）
- iter-104 依赖 iter-101（manifest 复用依赖分析）
- iter-105 依赖 iter-91~104 全部完成（文档同步全部新功能与结构变更）

## 风险与缓解

- **iter-91/92 拆分风险**：拆分可能引入函数调用开销，破坏性能基线。缓解：
  拆分后立即跑基线对比，退化 > 5% 回退拆分
- **iter-93 Protocol 风险**：Protocol 仅类型检查期生效，运行时无开销，但
  pyrefly 对 Protocol 支持可能不完整。缓解：先用 `cast()` 兜底，pyrefly
  升级后再切换 Protocol
- **iter-96 Windows CI 风险**：Windows runner 慢于 Linux，可能拉长 CI
  反馈时间。缓解：Windows 矩阵仅跑非 slow 测试，slow 走 cron job
- **iter-99 macOS 风险**：Mach-O 格式与 .dylib 加载机制与 Linux ELF 差异大。
  缓解：参考 python-build-standalone 自带 python 可执行文件结构，CI 添加
  macOS runner 验证
- **iter-101 依赖分析风险**：objdump 在 Windows 可能不可用。缓解：Windows
  用 Python `pefile` 库解析 PE 导入表，Linux/macOS 用 `ldd`/`otool`，
  回退到 objdump
- **iter-102 zipimport 风险**：部分 C 扩展不支持 zipimport。缓解：C 扩展
  与纯 Python 分离，仅压缩纯 Python 包到 .zip
- **iter-103 sigstore 风险**：sigstore 需要网络与 OIDC 认证，离线环境
  不可用。缓解：`--sign-exe` 可选，未指定时跳过签名；提供
  `--sign-exe-certificate <pfx>` 用传统代码签名证书
- **iter-105 sphinx-autodoc 风险**：fspack 模块导入有副作用。缓解：用
  `autodoc_mock_importes` 或显式 `autoclass`/`autofunction` 指定模块

## 20 轮路线图总览

| 轮次 | 阶段 | 主题 | 风险 |
|------|------|------|------|
| iter-86 | 1 | fsp doctor 环境诊断 | 低 |
| iter-87 | 1 | --dry-run 预览模式 | 低 |
| iter-88 | 1 | 打包产物大小报告 | 低 |
| iter-89 | 1 | --log-file 日志持久化 | 低 |
| iter-90 | 1 | --profile 耗时分析 | 低 |
| iter-91 | 2 | nuitka_compile/env 拆分 | 中 |
| iter-92 | 2 | wheel_pip/pipeline 拆分 | 中 |
| iter-93 | 2 | mixin Protocol 类型声明 | 中 |
| iter-94 | 2 | 配置加载缓存 | 中 |
| iter-95 | 2 | AST 分析内存优化 | 中 |
| iter-96 | 3 | CI Windows/slow/benchmark 三 job | 中高 |
| iter-97 | 3 | Linux 平台测试覆盖补强 | 中 |
| iter-98 | 3 | 测试 fixture 共享化 | 低 |
| iter-99 | 3 | macOS runtime + loader | 高 |
| iter-100 | 3 | macOS 安装包 + 里程碑收尾 | 高 |
| iter-101 | 4 | DLL/so 传递依赖分析 | 中高 |
| iter-102 | 4 | 启动时间优化 | 中高 |
| iter-103 | 4 | 依赖哈希校验 + SBOM + 签名 | 高 |
| iter-104 | 4 | manifest.json 产物清单 | 中 |
| iter-105 | 4 | 文档体系完善 | 低 |
