# 迭代 12：依赖解析缓存与运行时 stage 拆分

## 迭代目标

1. 缓存依赖解析结果到 JSON 文件，命中时跳过 pip 调用，将下载依赖阶段从 ~1.18s 降到 ~6ms
2. 拆分"准备运行时"为"下载运行时"与"解压运行时"两个独立 stage

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/fspack/builder.py` | 新增 `hashlib`/`json` 导入；新增 `_deps_cache_key`/`_load_deps_cache`/`_save_deps_cache` 三个缓存辅助函数；`download_wheels` 加入缓存检查与写入；`build` 函数拆分运行时为下载+解压两 stage，替换 `ensure_embed`/`ensure_standalone` 为 `download_*`+`extract_*` 直接调用 |
| `tests/test_builder.py` | 导入缓存辅助函数；5 个 build 测试的 mock 从 `ensure_embed`/`ensure_standalone` 改为 `download_embed`+`extract_embed`/`download_standalone`+`extract_standalone`；新增 12 个测试覆盖缓存键、缓存读写、缓存命中跳过 pip、OSError 容错、runtime 已就绪跳过等场景 |

## 关键决策与依据

### 依赖解析缓存设计

- **缓存键**：`sha256(sorted(packages) + py_version + platform_tags)` 前 16 位 hex。sorted 保证包顺序不影响键；不同 py_version/platform_tags 产生不同键。
- **缓存文件**：`cache_dir/.deps-<key>.json`，内容 `{"wheels": ["name1.whl", "name2.whl"]}`。与 wheel 文件同目录，`fspack c` 不清理 wheel 缓存。
- **命中校验**：逐个检查 wheel 文件存在，任一缺失视为未命中（避免 wheel 被手动删除后仍跳过 pip）。
- **best-effort 写入**：OSError 仅 warning 不影响构建。

### 运行时 stage 拆分设计

- **不新增 `_if_needed` 函数**：req 原计划新增 `extract_embed_if_needed`/`extract_standalone_if_needed`，实际实现将 `runtime_ready` 检查直接放在 `build` 函数中。原因：三处逻辑（runtime_ready 判断、download 调用、extract 调用）紧密耦合，提取为独立函数反而增加调用方协调成本。
- **顺序 with stage**：下载与解压用两个独立 `with tracker.stage(...)` 而非嵌套，因 StageRecorder 在 with 退出时 `_finalize`，嵌套会导致计时重叠。
- **runtime 已就绪优化**：检查 `dll_marker`/`python_bin` 存在时，下载与解压均 `hit_cache()` 跳过，不调 `download_*`/`extract_*`。保留原 `ensure_embed`/`ensure_standalone` 的优化逻辑（重复构建全跳过）。

### 不修改 embed.py/standalone.py

`ensure_embed`/`ensure_standalone` 保留不动，仍可被外部调用。`build` 直接调 `download_embed`/`extract_embed`/`download_standalone`/`extract_standalone`，不再经 `ensure_*` 中转。

## 验证结果

### 门禁

- `ruff check src tests`：All checks passed
- `ruff format --check src tests`：45 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack --cov-fail-under=95`：296 passed, coverage 98.42%（builder.py 99%）

### 实测

| 场景 | 下载依赖耗时 | 变化 |
|------|-------------|------|
| iter-11 后（--no-index 命中） | 1.36s | pip 仍执行 |
| iter-12 后（deps cache 命中） | 6ms | pip 完全跳过 |

| 场景 | 下载运行时 | 解压运行时 |
|------|-----------|-----------|
| 首次构建 | 1ms（embed 缓存命中） | 80ms（解压） |
| 重复构建 | 0ms（runtime 已就绪） | 0ms（runtime 已就绪） |
| clean 后重建 | 1ms（embed 缓存命中） | 66ms（解压） |

pyside2app `--debug` 运行输出 "hello from PySide2"，构建正确。

## 遗留事项

- `ensure_embed`/`ensure_standalone` 未重构复用新逻辑（req 原计划），保留原样向后兼容。后续可考虑删除或重构。
- builder.py 77-78 行（自动版本选择分支）仍未覆盖，需构造 `resolved != info.py_version` 场景的测试。
