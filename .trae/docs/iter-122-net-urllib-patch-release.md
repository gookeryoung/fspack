# iter-122：释放 net 顶部 urllib 约束（B 方向：改测试 patch 路径）

## 需求清单

- [x] 全量搜索 `fspack.packaging.net.urllib.request.urlopen` patch 路径（27 处命中，26 处实际 patch + 1 处注释）
- [x] 改为全局 `urllib.request.urlopen`（语义等效：net 顶部 `import urllib.request` 后 `net.urllib` 就是全局 urllib 包对象）
- [x] net.py 顶部移除 `import urllib.request`，移到 `Downloader.download` 方法内
- [x] 更新 `test_net_module_no_top_level_heavy_imports` 断言（顶部不再含 `import urllib.request`）
- [x] 新增 import 基线测试：`import fspack.builder` 后 `urllib.request` 不在 `sys.modules`
- [x] 全套门禁通过（ruff/format/pyrefly/pytest 1845 passed/coverage 95.27%）
- [x] import 时间测量：从 ~104.6ms 降到 ~88.6ms（省 ~16ms）

## 迭代目标

延续 iter-121 的懒加载方向，针对 iter-121 遗留的 net.py 顶部
`import urllib.request` 约束（25+ 处测试 patch `fspack.packaging.net.urllib.request.urlopen`
硬约束），通过改测试 patch 路径为全局 `urllib.request.urlopen` 释放该约束，
将 `urllib.request`（~15ms，含 http/client/email 等）从 `import fspack.builder`
热路径上移除，进一步降低 `fsp b` 启动延迟。

## 改动文件清单

### src/fspack/packaging/net.py

- 顶部移除 `import urllib.request`；
- `Downloader.download` 方法内新增 `import urllib.request`（首次下载时才加载）；
- docstring 更新：说明全局 patch 语义（`monkeypatch.setattr("urllib.request.urlopen", ...)`
  与方法内 `import urllib.request` 拿到同一模块对象）。

### tests/_stubs.py

- L70 注释中的 patch 路径示例 `fspack.packaging.net.urllib.request.urlopen` →
  `urllib.request.urlopen`（1 处）。

### tests/test_net.py

- 4 处 `monkeypatch.setattr("fspack.packaging.net.urllib.request.urlopen", ...)`
  → `monkeypatch.setattr("urllib.request.urlopen", ...)`。

### tests/test_runtime.py

- 6 处 patch 路径同步替换。

### tests/test_offline_integration.py

- 10 处 patch 路径同步替换。

### tests/test_offline_mode.py

- 5 处 patch 路径同步替换。

### tests/test_cli.py

- `test_net_module_no_top_level_heavy_imports` docstring 更新：守护范围扩展到
  `urllib.request`（原仅 rich.progress/console）；断言 `import urllib.request`
  改为 `not in top_runtime`；
- 新增 `test_builder_import_does_not_load_urllib_request`：子进程内执行
  `import fspack.builder`，断言 `urllib.request` 未进入 `sys.modules`。

## 关键决策与依据

### 1. patch 路径替换的语义等效性

**依据**：`monkeypatch.setattr("fspack.packaging.net.urllib.request.urlopen", X)`
的解析路径是：导入 `fspack.packaging.net` → 取 `urllib` 属性（即全局 `urllib` 包对象，
因 `import urllib.request` 会绑定 `urllib` 到 net 模块命名空间）→ 取 `request` 属性
（即 `urllib.request` 模块）→ patch `urlopen`。

改为 `monkeypatch.setattr("urllib.request.urlopen", X)`：monkeypatch 自动
`import urllib.request` 后 patch `urlopen`。两者 patch 的是同一个 `urllib.request.urlopen`
属性（Python 模块对象全局唯一）。

**风险**：全局 patch 会影响所有模块对 `urllib.request.urlopen` 的调用。但测试套件中
只有 net.py 的 `Downloader.download` 调用 `urllib.request.urlopen`，且每个测试
monkeypatch 在 fixture 作用域内自动还原，无跨测试污染。

### 2. net.py 顶部完全轻量化

**依据**：iter-121 已将 rich.progress/console/StageRecorder 移到方法内，本轮再移除
`urllib.request` 后，net.py 顶部仅剩 `from pathlib import Path` 与 `TYPE_CHECKING`
块（仅类型注解）。`import fspack.packaging.net` 成本降至 ~0.25ms（自身），
不再触发任何重模块加载。

### 3. 子进程测试守护 import 基线

**依据**：源码守护测试（`test_net_module_no_top_level_heavy_imports`）通过
`inspect.getsource` 检查顶部导入区，但无法捕捉间接加载（如 `from fspack.X import Y`
连锁触发 Y 模块顶部 import）。子进程测试 `test_builder_import_does_not_load_urllib_request`
在干净进程中执行 `import fspack.builder`，直接检查 `sys.modules`，覆盖任何间接加载路径。

模式与既有 `test_help_does_not_load_heavy_modules` 一致（subprocess + sys.modules 检查）。

## 代码实现情况

### net.py 改造前后对比

```python
# 改造前（顶部）
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

# 改造后（顶部）
from pathlib import Path
from typing import TYPE_CHECKING
```

```python
# download 方法内（改造后）
def download(self, url, dest, *, stage=None, label=""):
    import urllib.request
    from rich.progress import (BarColumn, ...)
    from fspack.console import console
    # ... 实际下载逻辑
```

### 测试 patch 路径改造

```python
# 改造前
monkeypatch.setattr("fspack.packaging.net.urllib.request.urlopen", fake_urlopen)

# 改造后
monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
```

### 新增子进程基线测试

```python
def test_builder_import_does_not_load_urllib_request():
    code = (
        "import sys\n"
        "import fspack.builder\n"
        "heavy = [m for m in ('urllib.request',) if m in sys.modules]\n"
        "sys.exit(1 if heavy else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, "import fspack.builder 不应加载 urllib.request"
```

## 整合优化情况

- 与 iter-121 的延迟导入模式一致：顶部 `TYPE_CHECKING` + 方法内 `import`；
- 源码守护测试断言统一：`import urllib.request` / `from rich.progress import` /
  `from fspack.console import` 三类顶部运行时导入全部 `not in top_runtime`；
- 子进程 import 基线测试与 `test_help_does_not_load_heavy_modules` 模式一致，
  可复用扩展守护其他重模块（后续 iter 可扩展 `rich.progress` / `fspack.console`
  等到同一测试或新增同类测试）。

## 测试验证结果

### 全套门禁

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：116 files already formatted
- `uv run pyrefly check src/fspack/packaging/net.py`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1845 passed, 12 skipped, 10 deselected, coverage 95.27%
  （较 iter-121 的 1844 passed +1，即新增 `test_builder_import_does_not_load_urllib_request`）
- net.py 覆盖率：100%（43 stmts / 0 miss / 6 branch / 0 brpart）

### import time 测量

`uv run python -X importtime -c "import fspack.builder"`：

| 模块 | iter-121 | iter-122 | 变化 |
|---|---|---|---|
| fspack.packaging.net | 1.3ms（含 urllib.request 14.9ms） | 0.25ms 自身 | -1.05ms 自身 |
| urllib.request | 14.9ms（在 net 加载链上） | **未加载** | **-14.9ms** |
| fspack.packaging.builtin | 24.7ms cumulative | 3.16ms cumulative | -21.5ms* |
| fspack.progress（含 rich.progress 8.4ms） | 仍加载 | 12.4ms cumulative | 不变（stages.py 顶部） |
| fspack.console | 17.3ms | 18.2ms | 不变（pipeline 顶部） |
| **fspack.builder 总计** | **104.6ms** | **88.6ms** | **-16.0ms** |

*builtin cumulative 大幅下降是因为 net 不再连锁加载 urllib.request，
urllib.request 的 14.9ms 完全从 builtin 加载链上消失。

### 子进程 sys.modules 验证

```
$ uv run python -c "import sys, fspack.builder; print('urllib.request loaded:', 'urllib.request' in sys.modules)"
urllib.request loaded: False
```

`urllib.request` 确认未进入 `sys.modules`。

### iter-122 收益

- `fspack.builder` 总 import 时间从 104.6ms 降到 88.6ms（省 16ms，~15% 降幅）；
- `urllib.request`（含 http/client/email 等子模块）完全从 `import fspack.builder`
  热路径上移除，仅在实际下载时才加载；
- net.py 顶部完全轻量化（仅 pathlib + TYPE_CHECKING）；
- 新增子进程 import 基线测试守护，防止回退。

## 遗留事项

- [ ] iter-123：评估 stages.py 顶部 `BuildTracker` 延迟导入（预计省 ~8ms rich.progress）
  - 约束：测试通过 `fspack.packaging.pipeline.stages.<symbol>` patch 模块属性，
    需评估哪些符号可移到方法内，哪些必须保留顶部绑定
- [ ] iter-124：评估 pipeline/__init__.py 顶部 `fspack.console` 延迟导入（预计省 ~17ms）
  - 约束：测试 patch `fspack.packaging.pipeline.write_pth` / `copy_source`
- [ ] iter-125：评估 `fspack.progress` 顶部 `rich.progress` 延迟导入
  （BuildTracker 在 stages.py 顶部触发 progress 加载，progress 顶部又加载 rich.progress）

## 下一轮计划

进入 iter-123：评估 stages.py 顶部 `BuildTracker` 延迟导入。

1. 全量搜索 `fspack.packaging.pipeline.stages.<symbol>` patch 路径，识别哪些符号
   被测试 patch（必须保留顶部绑定）哪些仅内部使用（可移到方法内）；
2. 评估 `BuildTracker` 是否可在 `_execute_build` 方法内 import（仅构建时才加载
   rich.progress）；
3. 测量 import 时间对比（预期从 ~88.6ms 降到 ~80ms）；
4. 全套门禁验证。
