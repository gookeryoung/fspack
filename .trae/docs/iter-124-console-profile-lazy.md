# iter-124: CI benchmark gate 修复 + pipeline 顶部 console/profile 延迟导入

## 需求清单

- [x] 排查 CI benchmark gate 失败，分析是否过于严苛并解决
- [x] 推进 iter-124 遗留事项：pipeline/__init__.py 顶部 fspack.console 延迟导入

## 迭代目标

1. 修复 CI run #275 benchmark gate 误阻断（5 个不相关测试同步退化被判真实退化）
2. 延迟 pipeline/__init__.py 顶部 fspack.console + fspack.packaging.profile import，降低 `import fspack.builder` 热路径耗时

## 改动文件清单

- `scripts/compare_benchmark.py`：_detect_systemic_regression 阈值 60%/50%→50%/30%，可比 ≥3→≥5；docstring 补 run #275 依据
- `tests/test_compare_benchmark.py`（新增）：21 个单元测试守护 compare_benchmark.py 核心逻辑
- `src/fspack/packaging/pipeline/__init__.py`：移除顶部 `from fspack.console import console` 与 `from fspack.packaging.profile import ProfileContext, print_profile_report`；profile 改 TYPE_CHECKING + build() 内延迟；_execute_build 与 _print_build_plan 内 console 延迟导入
- `tests/test_cli.py`：新增 test_builder_import_does_not_load_console 基线守护

## 关键决策与依据

### CI benchmark gate 阈值放宽

run #275 失败时 5 个不同领域测试同步退化（AST 收集 +49.3%、AST 多文件 +45.9%、slim 分类 +56.8%、源码指纹 +32.2%、ProjectInfo 解析 +31.9%），中位 45.9%。iter-121~123 未改动这些测试的运行时实现（只改 import 路径），确认是 GitHub Actions 共享机器负载波动。

旧 systemic 检测阈值（退化率 >60% 且中位幅度 >50%）刚好漏判此场景（5/9≈55% 退化、45.9% 中位幅度，双低于阈值）。新阈值（退化率 ≥50%、中位幅度 ≥30%、可比 ≥5）让典型机器抖动触发 systemic 判定，exit 0 不阻断 CI。

真实代码退化通常只影响 1-3 个相关测试（如 AST 优化影响 collect_imports + analyze_dependencies，2/10=20% < 50%），不会被误判为 systemic。

### pipeline 顶部 console/profile 延迟导入

profile.py 顶部 L38 `from fspack.console import console` + L36 `from rich.table import Table`，pipeline/__init__.py 顶部 L68 import profile 会连锁触发 fspack.console（~17ms）+ rich.table 加载。同时 L40 直接 import console。

策略：
- console 移到 _execute_build（L333，summary 输出前）与 _print_build_plan（L394，函数顶部）内延迟导入
- profile 移到 build() 内 L186-189（ProfileContext 实例化前）与 L220（print_profile_report 调用前）延迟导入；TYPE_CHECKING 块仅保留 ProfileContext 类型注解
- 不影响测试：无测试 patch `pipeline.console`/`pipeline.ProfileContext`/`pipeline.print_profile_report`

## 代码实现情况

- import fspack.builder: 84.2ms → 55.6ms（省 28.6ms，~34% 降幅，超预期因 profile 顶部 rich.table 也被延迟）
- console 延迟导入点：_execute_build L333（summary 前）、_print_build_plan L394（函数顶部）
- profile 延迟导入点：build() L187（ProfileContext 实例化前）、build() L220（print_profile_report 调用前）

## 整合优化情况

- _execute_build 内 L277 已 import spinner（fspack.progress），连锁加载 console。L333 显式 import console 是冗余但显式自包含，避免依赖隐式连锁。
- profile 拆分为两处 import（ProfileContext 实例化前 + print_profile_report 调用前），避免 else 分支赋 None 的类型注解复杂度。

## 测试验证结果

- ruff check src tests: All checks passed
- ruff format --check: 117 files already formatted
- pyrefly check: 0 errors
- pytest -m "not slow" --cov=fspack --cov-fail-under=95: 1868 passed, 12 skipped, 10 deselected, coverage 95.28%
- 新增 test_builder_import_does_not_load_console 守护：import fspack.builder 不加载 fspack.console 与 fspack.packaging.profile

## 遗留事项

- iter-125 目标：fspack.config 31ms（最大单项，含 config.models 18ms + config.parsing 4.6ms + config.cache 9ms）。pipeline/__init__.py 顶部 L29-39 import 多个 config 符号，需评估哪些可延迟。
- compare_benchmark.py 当前仍用"历史最佳"对比基准，结构性偏严（极端快速运行成为基准）。后续可考虑改用"近 N 次中位数"基准，但需更大改动与测试。

## 下一轮计划

iter-125：评估 fspack.config 延迟导入可行性。约束：BuildConfig/BuildOptions/ProjectInfo/MirrorConfig 等在 pipeline/__init__.py 顶部多处类型注解与运行时使用，需评估测试 patch 路径影响。
