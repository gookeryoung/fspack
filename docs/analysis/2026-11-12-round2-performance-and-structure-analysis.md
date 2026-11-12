# fspack 性能与结构分析（第二轮）

- 日期：2026-11-12
- 轮次：第二轮（M1–M5 落地后重新摸底；不复用第一轮结论与数字，全部重新实测）
- 代码基线：`ae0ed69`（M1–M5 + 文档/ADR/布局守卫修正后）
- 采集脚本与原始数据：工作区外 `tmp_perf/`（`bench_all.py`、`bench_ast.py`、`bench_cache_ab.py`、`analyze_struct.py`、`bench_out/bench_after_m5.json`）
- 本文只读分析，不修改代码、不提交入库

## 0. 口径与噪声

- 机器：Windows，24 逻辑核，构建机与目标机同机（win-x64，Python 3.14.6 embed + Win7 重编译版 runtime）。
- 每场景一条 profile（`fsp b -P -PO`），墙钟由外部计时；`empty` 场景用独立空缓存目录并强制重新下载。
- **噪声很大**：同 HEAD 连续两次可差 2 倍（Defender 实时扫描与首读放大）。本报告所有"区间"来自重复观测，优化取舍只认**同批次配对**的差值。
  - 例：`Win7 兼容扫描` 同一 dist 在不同次构建里 72.6 / 90.5 / 184.9 / 189.6 / 517.9 ms 都出现过；
  - 无缓存直调 `scan_dist_win7` 单测为 49–52 ms（缓存命中）与 185 ms（清缓存）。
- 结论层面只采用两类证据：① 阶段内同一函数的有/无缓存配对；② 同一工程串行 vs 并行的配对。

## 1. 八场景实测（当前值）

| 场景 | 墙钟 | recorder | CPU | 主要成本（按阶段） |
| --- | --- | --- | --- | --- |
| hello（清 dist，缓存热） | 1.011 s | 0.820 s | 0.453 s | 精简标准库 193 / 预编译字节码 196 / Win7 扫描 185 / 解压运行时 68 / loader 校验 39 |
| hello_hot | 0.382 s | 0.193 s | 0.141 s | Win7 扫描 73 / 解析项目 16 / manifest 16 / 分析依赖 15 |
| rich（1 个第三方包，4 wheel） | 2.029 s | 1.804 s | 1.328 s | 预编译字节码 545 / Win7 扫描 196 / 精简标准库 193 / 解压 wheel 164 / 下载依赖 152 / SBOM 119 / manifest 94 |
| rich_hot | 0.663 s | 0.471 s | 0.438 s | Win7 扫描 91 / SBOM 59 / manifest 47 / 解析项目 17 |
| medium（304 文件，4 入口） | 2.390 s | 2.201 s | 1.047 s | 预编译字节码 528 / Win7 扫描 518 / 精简标准库 180 / 分析依赖 173 / Win7 组件替换 135 / loader 校验 130 / 复制源码 129 / manifest 116 / 图标 80 |
| medium_hot | 0.643 s | 0.454 s | 0.328 s | Win7 扫描 190 / manifest 64 / 复制源码 53 / 分析依赖 21 |
| empty（空缓存，含下载） | 6.618 s | 6.363 s | 1.797 s | 下载依赖 2476 / 下载运行时 1420 / 生成 C loader 642 / 预编译字节码 514 / 解压 wheel 259 / Win7 扫描 223 / 精简标准库 194 / SBOM 158 / manifest 104 |
| installer（`fsp p`，不重建） | 2.166 s | — | — | `makensis` 子进程 1.65 s（cProfile），Python 侧约 30 ms |
| startup | `-V` 58.9 ms | `--help` 81.2 ms | — | 中位数（20 次） |

补充：以下三段不在上表但直接决定"还有多少空间"：

- **首次构建**：`empty` 6.62 s 中 3.90 s 是网络下载（运行时 1.42 s + 4 个 wheel 2.48 s），Python 侧 CPU 1.80 s。
- **AST**：`medium` 冷分析 172 ms（304 文件 / 0.46 MiB）；热构建命中 21 ms。大工程见第 2.5 节。
- **前端**：新建 `perfweb`（`web-static-dirs = ["frontend/deploy"]`，无依赖）测得 `构建前端` 732 ms，其中 `pnpm install`（无依赖也要跑）约 600 ms，`node build.js` 约 130 ms。

## 2. 剩余热点（缓存未覆盖的部分）

### 2.1 每次干净构建都付：embed 标准库 zip 精简 180–194 ms，只换回约 100 KiB

- 阶段每次从 embed zip 重新解压 runtime 后**全量重写** `python3XX.zip`：实测 4 MB zip 重写 212 ms（探针 `probe_trim.py`）。
- 收益：剥离 9–10 条目后 `python311.zip` 从约 4.28 MB 降到 4.18 MB（约 100 KiB）。
- 空间：把"精简后的 zip"按 `(embed 包哈希, 档位)` 缓存到缓存根目录（与 `win7-dll/` 同思路），重建时直接拷贝文件（< 10 ms），可省 **约 190 ms/次**，体积不变。
- 风险：低。缓存键含 embed zip 内容哈希 + 精简档位（保守/激进）+ 剥离规则版本即可；`fsp cache clean` 已有孤儿清理。

### 2.2 Win7 阶段：`patch_dist_win7` 每次都全量重写（61 ms），扫描缓存命中态仍有 49–190 ms

拆开测（`bench_cache_ab.py`，perfhello dist 1535 文件 / 34 个 PE）：

| 子步骤 | 耗时 | 说明 |
| --- | --- | --- |
| `patch_dist_win7` | 61 ms | 每构建必跑，无缓存；对每个 PE 读导入表、按需改写 |
| `scan_dist_win7`（缓存命中） | 49–52 ms | 0 次 PE 解析，成本是遍历 1535 个文件 + 读 JSON |
| `scan_dist_win7`（清缓存） | 185 ms | 34 个 PE 全解析 ≈ 4 ms/文件 |
| `inject_win7_shims` | < 1 ms | 已存在则跳过 |
| `write_win7_report` | < 1 ms | — |

- 空间 A：给 patch 加按文件缓存（键 `size + mtime_ns + 改写规则版本`，写入 `dist/build`），预计省 **约 60 ms/次**；同时 patch 与 scan 共用一次 PE 枚举，省约 20–30 ms。
- 空间 B：热构建里该阶段仍是第一或第二大项（73–190 ms），其中被 patch 改写的文件会让扫描缓存条目失效并重解析——把"patch 结果"作为扫描缓存的输入指纹（而不是文件 mtime）可消除这部分抖动。
- 风险：中。缓存命中的前提是文件没被外部改动；`(size, mtime_ns)` 在 Windows 上足够，但需要把改写规则版本纳入键，否则规则更新后会用旧结论。

### 2.3 SBOM + 产物清单：两次独立遍历（合计 100–220 ms，热构建 50–110 ms）

- SBOM：rich 119 ms / 冷、59 ms 热；medium 8 ms；empty 158 ms。它读每个 `*.dist-info/METADATA`，**没有任何缓存**。
- manifest：rich 94 ms / 冷、47 ms 热；empty 104 ms。哈希已按 `(size, mtime_ns)` 备忘录复用（perfhello 实测：冷 226 ms → 备忘录 97 ms；纯 sha256 全量 125 ms），剩下的 97 ms 是遍历+stat+JSON 读写。
- 空间：两者合并为一次 dist 遍历（共享文件列表与 stat），并把 SBOM 结果按 site-packages 指纹缓存，预计 **省 80–150 ms**（medium 冷构建、rich 冷构建收益最大）。
- 风险：低（纯读取与聚合，不改变产物语义）。

### 2.4 预编译字节码：196 ms（hello）/ 528–545 ms（rich、medium）

- 已用 `compileall -j` 并行；热构建 stamp 命中后 0.6–4 ms。
- 空间：剩下的是 CPython 编译本身，属于"要么付、要么不预编译"。若要再压，只能缩小编译范围（例如只编译入口可达模块，需要 import 图），收益与风险都不划算。
- 结论：**不建议**继续优化这一段。

### 2.5 AST / 前端

- AST：M5 已把 848 文件 / 6.2 MiB 的代码密集工程从串行 1.19 s 降到并行 0.78 s（省 0.41 s）；1268 文件 / 9.3 MiB 从 1.70 s 降到 0.98 s（省 0.72 s）；注释密集工程（13.2 MiB，串行 0.49 s）不再被误判为并行（否则多付 0.21 s）。
- AST 剩余空间：整阶段指纹缓存是"全有或全无"，改一个文件要重扫全树（848 文件≈0.78 s）。按文件缓存 import 结果（`size + mtime_ns + py_version + 规则版本`）可让单文件改动的分析降到毫秒级，预计收益 **0.5–0.8 s / 次局部改动**。风险中等（缓存键漏项会产生陈旧 import 集合），属"需立项"。
- 前端：`pnpm install` 在无依赖工程上仍要跑约 600 ms。空间：`package.json` 无 dependencies 且无 lockfile、`node_modules` 不存在时跳过 install（省 **约 600 ms**）；真实前端（有依赖）省不了，构建本身是秒级到分钟级，属工程侧成本。
- 解压：`解压运行时` 64–72 ms（embed zip）、`解压 wheel(精简)` 164–259 ms。都是 zip 解压 + 精简判定，空间主要是"只解压需要的条目"（Qt 场景收益大），但会引入筛选正确性风险，**需立项**。

### 2.6 安装器与运行时下载

- 安装器 2.17 s 中 1.65 s 在 `makensis`（LZMA 压缩）外部进程，Python 侧约 30 ms。空间只能来自"压缩级别 / 安装器格式"（体积换时间），属产品取舍，**需立项**。
- 运行时下载 1.42 s（约 11 MB，镜像）与依赖下载 2.48 s（4 wheel）都是网络成本。空间来自并行分段下载或多镜像竞速，收益强依赖网络环境，单机数据不足以下结论。

## 3. 本轮新增四类缓存：命中判断的风险与增长

共同实现：`packaging/action_cache.py`，标记写到 `<dist>/build/action-<name>.json`（`build/` 不进安装包、不参与 dist 扫描、`fsp c` 一并清除）。版本号 `ACTION_CACHE_VERSION` 是唯一的手动失效杠杆。

| 缓存 | 键 | 命中条件 | 陈旧风险 | 结论 |
| --- | --- | --- | --- | --- |
| 图标转换（`icon.py`） | `sha1(源文件字节) + 文件名 + tag` | 标记存在 + 目标 `.ico` 存在且非空 | Pillow 版本或 ICO 尺寸/压缩参数变化**不在键内**；换 Pillow 后图标不会重转（视觉差异，功能无碍）。**建议键加 Pillow 版本** | 风险低；命中收益大（medium 冷 80 ms → 0.4 ms） |
| embed stdlib 精简（`runtime/trim.py`） | 走 action_cache | 标记存在 | 精简规则改动需 bump 版本；目前只有"条目数"写进 detail，没有指纹 | 风险低 |
| Win7 dist 扫描（`win7/scan.py`） | 每文件 `(size, mtime_ns)`；JSON 在 `dist/build/win7scan.json` | 逐文件命中，未命中才解析 PE | ① 扫描规则/API 清单变化不体现于键（需 bump `_SCAN_CACHE_VERSION`）；② 同尺寸且保留 mtime 的替换（`copy2` 从缓存取文件）会漏报；③ **patch 先改写文件 → mtime 变 → 相关条目必然失效并重解析**，这是热构建仍有 73–190 ms 的主因 | 风险中低；建议把 patch 结果纳入键 |
| loader 校验（`win7/scan.py`） | 每 exe `(size, mtime_ns)` + shim 签名 | 只缓存"通过"，未通过不缓存 | 规则更新需 bump 版本；键用绝对路径 → 移动项目目录会整体失效（只是少命中，不会错判） | 风险低；实测命中 0.3 ms |
| manifest 哈希备忘录（`manifest.py`） | `(size, mtime_ns)` → sha256 | 逐文件命中 | 同尺寸同 mtime 的内容替换会给出旧哈希（实践中难触发）；备忘录随 dist 删除 | 风险低；实测省 125 ms 哈希 |

**缓存目录膨胀**（`F:\Dev\fspack-cache` 实测 3.25 GB，无老化策略）：

| 目录 | 体积 | 增长方式 |
| --- | --- | --- |
| `wheels/` | 1353 MiB（420 个文件） | 按包名+版本累积，永不淘汰 |
| `nuitka-winlibs-mingw/` | 997 MiB | 工具链，固定 |
| `nuitka-work/` | 240 MiB | 编译缓存，自愈时会整目录清空 |
| `python/` | 147 MiB | 按 standalone 版本 |
| `standalone-windows/` | 135 MiB | 按版本 |
| `standalone/` | 93 MiB | 按版本 |
| `loaders/` | 87 MiB（**589 个文件**） | **按 loader 源码哈希逐条累积**：每个不同项目路径/入口都会编出不同 exe（入口路径被烧进 C 源码），开发机上很快上百条 |
| `nuitka/` | 80 MiB | 按 py 版本 |
| `embed/` | 68 MiB | 按版本 |
| `win7-dll/` | 33 MiB | 按版本 |

- `fsp cache status/clean` 只处理**损坏/孤儿**，没有按年龄或体积的淘汰；`loaders/` 与 `wheels/` 是主要增长点。
- 建议补：`loaders/` 上限（如保留最近 100 个，按 mtime LRU）；`wheels/` 可选 `--gc`（按最后使用时间）；两者都只删可重建文件，风险低。
- 另：`Win7 兼容扫描` 阶段 `hit=0` 恒为 0（它不整阶段跳过），容易被误读为"缓存没生效"。建议在 detail 里输出"命中 n/m 文件"这一纯观测项。

## 4. 结构盘点（按当前代码）

总规模：**148 个模块 / 28 954 行**（非空非注释行，排除 `assets/` 模板载荷）。

| 包 | LOC | 占比 | 文件 |
| --- | --- | --- | --- |
| `fspack.packaging` | 17 717 | **61.2%** | 89 |
| `fspack.doctor` | 3 370 | 11.6% | 15 |
| `fspack.config` | 1 654 | 5.7% | 7 |
| `fspack.slim` | 1 440 | 5.0% | 9 |
| `fspack.analyzer` | 860 | 3.0% | 4 |
| `cli_parser` / `cli` / `cli_init` | 687 / 592 / 292 | 5.4% | 3 |
| 其余（runner*、templates、progress、wizard、console、叶子模块…） | 1 603 | 5.5% | 19 |

`packaging` 内部：顶层平铺 15 个文件 3 505 行（19.8%）、`nuitka` 2 919（16.5%）、`pipeline` 2 399（13.5%）、`wheels` 2 314（13.1%）、`win7` 1 652（9.3%）、`installer` 1 576（8.9%）、`runtime` 1 535（8.7%）、`loader` 852（4.8%）、`dep_analyzer` 513（2.9%）、`pyc` 452（2.6%）。

### 4.1 模块环：7 个 SCC，其中只有 2 个是"真实逻辑环"，其余是门面环

| SCC | 规模 | 性质 | 说明 |
| --- | --- | --- | --- |
| builder / packaging / nuitka / installer | 19 模块 / 39 内部边 | **门面环**（覆盖面大） | 闭合边只有 `installer.dist_prep → fspack.builder`、`nuitka.standalone → fspack.builder`、`fspack.builder → fspack.packaging`、`packaging → nuitka`（门面）；其余是包内向下依赖与 `nuitka.indexes/progress → nuitka` 的门面回边 |
| wheels | 6 模块 / 14 边 | **含真实环** | `downloader → resolver`（顶层 import）与 `resolver → downloader`（函数内 `_run_pip`）；`parallel → resolver`、`uv_bridge → resolver` 是 `TYPE_CHECKING` 边 |
| config | 4 模块 / 8 边 | **真实环** | `app_type → models`、`entries → app_type/models`、`models → app_type`（函数内）、`models → parsing`（函数内）、`parsing → app_type/entries/models` |
| dep_analyzer | 4 模块 / 6 边 | 门面环 | `common/graph/pe/elf/macho → dep_analyzer`（facade） |
| pyc / runtime.trim | 3 模块 / 4 边 | 门面环 + 懒加载 | `pyc.__init__ → runtime.trim`、`pyc.compile → pyc`（`_P()` 延迟 shim）、`runtime.trim → pyc` |
| runtime | 2 模块 | 门面环 | `download → runtime`（facade） |
| installer | 2 模块 | 门面环 | `facade → installer`（facade） |

包级环只有 `fspack.builder ↔ fspack.packaging`（唯一，符合"包门面例外"）。

**结论**：任务假设"M4 之后只剩包门面环"**不成立**——`config` 内部 4 模块与 `wheels.resolver ↔ downloader` 仍是真实（函数内 import 规避的）环，且 `builder` 是"模块级门面"，把 19 个模块卷进一个 SCC。

### 4.2 叶子模块使用情况：正确且收敛

| 叶子 | fan-in | 使用者 | 结论 |
| --- | --- | --- | --- |
| `astutil`（22 行） | 2 | `analyzer.ast_scan`、`config.app_type` | 正是设计意图：config 不再依赖 analyzer |
| `archive`（24 行） | 2 | `packaging.runtime.extract`、`slim.unpack` | slim 不再依赖 packaging |
| `metrics`（76 行） | 24 | packaging 各阶段、`progress`、`slim.unpack` | `StageRecord` 不再从 `progress` 取，rich 不再被纯逻辑模块拖入 |
| `jsoncache`（68 行） | 3 | `pipeline.dist_helpers`、`pipeline.frontend_stage`、`wheels.cache` | 只依赖标准库 |

没有"拆了但没人用"或"绕开叶子继续直连"的情况。

### 4.3 ADR 与布局守卫：缺口已补，但覆盖面仍小于实际规则

已补：

- `docs/adr/0001-layering.md`（依赖方向、叶子清单、facade 例外、未采纳方案），已进 `docs/index.rst` 目录树；
- `tests/test_layout.py`：AST 扫描（含函数体内延迟导入与包内相对导入），守卫 `config ↛ analyzer`、`analyzer/slim ↛ packaging`、`packaging → console/progress` 白名单（双向校验防僵尸豁免）、`metrics/astutil/archive/jsoncache` 叶子纯度；6 个用例通过。

仍缺（可作为后续增量，非阻塞）：

1. 守卫没有覆盖 `config` 内部环与 `wheels.resolver ↔ downloader`（本文第 4.1 节的两个真实环）；
2. 守卫没有禁止 `packaging ↛ doctor/cli/wizard/templates` 的上行依赖（当前确实没有，但没被机械约束）；
3. 白名单 19 个文件是"人工登记"的展示入口，新增阶段文件时需要同步维护（报错信息已给出指引）。

### 4.4 `packaging` 61.2% 是否还要继续拆

- 第一轮报告的 55% 是**旧口径**；按当前代码（src 内非空非注释行）为 **61.2% / 89 个文件**，其中 `pipeline+runtime+wheels+nuitka+installer+win7+loader+pyc+dep_analyzer` 已按职责分包，真正"未归类"的是顶层 15 个文件 3 505 行。
- 单文件最大：`profile_log` 569、`runtime/trim` 548、`nuitka/compile` 531、`pipeline/executor` 479、`nuitka/env` 433、`entry` 410、`pipeline/deps_stage` 399。
- 判断：**不需要以"降占比"为目标继续拆**。占比高是编排层的正常形态，继续拆只会增加跨包跳转与 facade 面积。真正值得做的是**两个真实环**与**单文件 > 550 行**的三处（`profile_log`、`runtime/trim`、`nuitka/compile`），按"变更频率"而非"行数"决定。

## 5. 分档建议

### A. 低风险、可立即做（每项都有实测收益，改动面 ≤ 2 个文件）

| # | 改动 | 预计收益 | 改动面 / 风险 |
| --- | --- | --- | --- |
| A1 | 缓存"精简后的 embed stdlib zip"（键：embed 内容哈希 + 档位 + 规则版本；存 `cache/embed-trimmed/`） | 每次干净构建 **≈190 ms** | `packaging/runtime/trim.py` + 缓存路径约定；风险低（只换"重写 zip"为"拷贝文件"） |
| A2 | `SBOM` 与 `manifest` 合并一次 dist 遍历 + SBOM 按 site-packages 指纹缓存 | **80–150 ms**（冷构建） | `pipeline` 两个 stage + `sbom.py`；风险低（只读聚合） |
| A3 | `package.json` 无 dependencies、无 lockfile 且无 `node_modules` 时跳过 `npm install` | **≈600 ms**（无依赖前端工程） | `pipeline/frontend_stage.py`；风险低（跳过的是空操作） |
| A4 | Win7 `patch_dist_win7` 加按文件 action 缓存，并与扫描共用一次 PE 枚举 | **≈60–90 ms/次** | `win7/patch.py` + `win7/scan.py`；风险中低（键必须含改写规则版本） |
| A5 | `Win7 兼容扫描` detail 输出"命中 n/m 文件" | 纯可观测性（当前 `hit=0` 误导优化方向） | 1 个 stage；风险极低 |
| A6 | 图标缓存键加 Pillow 版本 | 防换 Pillow 后陈旧 | `icon.py` + 键构造；风险低 |
| A7 | `loaders/` 按 mtime 保留最近 N 个（如 100）+ `fsp cache clean` 支持按年龄清理 | 抑制 589 文件 / 87 MiB 继续膨胀 | `doctor/cache*.py` + loader 缓存写入点；风险低（删的都是可重建文件） |
| A8 | 布局守卫补 `jsoncache` 叶子（本轮已做）与 `packaging ↛ doctor/cli/wizard` 上行禁止（未做） | 防回退 | `tests/test_layout.py`；风险极低 |

### B. 需立项（收益明确，但涉及设计选择或跨模块改动）

| # | 项目 | 预期收益 | 改动面 / 风险 |
| --- | --- | --- | --- |
| B1 | 按文件缓存 AST import 结果（键含 py_version 与规则版本），替代"整阶段全有或全无" | 大工程单文件改动 **省 0.5–0.8 s** | `analyzer/*` + `action_cache`；风险中（陈旧 import 集合会静默影响打包内容，需要完善键与测试） |
| B2 | 打平 `config` 内部 4 模块环（抽出 `config/types.py` 承载数据类，去掉 `models` 的函数内反向 import） | 可维护性；import 链更短 | 4 文件约 1 600 行；风险中（配置解析是主路径，需全量回归） |
| B3 | 打平 `wheels.resolver ↔ downloader`（把 `_run_pip` 下沉到 `wheels/process.py`） | 消除真实环 | 2 文件 + 测试 monkeypatch 面；风险中（下载/回退路径是核心） |
| B4 | 解压 wheel/运行时改为"只解压需要的条目"（按 AST 使用信息预筛） | `解压 wheel` 164–259 ms 可压缩一半以上（Qt 场景更明显） | `slim/unpack.py` + 调用方；风险中（漏解会导致运行期 ImportError） |
| B5 | 安装器耗时：压缩级别/格式取舍（或改用 zip 安装器） | 1.65 s → 0.5–1 s 量级 | `installer/*`；需产品决策（体积 vs 时间），风险中 |
| B6 | 运行时下载并行分段/多镜像竞速 | 网络侧 1.4 s 可能减半 | `runtime/download.py` + `net.py`；收益强依赖网络环境 |
| B7 | 拆 `profile_log`（569）/ `runtime/trim`（548）/ `nuitka/compile`（531） | 可维护性 | 各自 1 文件 → 2–3 文件；风险低但纯结构收益，建议随下次功能改动顺带做 |

### C. 不建议做

| # | 想法 | 理由 |
| --- | --- | --- |
| C1 | 继续以"降低 packaging 占比"为目标拆包 | 61.2% 是编排层正常形态；再拆增加跨包跳转与 facade 面积，不解决任何实际问题 |
| C2 | 优化预编译字节码（已 `-j` 并行） | 剩余是 CPython 编译本身；只能靠"少编译"（需要 import 图），收益/风险都不划算 |
| C3 | 用"采样计时"替代 AST 节点密度决策并行 | 首次计时受 AV/冷缓存影响，噪声大于收益；M5 的节点密度已能分开两类负载 |
| C4 | 给 Win7 扫描做"整树哈希一次命中" | dist 每次构建 mtime 都变（patch/复制），整树哈希必然失效，收益接近 0 |
| C5 | 缓存 SBOM 文本 / 锁文件内容到缓存根目录 | 失效面（dist-info、wheel 版本）大，收益仅首建 158 ms；A2 的"按 site-packages 指纹缓存"已足够 |
| C6 | 把 `builder` 的 re-export 全部删除以消掉 19 模块 SCC | `builder` 是公开兼容面（CLI/doctor/测试都用），删除会破坏 API；该环是门面环，代价可接受 |

## 6. 复现

```powershell
# 八场景（empty 会联网重新下载）
python tmp_perf\bench_all.py hello hello_hot rich rich_hot medium medium_hot empty installer startup --label after_m5
# AST 并行门槛（工程样本：perfbig/perfbig2 注释密集，perfbig3/4/5 代码密集）
python tmp_perf\bench_ast.py tmp_perf\projects\perfbig5 --repeat 4
# 缓存有/无配对
python tmp_perf\bench_cache_ab.py
# 结构（LOC / SCC / fan-in）
python tmp_perf\analyze_struct.py
```

## 7. 与第一轮报告的关系

- 第一轮的绝对数字（如"分析依赖 2.05 s 是一过性毛刺"）在本轮**未复用**；本轮在 `ae0ed69` 上全部重新采集。
- 第一轮结论中"packaging 占 55%"在本轮重新计量为 **61.2%**（口径：src 内非空非注释行），结论方向不变（不需要为降占比而拆包）。
- 第一轮未覆盖的 M5（并行门槛）与本轮新增的四类缓存风险，已在本轮补齐。
