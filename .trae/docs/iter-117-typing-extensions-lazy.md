# iter-117：typing_extensions 运行时导入消除（启动性能优化 P0）

## 需求清单

- [x] `_compat.py` 改为 TYPE_CHECKING 模式（≥3.12 用 typing.override，类型检查期用 typing_extensions，运行时 no-op）
- [x] `__init__.py` 删除 typing_extensions 探测式 stub 逻辑
- [x] `pyproject.toml` typing-extensions 从运行时 dependencies 移至 lint extra
- [x] 全套门禁通过（ruff/pyrefly/pytest 1840 passed/coverage ≥ 95%）

## 迭代目标

本迭代是「启动性能与结构优化」4 轮系列的第 1 轮（P0）。实测发现
`fspack/__init__.py` 顶部的 typing_extensions 探测式 stub 导入是 CLI 启动
最大开销：冷启动 51.5ms（含 `_socket` 32.7ms），占 `import fspack.cli`
67ms 的 77%。而该导入的唯一消费方是 `_compat.py` 的 `override`，且
`typing_extensions.override` 运行时本就是 no-op（仅设置 `__override__`
标记）——真实导入毫无必要。

## 改动文件清单

### src/fspack/_compat.py

`override` 获取改为三分支：

```python
if sys.version_info >= (3, 12):
    from typing import override
elif TYPE_CHECKING:
    # 类型检查期用 typing_extensions 保留 pyrefly 对 @override 的语义检查
    from typing_extensions import override
else:
    # 运行时 no-op：与 typing_extensions.override 运行时行为等价
    _F = TypeVar("_F")

    def override(method: _F, /) -> _F:
        return method
```

### src/fspack/__init__.py

删除整段 typing_extensions 探测式 stub（20 行）。原逻辑为兼容 embed
python 3.8 携带过新 typing_extensions 的 AttributeError，既然运行时不再
导入 typing_extensions，问题根源消除，stub 不再需要。

### pyproject.toml

- 运行时 dependencies 移除 `typing-extensions>=4.0; python_version < '3.13'`
- lint extra 新增 `typing-extensions>=4.0; python_version < '3.12'`
  （仅 pyrefly 类型检查 TYPE_CHECKING 分支需要）

## 关键决策与依据

1. **运行时 no-op 而非惰性导入**：`@override` 在类定义期（导入期）执行，
   惰性代理无法避开导入开销；而 typing_extensions.override 运行时行为
   等价于返回原函数，no-op 零开销且行为等价。类型安全由 TYPE_CHECKING
   分支保留（pyrefly 仍做 override 语义检查）。

2. **直接删除 stub 而非改为惰性 finder**：原 stub 的目的是让损坏的
   typing_extensions 可用，既然 fspack 运行时完全不导入它，任何"损坏
   检测"都是多余副作用。删除后 `sys.modules` 不再被 fspack 干预，
   对用户环境更友好。

3. **依赖移至 lint extra 而非彻底删除**：pyrefly 分析 TYPE_CHECKING 分支
   时需要 typing_extensions 可导入；Python <3.12 的 lint 环境保留该依赖。

## 代码实现情况

完成，见改动文件清单。

## 整合优化情况

- 无重复代码引入；`_compat.py` 仍为唯一兼容 shim 入口。
- 附带环境修复：`.venv` 损坏（Scripts 下 python.exe 缺失、删除被锁），
  重命名为 `.venv_broken` 后 `uv sync` 重建成功（残骸待系统释放句柄后清理）。

## 测试验证结果

### 性能收益（-X importtime 实测）

| 指标 | 修改前 | 修改后 |
|------|--------|--------|
| `import fspack.cli`（冷启动 cumulative） | 67.0ms | 22.8ms（**-66%**） |
| `fspack/__init__.py` 自身 | 53.7ms | 0.5ms |
| typing_extensions / `_socket` | 在导入链中 | **彻底消失**（builder 链亦无） |

### 门禁

- ruff check / ruff format --check：All checks passed
- pyrefly check：0 errors（10 suppressed，与 iter-116 持平）
- pytest：1840 passed, 12 skipped（与 iter-116 持平）
- coverage：95.25% ≥ 95%（与 iter-116 持平）

## 遗留事项

- `.venv_broken` 残骸目录待句柄释放后手动清理
- `build_parser()` 仍因 `choices=_mirrors_choices()` 加载 config（~20ms），
  由 iter-118 解决

## 下一轮计划

iter-118：`--mirror` choices 真懒加载——移除 `build_parser()` 构建期的
`fspack.config` 导入，`fsp --help`/`run`/`clean`/`init` 不再白付 config
加载成本（~20ms）。
