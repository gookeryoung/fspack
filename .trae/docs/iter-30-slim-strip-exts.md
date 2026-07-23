# iter-30 精简打包扩展名剥离

## 需求清单

- [x] 分析精简规则是否需要忽略 .h/.cpp/.lib 等文件
- [x] 基于常规库分析其余不需要的文件和目录
- [x] 实施扩展名剥离规则
- [x] 扩展通用剥离子目录（benchmarks/__pycache__）
- [x] Qt spec 应用 COMMON_EXCLUDE_SUBDIRS
- [x] 测试覆盖与门禁验证

## 迭代目标

精简打包规则此前仅按目录名剥离，未按文件扩展名剥离。导致 wheel 中散落的
`.h`/`.cpp`/`.lib`/`.pdb`/`.pyc`/`.exe` 等编译时/调试/缓存文件如果出现在
保留的子目录中，会被原样解压到 dist，造成体积浪费（如 numpy 的
`numpy/core/include/` 含约 700+ 个 `.h` 文件）。

本次迭代引入基于扩展名的统一剥离规则，并扩展通用剥离子目录。

## 改动文件清单

- `src/fspack/slim/base.py`：新增 `STRIP_EXTS` 常量与 `_is_strip_ext` 辅助方法；
  `_classify_top_or_meta` 与 `_default_classify` 增加 STRIP_EXTS 早期剥离分支；
  `COMMON_EXCLUDE_SUBDIRS` 扩展 `benchmarks`/`__pycache__`
- `src/fspack/slim/qt.py`：删除已被 STRIP_EXTS 覆盖的顶层 `.exe` 剥离分支；
  子目录分支增加 `COMMON_EXCLUDE_SUBDIRS` 检查（与 `_QT_EXCLUDE_SUBDIRS` 取并集）
- `tests/test_slim.py`：新增 `TestStripExts`（28 个测试）与
  `TestCommonExcludeSubdirsExtended`（8 个测试）两个测试类

## 关键决策与依据

### 1. STRIP_EXTS 扩展名集合

收录 16 个扩展名，按用途分组：

- C/C++ 头文件：`.h`/`.hpp`/`.hxx`/`.hh`
- C/C++ 源码：`.cpp`/`.cc`/`.cxx`/`.c`
- 静态库/导入库：`.lib`/`.a`
- Windows 链接/调试中间产物：`.pdb`/`.exp`/`.ilk`
- 字节码缓存：`.pyc`/`.pyo`
- 辅助可执行文件：`.exe`

依据：这些文件运行时绝对不需要。Python 解释器不读取其内容，唯一理论风险是
某些库通过 `__file__` 检查路径存在性，但实际罕见。

### 2. 剥离位置——`_classify_top_or_meta` 与 `_default_classify` 双重应用

- `_classify_top_or_meta`（Qt spec 调用）：在 metadata 检查后、跨包 shared
  检查前应用 STRIP_EXTS。这样 Qt spec 顶层与跨包的 `.h`/`.exe` 等都被剥离
- `_default_classify`（默认/numpy/lxml/matplotlib/scipy spec 调用）：在
  metadata 检查后、nested_excludes 检查前应用 STRIP_EXTS

两处都跳过目录条目（以 `/` 结尾），仅检查文件扩展名。

### 3. Qt spec `.exe` 分支删除

原 Qt spec 在顶层文件分支有 `if suffix == ".exe": return ("exclude", None)`。
由于 `_classify_top_or_meta` 现在统一处理 STRIP_EXTS（含 `.exe`），且
`.exe` 会在 `_classify_top_or_meta` 阶段就被剥离，不会进入顶层文件分支，
该专用分支冗余，删除以避免重复。Qt spec 的 `.exe` 剥离行为不变（通过
STRIP_EXTS 实现），向后兼容。

### 4. Qt spec 应用 COMMON_EXCLUDE_SUBDIRS

原 Qt spec 子目录分支仅检查 `_QT_EXCLUDE_SUBDIRS`，不应用
`COMMON_EXCLUDE_SUBDIRS`。导致 Qt 库中的 `tests`/`docs`(复数)/`benchmarks`/
`__pycache__` 等目录不会被剥离（虽然 Qt wheel 中罕见，但保险起见应统一）。

修改为 `if subdir in cls.COMMON_EXCLUDE_SUBDIRS or subdir in _QT_EXCLUDE_SUBDIRS`。
`_QT_EXCLUDE_SUBDIRS` 中的 `examples`/`doc` 与 `COMMON_EXCLUDE_SUBDIRS` 重叠，
保留冗余项以语义清晰（`_QT_EXCLUDE_SUBDIRS` 是 Qt 专属剥离集合的显式声明）。

### 5. COMMON_EXCLUDE_SUBDIRS 扩展

新增 `benchmarks`（性能基准测试，numpy/scipy 等含）与 `__pycache__`（字节码
缓存目录，偶有 wheel 含）。`demos`/`tutorials`/`_examples` 等变体未加入，
因实际 wheel 中罕见且 `examples` 已覆盖多数场景；如需可后续扩展。

### 6. 不剥离的扩展名（确认保留）

- `.py`/`.pyd`/`.pyi`/`.so`：Python 源码/扩展模块/类型存根，运行时必需
- `.dll`：共享库，C 扩展运行时依赖
- `.py.typed`/`py.typed`：PEP 561 类型标记
- `.json`/`.txt`/`.dat` 等：运行时配置/数据文件
- `.dist-info/**`：元数据，始终保留

## 代码实现情况

### base.py 改动

```python
# 新增常量
STRIP_EXTS: frozenset[str] = frozenset({
    ".h", ".hpp", ".hxx", ".hh",
    ".cpp", ".cc", ".cxx", ".c",
    ".lib", ".a",
    ".pdb", ".exp", ".ilk",
    ".pyc", ".pyo",
    ".exe",
})

# COMMON_EXCLUDE_SUBDIRS 新增
"benchmarks",  # 性能基准测试
"__pycache__",  # 字节码缓存目录

# 新增辅助方法
@classmethod
def _is_strip_ext(cls, entry: str) -> bool:
    if entry.endswith("/"):
        return False
    filename = entry.rsplit("/", 1)[-1]
    return Path(filename).suffix.lower() in cls.STRIP_EXTS

# _classify_top_or_meta 增加 STRIP_EXTS 检查（metadata 后、跨包 shared 前）
# _default_classify 增加 STRIP_EXTS 检查（metadata 后、nested_excludes 前）
```

### qt.py 改动

- 删除顶层文件分支中的 `if suffix == ".exe": return ("exclude", None)`
- 子目录分支改为 `if subdir in cls.COMMON_EXCLUDE_SUBDIRS or subdir in _QT_EXCLUDE_SUBDIRS`

## 整合优化情况

- Qt spec 的 `.exe` 剥离逻辑统一到基类 `STRIP_EXTS`，消除重复
- Qt spec 现在应用 `COMMON_EXCLUDE_SUBDIRS`，与默认/numpy/lxml/matplotlib/scipy
  spec 行为一致
- `_is_strip_ext` 作为公共辅助方法供未来新 spec 复用

## 测试验证结果

### 新增测试

- `TestStripExts`（28 个测试）：
  - `_is_strip_ext` 辅助方法行为（扩展名识别、大小写不敏感、目录条目跳过、无扩展名）
  - 默认 spec 顶层/子目录扩展名剥离（.h/.cpp/.lib/.a/.pdb/.pyc/.exe）
  - 默认 spec 确认 .pyd/.dll/.py/.pyi 不受影响
  - Qt spec 顶层/子目录/跨包扩展名剥离
  - numpy/lxml/matplotlib/scipy spec 扩展名剥离
  - `.dist-info` 不受 STRIP_EXTS 影响
  - 端到端 wheel 解压验证（含 .h/.cpp/.lib/.pdb/.pyc/.exe 的 wheel 解压后剥离）
- `TestCommonExcludeSubdirsExtended`（8 个测试）：
  - 默认/numpy spec 的 benchmarks/__pycache__ 剥离
  - Qt spec 的 benchmarks/__pycache__/tests/docs 剥离（COMMON_EXCLUDE_SUBDIRS 覆盖）

### 门禁结果

- `ruff check src tests`：All checks passed!
- `ruff format --check src tests`：50 files already formatted
- `pyrefly check`：0 errors
- `pytest -m "not slow" --cov=fspack --cov-fail-under=95`：598 passed，coverage 97.83%
- slim 模块覆盖率：base.py 100%、default.py 100%、libs.py 100%、qt.py 100%

## 遗留事项

- `demos`/`tutorials`/`_examples` 等示例目录变体未加入 COMMON_EXCLUDE_SUBDIRS，
  实际 wheel 中罕见，如遇可在库专属 spec 的 extra_excludes 中处理
- `.so.dbg`/`.so.debug`（Unix 调试符号）未加入 STRIP_EXTS，实际 wheel 中罕见

## 下一轮计划

无。本次迭代完成扩展名剥离规则引入，所有门禁通过。
