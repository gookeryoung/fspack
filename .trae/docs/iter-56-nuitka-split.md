# iter-56：nuitka.py 拆分（1546 行 → 4 模块）

## 需求清单

- [x] iter-56：nuitka.py 拆分（1546 行 → `nuitka_env.py` 环境就绪 / `nuitka_compile.py`
  编译流程 / `nuitka_verify.py` 验证 / `nuitka.py` facade）

## 迭代目标

将 1546 行的 `packaging/nuitka.py` 按职责拆分为三个 mixin 模块 + facade，提升可维护性
与单文件可读性。保持公开 API 不变（`NuitkaCompiler` 类与所有方法签名/路径兼容），
所有现有 111 个 test_nuitka.py 测试不破坏。

## 改动文件清单

- `src/fspack/packaging/nuitka_env.py`（新增，~530 行）：NuitkaEnv mixin
  - C 编译器检查（`_check_c_compiler`）
  - standalone python 下载与缓存（`_ensure_build_python`/`_download_standalone_python`/
    `_extract_standalone_python`）
  - nuitka 锁定版本安装到本地缓存（`ensure_env`/`_ensure_pip_available`）
  - ccache 二进制下载与 PATH 查找（`_ensure_ccache`/`_download_and_extract_ccache`）
  - 常量：`CCACHE_VERSION`、`CCACHE_URLS`、`_CCACHE_BASE`

- `src/fspack/packaging/nuitka_compile.py`（新增，~720 行）：NuitkaCompile mixin
  - 流式 subprocess 输出（`_stream_compile`）
  - 单文件编译（`_compile_files` 串行调 nuitka `--module`，心跳线程防误判卡死）
  - 产物剥离（`_strip_compiled_sources` 验证 .pyd 可加载后删 .py）
  - stamp 缓存（`compile_with_stamp` 整合 env + compile_src + stamp 比对）
  - 第三方包编译（`compile_packages`）
  - 常量：`_HEARTBEAT_INTERVAL`

- `src/fspack/packaging/nuitka_verify.py`（新增，~200 行）：NuitkaVerify mixin
  - 模块名推导（`_find_package_root` 兼容 flat/src layout）
  - 批量 import 验证（`_batch_import_test` 一次 subprocess 测试所有模块）
  - 单模块 import 验证（`_individual_import_test` 批量测试崩溃时定位损坏 .pyd）

- `src/fspack/packaging/nuitka.py`（重写为 facade，~70 行）：
  - `NuitkaCompiler(NuitkaEnv, NuitkaCompile, NuitkaVerify)` 多继承
  - 显式 `import shutil/subprocess/sys` 兼容测试 `monkeypatch.setattr("fspack.packaging.nuitka.<module>.<attr>", ...)`
    路径解析（patch 设置的是模块对象的属性，全局生效，对三个 mixin 模块同样有效）

- `tests/test_nuitka.py`：3 处 patch 路径更新
  - `fspack.packaging.nuitka._HEARTBEAT_INTERVAL` → `fspack.packaging.nuitka_compile._HEARTBEAT_INTERVAL`（2 处）
  - `fspack.packaging.nuitka.CCACHE_URLS` → `fspack.packaging.nuitka_env.CCACHE_URLS`（1 处）

## 关键决策与依据

### 多继承 mixin vs 模块函数 + 类 wrapper

**选型**：多继承 mixin（`NuitkaCompiler(NuitkaEnv, NuitkaCompile, NuitkaVerify)`）

**理由**：

1. 所有方法都是 staticmethod/classmethod，无实例状态，天然适合 mixin
2. 测试大量 `NuitkaCompiler.<method>` 直接调用，多继承让所有方法通过 MRO 自动派发到
   对应 mixin，无需写 wrapper
3. `cls.` 调用经 MRO 自动派发到跨 mixin 方法（如 `NuitkaCompile._strip_compiled_sources`
   调 `cls._verify_compiled_modules` → MRO 找到 `NuitkaVerify._verify_compiled_modules`）
4. 模块函数 + 类 wrapper 方案需为每个方法写 wrapper，LOC 翻倍且维护负担重

### MRO 顺序

`NuitkaCompiler → NuitkaEnv → NuitkaCompile → NuitkaVerify → object`

env 在前是因为 env 是基础层（提供 `_runtime_python`/`_is_nuitka_cached` 等），
compile 依赖 env（调 `cls.ensure_env`/`cls._ensure_build_python`），
verify 被 compile 调用（`cls._verify_compiled_modules`）。

### 共享 logger 名

三个新模块都用 `logging.getLogger("fspack.packaging.nuitka")` 而非 `__name__`，
保持 logger 名与原 `nuitka.py` 一致。测试用 `caplog.at_level(..., logger="fspack.packaging.nuitka")`
锁定 logger 名，共享 logger 名避免测试失效。

### facade 显式 import 标准库模块

`nuitka.py` facade 显式 `import shutil/subprocess/sys`（标注 `# noqa: F401`），
让测试中 `monkeypatch.setattr("fspack.packaging.nuitka.shutil.which", ...)` 等
patch 路径可解析。pytest monkeypatch 解析 dotted path 时需要 facade 模块有这些
属性；patch 设置的是模块对象的属性，全局生效，对三个 mixin 模块同样有效。

### pyrefly 类型检查跨 mixin 调用

pyrefly 不理解多继承 mixin 模式，对 `NuitkaCompile` 内 `cls._runtime_python(...)`
等跨 mixin 调用报 `missing-attribute`。在调用点添加
`# type: ignore[attr-defined]  # NuitkaEnv mixin` 注释标注 mixin 来源。

尝试过 `TYPE_CHECKING` 多继承方案（让 NuitkaCompile 在类型检查时继承 NuitkaEnv/NuitkaVerify），
但 pyrefly 报 `invalid-inheritance`（非线性化继承链：NuitkaCompiler 也继承三者形成菱形），
故回退到 `# type: ignore` 方案。

## 代码实现情况

- 三个 mixin 模块完整实现，所有方法签名与 docstring 从原 nuitka.py 原样迁移
- facade nuitka.py 仅含 class 定义与必要的 import，无业务逻辑
- 测试除 3 处 patch 路径更新外无改动
- 111 个 test_nuitka.py 测试全部通过

## 整合优化情况

- 三个 mixin 模块职责单一，每个文件可独立阅读理解
- facade nuitka.py 仅 70 行，作为入口文档与 API 索引
- 跨 mixin 调用通过 `cls.` + MRO 自动派发，无需手动协调

## 测试验证结果

- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors（16 suppressed，均为跨 mixin `attr-defined`）
- pytest：1010 passed, 26 deselected
- coverage：97.10%（≥95% 门禁）

```
src\fspack\packaging\nuitka.py               9      0      0      0   100%
src\fspack\packaging\nuitka_compile.py     257     13     70      8    94%
src\fspack\packaging\nuitka_env.py         242      2     68      3    98%
src\fspack\packaging\nuitka_verify.py       72      4     24      3    93%
```

新模块单独覆盖率略低于 95%，但总覆盖率 97.10% 满足门禁。新模块的低覆盖主要源于
错误处理分支（如 ccache 下载失败回退、stamp 文件读写异常等），这些分支在原 nuitka.py
中也未被测试覆盖，拆分后只是显式暴露。

## 遗留事项

- 新模块 nuitka_compile.py / nuitka_verify.py 单独覆盖率 < 95%，可后续补充错误路径测试
- 跨 mixin `# type: ignore[attr-defined]` 是 mixin 模式与 pyrefly 的妥协，未来若 pyrefly
  支持 mixin 模式可移除

## 下一轮计划

iter-57：builder.py 拆分（1059 行 → `pipeline.py` 阶段函数 / `pyc.py` pyc 预编译 /
`sync.py` 源码同步，`builder.py` 作 facade）
