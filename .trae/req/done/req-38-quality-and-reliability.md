# 代码质量与可靠性提升（10 项迭代 iter-61 ~ iter-70）

## 背景

req-37 完成性能基线建立与 5 个大文件拆分（iter-51~60），项目结构与性能
已显著改善。当前需转向代码质量深化、类型安全、测试覆盖与可靠性提升，
进一步降低维护成本与运行时故障率。

### 现状基线（2026-07-27）

**低覆盖模块（<95%）**：

| 模块 | 覆盖率 | 缺失行 |
|------|--------|--------|
| `packaging/runtime.py` | 90% | 144, 196-205, 294, 337, 377 |
| `packaging/sync.py` | 90% | 137, 153, 165-167, 181 |
| `packaging/nuitka_verify.py` | 93% | 107, 116, 185-190 |
| `config/versions.py` | 93% | 252, 254, 259-262 |
| `packaging/nuitka_compile.py` | 94% | 259, 262-263, 287, 316, 514-515, 548, 552-553, 603-604, 723 |

**大文件（>600 行）**：

| 模块 | 行数 | 拆分方向 |
|------|------|---------|
| `packaging/installer.py` | 742 | NSIS / Linux .deb+tar.gz / zip 便携包 |
| `packaging/loader.py` | 701 | C 源码生成 / 编译流程 / 缓存 |
| `packaging/pipeline.py` | 663 | 阶段函数按构建阶段分组 |
| `packaging/nuitka_compile.py` | 730 | 编译流程 / 产物剥离 |

**类型安全**：pyrefly 86 个抑制警告（主要为 mixin 跨类调用 `attr-defined`）

**总覆盖率**：97.16%（门禁 ≥95%）

## 10 项迭代任务

### 结构优化（iter-61 ~ iter-62）

- [ ] **iter-61 installer.py 拆分**：742 行 → `installer_nsis.py`（Windows NSIS）/
  `installer_linux.py`（.deb + tar.gz）/ `installer_zip.py`（跨平台便携包），
  `installer.py` 作 facade。基类 `Installer` 保留在 `installer.py`，子类按平台拆分
- [ ] **iter-62 loader.py 拆分**：701 行 → `loader_source.py`（C 源码模板生成）/
  `loader_compile.py`（编译命令构造与执行 + 缓存），`loader.py` 作 facade。
  基类 `LoaderCompiler` 保留，平台子类不拆（Windows/Linux 各 ~100 行）

### 覆盖率补强（iter-63 ~ iter-65）

- [ ] **iter-63 runtime.py + sync.py 覆盖率补强**：90% → ≥95%。补充 SSL 校验失败
  回退、损坏 zip 重下载、源码同步权限错误、符号链接处理等边缘场景测试
- [ ] **iter-64 nuitka_verify.py + versions.py 覆盖率补强**：93% → ≥95%。补充
  模块名推导边界（flat/src layout 混合）、PEP 440 通配符匹配、批量 import
  崩溃定位等测试
- [ ] **iter-65 nuitka_compile.py 覆盖率补强**：94% → ≥95%。补充心跳线程超时、
  stamp 文件损坏、.pyd 加载失败保留 .py 等错误路径测试

### 类型安全（iter-66）

- [ ] **iter-66 pyrefly 抑制警告清理**：86 → ≤40。分类处理：
  mixin `attr-defined`（加 Protocol 类型声明替代 `# type: ignore`）、
  第三方库缺失 stub（加 `# pyrefly: ignore[missing-module]` 精确标注）、
  动态属性（改用 dataclass + `cast()` 收窄）

### 性能优化（iter-67 ~ iter-68）

- [x] **iter-67 wheel 并行下载**：`_download_online` 中 `pip download --no-deps`
  逐个下载改为 `ThreadPoolExecutor` 并行（I/O 密集网络下载），uv 解析出的
  依赖列表分批并发下载。基线对比验证提速 ≥20%
- [x] **iter-68 CLI 启动懒加载优化**：`fsp` 入口延迟导入重模块（nuitka/installer），
  仅在对应子命令触发时加载。测量 `fsp --help` 启动时间，目标 ≤100ms

### 可靠性与文档（iter-69 ~ iter-70）

- [x] **iter-69 examples 端到端集成测试完善**：扩展现有 slow 测试，覆盖多入口、
  Nuitka 编译、Linux 跨平台构建、精简规则组合场景。每 5 次开发循环至少跑一次
- [x] **iter-70 架构文档与模块索引同步**：更新 README 架构图、补充模块职责
  索引（含 iter-56~62 拆分后的新模块）、更新开发文档中的导入路径示例

## 验收标准

- 每次迭代全套门禁通过（ruff/pyrefly/pytest/coverage ≥ 95%）
- 覆盖率不下降，低覆盖模块（<95%）均提升至 ≥95%
- 结构拆分保持公开 API 不变，所有现有测试不破坏
- 性能优化迭代须对比基线，退化 > 10% 失败
- pyrefly 抑制警告数下降 ≥50%

## 实施顺序

1. iter-61~62（结构优化，先减大文件降低后续迭代阅读成本）
2. iter-63~65（覆盖率补强，为新拆分模块补充测试）
3. iter-66（类型安全，在测试稳定后清理类型抑制）
4. iter-67~68（性能优化，在结构稳定后做基线对比）
5. iter-69~70（可靠性与文档，收尾验证与同步）
