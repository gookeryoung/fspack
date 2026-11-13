# fspack 多目标打包方案（对照 cibuildwheel）

- 日期：2026-11-13
- 版本基线：`0.6.14`，分支 `main`，HEAD `e5540f9`
- 范围：`src/fspack/`、`.github/workflows/`、`tests/`、`docs/`
- 性质：**只读分析与方案设计**。本文不改任何代码，结论以源码行号、CI 配置与测试文件为证据。

## 0. 结论摘要

1. **fspack 目前不是「多目标打包工具」，而是「单目标打包工具 + 一个目标平台开关」。**
   每次 `fsp b` 只能产出一个平台、一个架构、一个 Python 版本的产物；架构完全由构建机隐式决定，
   CLI 没有 `--arch`；多版本只能手工多次调用，且产物目录固定为 `dist/`，会互相覆盖。
   所以「矩阵」现在只存在于 CI 的**单元测试**里（3 OS × 7 Python），不存在于**打包**里。

2. **真实产出能力从强到弱：Windows x86_64 > Linux x86_64 > macOS（两架构）> 其余全部空白。**
   - Windows x86_64 embed 是唯一有完整版本表（3.8–3.14 + 自由线程）、哈希清单、真实 CI 产物（`release.yml`）的目标；
   - Linux x86_64 standalone 有真实 CI 产物，但版本表只到 3.10–3.14，**3.8/3.9 不是「没跑过」而是「没有运行时资产」**；
   - macOS 代码路径、哈希、命名归一化都齐，但**没有任何一次真实构建进过 CI**，属「声称支持、未跑过」；
   - Linux arm64 / Windows arm64 是空白（URL 拼接硬编码 x86_64）；
   - Windows 目标的「自由线程（3.13t/3.14t）」有代码与哈希，但无真实构建验证。

3. **cibuildwheel 真正值得借的是「把矩阵声明变成唯一事实源，再让工具自己展开 CI job」**，
   不是它的命令行。fspack 缺的正是这层：没有声明、没有 cell 概念、没有逐格隔离、产物命名不含架构、
   没有平台/架构格式校验、没有跳过与允许失败的表达、本地无法用同一条命令复现远端某一格。

4. **架构取向：以原生构建为唯一「可发布」路径，交叉构建只保留 Windows 目标。**
   - 保留：Linux/macOS 构建机 → Windows 目标（mingw 交叉，已有 e2e 覆盖）；
   - 明确不做：Windows/macOS → Linux 目标、Linux/Windows → macOS 目标（工具链、SDK、签名三重障碍）；
   - arm64 一律走原生 arm runner，不做 QEMU 仿真构建作为发布路径；
   - 不建议现在做 Windows arm64。

5. **分四期落地**：Phase 0（立刻可做，纯修正与护栏）、Phase 1（需先定 cell 契约）、Phase 2（多架构原生）、
   Phase 3（可选，容器编排）。不建议做的事项单列在附录 B。

## 1. 盘点口径

所有结论来自静态检索与只读比对，未执行任何写盘命令：

- **平台/架构判定**：`src/fspack/platform.py`、`src/fspack/packaging/pipeline/runtime_stage.py`；
- **运行时资产**：`src/fspack/config/versions.py`（版本表）、`src/fspack/packaging/runtime/urls.py`（URL/文件名）、
  `src/fspack/packaging/runtime/hashes.py`（sha256 清单）；
- **构建流水线**：`src/fspack/packaging/pipeline/`（executor / runtime_stage / compile_stage）；
- **产物与命名**：`src/fspack/packaging/installer/`、`src/fspack/packaging/manifest.py`；
- **CI 证据**：`.github/workflows/ci.yml`、`.github/workflows/release.yml`；
- **测试证据**：`tests/test_build_dry_run.py`、`tests/test_e2e_slow.py`、`tests/test_runtime_hash.py`、
  `tests/test_runtime.py`、`tests/test_platform.py`、`tests/test_installer.py`。

## 2. 现状：多目标支持到哪一步

### 2.1 三个维度各自被什么决定

| 维度 | 决定方式 | 代码位置 | 是否可显式指定 |
|---|---|---|---|
| 平台 | `--target windows\|linux\|macos`，默认宿主平台 | [executor.py:212](../../src/fspack/packaging/pipeline/executor.py#L212)；[cli.md:35](../../docs/cli.md#L35) | 是 |
| 架构 | **完全隐式**：Linux/Windows 固定 x86_64；macOS 取 `platform.machine()` | [urls.py:87-92](../../src/fspack/packaging/runtime/urls.py#L87-L92)；[runtime_stage.py:213-218](../../src/fspack/packaging/pipeline/runtime_stage.py#L213-L218) | **否（无 `--arch`）** |
| Python 版本 | `--py-version` > `.python-version` > `requires-python` > 平台默认，单值解析 | [versions.py:218-272](../../src/fspack/config/versions.py#L218-L272) | 只能一次一个 |
| 运行时形态 | 平台决定：Windows 标准版→embed；Windows 自由线程→standalone；Linux/macOS→standalone | [runtime_stage.py:110-115](../../src/fspack/packaging/pipeline/runtime_stage.py#L110-L115) | **否（无开关）** |

结论：**平台是唯一的一等公民，架构和运行时形态都是平台的派生属性。** 这套模型天生只能表达「一个目标」，
无法表达「同一项目、同一版本、多个目标」。

### 2.2 真实支持矩阵

图例：**已验证** = 有真实 CI 构建产物/端到端执行；**未验证** = 代码路径与哈希齐全但无真实构建；
**空白** = 代码未实现或无上游资产；**不适用** = 设计上不存在。

| 平台 | 架构 | Python | 运行时形态 | 状态 | 证据 |
|---|---|---|---|---|---|
| Windows | x86_64 | 3.8–3.14 | embed | **部分已验证（3.11、3.14.6），其余未验证** | 版本表 [versions.py:43-60](../../src/fspack/config/versions.py#L43-L60)；哈希 `hashes.py:39`；真实构建 `release.yml:101` |
| Windows | x86_64 | 3.13t / 3.14t | standalone（freethreaded tarball） | **未验证** | 分流 [runtime_stage.py:113-115](../../src/fspack/packaging/pipeline/runtime_stage.py#L113-L115)；命名 [urls.py:93-97](../../src/fspack/packaging/runtime/urls.py#L93-L97)；无 e2e |
| Windows | x86_64 | 任意 | stdlib zip / slim runtime | **不适用** | 显式跳过 [stdlib_zip.py:140-141](../../src/fspack/packaging/runtime/stdlib_zip.py#L140-L141)；[architecture.rst:54-57](../../docs/architecture.rst#L54-L57) |
| Windows | arm64 | — | — | **空白** | URL 硬编码 `x86_64-pc-windows-msvc` [urls.py:90](../../src/fspack/packaging/runtime/urls.py#L90) |
| Linux | x86_64 | 3.10–3.14 | standalone | **部分已验证（3.11.15、3.14.6），其余未验证** | 版本表 [versions.py:66-76](../../src/fspack/config/versions.py#L66-L76)；真实构建 `release.yml:171` |
| Linux | x86_64 | 3.13t / 3.14t | standalone | **未验证** | `versions.py:74-75`；命名 `urls.py:95-97` |
| Linux | x86_64 | 3.8 / 3.9 | — | **空白（无上游资产）** | `KNOWN_STANDALONE_VERSIONS` 无 3.8/3.9 键（[versions.py:66-76](../../src/fspack/config/versions.py#L66-L76)） |
| Linux | arm64 | — | — | **空白** | URL 硬编码 `x86_64-unknown-linux-gnu` [urls.py:92](../../src/fspack/packaging/runtime/urls.py#L92)；wheel tag 仅 x86_64 [platform.py:56](../../src/fspack/platform.py#L56) |
| macOS | arm64 | 3.10–3.14（+t） | standalone | **已验证（3.11，e2e-macos on macos-latest）** | 架构探测 [runtime_stage.py:213-218](../../src/fspack/packaging/pipeline/runtime_stage.py#L213-L218)；哈希 `hashes.py:51-72`；命名归一化 [urls.py:60](../../src/fspack/packaging/runtime/urls.py#L60)；CI: `e2e-macos` 矩阵 arm64 格 |
| macOS | x86_64 | 3.10–3.14（+t） | standalone | **待验证（已配置 CI 格）** | 同上；Intel runner 用 `macos-15-intel`（`macos-13` 已退役），由 `e2e-macos` 矩阵覆盖 |
| 全部 | 任意 | 任意 | Nuitka `.pyd` | **仅同平台** | 跨平台静默跳过 [compile_stage.py:110](../../src/fspack/packaging/pipeline/compile_stage.py#L110) |
| 全部 | 任意 | 任意 | pyc 预编译 | **仅同平台** | 跨平台静默跳过 [compile_stage.py:156](../../src/fspack/packaging/pipeline/compile_stage.py#L156) |

补两条容易误读的事实：

- **「Windows 支持 3.8–3.14」是相对的。** `KNOWN_EMBED_VERSIONS` 确实覆盖 3.8–3.14，
  但 CI 真实构建只走过 3.11（e2e 工程的 `requires-python = ">=3.8,<3.12"` 解析到 3.11.9）
  与 3.14.6（fspack 自身 `requires-python = ">=3.8"` 解析到最高版本）。
  3.8/3.9/3.10/3.12/3.13 的 embed 链路**没有一次真实运行**。
- **Linux/macOS 的 3.8/3.9 不是「版本表缺省」这么温和。** `--py-version 3.8` 在短版本归一化时
  返回 `None` 后仍用原串（[versions.py:246-250](../../src/fspack/config/versions.py#L246-L250)），
  最终会拼出 `cpython-3.8+20260718-...` 这种不存在的 URL 而下载 404；
  `.python-version` 写 3.8 则被静默丢弃并回退到 `requires-python` 的最高兼容版本
  （[versions.py:252-270](../../src/fspack/config/versions.py#L252-L270)）。前者是明确报错，后者是**静默降级**。

### 2.3 运行时形态的作用范围

| 形态 | 适用平台 | 关键实现 | 说明 |
|---|---|---|---|
| embed zip | Windows 标准版 | [urls.py:44-55](../../src/fspack/packaging/runtime/urls.py#L44-L55) | 文件名硬编码 `amd64`，无 arm64 分支 |
| standalone tarball | Linux / macOS / Windows 自由线程 | [urls.py:63-97](../../src/fspack/packaging/runtime/urls.py#L63-L97) | 平台三元组写死；仅 macOS 有 `macos_arch` 参数 |
| stdlib zip | Linux / macOS | [stdlib_zip.py:134-147](../../src/fspack/packaging/runtime/stdlib_zip.py#L134-L147) | 依赖「运行时 python 可执行」；交叉构建时该二进制不可执行 |
| slim runtime | Linux / macOS | [executor.py:488-492](../../src/fspack/packaging/pipeline/executor.py#L488-L492) | strip libpython + 删 python 二进制 |

**交叉构建的隐性代价**：Linux 构建机产 Windows 产物时，目标 runtime 是 embed zip，
`stdlib zip`/`slim runtime` 本来就不适用，但 `pyc 预编译`与 `Nuitka`同样被跳过
（因为 runtime 的 python 无法执行）。结果是**同一个文件名下，交叉产物与原生产物内容不同**
（无 `.pyc`、无 `.pyd`），而产物名里没有任何字段能区分——这是一个可复现性缺口，见 G6。

### 2.4 CI 矩阵与已有产物

| job | 矩阵 / runner | 覆盖内容 | 与「打包」的关系 |
|---|---|---|---|
| `lint` | ubuntu | ruff / pyrefly / pip-audit | 无关 |
| `test` | 3 OS × 7 Python = **21 格**（[ci.yml:53-60](../../.github/workflows/ci.yml#L53-L60)） | 单元测试，`-m "not slow"` | **不产出任何打包产物** |
| `e2e` | 仅 ubuntu（[ci.yml:95-96](../../.github/workflows/ci.yml#L95-L96)） | 5 个真实构建/运行用例 + 离线用例 | 真实构建：Linux 原生、Linux→Windows 交叉 |
| `benchmark` | ubuntu，仅 push main | 性能基线 | 无关 |
| `pack-windows` | windows-latest（[release.yml:58-59](../../.github/workflows/release.yml#L58-L59)） | 打包 fspack 自身 + NSIS/zip | **真实产物** |
| `pack-linux` | ubuntu（[release.yml:136-137](../../.github/workflows/release.yml#L136-L137)） | 打包 fspack 自身 + deb/tar.gz/zip | **真实产物** |
| macOS 打包 | — | — | **不存在** |

值得注意的是：`test` 的 21 格看起来像一个「完整的平台矩阵」，但它只证明**代码路径**在三个平台可 import、
可跑单测，不证明任何一格能真正产出可运行产物。这正是「声称支持」与「已验证」的分界线。

`e2e` 的真实构建矩阵实际只有两格：

- Linux 原生（`test_e2e_native_build_and_run_smoke` 等 3 个）；
- Linux → Windows 交叉（[test_e2e_slow.py:199-237](../../tests/test_e2e_slow.py#L199-L237)）。

macOS 在 e2e 里只有 `skip` 分支（[test_e2e_slow.py:57-59](../../tests/test_e2e_slow.py#L57-L59)），从未被执行。

### 2.5 产物命名现状

统一基名：`<name>-<version>-py<full_version>-<platform>-slim`（[dist_prep.py:120-131](../../src/fspack/packaging/installer/dist_prep.py#L120-L131)）。

| 格式 | 实际文件名 | 是否含架构 | 代码 |
|---|---|---|---|
| Windows setup | `name-1.0-py3.14.6-windows-slim-setup.exe` | 否 | [facade.py:170](../../src/fspack/packaging/installer/facade.py#L170) |
| zip / 7z | `name-1.0-py3.14.6-windows-slim.zip` | 否 | [zip.py:68](../../src/fspack/packaging/installer/zip.py#L68) |
| Linux tar.gz | `name-1.0-py3.14.6-linux-slim.tar.gz` | 否 | [linux.py:88](../../src/fspack/packaging/installer/linux.py#L88) |
| Linux deb | `name_1.0-py3.14.6-slim_amd64.deb` | **写死 amd64** | [linux.py:49-59](../../src/fspack/packaging/installer/linux.py#L49-L59)、[linux.py:95](../../src/fspack/packaging/installer/linux.py#L95) |
| manifest | `name-1.0-manifest.json` | 否，且**不含 py/platform** | [manifest.py:211-225](../../src/fspack/packaging/manifest.py#L211-L225) |
| SBOM | `name-1.0-sbom.json` | 否，且不含 py/platform | [cli_parser.py:345](../../src/fspack/cli_parser.py#L345) |

同时还要注意 `_deb_arch()` 的注释已经承认：早前 macOS arm64 runner 产出的 `.deb` 被误标 arm64，
修复方式是**固定 amd64**（[linux.py:49-59](../../src/fspack/packaging/installer/linux.py#L49-L59)）。
这是「架构不可表达」导致的直接后果：工具没有架构维度，只能在单个产物里写死一个值。

### 2.6 测试覆盖分级

| 级别 | 代表用例 | 证明了什么 |
|---|---|---|
| 命名/URL 纯函数 | [test_runtime.py:373-450](../../tests/test_runtime.py#L373-L450) | tarball 名、macOS 双架构归一化正确 |
| 哈希清单完整性 | [test_runtime_hash.py:44-59](../../tests/test_runtime_hash.py#L44-L59) | 每个已知版本在 Linux/Win/macOS x86_64/macOS arm64 都有 sha256 |
| 交叉守卫 | [test_build_dry_run.py:469-503](../../tests/test_build_dry_run.py#L469-L503) | 非法组合被显式拒绝 |
| macOS 平台常量 | [test_platform.py:19-21](../../tests/test_platform.py#L19-L21)、[test_platform.py:65-68](../../tests/test_platform.py#L65-L68) | 常量与 wheel tag 形态 |
| 安装器分派 | [test_installer.py:945-1006](../../tests/test_installer.py#L945-L1006) | macOS 格式解析/分派（工具全部 mock） |
| 真实构建 | [test_e2e_slow.py:114-237](../../tests/test_e2e_slow.py#L114-L237) | 仅 Linux 原生 + Linux→Windows 交叉 |

**分级结论**：命名与 URL 层的测试很扎实；**真实产物层只有 Linux 与 Linux→Windows 两格**。
macOS 的所有测试都停在「格式选择」和「字符串拼接」，没有一次 `pkgbuild`/`hdiutil` 真实执行。

### 2.7 缺口清单

| # | 缺口 | 直接后果 |
|---|---|---|
| G1 | 无矩阵声明，无 cell 概念 | 多目标只能靠外部脚本循环，无法校验完整性 |
| G2 | 无 `--arch` | 架构能力被构建机锁死；macOS 双架构无法在一次运行中产出 |
| G3 | 产物目录/命名不含目标 | 多目标产物互相覆盖；manifest/SBOM 必然被覆盖 |
| G4 | 无产物格式校验（PE/ELF/Mach-O 机器类型） | 交叉产物「表面成功、格式错误」只能靠人眼发现 |
| G5 | 无跳过/允许失败表达 | CI 只能整 job 失败；实验格无法灰度 |
| G6 | 交叉构建静默降级（无 pyc / 无 Nuitka）但同名同目录 | 同一文件名对应不同内容，不可复现 |
| G7 | macOS 目标零真实 CI 构建 | 文档声称支持，实际未验证 |
| G8 | Linux/macOS 3.8/3.9 无资产但版本解析会走进去 | 一种是 404，一种是静默换版本 |
| G9 | macOS wheel tag 同时返回 x86_64 与 arm64 | arm64 产物可能混入 x86_64 wheel |
| G10 | 本地无「复现远端某格」入口 | 线上失败只能靠读日志猜环境 |

## 3. cibuildwheel 值得借鉴的组织方式

先明确：**它的 CLI 只是外壳，真正的价值在四件事——声明、展开、隔离、校验。**

| # | 机制 | cibuildwheel 的做法 | fspack 现状 | 缺什么 |
|---|---|---|---|---|
| B1 | 矩阵声明 | `pyproject.toml` 里 `build`/`skip` 用选择器串（如 `cp311-*`），按平台分节覆盖，`[[tool.cibuildwheel.overrides]]` 对单格加参数 | 无任何声明 | 需要一个能表达 platform/arch/python/runtime 的声明区 |
| B2 | 声明 → CI job 的唯一展开 | `cibuildwheel --print-build-identifiers` 输出格列表，CI 用它生成 `strategy.matrix`，本地与远端共用同一份解析 | 无；CI 的 21 格是手写 `os × python-version` | 需要一个 `fsp matrix list --json` 之类的展开入口 |
| B3 | 每格独立隔离环境 | Linux 每架构一个 manylinux 容器，macOS/Windows 原生；每格从干净环境开始 | 全部在宿主环境执行，工具链与缓存共用 | 逐格环境契约（编译器、SDK、缓存边界） |
| B4 | 每格可复现 | 容器镜像按 digest 固定、构建依赖固定、`before-build` 钩子固定 | 运行时由 release tag + sha256 固定（这点很好），但编译器/系统库不固定 | 记录并校验工具链版本 |
| B5 | 产物命名与校验 | PEP 425/600 平台标签 + abi 标签；`auditwheel`/`delocate`/`delvewheel` 修复并校验；干净环境安装测试 | 名字含 py/platform，缺 arch；有 sha256 manifest 与 Win7 导入表门禁，但无格式探测、无干净环境安装 | arch 段 + 二进制机器类型校验 + 可选安装冒烟 |
| B6 | 跳过与允许失败 | `skip` 选择器、`test-skip`、CI 里 `continue-on-error` | 只有 pytest marker；打包侧没有 | cell 级 `skip` / `allow_failure` |
| B7 | 本地复现远端某格 | 同一个工具 + `--only <identifier>` + `--platform`，容器由工具拉起 | 只能手工 `fsp b --target X --py-version Y`，且不知道远端用了什么环境 | cell id + `--cell` + （可选）容器指令生成 |
| B8 | 构建与测试分离 | 每格产物装进干净环境跑测试，而不是在构建环境里自测 | e2e 是「构建后在本机直接跑」，跨架构格无法这样验 | 冒烟执行需按「本机能否运行该架构」判定 |
| B9 | 失败产物留存 | 每格日志与产物独立上传 | release job 有 failure artifacts（`release.yml:117-123`），e2e 无 | 逐格产物/日志留存 |
| B10 | 空矩阵语义 | `--allow-empty` 明确区分「没有格」与「全部失败」 | 无 | 报告里区分 `skipped` 与 `failed` |

其中 **B2 是最关键、也最容易被忽略的一条**。cibuildwheel 的矩阵不是在 workflow 里手写的，
而是工具自己算出来再喂给 CI 的。这保证了：

- 本地跑某一格与 CI 跑同一格，解析规则完全一致；
- 新增一格只改声明，不改 workflow；
- 跳过规则只有一处，不会出现「本地跳了、CI 没跳」。

fspack 目前的 21 格单测矩阵与 `release.yml` 的两个打包 job 是三份互相独立的手写配置，
没有任何机制保证它们与代码里的版本表一致。`test_runtime_hash.py:44-59` 是唯一的一致性护栏，
而且它只校验「哈希是否齐全」，不校验「这些组合是否真的能构建」。

## 4. 交叉编译还是原生构建

### 4.1 可行性边界

| 构建机 → 目标 | 可行性 | fspack 现状 | 主要障碍 |
|---|---|---|---|
| Linux/macOS/Windows → Windows x86_64 | **可行（已实现）** | 放行并 e2e 覆盖（`executor.py:99-100`） | 签名需 Windows 工具链；pyc/Nuitka 降级 |
| Linux → Linux x86_64 | 原生 | 放行 | 无 |
| Windows/macOS → Linux x86_64 | **不可行（现实约束）** | 显式拒绝（`executor.py:112-117`） | 缺 ELF 工具链 + glibc sysroot；`auditwheel` 语义；`.deb` 工具链 |
| 任意非 macOS → macOS | **不建议** | 显式拒绝（`executor.py:107-111`） | Apple SDK 授权、Mach-O 链接、`install_name_tool`/`codesign` 只能在 macOS |
| macOS arm64 → macOS x86_64 | **技术可行**，验证受限于 Rosetta | 不支持（架构由宿主决定） | 跨架构 wheel 选择；只能靠 Rosetta 冒烟 |
| macOS x86_64 → macOS arm64 | **技术可行**，但无法验证 | 不支持 | Intel 机无法执行 arm64 产物，等于「盲发」 |
| 任意 → Linux arm64 | 可行但代价高 | 空白 | 需要 aarch64 交叉 gcc 或 QEMU；wheel tag 需新增 `manylinux*_aarch64` |
| 任意 → Windows arm64 | **不建议** | 空白 | 运行时资产、工具链、生态需求都最弱 |

### 4.2 风险分类

| 风险 | 出现在哪条路径 | 具体表现 |
|---|---|---|
| 运行时下载 | 所有交叉路径 | URL 平台段写死 `x86_64-*`（`urls.py:87-92`），新增架构必须同时补 URL、资产名归一化、sha256 清单（`hashes.py`）与 `test_runtime_hash.py` |
| C loader 编译 | Linux→Windows 已解决；其他路径未解决 | loader 由内置 C 源码 + 编译器产出（`packaging/loader/toolchain.py:28-29`）；`mingw` 前缀只在 Windows 目标使用 |
| 二进制兼容 | Linux 目标、arm64 | 交叉产出的 ELF 必须满足最老 glibc；`auditwheel` 类修复缺失；wheel 平台标签必须与产物一致 |
| 签名 | 全部交叉路径 | Windows `signtool` 只在 Windows；macOS `codesign` 只在 macOS（`docs/distribution.md:30-40`）；跨机构建后无法本地签名 |
| 可验证性 | macOS arm64 from Intel / 任意跨架构 | 产物无法在构建机上执行，冒烟测试必须跳过——「构建成功」不能等同于「可用」 |
| 可复现性 | Linux→Windows | pyc/Nuitka 静默跳过（`compile_stage.py:110,156`），产物内容与原生格不同，但名字相同 |

### 4.3 取舍建议（明确结论）

1. **发布路径只认原生构建。** Windows/macOS/Linux 各用自己的 runner，`--target` 等于宿主平台。
   理由：只有原生格能做「构建 + 运行」闭环，也只有原生格能签名。
2. **Windows 目标保留交叉能力，但不作为发布主路径。** 它的价值是「在 Linux CI 上做快速回归」
   （已有 `test_e2e_slow.py:199-237` 支撑），而不是替代 Windows runner 出正式包。
3. **不做 Linux 目标与 macOS 目标的交叉。** 不是「暂时没做」，而是**不建议做**：
   ELF/Mach-O 的工具链与签名约束会把 fspack 拖进「自研 cibuildwheel」的泥潭，
   收益远小于「加一个 arm runner」。
4. **arm64 走原生 runner，不走 QEMU 仿真。** 仿真构建的耗时与不确定性会污染 CI 信号，
   且无法验证产物真正可运行。
5. **Windows arm64 暂不做。** 需求最弱、资产与工具链最不成熟，投入产出比最低。
6. **如果一定要在 Intel Mac 上产 arm64（过渡方案）**，必须同时接受「不可本地验证」，
   并把该格标记为 `allow_failure` + `unverified`，不能进入正式发布清单。

## 5. 方案草案

### 5.1 设计原则

1. **声明是唯一事实源**：矩阵写在 `pyproject.toml`，CI 不手写格，只消费 `fsp matrix list`。
2. **cell id 是一等公民**：它同时是矩阵行、`dist` 子目录名、产物名后缀、日志与报告的键。
3. **正交四元组**：platform × arch × python × runtime，任何非正交的例外都必须显式写出来。
4. **跳过 ≠ 失败 ≠ 未验证**：三者语义分开，报告里分别计数。
5. **原生优先**：cell 声明里不写「用哪台机器」，由 runner 映射表推导；跨架构格必须标注可验证性。

### 5.2 矩阵声明形态（用户怎么写）

推荐两种写法，**展开式**给日常使用，**显式列**给发布通道；两者可共存，最终都归一成 cell 列表。

```toml
[tool.fspack.matrix]
# 展开式：四元组做笛卡尔积，再按 skip 过滤
platforms = ["windows", "linux", "macos"]
arches    = ["x86_64", "arm64"]
pythons   = ["3.11", "3.13"]
runtime   = "auto"          # auto: windows 标准版→embed；其余→standalone

# 跳过规则：glob 匹配 cell id（也接受平台/架构简写），先过滤、后展开
skip = [
  "windows-arm64-*",   # 上游资产未接入（空白）
  "linux-arm64-*",     # Phase 2 前不支持
  "macos-x86_64-*",    # 无 Intel runner
]

# 显式列：优先级高于展开式，用于发布通道精确控制
targets = [
  "windows-x86_64-py3.14.6-embed",
  "linux-x86_64-py3.14.6-standalone",
  "macos-arm64-py3.14.6-standalone",
]

# 单格覆盖：只允许改「行为」，不允许改「身份」
[tool.fspack.matrix.overrides]
"windows-*-py3.13.14t-*" = { allow_failure = true }
"macos-arm64-*"         = { smoke = "required" }
```

要点：

- **`pythons` 写 minor（`3.11`）还是 full（`3.11.9`）由解析器决定**：写 minor 时按目标平台版本表
  解析到该平台可用的补丁版本（与 `resolve_py_version` 现有语义一致）；
  展开后的 cell id 一律使用 **full version**，保证可复现。
- **`runtime = "auto"`** 是唯一默认值；只有需要强制形态时才写 `embed` / `standalone`
  （例如排查「为什么 Windows 上没用 embed」）。
- **`skip` 只作用于展开式**；`targets` 里显式列出的格即使命中 `skip` 也保留，
  但输出一条 `warning`，避免「写了却没生效」的静默。
- **`overrides` 的键是 glob**，值只能包含 `allow_failure` / `smoke` / `timeout` 这类行为字段，
  不能改 cell 身份（架构/Python/形态），否则等于绕过矩阵。

### 5.3 cell id 与展开规则

语法：

```text
<platform>-<arch>-py<full-version>[t]-<runtime>
```

示例：

```text
windows-x86_64-py3.11.9-embed
windows-x86_64-py3.13.14t-standalone
linux-x86_64-py3.11.15-standalone
macos-arm64-py3.14.6-standalone
```

展开算法（顺序固定，便于测试）：

1. 若 `targets` 非空 → 直接解析为 cell 列表（校验语法、校验组合合法性）；
2. 否则对 `platforms × arches × pythons × runtime` 做笛卡尔积；
3. 每个组合先做**能力校验**：平台是否支持该 runtime、该平台是否有该 Python 的运行时资产；
   不满足则记为 `skipped(unsupported-<reason>)`，**不报错**；
4. 应用 `skip` 过滤，记为 `skipped(user-skip)`；
5. 应用 `overrides`；
6. 输出稳定排序（platform → arch → python 降序 → runtime）的 cell 列表。

关键语义：**第 3 步的「能力校验」必须来自代码里的真实资产表**，而不是另写一份常量。
可直接复用 `KNOWN_EMBED_VERSIONS` / `KNOWN_STANDALONE_VERSIONS` 与 `platform.py` 的架构常量。

### 5.4 如何展开成 CI job

新增一个 `plan` job，产物就是矩阵本身：

```yaml
jobs:
  plan:
    runs-on: ubuntu-latest
    outputs:
      matrix: ${{ steps.m.outputs.matrix }}
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v10.3.3
      - run: uv sync --frozen --extra test
      - id: m
        run: |
          echo "matrix=$(uv run fsp matrix list --json --ci)" >> "$GITHUB_OUTPUT"

  pack:
    needs: plan
    strategy:
      fail-fast: false
      matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}
    runs-on: ${{ matrix.runner }}
    continue-on-error: ${{ matrix.allow_failure }}
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v10.3.3
      - run: uv sync --frozen --extra test
      - run: uv run fsp b . --cell "${{ matrix.cell }}"
      - run: uv run fsp verify --cell "${{ matrix.cell }}"
      - uses: actions/upload-artifact@v6
        with:
          name: pack-${{ matrix.cell }}
          path: dist/${{ matrix.cell }}/
          if-no-files-found: error
```

`fsp matrix list --json --ci` 输出的是 GitHub Actions 的 `include` 形态，每项包含：

```json
{
  "cell": "linux-x86_64-py3.11.15-standalone",
  "runner": "ubuntu-latest",
  "timeout": 30,
  "allow_failure": false,
  "smoke": "required"
}
```

runner 映射表（建议内置，可被声明覆盖）：

| platform | arch | runner |
|---|---|---|
| windows | x86_64 | `windows-latest` |
| linux | x86_64 | `ubuntu-latest` |
| linux | arm64 | arm64 runner（如 `ubuntu-24.04-arm`，以组织可用性为准） |
| macos | arm64 | `macos-latest`（arm64） |
| macos | x86_64 | `macos-15-intel`（`macos-13` 已退役） |
| windows | arm64 | 无（未支持） |

这样，「矩阵规模」这个决策从 workflow 里搬到了 `pyproject.toml` + 代码资产表，
新增/删除一格不会产生 workflow 与代码的漂移。

### 5.5 每格产物命名与校验

#### 命名

统一规则：**在现有基名里插入 arch，并让所有派生产物继承 cell id。**

```text
<name>-<version>-py<full-version>-<platform>-<arch>-slim.<ext>
```

| 产物 | 命名 |
|---|---|
| exe | `dist/<cell>/<name>.exe`（loader） |
| setup | `<name>-<ver>-py<ver>-windows-x86_64-slim-setup.exe` |
| zip / 7z | `<name>-<ver>-py<ver>-<platform>-<arch>-slim.zip` |
| tar.gz | `<name>-<ver>-py<ver>-linux-<arch>-slim.tar.gz` |
| deb | `<name>_<ver>-py<ver>-slim_<debarch>.deb`，`debarch` 由 cell 推导（x86_64→amd64，arm64→arm64） |
| manifest | `<name>-<ver>-py<ver>-<platform>-<arch>-manifest.json` |
| SBOM | `<name>-<ver>-py<ver>-<platform>-<arch>-sbom.json` |

目录约定（这是解决 G3/G6 的关键）：

```text
dist/
  <cell-id>/          # 每格独立 dist，杜绝跨格覆盖
    runtime/
    src/
    <name>.exe
    release/
      ...-manifest.json
```

同时建议 `fsp b` 增加 `--out <dir>`（或 `--cell` 派生默认目录），把 `executor.build(dist_dir=...)`
已有的能力暴露到 CLI。**注意：`dist_dir` 参数早已存在（[executor.py:141](../../src/fspack/packaging/pipeline/executor.py#L141)），只是没暴露，改动面很小。**

#### 校验

按「零新依赖」原则设计（`pefile` 已是运行时依赖）：

| 编号 | 校验项 | 方法 | 失败处理 |
|---|---|---|---|
| V1 | 产物存在且非空 | 文件枚举 + 大小 | 失败 |
| V2 | 二进制机器类型与 cell 一致 | PE：`pefile.PE().FILE_HEADER.Machine`（0x8664/0xAA64）；ELF：`struct` 读 `e_machine`（62/183）；Mach-O：`struct` 读 `cputype`（0x01000007/0x0100000C） | 失败 |
| V3 | 运行时版本与 cell 一致 | Windows 断言 `runtime/python3XX.dll`；Linux/macOS 断言 `runtime/python/lib/python3.X` | 失败 |
| V4 | 安装包架构字段一致 | `.deb` 读 `control` 的 `Architecture:`；NSIS/zip 复用 V2 | 失败 |
| V5 | 清单完整性 | 复用 `collect_manifest`，逐条 sha256 自检 | 失败 |
| V6 | 运行冒烟 | 仅当 `cell.arch` 可在当前机器执行（原生，或 macOS x86_64 on arm 走 Rosetta） | `skipped(no-emulator)` |

V6 的跳过必须是**显式记录**，而不是静默通过——这正是当前 `test_e2e_slow.py` 在 macOS 上
`pytest.skip` 的做法，值得保留到新模型里。

### 5.6 失败与跳过的表达

统一报告 `dist/matrix-report.json`：

```json
{
  "schema": 1,
  "cells": [
    {"cell": "linux-x86_64-py3.11.15-standalone", "status": "built", "verify": "passed", "smoke": "passed"},
    {"cell": "macos-x86_64-py3.11.15-standalone", "status": "skipped", "reason": "no-runner"},
    {"cell": "windows-arm64-py3.11.9-embed", "status": "skipped", "reason": "unsupported-runtime"},
    {"cell": "windows-x86_64-py3.13.14t-standalone", "status": "failed", "phase": "verify", "detail": "PE machine 0x14c != 0x8664"}
  ],
  "summary": {"built": 1, "skipped": 2, "failed": 1}
}
```

状态机：

```text
pending → built → verified → smoked        （成功链）
pending → skipped(reason)                  （预期内，不失败）
pending → failed(phase, detail)            （预期外）
```

退出码约定：

| 退出码 | 含义 |
|---|---|
| 0 | 所有未跳过的格均 `verified`（`smoke=required` 的格必须 `smoked`） |
| 1 | 至少一格 `failed`，且未启用 `--keep-going` |
| 2 | 至少一格 `failed`，但启用了 `--keep-going`（CI 仍应判失败） |
| 3 | 解析/声明错误（cell 语法、非法组合） |

跳过原因的封闭枚举（禁止自由文本）：

- `unsupported-runtime`：该平台没有该运行时形态；
- `unsupported-python`：该平台没有该 Python 版本的资产；
- `host-mismatch`：当前不是该格的原生构建机；
- `no-runner`：CI 没有对应 runner；
- `user-skip`：声明里的 `skip` 命中；
- `no-emulator`：产物无法在本机执行（仅影响 smoke）。

`allow_failure` 的语义严格限定为「**失败不阻断整体，但必须在报告里计为 failed**」，
不允许把失败改写成 skipped。

### 5.7 本地复现远端某一格

目标：一条命令，cell id 与 CI 完全一致。

```bash
fsp matrix list                       # 看有哪些格、哪些被跳过、原因
fsp b --cell linux-x86_64-py3.11.15-standalone
fsp verify --cell linux-x86_64-py3.11.15-standalone
```

当宿主不匹配时，不应静默降级，而应打印**可复制的复现方式**：

```text
该格需要 Linux x86_64 构建机（当前: windows x86_64）。
可选：
  1) WSL2: 在 Ubuntu 中执行 fsp b --cell linux-x86_64-py3.11.15-standalone
  2) Docker: docker run --rm -v "%CD%:/w" -w /w python:3.11 bash -c \
       "pip install fspack && fsp b . --cell linux-x86_64-py3.11.15-standalone"
  3) 仅预览: fsp b --cell ... --dry-run
```

是否由 fspack 自己拉起容器，建议**放在 Phase 3 且默认关闭**：
Phase 0–2 只要求「打印正确的复现指令」，不接管容器生命周期。
理由：容器编排需要处理挂载、缓存、权限、Windows 路径，属于 cibuildwheel 已经解决而 fspack
不必重复解决的问题；真正需要的是**声明一致**与**校验一致**。

### 5.8 与现有代码的衔接点（仅列改动位置，本轮不改）

| 改动点 | 文件 | 性质 |
|---|---|---|
| 新增矩阵声明解析与 cell 展开 | 新模块（如 `src/fspack/matrix.py`） | 新增 |
| `build()` 接受 cell / dist 子目录 | [executor.py:137-152](../../src/fspack/packaging/pipeline/executor.py#L137-L152) | 参数透传（`dist_dir` 已存在） |
| `--arch` / `--cell` / `--out` CLI | [cli_parser.py](../../src/fspack/cli_parser.py) | 新增选项 |
| URL/资产名支持 arm64 | [urls.py:63-97](../../src/fspack/packaging/runtime/urls.py#L63-L97) | 扩展分支 |
| wheel 平台标签按架构收窄 | [platform.py:36-58](../../src/fspack/platform.py#L36-L58) | 修复 G9 |
| `_deb_arch()` 由 cell 推导 | [linux.py:49-59](../../src/fspack/packaging/installer/linux.py#L49-L59) | 修复写死 |
| 产物基名加 arch | [dist_prep.py:125-131](../../src/fspack/packaging/installer/dist_prep.py#L125-L131) | 命名变更（用户可见） |
| manifest/SBOM 名加 cell | [manifest.py:211-225](../../src/fspack/packaging/manifest.py#L211-L225)、[cli_parser.py:345](../../src/fspack/cli_parser.py#L345) | 命名变更（用户可见） |
| 产物格式校验 | 新模块（如 `verify.py`） | 新增 |
| macOS 架构显式化 | [runtime_stage.py:213-218](../../src/fspack/packaging/pipeline/runtime_stage.py#L213-L218) | 由 cell 传入，不再探测宿主 |

**兼容性提醒**：产物命名加 arch 是破坏性变更（用户脚本可能按旧名取包）。
建议在 changelog 与发布说明里写清楚迁移方式，不提供旧名兼容开关；
`manifest/SBOM` 改名同样需要说明。

## 6. 分阶段计划

| 阶段 | 解决什么 | 主要动作 | 风险 | 验收方式 | 标注 |
|---|---|---|---|---|---|
| **Phase 0** 事实校正与护栏 | 让「文档说的」等于「实际能做的」；堵住静默错误 | ① 文档矩阵按第 2 节重写（明确 Linux/macOS 无 3.8/3.9、macOS 未在 CI 构建）；② 新增产物机器类型校验（PE/ELF/Mach-O）；③ 产物名加 arch，manifest/SBOM 加 py+platform；④ macOS 目标在非原生架构上的行为显式化；⑤ 补 macOS 原生真实构建 job（arm64 + x86_64 双格） | 命名变更影响用户脚本；新增 macOS job 可能暴露真实缺陷（这是好事） | macOS 两架构各至少 1 个真实构建；`fsp verify` 对 PE/ELF/Mach-O 三种格式各有正反用例；旧名→新名迁移说明进 changelog | **立刻可做** |
| **Phase 1** 矩阵契约 | 建立 cell 模型与声明，CI 由声明展开 | ① `[tool.fspack.matrix]` 解析与 `fsp matrix list --json`；② `--cell` + `dist/<cell>/`；③ `matrix-report.json` 与退出码；④ `plan` job 接入 CI（先用 3 格：win/linux/macos 原生各一） | 契约一旦发布就是用户可见 API，后续改名成本高 | 同一 cell 在本地与 CI 产出同名同校验结果；`skip`/`allow_failure`/`host-mismatch` 三态各有测试 | **需先定契约** |
| **Phase 2** 多架构原生 | 把 arm64 从空白变可用 | ① `--arch` 与 URL/资产名 arm64 分支（Linux/Windows/macOS）；② wheel tag 按架构收窄；③ `.deb` arch 由 cell 推导；④ Linux arm64 与 macOS 双架构原生 runner；⑤ arm64 冒烟（原生或 Rosetta） | 上游资产命名/可用性变化；arm runner 成本；macOS x86_64 runner 逐步退场 | 每个新增架构至少有「构建 + 格式校验 + 冒烟」三项；`test_runtime_hash` 扩展到新架构 | **需先定契约**（依赖 Phase 1） |
| **Phase 3** 可选增强 | 本地/容器的便捷性 | ① `--via docker` 由 fspack 拉起容器；② 构建与测试分离（干净环境安装冒烟）；③ 工具链版本记录与比对 | 容器路径/挂载/缓存复杂度高；投入产出比低于前两期 | 本地一条命令可完整复现远端格；工具链漂移可被检出 | **不建议做**（至少不在 Phase 2 之前做） |

不建议做的事项：

- Windows/macOS → Linux 目标交叉；
- 任意非 macOS → macOS 目标交叉（osxcross）；
- QEMU 仿真作为正式发布格；
- Windows arm64 支持；
- universal2 合并产物（先各自出包，合并是另一个量级的问题）。

## 7. 成本评估

### 7.1 矩阵规模

以「平台 × 架构 × Python 版本」计（不含自由线程）：

| 平台/架构 | 可用 Python | 格数 |
|---|---|---:|
| Windows x86_64 | 3.8–3.14 | 7 |
| Windows arm64 | —（未支持） | 0 |
| Linux x86_64 | 3.10–3.14 | 5 |
| Linux arm64 | —（未支持） | 0 |
| macOS x86_64 | 3.10–3.14 | 5 |
| macOS arm64 | 3.10–3.14 | 5 |
| **合计（理论上限）** | — | **22** |

若把自由线程（3.13t/3.14t）计入，理论上限升至 **30**（Windows 9 + Linux 7 + macOS 7 × 2）。
若把尚未实现的 Linux/Windows arm64 也按现有资产表补全，则接近 **40+**。

推荐的实际运行规模：

| 通道 | 格数 | 触发方式 |
|---|---:|---|
| PR 门禁 | 1（Linux 原生 dry-run + 单测） | 每次 PR |
| 主干回归 | 3（Windows / Linux / macOS arm64 原生，固定 3.11） | push main |
| 发布 | 3（三平台原生，当前最高版本 3.14.6） | tag |
| 兼容巡检 | +6（3.10 / 3.12 / 3.13 × 三平台） | 每晚或手动 |
| 按需 | 自由线程、macOS x86_64、3.8/3.9 | 手动 workflow_dispatch |

### 7.2 CI 时间量级

依据现有配置的量级（`ci.yml` 单 job 超时 30 分钟、`e2e` 20 分钟、release 打包 20 分钟），
以及真实构建包含「下载运行时 + 下载依赖 + 编译 loader + 打包」：

| 场景 | 单格耗时（量级） | 并行后 wall time | runner 分钟 |
|---|---|---|---|
| 缓存命中 + 小项目 | 1–3 分钟 | — | — |
| 冷缓存 + 中等依赖 | 5–10 分钟 | — | — |
| 3 格发布矩阵 | — | 8–15 分钟 | 25–45 |
| 9 格兼容矩阵 | — | 15–25 分钟 | 90–180 |
| 30 格全矩阵 | — | 30–60 分钟（受并发限制） | 300–500 |

对照现状：`test` 的 21 格是**便宜的**（纯单测，通常 2–5 分钟/格），
而真实打包格是**昂贵的**（下载 + 编译）。因此不能把「单测矩阵的 21 格」直接当成
「打包矩阵也能铺 21 格」——这是成本评估里最容易犯的错误。

### 7.3 长期保留 vs 按需触发

**长期保留（每 PR / 每 push）**：

- Windows x86_64（embed，3.11）——唯一有原生 NSIS 安装包的平台；
- Linux x86_64（standalone，3.11）——`.deb` 主战场；
- macOS arm64（standalone，3.11）——补上目前零真实构建的缺口。

**长期保留（发布时）**：上述三格 + 当前最新 Python（当前 3.14.6）。

**按需触发**：

- Python 3.8/3.9（Windows embed only；EOL，仅回归用）；
- Python 3.10 / 3.12 / 3.13（兼容巡检，夜间）；
- 自由线程 3.13t / 3.14t（实验特性，明确 `allow_failure`）；
- macOS x86_64（Intel runner 逐步退场，按需）；
- Linux arm64 / Windows arm64（前者 Phase 2 后转长期，后者暂不做）。

## 8. 风险与未决问题

1. **命名破坏性变更是最大的用户可见风险。** 产物名加 arch 会影响下游脚本、Release 附件命名、
   安装文档。需要在 Phase 0/1 就给出迁移说明，而不是留到发布时才发现。
2. **macOS 真实构建一旦进 CI，很可能立即暴露缺陷。** 当前 macOS 相关的测试全部是 mock
   （`test_installer.py:945-1006`），`pkgbuild`/`hdiutil`/`codesign` 没有一次真实执行。
   这是好事，但要预留修复窗口，不要在发布前一周才加。
3. **`runtime = "auto"` 的隐含耦合。** 「Windows 标准版用 embed、自由线程用 standalone」这条规则
   目前藏在 `runtime_stage.py:113-115` 的分支里。矩阵声明如果要表达 runtime，必须把这条规则
   显式化，否则用户写 `runtime = "embed"` + 自由线程会得到不可解释的结果。
4. **版本解析的静默降级（G8）需要在 Phase 0 一并处理。** 否则矩阵报告里会出现
   「声明了 3.8，实际构建 3.10」这种难以排查的错位。
5. **`wheel_platform_tags` 的双标签（G9）在 arm64 上必须收窄。** 否则 arm64 格可能装进 x86_64 wheel，
   构建成功但产物不可运行——这类问题没有格式校验根本发现不了。
6. **未决**：产物命名是否要引入 abi tag（如 `cp311`）？本文建议**不引入**，
   因为 fspack 的产物是「应用 + 内嵌运行时」，不是 wheel，abi 概念对终端用户无意义，
   只需 `py<full-version>` + arch 即可唯一标识。

## 附录 A 证据索引

| 主题 | 位置 |
|---|---|
| 平台枚举与架构常量 | [platform.py:16-25](../../src/fspack/platform.py#L16-L25) |
| wheel 平台标签 | [platform.py:36-58](../../src/fspack/platform.py#L36-L58) |
| Windows embed 版本表 | [versions.py:38-60](../../src/fspack/config/versions.py#L38-L60) |
| Linux/macOS standalone 版本表 | [versions.py:62-76](../../src/fspack/config/versions.py#L62-L76) |
| 版本解析优先级与静默回退 | [versions.py:218-272](../../src/fspack/config/versions.py#L218-L272) |
| embed zip 命名（写死 amd64） | [urls.py:44-55](../../src/fspack/packaging/runtime/urls.py#L44-L55) |
| standalone tarball 命名（写死 x86_64） | [urls.py:63-97](../../src/fspack/packaging/runtime/urls.py#L63-L97) |
| 交叉构建守卫 | [executor.py:90-117](../../src/fspack/packaging/pipeline/executor.py#L90-L117) |
| 运行时形态分流 | [runtime_stage.py:110-115](../../src/fspack/packaging/pipeline/runtime_stage.py#L110-L115) |
| macOS 架构探测 | [runtime_stage.py:213-218](../../src/fspack/packaging/pipeline/runtime_stage.py#L213-L218) |
| Nuitka / pyc 仅同平台 | [compile_stage.py:110](../../src/fspack/packaging/pipeline/compile_stage.py#L110)、[compile_stage.py:156](../../src/fspack/packaging/pipeline/compile_stage.py#L156) |
| stdlib zip 仅 Linux/macOS | [stdlib_zip.py:134-147](../../src/fspack/packaging/runtime/stdlib_zip.py#L134-L147) |
| 安装格式与平台默认 | [facade.py:47-76](../../src/fspack/packaging/installer/facade.py#L47-L76) |
| 产物基名 | [dist_prep.py:120-131](../../src/fspack/packaging/installer/dist_prep.py#L120-L131) |
| `.deb` 架构写死 | [linux.py:49-59](../../src/fspack/packaging/installer/linux.py#L49-L59) |
| manifest 命名 | [manifest.py:211-225](../../src/fspack/packaging/manifest.py#L211-L225) |
| CI 单测矩阵 | [ci.yml:53-60](../../.github/workflows/ci.yml#L53-L60) |
| CI e2e 仅 ubuntu | [ci.yml:95-96](../../.github/workflows/ci.yml#L95-L96) |
| 发布原生打包（无 macOS） | [release.yml:58-59](../../.github/workflows/release.yml#L58-L59)、[release.yml:136-137](../../.github/workflows/release.yml#L136-L137) |
| 交叉构建 e2e | [test_e2e_slow.py:199-237](../../tests/test_e2e_slow.py#L199-L237) |
| 交叉守卫测试 | [test_build_dry_run.py:469-503](../../tests/test_build_dry_run.py#L469-L503) |
| 哈希清单完整性测试 | [test_runtime_hash.py:44-59](../../tests/test_runtime_hash.py#L44-L59) |
| 文档中的平台承诺 | [README.md:177-191](../../README.md#L177-L191)、[architecture.rst:19-22](../../docs/architecture.rst#L19-L22) |
| 签名能力矩阵 | [distribution.md:27-40](../../docs/distribution.md#L27-L40) |

## 附录 B 不建议做的事

| 事项 | 原因 |
|---|---|
| Linux 目标的交叉构建 | ELF 工具链 + glibc sysroot + auditwheel 语义 + `.deb` 工具链，成本远高于加一台 runner |
| macOS 目标的交叉构建（osxcross 等） | Apple SDK 授权、Mach-O 链接、`codesign`/`notarytool` 都只能在 macOS；合规与工程成本双高 |
| QEMU 仿真作为正式发布格 | 耗时不可预测、无法验证真实运行；只适合作为「按需侦察」 |
| Windows arm64 支持 | 需求弱、资产与工具链最不成熟；投入产出比最低 |
| universal2 合并 | 需要 lipo 合并两套 runtime 与依赖，属于独立课题；先保证两架构各自可产出 |
| 在 fspack 内实现完整容器编排（Phase 3 之前） | 与 cibuildwheel 重合，且会引入挂载/权限/缓存的大量平台细节；先解决声明与校验的一致性 |
| 引入 abi tag 到产物名 | 应用产物不是 wheel，abi 对终端用户无意义；`py<full-version>` + arch 已足够唯一 |
