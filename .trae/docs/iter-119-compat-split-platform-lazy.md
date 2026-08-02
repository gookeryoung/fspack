# iter-119：_compat 拆分与 platform 延迟导入（结构解耦 P2）

## 需求清单

- [x] `CICompat` 从 `_compat.py` 移入 `console.py`（唯一消费方）
- [x] `_compat.py` 仅保留 `override`/`tomllib`，零第三方依赖
- [x] `platform.py` 的 `import platform` 延迟到 `detect_platform()` 内
- [x] 测试引用批量迁移（test_console.py 10 处 + test_platform.py 3 处 patch）
- [x] 全套门禁通过（ruff/pyrefly/pytest 1842 passed/coverage ≥ 95%）

## 迭代目标

`_compat.py` 原把"零开销 shim"（`override`/`tomllib`）与"重开销 shim"
（`CICompat`，顶部 `from rich.console import Console`）混居一个模块：
11 个消费方中 10 个只需 `override`，却被迫连带加载 rich（~17ms）。
本轮按依赖重量拆分职责，并顺带消除 `platform.py` 顶部 `import platform`
在 Windows 连带加载 `_wmi`（~1.5ms）的浪费。

## 改动文件清单

### src/fspack/console.py

- 移入 `CICompat` 类（`get_theme`/`ensure_utf8_stdio`/`make_console`，
  签名与行为不变），新增 `contextlib`/`os`/`sys`/`Theme` 导入；
- `ConsoleUI.__init__` 不再函数内 `from fspack._compat import CICompat`，
  直接调本模块 `CICompat.make_console()`；
- `__all__` 新增 `"CICompat"`；docstring 说明迁移缘由。

### src/fspack/_compat.py

- 删除 `CICompat` 类与 `rich`/`contextlib`/`os` 导入；
- docstring 重写：明确"零第三方依赖"设计约束及 CICompat 去向。

### src/fspack/platform.py

- 顶部 `import platform as _platform` 移至 `detect_platform()` 函数内；
- docstring 说明延迟原因（Windows `_wmi` ~1.5ms；`Platform` 枚举本身无需
  标准库 platform——如 `--target` 解析、类型注解场景）。

### tests/test_console.py（10 处）

- 6 处 `from fspack._compat import CICompat` → `from fspack.console import CICompat`；
- 4 处 `from fspack._compat import CICompat, sys` → `import sys` +
  `from fspack.console import CICompat`（原写法依赖 `_compat` 模块命名空间
  里的 `sys`，属于隐式耦合，顺手拆正）。

### tests/test_platform.py（3 处）

- `monkeypatch.setattr("fspack.platform._platform.system", ...)` →
  `monkeypatch.setattr("platform.system", ...)`：函数内 `import platform`
  拿到的是同一标准库模块单例，patch 标准库属性等效且不再依赖
  `fspack.platform` 的模块级命名空间。

## 关键决策与依据

1. **移入 console.py 而非新建模块**：`CICompat` 的唯一职责是为 rich
   Console 做 CI 环境适配，`console.py` 是其唯一消费方；新建
   `ci_compat.py` 会多一个模块边界却无新复用价值（边际效用原则）。
2. **不留向后兼容 re-export**：`CICompat` 是内部类（不在包级 `__all__`），
   直接移动 + 更新测试引用，避免 `_compat` 继续经 rich 残留间接依赖。
3. **slim 链 rich 加载不处理**：`fspack.slim.unpack → fspack.progress`
   加载 rich 是进度条功能所需（build 路径 console 反正要 rich），
   非 `_compat` 连累，本轮不动。

## 代码实现情况

完成，见改动文件清单。

## 整合优化情况

- `_compat.py` 从 87 行减至 47 行，职责单一（版本 shim）；
- `console.py` 从 69 行增至 119 行，但消除了跨模块函数内导入；
- 无新增模块、无重复代码。

## 测试验证结果

### 性能收益（实测）

| 指标 | iter-118 后 | iter-119 后 |
|------|------------|------------|
| `import fspack._compat` | 含 rich ~17ms | 11.4ms（仅 sys/typing/tomllib） |
| `import fspack.builder`（库用法） | 113.1ms | **93.4ms（-17%）** |
| `fspack.platform` 顶部导入 | enum + platform（含 `_wmi`） | 仅 enum |

### 门禁

- ruff check / ruff format --check：All checks passed
- pyrefly check：0 errors（11 suppressed）
- pytest：1842 passed, 12 skipped（与 iter-118 持平）
- coverage：95.25% ≥ 95%

## 遗留事项

- `.venv_broken` 残骸目录待清理（同 iter-117）

## 下一轮计划

iter-120（收尾轮）：builder facade 精简（50+ monkeypatch 兼容符号
→ 9 个公开 API，测试 patch 迁移到底层模块）+ cli.py 拆分
（`cli_parser.py` 承载参数声明）+ import-time 基线测试固化收益。
