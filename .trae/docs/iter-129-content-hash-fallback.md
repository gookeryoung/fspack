# iter-129 内容 hash 回退缓存层

## 需求清单

- [x] stamp 未命中但内容 hash 与历史成功构建匹配时跳过编译，重建 stamp（req-49 阶段 3）
- [x] hash 索引文件损坏时自动删除重建，不影响构建流程
- [x] hash 索引 LRU 淘汰，避免无限增长
- [x] 编译成功后更新 hash 索引

## 迭代目标

引入内容 hash 回退缓存层，解决 stamp 文件单独丢失/损坏时（dist 完整保留）仍要完整重编的问题：
1. 维护 `dist/.nuitka_hash_index.json` 记录历史成功构建的 `stamp_key`，与 stamp 同目录
2. `compile_with_stamp` 在 stamp 未命中后检查 hash 索引，命中则跳过编译并重建 stamp
3. 索引文件损坏（JSON 非法/结构错误）自动删除，与 iter-128 `_load_deps_cache` 策略一致
4. 索引 LRU 淘汰上限 50 条，按 ISO 时间戳排序删除最旧

## 改动文件清单

### 源码
- `src/fspack/packaging/nuitka/compile.py`：
  - 顶部新增 `import json`、`from datetime import datetime`
  - 新增模块级常量 `_HASH_INDEX_MAX = 50`
  - 新增模块级函数 `_hash_index_path(dist_dir)` 返回 `dist/.nuitka_hash_index.json`
  - 新增模块级函数 `_load_hash_index(dist_dir)`：读取索引，损坏删文件，OSError 返回空 dict，非 str 条目剔除并回写
  - 新增模块级函数 `_safe_unlink(path)`：删除文件，OSError 仅告警
  - 新增模块级函数 `_update_hash_index(dist_dir, stamp_key)`：合并新条目，LRU 淘汰超限条目，原子写入
  - `compile_with_stamp` 在 stamp 未命中后增加 hash 索引回退分支：索引命中时 `hit_cache()` + 重建 stamp + return
  - `compile_with_stamp` 末尾编译成功后调用 `_update_hash_index(dist_dir, stamp_key)`

### 测试
- `tests/test_nuitka.py`：
  - 导入 `_HASH_INDEX_MAX`、`_hash_index_path`、`_load_hash_index`、`_update_hash_index`、`json`
  - 新增 15 个测试：
    - `_hash_index_path` 路径
    - `_load_hash_index`：文件不存在/JSON 损坏删文件/非 dict 删文件/非 str 条目剔除回写/读取 OSError 不删文件/损坏+删除失败告警
    - `_update_hash_index`：新条目写入/合并已有/LRU 淘汰/写入 OSError 告警
    - `compile_with_stamp`：索引命中跳过+重建 stamp/索引未命中走编译+更新索引/索引损坏走编译+重建/索引命中但重建 stamp 失败告警

## 关键决策与依据

### 1. 索引与 stamp 同目录（dist/）
索引文件 `dist/.nuitka_hash_index.json` 与 stamp `dist/.nuitka_compile_stamp` 同目录。删除 dist 时一并清理，保证索引命中场景仅限于"dist 完整保留（.pyd 产物在）但 stamp 单独丢失/损坏"。这避免了"索引命中但产物已删"的误跳过——若用户删除 dist，索引也消失，必然走完整编译。

### 2. 索引命中语义与 stamp 命中一致
索引命中时直接 `hit_cache()` + 重建 stamp + return，与 stamp 命中语义完全一致。不额外验证 .pyd 产物存在——stamp 命中本身也不验证产物，索引回退保持相同风险水平（用户单独删除 .pyd 但保留 stamp/索引是边缘场景，两机制同等处理）。

### 3. 损坏处理策略与 iter-128 统一
`_load_hash_index` 区分内容损坏（JSON 非法/非 dict/类型错误）与 OSError：
- 内容损坏 → 删除文件并返回空 dict（明确损坏，下次重建）
- OSError → 不删除，返回空 dict（瞬时权限/磁盘错误，下次重试）

这与 iter-128 `_load_deps_cache` 的策略完全一致，保持代码库一致性。

### 4. LRU 淘汰按时间戳字符串排序
索引值是 `datetime.now().isoformat(timespec="seconds")`（如 `2026-08-04T10:30:00`），ISO 8601 格式保证字符串排序与时间排序一致。`_HASH_INDEX_MAX = 50` 覆盖常见多版本/多入口/多包组合场景，每条约 200 字节，索引文件 <10KB。

### 5. 索引写入失败不中断构建
`_update_hash_index` 内部 `_atomic_write_text` 调用被 try/except OSError 包裹，失败仅告警。索引是回退优化，写入失败不影响主流程——下次构建仍可走完整编译（stamp 仍会写入，stamp 命中路径不受影响）。

### 6. 重建 stamp 失败仍跳过编译
hash 索引命中但重建 stamp 失败（如只读文件系统）时，仍跳过编译并 `hit_cache()`。理由：索引命中即视为已编译，重建 stamp 只是优化下次构建的命中速度（stamp 命中比索引命中快一次文件读取），失败不应触发完整重编。

## 代码实现情况

### hash 索引回退分支
```python
# stamp 未命中但 hash 索引命中：dist 完整保留但 stamp 单独丢失/损坏时，
# 跳过编译并重建 stamp（iter-129）。索引与 stamp 同在 dist/，删除 dist 时
# 一并清理，保证索引命中场景仅限于 dist 完整保留的情况（.pyd 产物仍在）。
hash_index = _load_hash_index(dist_dir)
if stamp_key in hash_index:
    _logger.info("Nuitka stamp 未命中但 hash 索引命中，跳过编译并重建 stamp")
    stage.hit_cache()
    stage.set_detail(f"hash 索引命中，nuitka {nuitka_ver} 已编译（重建 stamp）")
    try:
        _atomic_write_text(stamp, stamp_key)
    except OSError as e:
        _logger.warning("重建 Nuitka stamp 失败: %s", e)
    return
```

### LRU 淘汰
```python
# LRU 淘汰：按时间戳升序排序，保留最新的 _HASH_INDEX_MAX 条
if len(index) > _HASH_INDEX_MAX:
    sorted_items = sorted(index.items(), key=lambda kv: kv[1])
    index = dict(sorted_items[-_HASH_INDEX_MAX:])
```

### 编译成功后更新索引
```python
try:
    _atomic_write_text(stamp, stamp_key)
except OSError as e:
    _logger.warning("写入 Nuitka stamp 失败: %s", e)
_update_hash_index(dist_dir, stamp_key)
```

## 整合优化情况

- `_load_hash_index` 的损坏处理逻辑与 iter-128 `_load_deps_cache` 保持一致，确保缓存层健壮性策略统一
- `_atomic_write_text`（iter-128 引入）被 `_update_hash_index` 复用，索引写入同样原子化
- `_safe_unlink` 抽取为独立函数，避免 `_load_hash_index` 内部嵌套 try/except 降低可读性

## 测试验证结果

- ruff check：0 errors
- ruff format：全部通过
- pyrefly check：0 errors
- pytest 全套：1979 passed, 12 skipped（比 iter-128 多 15 个新测试）
- coverage：95.60%（>= 95% 门禁，比 iter-128 的 95.56% 提升 0.04%）
- 守护测试 7 个：全部通过
- `compile.py` 覆盖率 99%（仅 `257->260` 累积上限分支未覆盖，iter-128 之前已存在）

## 遗留事项

无。iter-129 四项任务全部完成。

## 下一轮计划

iter-130 错误恢复（req-49 阶段 4）：增强构建流程中网络/磁盘错误的恢复能力。重点：
1. 分析现有错误处理路径，识别未覆盖的失败场景
2. 设计错误恢复策略（重试/降级/告警）
3. 实现恢复逻辑
4. 测试覆盖错误场景
