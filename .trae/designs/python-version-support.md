# Python 最低版本支持设计

## 目标版本

- `requires-python = ">=3.8"`，支持 Python 3.8-3.14（`[x]` 已实现）。
- CI 测试矩阵：ubuntu/windows/macos × Python 3.8/3.9/3.10/3.11/3.12/3.13/3.14（`.github/workflows/ci.yml`，`[x]`）。

## 兼容机制（四层）

### 1. 语法层：PEP 563 延迟注解求值（`[x]`）

- 全部源码与测试文件顶部使用 `from __future__ import annotations`，PEP 604 联合类型（`X | Y`）、内建泛型下标（`dict[str, int]`）在 3.8 以字符串形式延迟求值，零运行时成本。
- 约束：**运行时求值点**禁止裸用 3.9+ 语法——`cast()` 的类型参数用字符串前向引用（如 `cast("Callable[..., T]", f)`）；`TypeVar` bound、模块级别名赋值、`get_type_hints()` 调用点须回退 `typing.Union`/`typing.Callable` 写法。

### 2. 语法层：解析器差异（`[x]`）

- 禁用 `with (A, B):` 括号多项形式（3.8 LL(1) 解析器将其解析为 with 元组，运行时 `AttributeError: __enter__`，且收集期不报错）；多上下文管理器用反斜杠续行或嵌套 with。
- 3.8 深嵌套源码 `ast.parse` 可能抛 `MemoryError`（3.9+ PEG 解析器抛 `RecursionError`）：`fspack.analyzer.analysis` 的解析 worker 串行/并行两条路径的异常捕获元组均包含 `MemoryError`。

### 3. 标准库行为差异层（`[x]`）

| 差异点 | 3.8 行为 | 处理方式 |
|---|---|---|
| `enum.StrEnum` | 不存在 | `fspack._compat` 提供 StrEnum 兼容类，附 `__str__ = str.__str__` 对齐 3.11+ 语义（小写成员名） |
| `socket.timeout` vs `TimeoutError` | 两个独立类（3.10 起同一类） | `fspack.packaging.net` 异常判定用 `isinstance(exc, (socket.timeout, TimeoutError))` |
| `hashlib.md5(usedforsecurity=)` | 参数不存在（3.9+） | `fspack.doctor.bench` 以 `sys.version_info >= (3, 9)` 守卫分支调用 |
| `Path.is_relative_to` | 不存在（3.9+） | 用 `relative_to()` + `ValueError` 捕获替代（`source_strip.py`、`analyzer/fingerprint.py`） |
| `zip(..., strict=)` | 参数不存在（3.10+） | 移除 strict 参数，长度不等由调用侧断言保障 |

### 4. 依赖层：universal lock fork 策略（`[x]`）

- uv universal lock 对全 requires-python 范围单版本解析（最少 fork）；3.8 无法安装的依赖用 environment marker 显式强制 fork：
  - image extra：`Pillow>=11.0; python_version>='3.9'` + `Pillow>=9.4.0; python_version<'3.9'`
  - docs extra：`sphinx>=8.0; python_version>='3.10'` + `sphinx>=7.0; python_version<'3.10'`
- 主依赖：`tomli`（<3.11）、`typing_extensions`（<3.12）按 marker 条件引入。

## 工具链版本策略

- ruff `target-version = "py38"`（`ruff.toml`，`[x]`）——影响规则集，必须与最低版本一致。
- pyrefly `python-version = "3.13"`（`pyrefly.toml`，`[x]`）——类型检查语义基线，与运行兼容解耦；运行兼容由 CI 矩阵保障。
- pyrefly 锁定 `>=1.1.1,<1.2`——1.2+ 收紧 strict 规则（implicit-any 等）属工具链升级范畴，与版本兼容无关，锁旧版保持 typecheck 行为稳定。
- pytest-benchmark 与 xdist：pytest.ini 的 addopts 禁止注入 xdist 选项（benchmark job 冲突），xdist 分组在 Makefile target 控制。

## 测试约束

- 测试代码与源码同受 3.8 语法约束（`from __future__ import annotations` + 禁括号 with）。
- 模拟"模块未安装"用 `monkeypatch.setitem(sys.modules, name, None)`（None 触发标准 ImportError），**禁止 patch `builtins.__import__`**——pytest call/teardown 阶段的 lazy import 会被一并拦截导致 INTERNALERROR；需拦截特定模块时用转发式包装（非目标 import 放行原 `__import__`）。
