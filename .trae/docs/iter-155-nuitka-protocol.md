# iter-155: Nuitka mixin Protocol 类型声明，消除 attr-defined 抑制

## 需求清单
- [x] `NuitkaCompilerProtocol` 声明所有跨 mixin 调用的方法签名（包括辅助 staticmethod）
- [x] 所有 mixin 的 classmethod 首参数注解为 `cls: type[NuitkaCompilerProtocol]`
- [x] pyrefly nuitka 子包专项：0 errors（消除所有 missing-attribute / attr-defined）
- [x] 全量 2255 测试通过

## 迭代目标
1. 用 `typing.Protocol` 统一声明 NuitkaCompiler facade 的所有公开方法与辅助方法，使 pyrefly 能解析跨 mixin 的 `cls.<method>()` 调用
2. 消除所有 `# type: ignore[attr-defined]`（本轮迭代结束时代码中已无此类抑制——此前以 stub 占位法实现，Protocol 统一所有 mixin 类型后彻底消除）

## 改动文件清单

### 修改
1. `packaging/nuitka/protocol.py`（347 行）
   - 新增 13 个辅助方法声明：
     - **NuitkaEnv 辅助**（5）：`_check_c_compiler` / `_has_pip` / `_try_ensurepip` / `_try_uv_install_pip` / `_ensure_pip_available`
     - **NuitkaStandalone 辅助**（4）：`_build_python_cache_dir` / `_build_python_exe` / `_download_standalone_python` / `_extract_standalone_python`
     - **NuitkaCcache 辅助**（1）：`_download_and_extract_ccache`
     - **NuitkaVerify 辅助**（3）：`_find_package_root` / `_batch_import_test` / `_individual_import_test`
   - 所有声明按"提供者 mixin"分组注释，`@staticmethod` 与 `@classmethod` 与真实实现一致

2. `packaging/nuitka/env.py`
   - 新增 `TYPE_CHECKING` 导入 `NuitkaCompilerProtocol`
   - 两个 classmethod 首参注解：`ensure_env(cls: type[NuitkaCompilerProtocol], ...)` / `_ensure_pip_available(cls: type[NuitkaCompilerProtocol], ...)`

3. `packaging/nuitka/standalone.py`
   - 新增 `TYPE_CHECKING` 导入 Protocol
   - 三个 classmethod 加首参注解：`_ensure_build_python` / `_download_standalone_python` / `_extract_standalone_python`

4. `packaging/nuitka/verify.py`
   - 新增 `TYPE_CHECKING` 导入 Protocol
   - `_verify_compiled_modules(cls: type[NuitkaCompilerProtocol], ...)` 首参注解

5. `packaging/nuitka/ccache.py`
   - 新增 `TYPE_CHECKING` 导入 Protocol
   - `_ensure_ccache(cls: type[NuitkaCompilerProtocol], ...)` 首参注解

（strip/progress/compile 三个 mixin 已在 prior iteration 加注解，本轮未改动）

## 关键决策与依据

### 决策 1：Protocol 声明所有辅助 staticmethod，而非仅声明跨 mixin 的 public classmethod
pyrefly 的 `missing-attribute` 是基于对 `cls.xxx` 调用的静态分析，无论方法是 public/private、classmethod/staticmethod，只要在 classmethod 体内通过 `cls.` 访问，就要求 Protocol 有声明。典型例：
- `cls._has_pip(python_exe)`（NuitkaEnv 内部 staticmethod，仍需 Protocol 声明）
- `cls._batch_import_test(py_exe, roots, names)`（NuitkaVerify 内部 staticmethod，通过 `cls.` 调用）

### 决策 2：首参注解使用 `type[NuitkaCompilerProtocol]`（类对象自身类型）
所有方法为 classmethod，`cls` 是类对象（`type[X]`），不是实例（`X`）。用 `type[NuitkaCompilerProtocol]` 正确反映 `cls.<method>()` 的分发语义，避免实例类型注解导致的"attribute not on instance" 伪错误。

### 决策 3：Protocol 纯 TYPE_CHECKING 导入，运行时零开销
所有 mixin 中的导入都置于 `if TYPE_CHECKING:` 块，运行时不执行实际导入，避免引入循环依赖（Protocol 仅类型检查期用）。

## 代码实现情况
- **声明覆盖度**：Protocol 最终 347 行，按分组声明 30+ 方法：Env（10）、Standalone（5）、Ccache（2）、Strip（2）、Verify（4）、Compile（10）。
- **首参注解覆盖度**：所有 8 个 mixin classmethod 中未添加注解的 0 个（env/standalone/verify/ccache 本次 7 个 + 此前 compile/strip/progress 8 个 = 全部 15 个 classmethod）
- **pyrefly 专项**：`src/fspack/packaging/nuitka` 子包 pyrefly 检查从 15 missing-attribute 错误 → **INFO 0 errors**
- **全局 pyrefly**：仍有 108 errors（来自 `_prepare_windows_runtime` 等非 Nuitka 模块），后续迭代处理

## 测试验证结果
```
nuitka 专项（含 builder 集成）：321 passed, 11 skipped
pyrefly nuitka 子包：0 errors
全量回归：2255 passed, 12 skipped in 63.79s
```

## 遗留事项
- 全局 pyrefly 108 errors 主要在 `packaging/` 与 `platform/` 中 `-> Path` 注解未解析 `BuildContext` 等名字（需 import + TYPE_CHECKING 补全），进入 todo 后续处理
- 若后续新增 mixin 方法，同步更新 `NuitkaCompilerProtocol`：可考虑用 pytest 守护 Protocol 方法集合与 facade 实际 MRO 方法集合一一对应（可选，见 iter-159 测试新增）

## 下一轮计划
1. iter-156：`ProjectInfo.from_dir` 按 pyproject.toml mtime 做 lru_cache
2. 验证缓存命中率基准测试
3. 跑全量回归
