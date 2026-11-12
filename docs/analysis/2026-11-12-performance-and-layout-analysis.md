# fspack 性能与目录结构实测分析（2026-11-12）

- 分析对象：`F:\Dev\fspack`，提交 `5a8bc18`（工作区干净，本次未产生任何提交）
- 性质：只读分析。除本报告外未改动代码、配置、文档
- 结论先行：
  1. 冷构建的大头是 **C loader 编译（43.5%）**；有依赖时 **下载依赖（48.4%）** 跃居第一；首次无缓存时 **运行时下载（24.7%）** 也很重。
  2. 有依赖的项目，下载依赖段 2.7–3.5 s 里约 **3.2 s 是纯固定开销**（pip 进程 + 无索引预检），只有 1.6 MB 实际流量，可压缩空间最大。
  3. 热构建 342–730 ms，其中 **60%–87% 是可缓存的固定校验**（Win7 扫描、loader 校验、精简判断、图标转换），合计可省 220–410 ms。
  4. `fsp -V/--help` 实测 183 ms，其中 `build_parser()` 占 59 ms；与 `docs/architecture.rst` 记录的 "~61 ms" 不一致。
  5. 目录结构：`packaging` 占 src 的 55.7%；库代码深度 ≤ 2 层包，符合 ADR-0003 类约束；但有 6 个模块环，其中 25 模块的大环由 `packaging.installer` 为测试可 patch 而反向 `import fspack.builder` 造成；无任何布局守卫测试；仓库内不存在 `docs/adr`。

---

## 1. 测量环境与口径

| 项 | 值 |
|---|---|
| 主机 | Windows 11，x86_64，Defender 实时防护开启 |
| Python | 3.14.6（venv），另测 3.14.6t freethreaded |
| fspack | 0.6.12，源码模式（`src/` 直接 import） |
| uv | 0.13.1；pip 26.1.2（miniforge 3.13 解释器）；mingw gcc；NSIS |
| 缓存根 | `FSPACK_CACHE_DIR=F:\Dev\fspack-cache`（embed 3.8–3.14、standalone 3.14.6t、wheels、loaders、nsis 均已有缓存） |
| 阶段耗时 | `fsp -b -P -PO out.json` 内置 stage recorder，单次运行 |
| 启动耗时 | 子进程 20 次取中位数（Python 驱动，排除 PowerShell 计时误差） |
| 依赖原语 | 子进程 5–10 次取中位数 |

被测工程（均为本次在 `%TEMP%` 下临时生成，未入库）：

| 名称 | 规模 | 说明 |
|---|---|---|
| perfhello | 1 个入口、0 依赖 | helloworld 模板 |
| perfrich | 1 个入口、4 wheels（rich 生态） | 覆盖依赖下载/解包 |
| perfmedium | 4 个入口、304 个 .py、200 KB | 覆盖复制/AST/多 loader 并行/PNG 图标 |
| perfrich2 | 同 perfrich，空缓存根 | 覆盖真下载（embed 11.2 MB + wheels 1.7 MB） |

口径说明：

- 阶段耗时是**单次采样**，含磁盘/杀软抖动；下文对可疑值做了复测并标注。
- 冷/热定义：冷 = `dist/` 不存在或关键缓存缺失；热 = 同参数再次构建。
- 未测：前端构建（需 node 工程，未安装/未跑）、Linux/macOS 的目标运行时（缓存无 Linux standalone）、`stdlib zip 化`（仅 Linux/macOS 生效，本机恒为 0 ms）。

---

## 2. 性能

### 2.1 各阶段耗时拆分

**A. 最小工程冷构建（1.668 s，CPU 0.641 s，CPU 占比 0.38）**

| 阶段 | 耗时 | 占比 | 备注 |
|---|---:|---:|---|
| 生成 C loader | 725.2 ms | 43.5% | mingw x86_64-w64-mingw32-gcc，单入口 |
| Win7 兼容扫描 | 246.2 ms | 14.8% | 纯 Python PE 解析，34 个文件 |
| 预编译字节码 | 243.8 ms | 14.6% | 2 个目录 |
| 精简标准库 | 227.1 ms | 13.6% | 剥离 9 条目，只省 104 KB |
| 解压运行时 | 73.8 ms | 4.4% | embed zip |
| 下载运行时 | 7.6 ms | 0.5% | 缓存命中 |
| 解析项目 | 17.6 ms | 1.1% | |
| 生成产物清单 | 14.9 ms | 0.9% | 24.7 MB |
| Win7 loader 校验 | 23.6 ms | 1.4% | 1 个 exe |
| 复制源码 / 分析依赖 / SBOM | 6.5 / 3.0 / 6.5 ms | 0.9% | |
| 解析图标 | 0.2 ms | 0.0% | 默认 app.ico |

**B. 同工程热构建（342 ms，CPU 占比 1.00）**

| 阶段 | 耗时 | 占比 | 可缓存性 |
|---|---:|---:|---|
| Win7 兼容扫描 | 126.7 ms | 37.0% | 可（按文件 size+mtime/hash） |
| 精简标准库（已精简判断） | 58.2 ms | 17.0% | 可（marker） |
| Win7 loader 校验 | 38.6 ms | 11.3% | 可（loader 缓存命中时跳过） |
| 解析项目 | 18.0 ms | 5.3% | 否（读 pyproject） |
| 生成产物清单 | 17.6 ms | 5.1% | 部分（复用已算 hash） |
| SBOM | 7.6 ms | 2.2% | 可（输入不变） |
| 复制源码 | 5.8 ms | 1.7% | 已是增量 |
| 生成 C loader（缓存命中） | 5.2 ms | 1.5% | — |

**C. 带依赖工程（rich，冷 dist / 空缓存 / 热）**

| 阶段 | 冷 dist·缓存命中 5.536 s | 空缓存 7.868 s | 热构建 0.730 s |
|---|---:|---:|---:|
| 下载依赖 | **2678.4 ms（48.4%）** | **3476.0 ms（44.2%）** | 0（跳过） |
| 下载运行时 | 7.5 ms | 1943.7 ms（24.7%，11.2 MB） | 0 |
| 生成 C loader | 692.9 ms（12.5%） | 685.2 ms（8.7%） | 5.8 ms |
| 预编译字节码 | 628.4 ms（11.4%） | 579.0 ms（7.4%） | 0.8 ms |
| Win7 兼容扫描 | 316.9 ms（5.7%） | 258.1 ms（3.3%） | 125.6 ms（17.2%） |
| 解压 wheel（精简） | 255.8 ms（4.6%） | 188.4 ms（2.4%） | 0 |
| 精简标准库 | 206.1 ms（3.7%） | 195.7 ms（2.5%） | 50.7 ms（6.9%） |
| SBOM | 165.5 ms（3.0%） | 121.7 ms（1.5%） | 112.6 ms（15.4%） |
| 生成产物清单 | 134.0 ms（2.4%） | 92.7 ms（1.2%） | 118.7 ms（16.3%） |

**D. 中型工程（304 文件 / 4 入口）**

冷构建 5.107 s：

| 阶段 | 耗时 | 占比 | 备注 |
|---|---:|---:|---|
| 分析依赖 | 2052.4 ms | 40.2% | **异常值**，见下 |
| 生成 C loader | 819.4 ms | 16.0% | 4 入口并行（≈205 ms/入口） |
| Win7 兼容扫描 | 643.8 ms | 12.6% | 39 文件 |
| 预编译字节码 | 554.6 ms | 10.9% | |
| 精简标准库 | 178.6 ms | 3.5% | 省 178 KB |
| 生成产物清单 | 167.8 ms | 3.3% | |
| Win7 组件替换 | 142.1 ms | 2.8% | 冷时拉取重编译版 |
| 解析图标 | 138.8 ms | 2.7% | PNG→ICO |
| 复制源码 | 132.9 ms | 2.6% | 304 文件 |
| 解压运行时 | 74.7 ms | 1.5% | |

热构建 724 ms：

| 阶段 | 耗时 | 占比 |
|---|---:|---:|
| Win7 loader 校验 | 144.0 ms | 19.9% |
| Win7 兼容扫描 | 136.2 ms | 18.8% |
| 生成产物清单 | 115.1 ms | 15.9% |
| 解析图标 | 81.1 ms | 11.2% |
| 复制源码 | 54.4 ms | 7.5% |
| 精简标准库（判断） | 46.8 ms | 6.5% |
| 解析项目 | 19.0 ms | 2.6% |

> **关于 "分析依赖 2.05 s"**：清掉 `.dep_cache.json` 后在同一文件系统上复测，该阶段为 **160.1 ms**（总耗时 1.185 s），进程内直接调用 `DependencyReport.from_src` 为 101.8 ms。首次 2.05 s 是刚生成的 304 个文件被实时防护/首次读取拖慢的**一次性毛刺**，不是常态成本。常态：≈5 µs/文件量级的 AST 解析 + 指纹，304 文件约 160 ms。未触发并行路径（阈值 1000 文件 / 8 MiB）。

**E. freethreaded standalone（3.14.6t，8.020 s）**

| 阶段 | 耗时 | 占比 |
|---|---:|---:|
| 解压运行时（47.8 MB tar.gz） | 2559.1 ms | 31.9% |
| Win7 兼容扫描（85 文件，含 1 违规） | 1676.6 ms | 20.9% |
| 预编译字节码 | 1489.3 ms | 18.6% |
| 生成产物清单（93 MB） | 1036.7 ms | 12.9% |
| 生成 C loader | 670.2 ms | 8.4% |
| 精简 runtime（删 47 项，省 91.5 MB） | 266.6 ms | 3.3% |

**F. 安装器打包（`fsp p`，hello）**：总墙钟 **12.0 s**，几乎全部是 makensis 单线程 LZMA 压缩 24.7 MB dist（外部进程，fspack 侧无调度开销）。

#### 占比最高 / 可压缩

- 占比最高：C loader 编译（冷构 43.5%）＞ 下载依赖（有依赖 44–48%）＞ Win7 扫描（12–21%）＞ 字节码（7–15%）＞ 运行时下载/解压（首次 25–32%）。
- 可压缩：① 下载依赖固定开销（−2.0～2.5 s/次未命中）；② Win7 扫描缓存（−126～137 ms/构建，二进制多的项目 −1.5 s）；③ Win7 loader 校验（−38～150 ms）；④ 精简判断 marker（−46～58 ms）；⑤ 图标转换缓存（−81 ms）。
- **不可压缩或性价比低**：loader 编译受 mingw 进程 + `-O2` 限制（单入口 670–725 ms，已是并行可覆盖的粒度）；NSIS 12 s 是外部工具固有成本；复制/pyc 只在源码变更时发生。

### 2.2 启动与导入成本

| 命令 | 中位数 | 相对 python 基线 |
|---|---:|---:|
| `python -c pass` | 58.7 ms | — |
| `python -c "import fspack.cli"` | 101.1 ms | +42.4 ms |
| `python -c "import fspack.config"` | 145.6 ms | +86.9 ms |
| `python -c "import fspack.builder"` | 265.2 ms | +206.5 ms |
| `from fspack.cli import main; main(['-V'])` | 160.5 ms | +101.8 ms |
| `from fspack.cli import main; main(['--help'])` | 161.7 ms | +103.0 ms |
| `fspack.exe -V` | 182.6 ms | +123.9 ms |
| `fspack.exe --help` | 183.1 ms | +124.4 ms |

进程内相位（`phase_startup.py`，单次）：

```
import fspack.cli :  45.3 ms
build_parser()    :  59.4 ms
parse_args(['-V']):   2.3 ms
```

`-X importtime`（`-V` 全路径，累计 50.6 ms 内）：

| 模块 | cumulative | self |
|---|---:|---:|
| fspack.cli | 50.55 ms | 1.74 ms |
| argparse | 24.62 ms | 2.56 ms |
| re | 20.79 ms | 2.54 ms |
| site（启动期） | 20.14 ms | 7.42 ms |
| _colorize | 18.38 ms | 3.96 ms |
| shutil | 16.09 ms | 1.71 ms |
| dataclasses | 14.42 ms | 1.66 ms |
| pathlib | 12.00 ms | 1.82 ms |
| inspect | 9.40 ms | 2.66 ms |
| bz2 | 7.70 ms | 1.26 ms |

结论：

- 183 ms 的 `--help` 拆分为：解释器 + site ≈ 53 ms，fspack 模块导入 ≈ 45 ms，**构造 argparse 解析器 ≈ 59 ms**，console-script 包装 ≈ 22 ms。
- Python 3.14 下 `build_parser()` 会连带懒加载 `_colorize`/`shutil`/`dataclasses`/`bz2`（≈57 ms 累计），这是 59 ms 的主因；`cli_parser` 自身 self 仅 1.05 ms。
- 构建路径额外背 `fspack.builder` → 整个 `packaging` 树，265 ms 导入成本；相对 1.7–5.5 s 的构建可以接受，但 `-V/--help` 完全不需要它。
- 与 `docs/architecture.rst` 声称的 "`fsp --help` 冷启动 ~61 ms" 不符（差 3 倍），见 §4.6。

### 2.3 依赖下载段的固定开销

原语实测（本机）：

| 原语 | 中位数 | 说明 |
|---|---:|---|
| `python -m pip --version`（pip 探测） | 478 ms | `_find_pip_python()` 每次构建进程重探一次（进程内 `lru_cache`） |
| `uv --version` | 24 ms | uv 进程启动本身很便宜 |
| `uv pip compile`（warm，4 包） | 21 ms | 解析走 uv 本地缓存 |
| `uv pip compile`（首次，含网络） | 258 ms | |
| `uv pip download`（能力探测） | rc=2 | **uv 0.13.1 已移除该子命令**，恒走 pip 并行分支 |
| `pip download --no-index --find-links <cache>` | 1483 ms | 即使全部命中也要跑一次完整解析 |
| 单个 pip 下载进程（无缓存） | ≈1.5 s | 由 pip 启动 0.48 s + 解析 + 传输构成，4 路并行 |

真实构建归因：

- rich 冷 dist、wheels 已缓存：`下载依赖` = **2678 ms**，下载字节 **0**，命中 4。即 ≈2.7 s 全是 "pip 探测 + 无索引预检 + 解析/落盘"。
- rich 空缓存：`下载依赖` = **3476 ms**，实际传输 **1.67 MB**。固定开销 ≈3.2 s，真实传输按 20 MB/s 仅 ≈0.1 s。
- 缓存命中 vs 未命中差 ≈ **0.8 s**；但"命中"仍然要付 ≈2.7 s 的固定成本，说明 **命中路径没有被真正短路**。
- 第二次构建（deps cache 命中）：`下载依赖` = 0 ms，说明 `.deps_cache.json` 命中时确实完全跳过——固定开销只在"解析一次"时付。

口径提醒：本机 pip 属于 miniforge 3.13（`--version` 就 478 ms），若换成 embed/自带 pip 数字会低一些；但"每次构建重探 pip + 用 pip 做无索引预检 + uv 无 download 子命令"这三条与机器无关。

### 2.4 单项判定（收益量级 / 成本 / 结论）

| # | 项目 | 实测收益 | 改动量 | 风险 | 结论 |
|---|---|---|---|---|---|
| P1 | 图标按源文件 hash 缓存 | 80 ms/构建（PNG 项目） | 极小（`ensure_ico` 调用点） | 低 | **值得，立即做** |
| P2 | Win7 扫描按 (size,mtime) 缓存 | 126–137 ms/构建；85 二进制项目 1.6 s | 小（scan 结果侧车文件） | 低-中（失效逻辑） | **值得，立即做** |
| P3 | loader 缓存命中时跳过 PE 校验 | 38–150 ms/构建 | 极小 | 低 | **值得，立即做** |
| P4 | 已精简判断改 marker | 46–58 ms/构建 | 小 | 低-中（runtime 变更失效） | **值得，立即做** |
| P5 | `-V/--help` argv 快速路径 | 59 ms（183→124 ms） | 小（`cli.py` 顶部） | 低（需与 argparse 输出一致） | **值得，立即做** |
| P6 | 依赖链路去掉 pip 无索引预检 / 复用 uv 常驻 | 1.5–2.5 s/次未命中 | 中（downloader/resolver/uv_bridge） | 中（离线/跨平台/校验） | 需立项 |
| P7 | pip 解释器探测结果持久化 | 0.4–0.5 s/次未命中 | 小-中 | 中（环境漂移） | 需立项（并入 P6） |
| P8 | 统一增量 action cache（输入 hash→输出） | 220–410 ms/热构建 | 大（pipeline 编排 + 缓存层） | 中 | 需立项（可覆盖 P1–P4） |
| P9 | 产物清单复用已算 hash | 100–124 ms（多文件/大 dist；小工程仅 ~18 ms） | 中（跨阶段传 hash） | 低-中 | 需立项（收益中等） |
| P10 | 多入口 loader 编译再提速 | <100 ms | 大（共享 loader 预编译分发） | 中-高 | **不建议** |
| P11 | NSIS 12 s 优化 | 换压缩器才有效 | 大（换打包器/格式） | 高 | **不建议** |
| P12 | 拆分 `cli_parser.py` / `cli.py` | ≈0 | 中 | 低 | **不建议**（只增 import 点） |
| P13 | 热构建再削解析项目/复制源码 | 各 <20/55 ms | 中 | 低 | **不建议**（ROI 低） |
| P14 | 为构建路径做导入懒加载 | 265 ms，但一次构建只付一次 | 中 | 中 | **不建议**（除非做常驻/IDE 场景） |
| P15 | Windows embed 的 "精简标准库" 是否保留 | 花 180–230 ms 只省 0.10–0.18 MB | 小 | 低 | **需决策**（按体积优先保留，按速度优先改默认） |

---

## 3. 目录结构

### 3.1 规模

`src/fspack`：**190 个 .py，39,497 行**。

| 层 | 文件 | 行数 | 占比 |
|---|---:|---:|---:|
| packaging（含 pipeline/wheels/installer/nuitka/runtime/loader/pyc/win7/dep_analyzer） | 87 | 21,988 | 55.7% |
| doctor | 15 | 4,237 | 10.7% |
| assets（模板/资源，非库代码） | 48 | 3,390 | 8.6% |
| config | 7 | 2,203 | 5.6% |
| slim | 9 | 1,918 | 4.9% |
| analyzer | 4 | 1,058 | 2.7% |
| cli_parser / cli | 2 | 1,487 | 3.8% |
| runner + runner_profile | 2 | 908 | 2.3% |
| templates / cli_init / wizard | 6 | 1,177 | 3.0% |
| progress / console / format | 3 | 604 | 1.5% |
| 其余单文件（fsutil/jsoncache/platform/_compat/builder/exceptions） | 6 | 456 | 1.2% |

深度分布（相对 `src/fspack`）：`{1: 16, 2: 53, 3: 73, 5: 30, 6: 16, 7: 2}`。深度 5–7 的 48 个文件全部是 `assets/templates|init_templates` 下的**模板工程**，库代码最大深度 3（= `fspack/<包>/<子包>/模块.py`，两层包）。

packaging 内部：

| 子包 | 文件 | 行数 |
|---|---:|---:|
| nuitka | 12 | 3,209 |
| pipeline | 11 | 2,633 |
| wheels | 8 | 2,174 |
| installer | 11 | 1,652 |
| win7 | 7 | 1,649 |
| runtime | 6 | 1,589 |
| loader | 4 | 886 |
| dep_analyzer | 4 | 538 |
| pyc | 3 | 489 |
| 顶层文件（installer/icon/net/sync/…） | 14 | 3,720 |

### 3.2 子包职责是否清晰

清晰的部分：

- `config`（解析 + 模型）、`analyzer`（源码指纹/AST）、`slim`（spec/解包）、`templates`、`wizard`、`doctor`、`runner` 边界基本单一。
- `packaging` 下的 runtime / wheels / loader / win7 / pyc / installer / nuitka / pipeline 切分与构建阶段一一对应，可读性好。
- `builder.py`（66 行）是**纯 re-export 门面**，明确标注为兼容层。

不清晰的部分：

- `packaging` 顶层 14 个文件混杂了 "net / sync / entry / icon / manifest / lockfile / profile_log / dist_helpers" 等横向关注点，没有任何子包归属，已经 3,720 行。
- `progress` 被 `packaging` 引用 36 次、`console` 被引用 16 次——构建业务层直接依赖展示层，阶段可视化与业务逻辑耦合在调用点而非回调。
- `doctor` 4,237 行里 `cache_health.py` 单文件 767 行，与 `doctor/cache_contents.py`、`doctor/envs.py` 的职责有交叠（缓存健康 vs 缓存内容）。

### 3.3 跨层依赖与循环导入

按 AST 静态解析得到 6 个模块级强连通分量（SCC）：

| SCC | 成员数 | 性质 | 处理建议 |
|---|---:|---|---|
| builder + installer/nuitka/pipeline 全家桶 | 25 | **真环**：`packaging/installer/__init__.py` 为让测试 `patch("fspack.packaging.installer.build")` 反向 `from fspack.builder import build`；`installer/dist_prep.py`、`nuitka/standalone.py` 也 import builder | 清掉 re-export，测试改 patch 真实符号；dist_prep 直接依赖 pipeline |
| config ↔ analyzer | 7 | **真环**：`config.app_type`/`config.models` import `analyzer`，`analyzer.analysis` import `config` | 把 `app_type` 判定下沉到 analyzer，或把共享类型提到独立模块 |
| wheels（包门面 ↔ 子模块） | 5 | 门面环，Python 常见 | 保留 |
| packaging.pyc ↔ runtime.trim | 3 | 真环：`pyc.compile` 与 `trim` 互相引用 | 抽出共享的 "stdlib 清单" 模块 |
| runtime ↔ runtime.download | 2 | 门面包环 | 保留 |
| dep_analyzer（门面 ↔ elf/macho/graph） | 4 | 门面包环 | 保留 |

真正的**层间反向依赖**（非包门面）：

- `packaging → builder` 3 处（上述 25 模块环的根因）。
- `config → analyzer` 2 处（config 是最底层数据模型，却依赖分析层）。
- `slim → packaging` 1 处：`slim/unpack.py` import `packaging.runtime.extract`（解包能力放错了层）。
- `packaging → progress/console` 52 处：展示层耦合（不是环，但方向不对）。

`cli` 作为最顶层 import 全部层，属正常；`doctor → builder/packaging/config` 属正常诊断层。

### 3.4 过大文件（>700 行）

| 文件 | 行数 | 评价 |
|---|---:|---|
| `assets/templates/game/pygame_tetris/game.py` | 1,503 | 模板资源，不参与库，忽略 |
| `doctor/cache_health.py` | 767 | 建议按 "健康检查 / 报告渲染" 拆 |
| `cli_parser.py` | 763 | 几乎全是 argparse 声明，拆分无收益（不建议） |
| `cli.py` | 724 | 分发 + 命令实现混在一起，可把每个子命令体下沉到 `cli_commands/`（中优先级） |
| `config/models.py` | 722 | 模型 + 校验 + 推断混在一起，可与 `config/app_type.py` 重组 |

600–700 行区间还有若干（`packaging/pipeline/executor.py` 等）但均未越线。

### 3.5 对 ADR-0003 类布局约束的符合度

先说明事实：**fspack 仓库内没有 `docs/adr`，也没有任何 `*0003*` 文件**（`git log --all -- "*adr*"` 为空）。因此本节以同类项目 `fcmd/docs/adr/0003-package-layout.md` 的约束作为 "ADR-0003 类" 基线来核对，并给出 fspack 若要立 ADR 应采用的条款。

| 约束（ADR-0003 类） | fspack 现状 | 结论 |
|---|---|---|
| 包内目录不超过 2 层 | 库代码最大深度 3（含 `fspack` 根）= 2 层子包；更深的全是 assets 模板 | **符合** |
| 空目录不得存在 | 0 个空目录 | **符合** |
| 含 .py 的目录必须有 `__init__.py` | 库代码全部有；assets 下 25 个模板工程目录没有（刻意如此） | **基本符合**，但需要白名单规则 |
| 单文件领域（如 platform/exceptions/format）保持单文件 | 6 个这样的模块均单文件 | **符合** |
| 生成文件不得入库 | 未见生成物（版本走 `__init__.__version__`） | **符合** |
| 布局守卫测试（目录深度/空目录/`__init__`/生成物） | **不存在** | **不符合**（无强制手段） |
| 依赖方向单向（低层不 import 高层） | config→analyzer、slim→packaging、packaging→builder | **不符合** |
| 无循环导入 | 6 个 SCC（2 个真环） | **不符合** |

若要把这些写成 fspack 自己的 ADR-0003，建议条款 = 上表前 6 条 + "禁止 `packaging` import `builder`/`progress`/`console`；展示通过注入的回调/事件对象"，并配一个 `tests/test_layout.py`（AST 扫描 30 行即可）。

### 3.6 与现有记录不一致的地方

`docs/architecture.rst` 是仓库内唯一的架构记录（没有 ADR），实测发现以下偏差：

| 文档说法 | 实测/代码事实 |
|---|---|
| "`fsp --help` 冷启动从 ~100 ms 降到 ~61 ms" | 实测 **183 ms**（其中 parser 构造 59 ms、模块导入 45 ms、解释器+site 53 ms、console 包装 22 ms） |
| "标准库精简（默认，仅 Linux/macOS）" | Windows embed 也会精简（保守档，剥离 9–10 条目，花 180–230 ms 只省 0.10–0.18 MB） |
| "下载 wheel：uv 解析精确版本 → pip download --no-deps 并行下载（提速 ~17%）" | 本机 uv 0.13.1 **没有 `pip download`**，恒走 pip 并行；且缓存全命中时仍先跑 1.48 s 的无索引预检，命中路径没有短路 |
| 流水线 "12 个阶段" | profile 实际记录 20 个阶段名（含 win7 替换/zip 化/精简 runtime/SBOM/清单/锁） |
| "热构建只重做变更部分" | 图标 PNG 每次重转（81 ms）、Win7 扫描每次全量（126–137 ms）、loader 每次 PE 校验（38–150 ms），均未做输入级缓存 |

另：`fsp p` 的产物名用项目 `requires-python` 上界（3.11.9），与 `fsp b --py-version 3.14.6t` 的构建目标可以不同——这是预期语义，不是缺陷，但文档未说明，容易误解。

---

## 4. 分档建议

### 第一档：低风险、可立即做（合计 −220～−410 ms/热构建，−59 ms/启动）

| 项 | 改动面 | 预期收益 | 风险与验证 |
|---|---|---|---|
| L1 图标 hash 缓存 | `packaging/icon.py` 的 `ensure_ico`（读 src 的 size+mtime+sha1，命中直接复用 work_dir 里的 ico） | −81 ms（PNG 项目） | 低。单测：改图标必须重转；无 Pillow 回退不变 |
| L2 Win7 扫描增量缓存 | `packaging/win7/scan.py` + 调用点，缓存项 `(path,size,mtime,shim_ver) → 结果`，写 `.win7scan.json` 到 dist | −126～137 ms；85 二进制项目 −1.6 s | 低-中。单测：新增/替换 DLL 必须重扫；违规项不可缓存为通过 |
| L3 loader 校验复用 | `pipeline` 中 loader 缓存命中（`hit>0`）时，仅校验缓存的 exe 本体 hash 未变则跳过 PE 解析 | −38～150 ms | 低。单测：篡改 exe 必须被检出 |
| L4 精简判断 marker | `runtime/trim.py` 写 `.slim.json`（zip size+mtime+条目数），命中直接 return | −46～58 ms | 低-中。单测：手工改 zip 后必须重算 |
| L5 `-V/--help` 快速路径 | `cli.py` 顶部对 `argv == ['-V'|'--version'|'-h'|'--help']` 直接输出并退出（复用同一常量/help 文本前先确认格式一致） | −59 ms（183→124 ms） | 低。快照测试对比 argparse 输出逐字一致 |
| L6 清掉 installer→builder re-export | `packaging/installer/__init__.py` 删掉 `build` 重导出；`dist_prep.py`/`nuitka/standalone.py` 改 import 真实来源；同步改 9 处 `patch("fspack.packaging.installer.build")` | 结构收益：25 模块 SCC 瓦解；导入顺序风险消除 | 中低。`make check-fast` + 全量 not slow 必须全绿 |

> L1–L4 若走统一 action cache（P8），则不必各自打补丁；但 P8 需要立项，L1–L4 可作为过渡。

### 第二档：需立项（收益大、改动面大）

| 项 | 改动面 | 预期收益 | 风险 |
|---|---|---|---|
| M1 依赖解析链路重构 | `packaging/wheels/{downloader,resolver,uv_bridge,parallel}.py`：去掉 "先 pip --no-index 全量预检"，改为 "缓存目录元数据索引 → 未命中再 uv compile → uv/HTTP 取包"；pip 探测结果落盘 | 未命中构建 **−1.5～2.5 s**（3.48 s → 1.0～1.5 s） | 中。离线模式、hash 校验、`--find-links` 语义、uv 版本差异都要覆盖；需要新增契约测试 |
| M2 统一增量 action cache | 新缓存层 + `pipeline/executor.py` 编排改造（可覆盖 L1–L4、SBOM、产物清单） | 热构建 **−300～410 ms**（724 → ~320 ms） | 中。缓存键设计错误会导致脏产物，这是唯一"正确性级"风险，需要强失效测试 |
| M3 产物清单 hash 复用 | `copy/pyc/manifest` 之间传 hash | −100～124 ms | 低-中。hash 必须与独立重算一致（可用开关对比） |
| M4 层间解耦 | `config↔analyzer`、`slim→packaging`、`packaging→progress/console`（52 处） | 结构清晰、导入更轻（builder 265 ms 可降） | 中-高。跨包 API 变更，需分批 + 兼容层 |
| M5 AST 并行阈值复核 | 先造 1,000–3,000 文件工程实测串行 vs 并行（Windows spawn ≈60 ms/进程） | 未知，先测量 | 低（只测量）；若并行更慢则调阈值 |

### 第三档：不建议做

| 项 | 原因 |
|---|---|
| loader 编译再优化（共享预编译 loader / 改编译参数） | 单入口 670–725 ms 已是 mingw 固定成本，4 入口并行后 819 ms；可省 <100 ms，需改分发模型 |
| NSIS 12 s 优化 | makensis 单线程 LZMA 固有；只能换打包器/压缩格式，属产品形态决策 |
| 拆分 `cli_parser.py` | 763 行几乎全是声明式 argparse，拆分只增加 import 点，无维护收益 |
| 热构建继续抠复制源码/解析项目 | 分别 54 ms / 19 ms，且复制本身是增量语义 |
| 为构建路径做 import 懒加载 | 265 ms 一次付清，相对 1.7–5.5 s 构建占比低；除非做常驻 daemon/IDE 集成 |
| 消除全部 6 个 SCC | 4 个是包门面环（无害）；只值得处理 L6 与 M4 两处真环 |

---

## 5. 未测量项与复核方法

1. **前端构建**：需要 node 工程（`assets/init_templates/web/web-fastapi-react`），本次未安装/未执行 `npm build`。复核：在该模板上 `fsp b -P`，看 "构建前端" 阶段。
2. **Linux/macOS 专属阶段**：`标准库 zip 化`、Linux `精简 runtime`、ELF/Mach-O loader。本机缓存无 Linux standalone。复核：在 Linux 上对 hello 工程跑同一 `-P` 口径，或提供 Linux standalone 缓存后交叉构建。
3. **多入口 loader 并行的上限**：4 入口测得 819 ms（≈205 ms/入口），未测 8/16 入口的 worker 上限与内存。
4. **pip 版本差异**：本机 pip 26.1.2 启动 478 ms 偏慢；建议在 "uv 自带 pip / embed pip" 上复测 P6 的收益基线。
5. **杀软影响**：中型工程首次 AST 2.05 s vs 复测 160 ms，说明首次读取抖动可达 10 倍；所有冷构建数字应按 ±30% 看待。

## 附录：复现命令

```powershell
# 阶段拆分（任意工程）
fspack b <project> -P -PO <out>.json

# 启动成本
python -X importtime -c "from fspack.cli import main; main(['-V'])" 2> import.txt
# 或用脚本对 20 次 subprocess 取中位数

# 依赖原语
python -m pip --version
uv --version
uv pip compile --python-version 3.14 --python-platform windows --no-header `
  --index-url https://mirrors.aliyun.com/pypi/simple/ req.txt
uv pip download --help          # 0.13.1: rc=2，子命令不存在

# 空缓存构建
$env:FSPACK_CACHE_DIR='<empty dir>'; fspack b <project> -P -PO cold.json

# freethreaded
fspack b <project> --py-version 3.14.6t -P -PO ft.json

# 结构统计（AST 环检测 / 跨层边）
python <ast_scan>.py   # Tarjan SCC + 层间边计数，脚本见本次分析临时目录
```

> 报告未提交入库（按要求）。
