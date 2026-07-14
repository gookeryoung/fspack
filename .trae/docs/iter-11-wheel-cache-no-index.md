# 迭代 11：wheel 缓存优化（--no-index 快速路径）

## 迭代目标

用户反馈：项目清理（`fspack c`）后重新构建（`fspack b`）时，wheel 依然需要重新下载。

实测确认：wheel 已正确缓存到 `~/.fspack/cache/wheels/`（缓存命中、0 字节下载），但 `pip download` 命令仍执行 ~1.6s 并查询网络 index，让用户感觉"在重新下载"。

目标：清理后重建时优先从本地缓存解析依赖，跳过网络查询；缓存不完整时自动回退到带 index 的完整下载；同时修正下载与解压共用 stage 导致的项数重复计数。

## 改动文件清单

- `src/fspack/builder.py`：
  - `download_wheels` 改为先 `--no-index --find-links cache_dir` 尝试，失败回退到带 `-i index` 的完整下载
  - 提取 `_run_pip(cmd, label, *, suppress_error=False)` 辅助函数统一处理 `subprocess.run` 异常转换
  - stage 备注改为 `{n} wheels, {缓存命中|新增 m}` 格式，明确标注缓存状态
  - `build` 函数拆分"下载依赖"与"解压 wheel"为两个独立 stage
- `tests/test_builder.py`：
  - 修改 `test_download_wheels_cmd_construction`：断言 `--no-index` in cmd，`-i index` not in cmd
  - 新增 `test_download_wheels_fallback_cmd_has_index`：验证 `--no-index` 失败回退到带 `-i` 命令
  - 新增 `test_download_wheels_no_index_skips_network`：验证 `--no-index` 成功时只调用 pip 一次
- `.trae/req/req-11-wheel-cache-no-index.md`：需求记录
- `project_memory.md`：更新 Wheel 缓存与统计汇总章节

## 关键决策与依据

### 1. `--no-index` 优先 + 回退策略

**决策**：`download_wheels` 先用 `--no-index --find-links cache_dir` 从本地缓存解析依赖，命中则跳过网络查询；`CalledProcessError` 时回退到带 `-i index` 的完整下载。

**依据**：
- 实测 `--no-index` 0.97s vs 带 index 1.32s，且 `--no-index` 离线可用、不查询网络
- 原 memory 记载"不用 `--no-index`"的原因是 pypdf 的 `typing_extensions; python_version < "3.11"` marker 在运行 pip 的 python 3.8 下触发，导致 `--no-index` 找不到 typing_extensions 报错。现系统 python 升级到 3.13，marker 评估 `python_version < "3.11"` 为 False，不再触发该依赖
- 保留回退机制以应对：系统 python < 3.11 环境、缓存不完整、条件依赖未满足等场景

### 2. `_run_pip` 辅助函数

**决策**：提取 `_run_pip(cmd, label, *, suppress_error=False)` 统一处理异常转换。
- `suppress_error=True`：`CalledProcessError` 返回 None（用于 `--no-index` 回退路径）
- `suppress_error=False`：`CalledProcessError` 转 `DependencyError`（含 stderr）
- `FileNotFoundError` 总是转 `DependencyError`

**依据**：避免 `download_wheels` 中重复的 try/except 块；`suppress_error` 参数清晰表达两种调用语义。

### 3. download/unpack 分开 stage

**决策**：`build` 函数中 `download_wheels` 与 `unpack_wheels` 分别用独立 stage。

**依据**：原实现共用 stage 导致 `stage.processed()` 被调用两次（下载 +1，解压 +1），"项数"列显示 2 但实际只有 1 个 wheel。分开后"项数"列准确反映各阶段处理项数。

## 验证结果

### 门禁

- `uv run ruff check src tests`：All checks passed
- `uv run ruff format --check src tests`：45 files already formatted
- `uv run pyrefly check`：0 errors
- `uv run pytest -m "not slow" --cov=fspack --cov-fail-under=95`：283 passed, 12 deselected, coverage 98.34%

### 实测

**清理后重建（pygame_snake）**：
```
│ 下载依赖      │ 1.27s │ 命中 1 │    - │    1 │    - │ 1 wheels, 缓存命中    │
│ 解压 wheel    │ 176ms │      - │    - │    1 │    - │ 1 wheels 解压         │
```
- 日志输出"缓存解析成功，跳过网络查询"
- 耗时从 1.66s 降到 1.27s
- 下载与解压分两行显示，项数准确

**清理后重建（cli_office/pypdf）**：
- 同样走 `--no-index` 路径，"缓存解析成功，跳过网络查询"
- 耗时 1.20s

**site-packages 已有依赖（未 clean 重建）**：
```
│ 下载依赖      │  0ms │      - │    - │    - │    1 │ 已存在跳过             │
```
- 只显示"下载依赖"一行，备注"已存在跳过"，不进入解压阶段

## 遗留事项

无。
