# 平台扩展与运行时优化（10 项迭代 iter-91 ~ iter-100）

## 背景

req-37~40 完成性能基线建立、大文件拆分、代码质量提升、功能增强、深度重构
与性能基线守护（iter-51~90）。项目结构、类型安全、性能基线、文档生态均达
稳态。本需求转向**平台扩展**（macOS 支持）、**运行时优化**（体积与启动）、
**安全加固**、**可观测性**与**插件机制**，并在 iter-100 里程碑收尾全量回顾
iter-51~100 的优化成果。

### 现状基线（2026-07-27，iter-90 完成后预期状态）

**平台支持**：

| 平台 | runtime | loader | 安装包 | 状态 |
|------|---------|--------|--------|------|
| Windows | embed python | mingw + .dll | NSIS | 完整支持 |
| Linux | python-build-standalone | gcc + .so | .deb + tar.gz | 完整支持 |
| macOS | 无 | 无 | 无 | 不支持 |

**体积优化现状**：

- wheel 精简：AST 闭包按需解压（PySide6/Qt/numpy/scipy 等专属 spec）
- stdlib 精简：剥离 test/ensurepip/idlelib（仅 Linux）
- pyc strip：剥离 .py 仅留 .pyc（--pyc-strip）
- .pyi 剥离：类型存根统一剥离
- **缺口**：DLL/.so 传递依赖未分析（如 Qt6Core.dll 依赖的 ICU 未用时仍保留）；
  无运行时压缩；无重复文件检测

**启动时间现状**：

- `--no-site` 节省 20-30ms（省略 site.py 加载）
- .pyc 预编译加速首次启动
- **缺口**：sys.path 扫描未优化（site-packages 全目录扫描）；
  重量级模块未延迟导入；site.py 未精简

**安全现状**：

- 依赖下载无哈希校验（pip download 未加 --require-hashes）
- 安装包未签名（NSIS exe / .deb 无数字签名）
- 无 SBOM（软件物料清单）生成

**可观测性现状**：

- BuildTracker 表格显示阶段耗时
- `-v` 开启 DEBUG 日志
- **缺口**：无构建日志文件持久化；无耗时分析报告；无产物清单（manifest.json）

**扩展性现状**：

- SlimSpec 注册表支持自定义 spec，但需修改源码
- **缺口**：无 entry_points 插件机制；用户无法在不修改 fspack 源码的情况下
  注册自定义 spec 或构建阶段钩子

## 10 项迭代任务

### macOS 平台支持（iter-91 ~ iter-92）

- [ ] **iter-91 macOS runtime + loader**：(1) `Platform` 枚举新增 `MACOS`；
  `detect_platform` 识别 Darwin；(2) `StandaloneRuntime` 扩展支持 macOS
  （python-build-standalone 提供 macOS x86_64 + arm64 tarball）；
  (3) 新增 `MacLoader`（clang 编译，dlopen libpython3.X.dylib，
  Mach-O 格式，`@executable_path` 解析 runtime 路径）；(4) `wheel_platform_tags`
  新增 macOS 标签（macosx_11_0_x86_64 / macosx_11_0_arm64）；
  (5) 测试：mock clang 编译，验证 C 源码生成与缓存键
- [ ] **iter-92 macOS 安装包与签名**：(1) 新增 `MacInstaller`（.pkg 通过
  pkgbuild + productbuild，.dmg 通过 hdiutil）；(2) `build_release` 的
  `auto` 格式在 macOS 平台默认 .pkg + .dmg；(3) 代码签名基础：`--codesign`
  选项调 codesign 签名 .app 与 .pkg（ad-hoc 签名，正式签名需用户配置
  Developer ID）；(4) 测试：mock subprocess 验证 pkgbuild/productbuild/
  hdiutil/codesign 命令构造

### 打包体积优化（iter-93 ~ iter-94）

- [ ] **iter-93 DLL/so 传递依赖分析**：(1) 新增 `dep_analyzer.py` 模块，
  用 `objdump`（Linux/macOS）或 `dumpbin`（Windows）解析 .dll/.so/.dylib
  的依赖树；(2) Qt 闭包外但被保留的 DLL 若无依赖引用则剥离（如 opengl32sw.dll
  在无 OpenGL 模块时已被 slim-exclude 处理，此迭代扩展到所有 DLL）；
  (3) `--analyze-deps` 选项启用深度依赖分析（默认关闭，耗时）；
  (4) 体积报告新增"依赖分析节省"行；测试：构造 mock DLL 依赖树验证剥离逻辑
- [ ] **iter-94 运行时压缩与按需加载**：(1) `--compress-runtime` 选项将
  site-packages 压缩为 .zip 挂载到 sys.path（Python 3.8+ 支持 zipimport，
  启动时自动解压到内存）；(2) 大模块（>10MB）按需加载：首次 import 时
  从 .zip 解压到 `~/.fspack-cache/<project>/`（避免重复解压）；
  (3) 重复文件检测：多个 wheel 含相同数据文件时去重（如多个 Qt 包共享
  translations）；(4) 测试：验证 zipimport 兼容性、按需加载缓存命中率

### 启动时间优化（iter-95 ~ iter-96）

- [ ] **iter-95 延迟导入与 sys.path 优化**：(1) entry wrapper 注入
  `sys.path_hooks` 优先匹配 site-packages（减少默认 path 扫描）；
  (2) 重量级模块（如 numpy/pandas）的延迟导入钩子：首次 import 时才
  执行模块初始化（需用户 opt-in `--lazy-import numpy,pandas`）；
  (3) `.pth` 文件优化：site-packages 下 .pth 仅在 `--no-site` 关闭时
  处理，默认跳过（第三方 .pth 多为开发期工具如 pytest）；(4) 测试：
  测量启动时间基线，验证提速 ≥10ms
- [ ] **iter-96 site.py 精简与 .pyc 启动优化**：(1) 生成精简版 site.py
  （仅保留 site-packages 注册，剥离 easy-install.pth 处理、user site、
  venv 重定向等）；(2) Python 3.11+ 启用 `PYTHONPYCACHEPREFIX` 隔离
  .pyc 缓存目录；(3) `--pyc-verify` 选项：启动时验证 .pyc 魔数与
  Python 版本匹配，不匹配自动重编译（避免 .pyc 损坏导致启动失败）；
  (4) 测试：验证精简 site.py 不破坏第三方包导入

### 安全加固（iter-97）

- [ ] **iter-97 依赖哈希校验与 SBOM**：(1) `--require-hashes` 选项透传
  给 pip download，强制依赖哈希校验（防供应链篡改）；(2) 生成 SBOM
  （软件物料清单）`dist/release/<name>-<ver>-sbom.json`，含所有依赖
  名称/版本/哈希/许可证（SPDX 格式）；(3) 安装包签名：Windows 用
  sigstore 签名 .exe（`--sign-exe`），Linux 用 GPG 签名 .deb
  （`--sign-deb`）；(4) 测试：验证 SBOM 格式合规、签名命令构造

### 可观测性（iter-98）

- [ ] **iter-98 构建日志与产物清单**：(1) `--log-file <path>` 选项将
  构建日志写入文件（含时间戳、阶段耗时、缓存命中、错误堆栈），便于
  CI 上传与问题排查；(2) `--profile` 选项输出耗时分析报告（各阶段
  wall time / CPU time / 内存峰值）；(3) 生成 `dist/manifest.json`
  产物清单（含所有文件路径、大小、sha256、来源 wheel）；(4) 测试：
  验证日志格式、profile 报告、manifest 完整性

### 插件机制（iter-99）

- [ ] **iter-99 自定义 spec 与构建阶段插件**：(1) 用 `entry_points`
  注册自定义 SlimSpec：第三方包 `my_slim_specs` 在 pyproject.toml 声明
  `[project.entry-points."fspack.slim_specs"]`，fspack 启动时自动加载；
  (2) 构建阶段钩子：`fsp_build_pre` / `fsp_build_post` entry_points，
  允许插件在构建前后执行自定义逻辑（如额外精简、签名、上传）；
  (3) `fsp plugin list` 子命令列出已安装插件；(4) 测试：构造 mock
  插件包验证注册与调用

### 里程碑收尾（iter-100）

- [ ] **iter-100 iter-51~100 全量回顾与基线快照**：(1) 全量回归
  iter-51~100 所有优化，对比 iter-50 基线验证累计性能提升；(2) 更新
  性能基线快照为 `0100_iter100-final.json`，作为下一阶段（iter-101+）
  基准；(3) 文档同步：README 新增 macOS 支持说明、体积优化指南、
  安全签名指南、插件开发指南；(4) changelog 汇总 iter-51~100 变更；
  (5) 项目记忆更新：迁移重要决策到 ADR，清理过时 lessons learned；
  (6) 验收：全套门禁通过，性能基线累计提升 ≥20%（analyze_dependencies
  从 6.8ms 降至 ≤5.4ms）

## 验收标准

- 每次迭代全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- macOS 支持（iter-91~92）：`fsp b --target macos` 在 macOS 原生平台
  可构建，`fsp p --target macos` 生成 .pkg + .dmg
- 体积优化（iter-93~94）：典型项目（PySide6）体积减少 ≥10%
- 启动优化（iter-95~96）：典型项目启动时间减少 ≥20ms
- 安全加固（iter-97）：SBOM 格式合规，签名命令可执行
- 可观测性（iter-98）：日志文件、profile 报告、manifest.json 均可生成
- 插件机制（iter-99）：mock 插件可注册并被调用
- 里程碑（iter-100）：性能基线累计提升 ≥20%，文档同步完整

## 实施顺序

1. iter-91~92（macOS 平台支持，扩展平台覆盖范围）
2. iter-93~94（体积优化，在平台扩展后统一优化体积）
3. iter-95~96（启动优化，在体积优化后平衡体积与启动速度）
4. iter-97（安全加固，在功能稳定后补充安全特性）
5. iter-98（可观测性，在安全加固后提供运维支持）
6. iter-99（插件机制，在核心功能完整后开放扩展点）
7. iter-100（里程碑收尾，全量回顾与基线固化）

## 依赖关系

- iter-91 依赖 iter-58（config.py 拆分）完成，Platform 枚举在 config 模块
- iter-92 依赖 iter-91（MacLoader）与 iter-61（installer.py 拆分）完成
- iter-93 依赖 iter-84（qt.py 拆分）完成，DLL 分析扩展 slim 模块
- iter-94 依赖 iter-93（依赖分析）完成，压缩前需分析依赖
- iter-95 依赖 iter-73（--dry-run）完成，启动测量复用 dry-run 配置解析
- iter-97 依赖 iter-74（体积报告）完成，SBOM 复用体积统计
- iter-98 依赖 iter-78（CI 增强）完成，日志上传复用 CI artifact
- iter-99 依赖 iter-85（Protocol 类型声明）完成，插件接口用 Protocol 定义
- iter-100 依赖 iter-91~99 全部完成

## 风险与缓解

- **iter-91 macOS 风险**：macOS loader 的 Mach-O 格式与 .dylib 加载机制
  与 Linux ELF 差异大（`@rpath`/`@loader_path`/`@executable_path`）。
  缓解：参考 python-build-standalone 自带的 python 可执行文件结构，
  loader 用 `_dyld_get_image_name` 查找 libpython；CI 添加 macOS runner
  验证
- **iter-93 依赖分析风险**：objdump/dumpbin 在 Windows 可能不可用。
  缓解：Windows 用 Python `pefile` 库解析 PE 导入表，Linux/macOS 用
  `ldd`/`otool`，回退到 objdump
- **iter-94 zipimport 风险**：部分 C 扩展（.pyd/.so）不支持 zipimport
  （需从文件系统加载）。缓解：C 扩展与纯 Python 分离，仅压缩纯 Python
  包到 .zip，C 扩展保留文件系统
- **iter-97 sigstore 风险**：sigstore 需要网络与 OIDC 认证，离线环境
  不可用。缓解：`--sign-exe` 可选，未指定时跳过签名；提供
  `--sign-exe-certificate <pfx>` 用传统代码签名证书
- **iter-99 插件风险**：entry_points 加载第三方插件可能引入安全风险
  （恶意插件）。缓解：`--trust-plugins <name>` 显式信任指定插件，
  未信任的插件仅打印不加载
