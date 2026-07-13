# P10 迭代：C loader 缓存 + 下载统计修复

## 迭代目标

1. C loader 编译产物缓存到 `~/.fspack/cache/loaders/`，命中时直接复制到 dist，未命中才编译并回写缓存，避免重复编译。
2. 修复 `download_wheels` 未回写 `stage.add_bytes()` 导致汇总表"下载"列恒显示 `-` 的问题。

## 改动文件清单

| 文件 | 变更 |
|------|------|
| `src/fspack/loader.py` | 新增 `loader_cache_dir()`/`_loader_cache_key()`；`compile_loader` 增加 `cache_dir` 参数与缓存命中/回写逻辑；`mingw_available`/`gcc_available` 移除局部 `import shutil`（模块级已导入） |
| `src/fspack/builder.py` | `download_wheels` 在 pip download 前后统计 wheelhouse 新增 wheel 字节数，调 `stage.add_bytes()` |
| `tests/test_loader.py` | 6 个现有 `compile_loader` 测试加 `cache_dir=` 隔离本地缓存；新增 8 个缓存测试（命中/未命中回写/第二次命中/app_type 区分/Linux 无后缀/默认目录/编译路径 detail/回写失败） |
| `tests/test_builder.py` | 新增 2 个 `download_wheels` 测试（回写字节数/已存在 wheel 不计入） |
| `.trae/req/req-10-loader-cache-and-download-stats.md` | 需求记录 |

## 关键决策与依据

### 1. C loader 缓存键设计

- **键 = sha256(source + app_type + platform) 前 16 字符**：源码内容（由 entry_rel、py_xy、platform 决定）、应用类型（影响 `-mwindows` 编译选项）、目标平台（决定 mingw vs gcc）三者共同决定编译产物。任一改变则缓存失效，避免错误复用。
- **不纳入编译器版本**：mingw/gcc 版本升级时二进制会变，但缓存目的就是复用；用户可手动清 `~/.fspack/cache/loaders/`。纳入版本会显著降低命中率。
- **文件名 = `<hash>.exe`（Windows）/`<hash>`（Linux）**：与平台可执行文件命名一致，复制即可用。

### 2. 缓存回写 best-effort

- 编译成功后 `shutil.copy2(out_exe, cached_exe)` 回写缓存，失败仅 `logger.warning` 不影响构建。
- 回写失败场景：磁盘满、权限不足等。构建本身已成功（out_exe 已生成），缓存缺失仅影响下次构建速度。

### 3. 下载统计口径

- **口径 = wheelhouse 目录本次新增 wheel 总大小**：pip download 前记录已有 wheel 文件名集合，下载后对新增 wheel 累加 `stat().st_size`。
- **包含缓存复用部分**：pip 通过 `--find-links` 从 fspack cache 复制 wheel 到 wheelhouse 时，这些 wheel 也是"新增"的，计入字节数。该数字反映"本次构建处理的依赖体积"，对用户有参考价值。
- **不严格区分网络下载 vs 缓存复用**：严格区分需解析 pip stdout（"Downloading" vs "Using cached"），pip 输出格式不稳定且复杂。简单口径已满足"不再显示 `-`"的核心诉求。

## 验证结果

### 门禁（非 slow）

```
uv run ruff check src tests          All checks passed!
uv run ruff format --check src tests 53 files already formatted
uv run pyrefly check src             0 errors
uv run pytest -m "not slow" --cov    294 passed, 12 deselected, cov 98.27%
```

`loader.py` 100% 覆盖；`builder.py` 97%（未覆盖行为版本解析与 harvest 命中时 stage 回写，均为预先存在）。

### 端到端验证（pyside2app）

**第一次构建**（loader 缓存未命中）：

```
│ 下载依赖      │ 1m39.7s │      - │ 133.2MB │    4 │ 2 wheels 解压          │
│ 生成 C loader │   469ms │      - │       - │    - │ x86_64-w64-mingw32-gcc │
```

**第二次构建**（清理 dist，loader 缓存命中）：

```
│ 下载依赖      │  6.06s │      - │ 133.2MB │    4 │ 2 wheels 解压          │
│ 生成 C loader │   33ms │ 命中 1 │       - │    - │ 缓存命中               │
```

- 下载统计显示 133.2MB，不再 `-`。
- C loader 缓存命中，耗时 469ms → 33ms（~14 倍）。
- 整体构建 1m40.3s → 6.17s（wheel 缓存 + loader 缓存共同作用）。

## 遗留事项

- 下载统计口径包含缓存复用字节，未严格区分网络下载。如需严格区分，后续可解析 pip stdout。
