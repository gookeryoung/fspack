# iter-121：`fsp b` 热路径 import 惰性化（A 方向）

## 需求清单

- [x] net.py 顶部 rich.progress/console/StageRecorder 延迟到 `Downloader.download` / `create_ssl_context` 方法内
- [x] runtime.py 顶部 `net.Downloader` 移到 `RuntimeDownloader.download` 方法内；`StageRecorder` 改 TYPE_CHECKING
- [x] builtin.py 顶部 `StageRecorder` 改 TYPE_CHECKING（保留 `net.Downloader` 兼容测试 patch 路径）
- [x] loader/compile.py 顶部 `StageRecorder`/`spinner` 移到方法内
- [x] 新增 import 基线源码守护测试
- [x] 全套门禁通过（ruff/pyrefly/pytest 1844 passed/coverage 95.27%）

## 迭代目标

延续 iter-117~120 的懒加载方向，针对 `import fspack.builder` 热路径中
`fspack.packaging.net`（含 rich.progress 8 column 类 + console + StageRecorder）
与 `fspack.packaging.runtime`（含 net.Downloader）顶部导入触发重模块加载
的问题，将下载相关重模块延迟到实际下载/编译时才加载，降低 `fsp b` 启动延迟。

## 改动文件清单

### src/fspack/packaging/net.py

- 顶部移除 `from rich.progress import (BarColumn, ...)`、`from fspack.console import console`、
  `from fspack.progress import StageRecorder`、`import os`、`import ssl`；
- 顶部保留 `import urllib.request`（测试 `fspack.packaging.net.urllib.request.urlopen`
  patch 路径硬约束，25+ 处测试依赖）；
- 新增 `TYPE_CHECKING` 块导入 `SSLContext`/`StageRecorder`（仅类型注解）；
- `create_ssl_context` 内 `import os; import ssl`（首次创建 SSL 上下文时才加载）；
- `download` 内 `from rich.progress import ...; from fspack.console import console`
  （首次下载时才加载 rich.progress 多 column 类）。

### src/fspack/packaging/runtime.py

- 顶部移除 `from fspack.packaging.net import Downloader` 与 `from fspack.progress import StageRecorder`；
- 新增 `TYPE_CHECKING` 块导入 `StageRecorder`；
- `RuntimeDownloader.download` 方法内 `from fspack.packaging.net import Downloader`
  （首次下载运行时时才加载 net 模块）。

### src/fspack/packaging/builtin.py

- 顶部移除 `from fspack.progress import StageRecorder`；
- 顶部保留 `from fspack.packaging.net import Downloader`（测试
  `fspack.packaging.builtin.Downloader.download` patch 路径硬约束，
  test_builtin.py L232/276）；
- 新增 `TYPE_CHECKING` 块导入 `StageRecorder`。

### src/fspack/packaging/loader/compile.py

- 顶部移除 `from fspack.progress import StageRecorder, spinner`；
- 新增 `TYPE_CHECKING` 块导入 `StageRecorder`；
- `LoaderCompiler.compile` 方法内 `from fspack.progress import spinner`
  （首次编译 loader 时才加载 progress）。

### tests/test_cli.py

- 新增 `test_net_module_no_top_level_heavy_imports`：inspect 源码检查
  net.py 顶部运行时导入区（移除 TYPE_CHECKING 块后）不含
  `from rich.progress import` / `from fspack.console import`，且保留
  `import urllib.request`。守护 net.py 顶部轻量化不回退。

## 关键决策与依据

### 1. 保留 `fspack.packaging.net` 顶部 `import urllib.request`

**依据**：25+ 处测试通过 `monkeypatch.setattr("fspack.packaging.net.urllib.request.urlopen", ...)`
patch 下载流程（test_net/test_runtime/test_offline_mode/test_offline_integration）。
`urllib.request` 在函数内 `import` 不会绑定到模块命名空间，monkeypatch 路径
解析失败。改为 patch 全局 `urllib.request.urlopen` 涉及 25+ 处测试改动且
改变 patch 语义（影响所有模块），不在本轮范围。

**代价**：`urllib.request` 首次加载 ~15ms（含 http/client/email 等）仍在
`import fspack.builder` 路径上（builtin→net→urllib.request）。

### 2. 保留 `fspack.packaging.builtin` 顶部 `from fspack.packaging.net import Downloader`

**依据**：test_builtin.py L232/276 通过 `fspack.packaging.builtin.Downloader.download`
patch。移到方法内会破坏 monkeypatch 路径解析。net.py 顶部已轻量化
（不再加载 rich.progress/console/StageRecorder），加载 builtin 触发 net
模块定义成本很低（net 自身 ~1.3ms）。

### 3. `StageRecorder` 全部改 TYPE_CHECKING

**依据**：`StageRecorder` 在所有改动文件中仅用于类型注解（方法签名参数）。
`from __future__ import annotations` 使注解不在运行时求值，TYPE_CHECKING
块导入即可满足静态检查（pyrefly）与运行时分离。

### 4. `SSLContext` 类型注解改 TYPE_CHECKING

**依据**：`Downloader.__init__` 的 `ssl_ctx: SSLContext | None` 与
`create_ssl_context` 返回类型 `SSLContext` 均为注解。`ssl` 模块移到
`create_ssl_context` 内 import，注解用 `from ssl import SSLContext`
（TYPE_CHECKING 块）。

## 代码实现情况

### net.py 改造前后对比

```python
# 改造前（顶部）
from rich.progress import (BarColumn, DownloadColumn, Progress, ...)
from fspack.console import console
from fspack.progress import StageRecorder

# 改造后（顶部）
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ssl import SSLContext
    from fspack.progress import StageRecorder
```

### download 方法内延迟导入

```python
def download(self, url, dest, *, stage=None, label=""):
    from rich.progress import (BarColumn, DownloadColumn, Progress, ...)
    from fspack.console import console
    # ... 实际下载逻辑
```

## 整合优化情况

- 四个文件（net/runtime/builtin/loader.compile）统一采用 `TYPE_CHECKING` 块
  处理 `StageRecorder` 类型注解，模式一致；
- 延迟导入注释统一说明"避免 `import fspack.builder` 触发 X 加载"；
- net.py 顶部 `urllib.request` 保留的约束在 docstring 与测试断言中明确记录。

## 测试验证结果

### 全套门禁

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：116 files already formatted
- `uv run pyrefly check src/fspack/packaging/{net,runtime,builtin}.py src/fspack/packaging/loader/compile.py`：0 errors
  （注：`pyrefly check src` 报 72 errors 全部在 `src/fspack/assets/templates/`
  模板项目下，pre-existing 与本轮改动无关）
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：
  1844 passed, 12 skipped, 10 deselected, coverage 95.27%

### import time 测量

`uv run python -X importtime -c "import fspack.builder"`：

| 模块 | 改造前 | 改造后 | 变化 |
|---|---|---|---|
| fspack.packaging.net | 18.9ms（含 rich.progress 8.4ms） | 1.3ms 自身（顶部仅 urllib） | -17.6ms 自身 |
| fspack.packaging.runtime | 18.9ms（含 net） | 0.3ms（不再加载 net） | -18.6ms |
| fspack.packaging.builtin | 22.2ms（含 net 18.9ms） | 24.7ms cumulative（含 net 21.7ms） | +2.5ms* |
| fspack.packaging.loader.compile | 5.0ms（含 progress） | 5.6ms cumulative | +0.6ms* |
| **fspack.builder 总计** | **104.2ms** | **104.6ms** | **+0.4ms*** |

*注：builtin/loader.compile cumulative 略增是因为 net 加载顺序变化导致
urllib.request 首次加载从 net 转移到 builtin 触发。**总 import 时间几乎持平**
（~104ms），未达到 handoff 预期的 75ms 目标。

### 实际收益分析

总 import 时间未明显下降的根因：

1. **urllib.request 14.9ms 约束**：测试 patch 路径要求 net 顶部保留
   `import urllib.request`，这是 import fspack.builder 路径上最大的
   单模块成本，无法在 iter-121 范围内消除。
2. **rich.progress 8.4ms 来自 stages.py 顶部 progress**：
   `fspack.packaging.pipeline.stages` 顶部 `from fspack.progress import BuildTracker, StageRecorder`
   是约束1（测试 patch stages 模块属性），无法延迟。
3. **fspack.console 17.3ms**：pipeline/__init__.py 顶部必需（构建日志）。

要进一步降低 import 时间，需在 iter-122 中：
- 改测试 patch 路径 `fspack.packaging.net.urllib.request.urlopen` →
  `urllib.request.urlopen`（全局 patch），释放 net 顶部 urllib 约束；
- 评估 stages.py 顶部 `BuildTracker` 是否可 TYPE_CHECKING + 方法内 import
  （`StageRecorder` 已是类型注解，`BuildTracker` 在 `_execute_build` 内
  实例化，可延迟）。

### iter-121 价值

虽然总 import 时间未明显下降，但本轮改造仍有价值：

1. **模块加载更分散**：net/runtime/builtin/loader.compile 顶部不再
   连锁加载 rich.progress/console/progress，实际下载/编译时才加载；
2. **建立延迟加载模式**：`TYPE_CHECKING` + 方法内 import 模式可复用；
3. **import 基线源码守护测试**：`test_net_module_no_top_level_heavy_imports`
   防止回退；
4. **为 iter-122 扫清路径**：net 顶部仅剩 urllib 约束，下一步改测试
   patch 路径即可释放 14.9ms。

## 遗留事项

- [ ] iter-122：改测试 patch 路径 `fspack.packaging.net.urllib.request.urlopen` →
  `urllib.request.urlopen`，释放 net 顶部 urllib 约束（预计省 14.9ms）
- [ ] iter-123：评估 stages.py 顶部 `BuildTracker` 延迟导入（预计省 8.4ms rich.progress）
- [ ] iter-124：评估 pipeline/__init__.py 顶部 `fspack.console` 延迟导入（预计省 17.3ms）

## 下一轮计划

进入 iter-122：改测试 patch 路径释放 urllib 约束。

1. 全量搜索 `fspack.packaging.net.urllib.request.urlopen` patch 路径（25+ 处）；
2. 改为 `urllib.request.urlopen`（全局 patch，语义等效）；
3. net.py 顶部移除 `import urllib.request`，移到 `download` 方法内；
4. 新增 import 基线测试：`import fspack.builder` 后 `urllib.request` 不在 sys.modules；
5. 全套门禁验证；
6. 测量 import 时间对比（预期降至 ~90ms）。
