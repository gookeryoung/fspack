# iter-149: nuitka/compile.py 拆分为 compile/indexes/progress 三模块

## 需求清单
- [x] req-40: 深度重构与性能基线防护 — nuitka/compile.py 拆分深化
- [x] rule-01 闭环执行：公开 API 不变 / 测试路径兼容 / 全量回归通过

## 迭代目标
将 842 行的 `nuitka/compile.py`（超 500 行阈值）按职责拆分为三个聚焦模块：
- 纯索引管理（hash 索引 + 失败文件列表）
- 进度 mixin（流式编译输出 + 并行编译池）
- 编译编排主体（入口 + stamp 缓存）

保持 `fspack.packaging.nuitka.compile.*` 模块级 patch 路径兼容（测试 monkeypatch 不改），
181 个 nuitka 单元测试 + 4 个基准测试 100% 通过。

## 改动文件清单
### 新增
- `src/fspack/packaging/nuitka/indexes.py`（167 行）：hash 索引与失败文件管理
  - `_hash_index_path` / `_failed_files_path`
  - `_load_hash_index` / `_update_hash_index` / `_HASH_INDEX_MAX`
  - `_load_failed_files` / `_save_failed_files`
  - `_dispatch()` 运行时延迟导入 compile 模块拿 `_atomic_write_text` / `_safe_unlink`，保证 monkeypatch 生效
  - `_atomic_write_text` / `_safe_unlink` 别名暴露给 compile.py re-export

- `src/fspack/packaging/nuitka/progress.py`（265 行）：`NuitkaProgress` mixin
  - `_stream_compile`：流式输出 + 超时 + 死锁防护
  - `_compile_files`：ThreadPoolExecutor 并行编译 + 全局心跳进度
  - `_C()` 运行时延迟 dispatch 五个常量 + `ThreadPoolExecutor` 类，保证 monkeypatch 生效

### 修改
- `src/fspack/packaging/nuitka/compile.py`：从 842 行降至 490 行
  - 顶层定义独立的 `_atomic_write_text` / `_safe_unlink` 薄封装（直接调 util 层，避免与 indexes dispatch 递归）
  - 从 progress.py re-export 5 个进度常量 + ThreadPoolExecutor
  - 从 indexes.py re-export 其余索引函数/常量
  - `NuitkaCompile` 类仅剩 stamp 相关 + compile_src/compile_packages/compile_with_stamp 编排入口

- `src/fspack/packaging/nuitka/compiler.py`：继承列表新增 `NuitkaProgress`，MRO 顺序更新为
  `Env → Standalone → Ccache → Strip → Progress → Compile → Verify`，保证
  `cls._stream_compile` / `cls._compile_files` 通过 MRO 正确派发到 Progress mixin

## 关键决策与依据

### 1. dispatch 机制解决 monkeypatch 兼容
**问题**：测试 monkeypatch `compile._atomic_write_text`，但函数搬迁到 indexes.py
后调用的是 indexes 内部薄封装，patch 不生效。

**方案**：indexes/progress 两模块都实现**运行时延迟导入 + 动态 getattr**：
- 首次调用时 `import fspack.packaging.nuitka.compile`（顶层初始化已完成）
- 每次调用都 `getattr(mod, name)` 动态获取 compile 模块属性
- patch 修改的是 compile 模块对象属性，每次调用都会被感知
- fallback 到本地默认值保证 import 早期阶段可用

**依据**：monkeypatch 只改 compile 模块对象的属性，不改函数调用内部引用。
延迟动态 getattr 是唯一兼容方案。

### 2. compile.py 独立定义 `_atomic_write_text` 避免递归
**问题**：compile 层从 indexes re-export 该名字时，实际对象是 indexes 的
`_atomic_write_text_dispatch`（又从 compile 层拿同名属性），形成死循环递归。

**方案**：compile.py 独立定义自己的薄封装（直接调 util 层），不从 indexes
导入这两个名字。indexes 的 dispatch 从 compile 层动态获取时拿到的是
compile 自有函数对象（或被 patch 后的 mock），不再递归。

### 3. `_stream_compile` timeout 默认参数恢复字面绑定
**问题**：改成 `None` + 内部 dispatch 后，静态断言
`timeout_param.default == _COMPILE_TIMEOUT`（600.0）失败。

**方案**：还原 `timeout: float = _COMPILE_TIMEOUT` 字面绑定。该默认值不会
被任何测试 patch（测试集中在 `_HEARTBEAT_INTERVAL` / `_MAX_COMPILE_WORKERS`
/ `ThreadPoolExecutor` / 原子写），静态绑定即可。

**依据**：检查所有测试 grep，无任何测试 patch `_COMPILE_TIMEOUT` 并观察
默认 timeout 行为变化；该常量仅作签名文档化意义的默认绑定（显式传参可覆盖）。

## 代码实现情况
- `NuitkaCompiler` MRO 正确：7 个 mixin 线性继承无冲突
- 公开 API（`NuitkaCompiler.compile_with_stamp` / `compile_src` / `compile_packages`）
  签名不变
- 测试 patch 路径 100% 兼容：`_HEARTBEAT_INTERVAL`、`_MAX_COMPILE_WORKERS`、
  `_atomic_write_text`、`ThreadPoolExecutor`、`_safe_unlink`、`shutil.rmtree`
  （6 处全部生效）
- 181 个 nuitka 单元测试通过，4 个基准测试通过

## 整合优化情况
- 行数控制：compile.py 490 行（<500 行阈值），indexes.py 167 行、progress.py 265 行
- 无循环 import（dispatch 函数内部延迟导入）
- 无新增三方依赖

## 测试验证结果
| 测试套件 | 结果 | 备注 |
| --- | --- | --- |
| tests/test_nuitka.py (181 项) | 全部通过 | 单元测试：hash 索引/失败文件/stamp/心跳/编译编排 |
| tests/test_nuitka_compile_baseline.py (4 项) | 全部通过 | 基准矩阵：ccache hit/miss、serial/parallel |
| 导入链检查 | MRO 正确 | `NuitkaEnv → NuitkaStandalone → NuitkaCcache → NuitkaStrip → NuitkaProgress → NuitkaCompile → NuitkaVerify → object` |

基准性能（无回归，与基线一致）：
- ccache hit：32.84ms（1.00x）
- parallel compile：134.27ms（4.09x）
- ccache miss：265.67ms（8.09x）
- serial compile：513.67ms（15.64x）

## 遗留事项
- 无。iter-149 所有既定目标完成，未引入遗留问题。

## 下一轮计划
iter-150: `pipeline/stages.py` 深化拆分（stages/helpers/init 职责重划）。
预期将 pipeline 目录大文件按阶段职责（prepare/build/verify/archive/meta/sbom/release doctor）
拆为独立 mixin，主 `stages.py` 仅保留编排逻辑。
