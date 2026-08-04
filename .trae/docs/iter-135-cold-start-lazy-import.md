# iter-135: 冷启动 import 终极惰性化

## 需求清单

- [x] `pipeline/__init__.py` 顶部 `fspack.console` 移至函数内（解决 project_memory 遗留 ~17ms）
- [x] `stages.py` 顶部 `BuildTracker` 类型注解改用字符串前向引用，`progress` 导入移至 `build()` 内（解决 ~8ms）
- [x] `wheels/downloader.py` 顶部 `threading` 移至方法内
- [x] 守护测试扩展：`test_pipeline_no_top_level_console_import` 等源码级守护

## 迭代目标

收尾 req-49 L92-95 列出的冷启动 import 惰性化遗留项：
(1)(2) pipeline/__init__.py 与 stages.py 顶部重模块（fspack.console / fspack.progress /
fspack.packaging.profile）惰性化已在 iter-124 完成（TYPE_CHECKING 块 + 函数内延迟导入），
本轮新增源码级守护测试防止回退；(3) wheels/downloader.py 顶部 `threading` 移到
`_stream_subprocess` 函数内（虽然 site.py 启动期已加载 threading 无运行时性能收益，
但保持模块顶部零 stdlib 副作用约定与 net.py/runtime.py 等热路径模块一致）。

## 改动文件清单

- `src/fspack/packaging/wheels/downloader.py`：
  - 顶部 `import threading` 删除
  - `_stream_subprocess` 函数内开头延迟 `import threading`（注释说明 site.py 已加载，无实际开销）
- `src/fspack/packaging/runtime.py`：
  - 顺手修复 iter-134 之前遗留的 ruff format 格式问题（`cls.download` 多行参数拆分）
- `tests/test_cli.py`：
  - 新增 `test_pipeline_module_no_top_level_heavy_imports`：源码级守护 pipeline/__init__.py
    顶部不含 `fspack.console`/`fspack.progress`/`fspack.packaging.profile` 运行时导入
    （TYPE_CHECKING 块内允许）
  - 新增 `test_downloader_module_no_top_level_threading_import`：源码级守护 downloader.py
    顶部不含 `import threading`

## 关键决策与依据

### (1)(2) 项已完成的确认

收集阶段读 pipeline/__init__.py 与 stages.py 源码发现：

- pipeline/__init__.py L80-88：`BuildTracker`/`ProfileContext` 已在 `if TYPE_CHECKING:` 块内
  （`from __future__ import annotations` 使注解不在运行时求值）
- pipeline/__init__.py L173：`from fspack.progress import BuildTracker` 已在 `build()` 函数内
- pipeline/__init__.py L339/L430：`from fspack.console import console` 已在 `_execute_build`/
  `_print_build_plan` 函数内
- stages.py L63-67：`BuildTracker`/`StageRecorder` 已在 `if TYPE_CHECKING:` 块内

iter-124 已完成 (1)(2)，req-49 L92-93 描述的"~17ms"/"~8ms" 遗留实际已解决。本轮仅补充
源码级守护测试防回退（之前仅有子进程级 `test_builder_import_does_not_load_console`/
`test_builder_import_does_not_load_progress` 守护，源码级守护缺失）。

### threading 延迟导入的实际性能影响

`-X importtime` 实测确认 `threading` 是 `site.py` 启动期通过 `importlib.util` 链式加载的
（Python 解释器启动必需），fspack 顶部 `import threading` 实际是 dict 查询（sys.modules
缓存命中），零运行时开销。

延迟到 `_stream_subprocess` 函数内的价值不在性能，而在：

1. 保持模块顶部零 stdlib 副作用约定（与 net.py/runtime.py 等热路径模块一致）
2. 源码可读性：`threading` 仅在 `_stream_subprocess` 用到，函数内导入更贴近使用点
3. 静态分析一致性：ruff/linter 静态扫描顶部导入区可清晰识别模块依赖边界

### 源码级守护测试 vs 子进程级守护测试

已有子进程级守护测试（`test_builder_import_does_not_load_console` 等）通过 `sys.modules`
验证运行时不加载重模块，但无法定位回退引入的具体文件。源码级守护测试通过 `inspect.getsource`
+ `re.sub` 移除 TYPE_CHECKING 块后扫描顶部导入区，能精确拦截具体文件的顶部重模块导入回退。

两层守护互补：源码级快速定位、子进程级验证运行时行为。模式与既有
`test_net_module_no_top_level_heavy_imports`/`test_cli_module_no_top_level_console_import`
一致。

### pyrefly.toml 配置漂移（待用户复核）

pyrefly 1.1.1 对 `pyrefly.toml` 中 `project-excludes = ["src/fspack/assets/templates/**"]`
未生效，导致 `pyrefly check .` 报 72 个 errors（全部来自 `src/fspack/assets/templates/**`
下项目模板的第三方依赖缺失：flask/pygame/PySide2/numpy 等）。

临时方案：用 CLI `--project-excludes "**/assets/templates/**"` 验证 fspack 自身代码 0 errors。
修复 `pyrefly.toml` 配置属于工具链文件变更（rule-01 暂停条件第 2 条），本轮不修改，
标注"待用户复核"留待后续迭代处理。

## 代码实现情况

### downloader.py 顶部 threading 移除

```python
# 之前
import logging
import os
import re
import subprocess
import sys
import threading  # <- 删除
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

# 之后
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Sequence
```

### _stream_subprocess 函数内延迟导入

```python
def _stream_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """..."""
    # 延迟导入 threading：保持模块顶部零 stdlib 副作用约定（与 net.py/runtime.py
    # 等热路径模块一致）。site.py 启动期已加载 threading，此处为 dict 查询，无实际开销。
    import threading

    process = subprocess.Popen(...)
    ...
    thread = threading.Thread(target=_drain_stderr, daemon=True)
    thread.start()
    ...
```

### 源码级守护测试

```python
def test_pipeline_module_no_top_level_heavy_imports() -> None:
    import inspect
    import re
    from fspack.packaging import pipeline

    source = inspect.getsource(pipeline)
    top_section = source.split("def resolve_project_info")[0]
    top_runtime = re.sub(r"if TYPE_CHECKING:.*?(?=\n\n|\n[^\s])", "", top_section, flags=re.DOTALL)
    assert "from fspack.console import" not in top_runtime
    assert "from fspack.progress import" not in top_runtime
    assert "from fspack.packaging.profile import" not in top_runtime
    # ... 含 import 形式 6 个断言


def test_downloader_module_no_top_level_threading_import() -> None:
    import inspect
    import re
    from fspack.packaging.wheels import downloader

    source = inspect.getsource(downloader)
    top_section = source.split("\ndef ")[0]
    top_runtime = re.sub(r"if TYPE_CHECKING:.*?(?=\n\n|\n[^\s])", "", top_section, flags=re.DOTALL)
    assert "import threading" not in top_runtime
```

## 测试验证结果

### 新增测试（2 个）

- `test_pipeline_module_no_top_level_heavy_imports`：6 个断言覆盖 console/progress/profile
  的 `from X import` 与 `import X` 两种形式
- `test_downloader_module_no_top_level_threading_import`：1 个断言验证顶部无 `import threading`

### 既有守护测试不回归

- `test_builder_import_does_not_load_console`/`_progress`/`_urllib_request` 子进程级守护全通过
- `test_net_module_no_top_level_heavy_imports`/`test_cli_module_no_top_level_console_import`/
  `test_cli_module_no_top_level_platform_import` 源码级守护全通过
- `test_build_parser_does_not_load_config`/`test_help_does_not_load_heavy_modules` 不回归

### 门禁结果

- ruff check: All checks passed!
- ruff format --check: 119 files already formatted（顺手修复 runtime.py 预存格式问题）
- pyrefly: 0 errors（CLI `--project-excludes "**/assets/templates/**"`，fspack 自身代码）
- pytest: 2032 passed, 12 skipped（iter-134 为 2030 passed，新增 2 个测试）
- coverage: 95.68%（>= 95% 门禁，与 iter-134 持平），`downloader.py` 100%
- 10 benchmarks: 全通过
- import fspack.builder: 61.2ms（与 iter-134 ~64ms 持平，threading 延迟无性能收益如预期）

## 整合优化情况

- (1)(2) 项在 iter-124 已完成，本轮补源码级守护测试形成双层防护
- (3) 项 threading 延迟导入保持模块顶部轻量约定一致性
- 顺手修复 runtime.py 的 ruff format 格式漂移（iter-134 之前遗留，不属于本轮范围但避免门禁噪声）

## 遗留事项

- pyrefly.toml `project-excludes` 配置在 pyrefly 1.1.1 未生效（待用户复核，工具链配置变更需暂停）
- `import fspack.builder` 仍 ~61ms，主要瓶颈是 `fspack.config` 链式加载（~28ms，含
  dataclasses/typing/inspect/tomllib 等标准库），属 config 模块必需基础成本，无法延迟
- `fspack.packaging.pipeline.stages` 18ms（含 loader.compile 5.2ms + builtin 8.8ms），
  这两个模块是阶段函数必需依赖，无法延迟

## 下一轮计划

iter-136 tarball 安全 extract 完整化（req-49 L98-100，阶段 3 深度健壮性）：
1. `extract_standalone` 3.11 及以下用 `tarfile.open` + 手动 `data` filter（参考 PEP 706 backport）
2. `extract_embed` 校验 zip 条目路径无 `..` 与绝对路径
3. 测试覆盖恶意 tarball（路径穿越、符号链接攻击）
