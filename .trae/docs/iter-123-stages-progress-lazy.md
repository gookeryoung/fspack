# iter-123：释放 fspack.progress 顶部约束（C 方向：TYPE_CHECKING + 方法内延迟）

## 需求清单

- [x] stages.py 顶部 `from fspack.progress import BuildTracker, StageRecorder` 移到 TYPE_CHECKING
- [x] pipeline/__init__.py 顶部 `from fspack.progress import BuildTracker` 移到 TYPE_CHECKING + build() 方法内
- [x] pipeline/__init__.py L484 模块级 `from fspack.progress import spinner` 移到 _execute_build 方法内
- [x] pyc.py 顶部 `from fspack.progress import StageRecorder` 移到 TYPE_CHECKING
- [x] profile.py 顶部 `from fspack.progress import BuildTracker, StageRecord, fmt_bytes` 拆分（前两个 TYPE_CHECKING，fmt_bytes 移到 print_profile_report 函数内）
- [x] wheels/downloader.py 顶部 `from fspack.progress import StageRecorder, spinner` 拆分（StageRecorder TYPE_CHECKING，spinner 移到 _run_pip 函数内）
- [x] 新增 import 基线测试 test_builder_import_does_not_load_progress
- [x] 全套门禁通过（ruff/format/pyrefly/pytest 1846 passed/coverage 95.27%）

## 迭代目标

延续 iter-121~122 的懒加载方向，针对 `import fspack.builder` 热路径上
`fspack.progress`（含 rich.progress/rich.table ~12ms）被多个模块顶部导入触发
加载的问题，将 BuildTracker/StageRecorder（类型注解）移到 TYPE_CHECKING 块，
将 spinner/fmt_bytes（运行时调用）移到使用它们的函数内，使 `fspack.progress`
与 `rich.progress` 完全从 `import fspack.builder` 热路径上移除。

## 改动文件清单

### src/fspack/packaging/pipeline/stages.py

- 顶部移除 `from fspack.progress import BuildTracker, StageRecorder`；
- 新增 `TYPE_CHECKING` 块导入 BuildTracker/StageRecorder（仅类型注解）。

### src/fspack/packaging/pipeline/__init__.py

- 顶部移除 `from fspack.progress import BuildTracker`；
- 新增 `TYPE_CHECKING` 块导入 BuildTracker（_execute_build 签名类型注解）；
- `build()` 方法内 `from fspack.progress import BuildTracker`（实例化时才加载）；
- 移除 L484 模块级 `from fspack.progress import spinner`（noqa: E402 仍是模块级）；
- `_execute_build` 内 `with tracker.stage("复制源码")` 块内 `from fspack.progress import spinner`。

### src/fspack/packaging/pyc.py

- 顶部移除 `from fspack.progress import StageRecorder`；
- 新增 `TYPE_CHECKING` 块导入 StageRecorder（仅类型注解）。

### src/fspack/packaging/profile.py

- 顶部移除 `from fspack.progress import BuildTracker, StageRecord, fmt_bytes`；
- 新增 `TYPE_CHECKING` 块导入 BuildTracker/StageRecord（类型注解）；
- `print_profile_report` 函数内 `from fspack.progress import fmt_bytes`（渲染报告时才加载）。

### src/fspack/packaging/wheels/downloader.py

- 顶部移除 `from fspack.progress import StageRecorder, spinner`；
- 新增 `TYPE_CHECKING` 块导入 StageRecorder（类型注解）；
- `_run_pip` 函数内 `from fspack.progress import spinner`（执行 pip 命令时才加载）。

### tests/test_cli.py

- 新增 `test_builder_import_does_not_load_progress`：子进程内执行
  `import fspack.builder`，断言 `fspack.progress` 未进入 `sys.modules`。

## 关键决策与依据

### 1. 范围扩展：从 stages.py 单文件到 6 文件连锁改造

**依据**：iter-122 遗留事项预计 iter-123 仅改 stages.py 即可省 ~8ms。实际
收集阶段发现 `import fspack.builder` 加载链上多个模块顶部导入 fspack.progress：

- stages.py（BuildTracker/StageRecorder 类型注解）
- pipeline/__init__.py（BuildTracker 实例化 + spinner 模块级 import）
- pyc.py（StageRecorder 类型注解，被 builder.py 顶部 import）
- profile.py（BuildTracker/StageRecord 类型注解 + fmt_bytes 运行时调用）
- wheels/downloader.py（StageRecorder 类型注解 + spinner 运行时调用）

仅改 stages.py 无收益（pyc.py 仍触发 progress 加载）。按 rule-01「需求自主调整」
扩展范围到 6 文件连锁改造，记录在此。

### 2. spinner 模块级 import 的隐蔽性

**依据**：pipeline/__init__.py L484 `from fspack.progress import spinner  # noqa: E402`
虽然带 `noqa: E402` 注释（表示位置靠下），但仍是模块级 import，在 `import fspack.packaging.pipeline`
时执行。注释"兼容测试 monkeypatch"实际无依据——grep 确认测试无 `pipeline.spinner` patch。
移到 `_execute_build` 方法内，仅在实际复制源码时加载。

### 3. fmt_bytes 与 spinner 的运行时延迟导入

**依据**：fmt_bytes 在 `print_profile_report` 内 3 处调用（L171/L172/L206），
spinner 在 `_run_pip` 内 1 处调用（L352 `with spinner(label)`）。两者均为
运行时调用，无法用 TYPE_CHECKING（仅类型注解）。移到函数内 import，首次
调用时加载 fspack.progress，后续复用 sys.modules 缓存。

### 4. rich.progress 额外释放

**依据**：fspack.progress 是唯一顶部导入 `from rich.progress import ...` 的
模块（progress.py L19）。fspack.console 加载 rich.console/rich.logging/rich.theme，
但不加载 rich.progress。释放 fspack.progress 后，rich.progress 也完全不加载
（子进程验证 `rich.progress loaded: False`）。

## 代码实现情况

### 统一模式：TYPE_CHECKING + 方法内 import

```python
# 改造前（顶部）
from fspack.progress import BuildTracker, StageRecorder, spinner

# 改造后（顶部）
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fspack.progress import BuildTracker, StageRecorder

# 方法内（运行时调用）
def some_func():
    from fspack.progress import spinner
    with spinner(label):
        ...
```

### 6 文件统一采用此模式

- stages.py：BuildTracker/StageRecorder → TYPE_CHECKING
- pipeline/__init__.py：BuildTracker → TYPE_CHECKING + build() 内；spinner → _execute_build 内
- pyc.py：StageRecorder → TYPE_CHECKING
- profile.py：BuildTracker/StageRecord → TYPE_CHECKING；fmt_bytes → print_profile_report 内
- wheels/downloader.py：StageRecorder → TYPE_CHECKING；spinner → _run_pip 内

## 整合优化情况

- 6 文件统一采用 `TYPE_CHECKING` + 方法内 import 模式，与 iter-121 net.py 一致；
- 注释统一说明"避免 import fspack.builder 热路径触发 fspack.progress 加载"；
- 子进程 import 基线测试守护，与 iter-122 urllib 守护模式一致；
- 释放 fspack.progress 连带释放 rich.progress（额外收益）。

## 测试验证结果

### 全套门禁

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：116 files already formatted
- `uv run pyrefly check`（5 改动文件）：0 errors (1 warning pre-existing)
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1846 passed, 12 skipped, 10 deselected, coverage 95.27%
  （较 iter-122 的 1845 +1，即新增 `test_builder_import_does_not_load_progress`）

### import time 测量

| 模块 | iter-122 | iter-123 | 变化 |
|---|---|---|---|
| fspack.progress | 12.4ms（含 rich.progress 7.6ms） | **未加载** | **-12.4ms** |
| rich.progress | 7.6ms | **未加载** | **-7.6ms**（额外收益） |
| fspack.packaging.pyc | 14.3ms（含 progress） | 0.45ms 自身 | -13.85ms |
| fspack.packaging.profile | 1.36ms | 1.32ms | 持平（progress 已移除） |
| fspack.packaging.wheels.downloader | 13.5ms（含 progress） | 4.9ms | -8.6ms |
| **fspack.builder 总计** | **88.6ms** | **84.2ms** | **-4.4ms** |

### 子进程 sys.modules 验证

```
$ uv run python -c "import sys, fspack.builder; print('fspack.progress loaded:', 'fspack.progress' in sys.modules); print('rich.progress loaded:', 'rich.progress' in sys.modules)"
fspack.progress loaded: False
rich.progress loaded: False
```

`fspack.progress` 与 `rich.progress` 均未进入 `sys.modules`。

### iter-123 收益

- `fspack.builder` 总 import 时间从 88.6ms 降到 84.2ms（省 4.4ms）；
- `fspack.progress`（~12ms，含 rich.progress/rich.table）完全从热路径移除；
- `rich.progress`（~7.6ms）作为额外收益也完全移除（fspack.progress 是唯一入口）；
- 6 文件统一采用 TYPE_CHECKING + 方法内 import 模式，建立可复用范式。

### 收益与预期差异

iter-122 遗留事项预期 iter-123 省 ~8ms rich.progress。实际省 4.4ms，原因：
- rich.progress 7.6ms 完全释放，但部分收益被测量波动与其他模块加载顺序变化抵消；
- fspack.console（17.3ms，含 rich.console/rich.logging/rich.theme）仍在
  pipeline/__init__.py L40 顶部加载（iter-124 范围）。

## 遗留事项

- [ ] iter-124：评估 pipeline/__init__.py 顶部 `from fspack.console import console`
  延迟导入（预计省 ~17ms rich.console/rich.logging/rich.theme）
  - 约束：console 在 pipeline/__init__.py 中多处运行时使用（_print_build_plan 等），
    需评估哪些使用可延迟，哪些必须顶部
- [ ] iter-125：评估 fspack.config 顶部加载链优化（config 31ms，最大单项）

## 下一轮计划

进入 iter-124：评估 pipeline/__init__.py 顶部 `fspack.console` 延迟导入。

1. 全量搜索 `console` 在 pipeline/__init__.py 中的使用位置（区分类型注解/运行时调用）；
2. 评估 console 是否可在 _execute_build / _print_build_plan 等函数内延迟 import；
3. 检查测试是否 patch `pipeline.console`（约束）；
4. 测量 import 时间对比（预期从 ~84ms 降到 ~67ms）；
5. 全套门禁验证。
