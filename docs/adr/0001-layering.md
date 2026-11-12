# ADR 0001：分层规则与叶子模块

- 状态：已接受（2026-11-12，M4 落地）
- 相关提交：M4「层间解耦」（config→analyzer 反向依赖，slim→packaging 反向依赖）
- 相关守卫：`tests/test_layout.py`（AST 扫描，CI 与本地 `make test` 均会跑）

## 背景

fspack 的模块长期按"功能族"聚合成包（`packaging/`、`analyzer/`、`slim/`、
`config/`、`doctor/`），但随着包门面（facade）不断 re-export，出现了几类
违反"向下依赖"的边：

1. `config` 为了让 `app_type` 推断能收集 AST 导入，直接依赖
   `fspack.analyzer`——而 `analyzer` 又依赖 `config` 读配置，形成跨层往返；
   更糟的是 `import fspack.config` 会连锁拉起整个分析器与 `rich`。
2. `slim`（wheel 解包精简）为了打印进度条依赖 `fspack.packaging`，
   而 `packaging` 下载 wheel 时又调用 `slim`，同样是环。
3. `progress` 既放进度条/汇总表格，又放 `StageRecorder` 这样的纯数据结构。
   纯逻辑模块为了拿到 `StageRecord` 而引用 `progress`，等于被迫拖入 `rich`。

## 决策

### 1. 依赖方向

```
cli / builder / wizard / doctor
        ↓
   packaging（编排层）
        ↓
 runtime / wheels / loader / pyc / win7 / installer / nuitka / slim
        ↓
 analyzer / config / progress(展示) / metrics(数据) / astutil / archive / jsoncache
        ↓
        exceptions / _compat / platform / console(仅展示)
```

- 下层不得 import 上层：`config` 不依赖 `analyzer`；`analyzer`、`slim` 不依赖
  `packaging`。
- `packaging` 内部只有**编排入口**（pipeline 各 stage、installer 各平台实现、
  `profile*`、`size_report`、`manifest` 打印、`net` 下载进度、`wheels.process`
  的 spinner）可以引用 `fspack.console` / `fspack.progress`；纯逻辑模块
  （解包、解析、运行时准备）不得引用，避免 `fspack --help` 连锁加载 `rich`。
- 允许清单登记在 `tests/test_layout.py` 的 `ALLOWED_DISPLAY_IMPORTERS`，双向
  校验：新增违规失败；清理后忘记删条目也失败（防"僵尸豁免"）。

### 2. 叶子模块

拆出三个无内部依赖（或仅依赖 `fspack.exceptions`）的叶子模块，供多层复用：

| 模块 | 职责 | 替换掉的旧路径 |
| --- | --- | --- |
| `fspack/astutil.py` | `collect_imports`：单棵 AST 的导入收集 | `config` → `analyzer.ast_scan` 的反向依赖 |
| `fspack/archive.py` | `validate_zip_member`：zip 条目越界校验 | `slim` → `runtime`/`packaging` 的调用 |
| `fspack/metrics.py` | `StageRecord` / `StageRecorder`：阶段指标数据 | `progress` 里的数据结构部分 |

`jsoncache.py`（JSON 缓存读取骨架）同样按叶子模块对待：只依赖标准库。

### 3. 包门面（facade）的例外

每个包保留一个 `__init__.py` 作为 re-export 门面，允许"门面 → 包内任意模块"
的边（这是 facade 的唯一职责，也是 M4 之后唯一允许的剩余环）。内部模块之间
禁止互相 re-export 绕行；需要共用的原语下沉为叶子模块，而不是让门面当中转。

## 后果

- `import fspack.config`、`fspack.analyzer` 不再加载 `rich`；实测 8 个环境
  模块的 import 从 257 个模块 / ~130ms 降到 195 个 / ~107ms。
- 新增跨层引用会在 `tests/test_layout.py` 立即失败，并在报错信息里直接给出
  ADR 与允许清单，减少"改一行 import 让启动变慢"的隐性回退。
- 代价：需要同时维护 `ALLOWED_DISPLAY_IMPORTERS`；纯逻辑模块若确实要用进度
  显示，必须把展示逻辑上移到编排入口（显式参数或回调），这是有意的约束。

## 未采纳的方案

- **靠约定 + code review**：历史上正是约定失效才产生了上述三条反向边。
- **用 import-linter 等外部工具**：多一个依赖、多一套配置；现有 20 行 AST
  扫描已能覆盖包内相对导入与函数体内延迟导入。
- **把 `StageRecorder` 留在 `progress` 并允许下层引用**：会让所有纯逻辑模块
  间接依赖 `rich`，与"启动耗时"目标冲突。
