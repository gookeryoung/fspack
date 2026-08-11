# iter-150: pipeline 深化拆分（stages/helpers/init 职责重划）

## 需求清单
- [x] pipeline/stages.py（1249 行）按职责拆分为 5+ 子模块，全部 < 500 行
- [x] 保持 stages.py 对外名字兼容：所有常量/函数/类在 stages.py 重导出，支持测试 monkeypatch
- [x] __init__.py 主流程不修改对外 API，仅调整内部导入路径
- [x] 全部测试通过（test_build_dry_run/test_log_file/test_profile/test_builder/test_extras/test_nuitka/test_site_packages 等）

## 迭代目标
1. 将 stages.py 中职责明确的功能按阶段拆出，降低单文件复杂度
2. 保持测试 monkeypatch.setattr("...stages.xxx", ...) 兼容
3. 不修改外部调用点：pipeline/__init__.py 主流程及 CLI 调用不变

## 改动文件清单

### 新增模块
1. `fspack/packaging/pipeline/context.py`（53 行）
   - `BuildContext` 数据类：承载一轮打包的运行时上下文
   - 路径常量：`_DEFAULT_ICON`、`_MAX_LOADER_WORKERS`、`default_icon_path()`、`fspack_wheel_cache_dir()`

2. `fspack/packaging/pipeline/runtime_stage.py`（146 行）
   - `_prepare_runtime()`：按 target 分派 standalone / windows embed / 本地 python
   - `_prepare_standalone_runtime()`：standalone 下载 + 解压
   - `_prepare_windows_runtime()`：embed python 下载 + 解压 + _pth 写 site-packages 路径
   - `_slim_runtime()`：按 slim_rules 剥离冗余库与 Tcl/Tk

3. `fspack/packaging/pipeline/deps_stage.py`（219 行）
   - `_strip_version_specifier()`：版本规范剥离
   - `_analyze_dependencies()`：AST 静态 + 导入解析 + declared 依赖合并
   - `_dep_cache_path()` / `_dep_cache_load()` / `_dep_cache_save()`：依赖缓存管理
   - `_site_packages_has_deps()`：按规范化目录名判断依赖是否已安装
   - `_download_dependencies()`：按 declared/AST 下载 wheel，解包到 site-packages，按 slim_rules 过滤
   - `unpack_wheels()`：wheel 重定向 unpack + _cache_managed_wheels 路径标记

4. `fspack/packaging/pipeline/compile_stage.py`（268 行）
   - `_resolve_project_icon()`：CLI > 配置 > favicon > 默认 4 层优先级
   - `_compile_user_sources()`：Nuitka 编译 + 字节码预编译（data_dirs/web_static_dirs 剥离保护）
   - `_build_entry_loaders()`：多入口 C loader 并行编译（ThreadPoolExecutor，上限 _MAX_LOADER_WORKERS）
   - `_analyze_binary_dependencies()`：PE/ELF/Mach-O 依赖图 BFS 剥离无引用二进制 + 并行路径解析

5. `fspack/packaging/pipeline/dist_helpers.py`（173 行）
   - `_diagnose_dist_dir()`：6 层 dist 目录冲突诊断与建议
   - `_clean_dist_dir()`：有选择地清空非 runtime/src/site-packages 目录，拒绝根级高危路径
   - `_verify_dist_writable()`：预写锁文件验证 dist 目录可写

6. `fspack/packaging/pipeline/plan_printer.py`（126 行）
   - `print_build_plan()`：Rich 表格打印 dry-run 打包计划（版本、入口、依赖、文件、runtime、预估大小）

### 修改模块
7. `fspack/packaging/pipeline/stages.py`（100 行）—— 原 1249 行压缩为 facade
   - 从所有子模块 re-export 所有阶段函数、常量、类
   - 补充运行时依赖重导出：`ThreadPoolExecutor`（concurrent.futures）、`detect_platform`（platform）、`TkinterBundler`（builtin）、`download_wheels`、`compile_loader`、`download_standalone`/`download_embed`/`extract_standalone`/`extract_embed`/`write_pth`
   - 保持测试 monkeypatch.setattr("stages.<name>", ...) 兼容

8. `fspack/packaging/pipeline/__init__.py`（353 行）
   - 调整内部导入：BuildContext 从 context.py 导入；diagnose/clean/verify 从 dist_helpers 导入；plan 从 plan_printer 导入；阶段函数从 stages.py 导入（保持名字兼容）
   - `_build_package()` 主流程不变

## 关键决策与依据

### 决策 1：按阶段边界而非函数密度拆分
拆分边界：运行时准备、依赖处理、源码编译、dist 目录辅助、dry-run 计划。
- 依据：6 类功能调用链正交，各自依赖集合差异明显（runtime_stage 大量依赖 runtime/wheels，deps_stage 依赖 dep_analyzer/builtin），独立后可读性提升明显

### 决策 2：stages.py 作为 facade + monkeypatch 兼容层保留
- 依据：测试中 60+ 处 monkeypatch.setattr("fspack.packaging.pipeline.stages.<name>", ...)，直接删除会导致大量 AttributeError；保持 facade 同时不增加外部调用成本

### 决策 3：引入 _S/_RS/_CS/_DS 延迟 dispatch 机制
各子模块从 stages.py 读取运行时依赖时使用 `_S(name, fallback)` 函数，在首次调用时动态解析 stages 模块属性并缓存。
- 依据：
  1. 避免循环导入：context.py → runtime_stage → stages → runtime_stage 等循环被打破
  2. patch 生效：测试通过 `setattr("stages.ThreadPoolExecutor", ...)` 替换类后，`_S("ThreadPoolExecutor", _DefaultThreadPoolExecutor)` 返回 patch 后对象
  3. 无运行时成本：仅第一次调用触发 import，后续直接用 getattr

### 决策 4：ThreadPoolExecutor/detect_platform/TkinterBundler 同步 re-export
- 依据：测试 patch 的目标就是这三个名字（见 test_builder L210/L784/L802/L842）。若 stages 不暴露这些名字，pytest 会直接 AttributeError，即便子模块内部用 fallback 默认值，也无法感知 patch。

## 代码实现情况
- 拆分后模块规模：context 53、runtime_stage 146、deps_stage 219、compile_stage 268、dist_helpers 173、plan_printer 126、stages 100，全部 < 300 行（目标 < 500）
- 所有原 stages.py 的公开名字均在 stages.py 中 re-export，__all__ 显式列出 34 个名字
- 子模块 dispatch：
  - runtime_stage：download_standalone、download_embed、extract_standalone、extract_embed、write_pth
  - deps_stage：download_wheels、TkinterBundler
  - compile_stage：compile_loader、detect_platform、ThreadPoolExecutor
- 保持了所有类型注解：入口 wrapper、stage 上下文、dispatch Callable 返回值

## 整合优化情况
- 清理 stages.py 中的临时注释和 debug 标记，仅保留模块级 docstring 和 re-export
- `__init__.py` 的导入路径统一采用 `from .stages import ...` 或 `from .子模块 import ...`，保持一致
- 去掉 stages.py 中重复的 `from concurrent.futures import ThreadPoolExecutor`（顶部已移除）

## 测试验证结果

### 专项修复清单（迭代过程中）
1. `AttributeError: stages.detect_platform` → stages.py 重导出 + compile_stage 内 dispatch
2. `AttributeError: stages.ThreadPoolExecutor` → stages.py 重导出 + compile_stage 内 dispatch
3. `AttributeError: stages.TkinterBundler` → stages.py 重导出 + deps_stage 内 dispatch

### 全量回归
```
tests/test_builder.py            149 passed, 11 skipped
tests/test_extras.py
tests/test_build_dry_run.py
tests/test_log_file.py
tests/test_profile.py
tests/test_nuitka.py
tests/test_site_packages.py
tests/test_build_perf_baseline.py

总计：409 passed, 11 skipped in 5.04s
```

### 性能影响
- warm cache baseline: small 4.46ms / medium 4.55ms（与 baseline 一致，无明显回归）
- cold cache baseline: small 5.86ms / medium 12.15ms（与 baseline 一致）
- 拆分未引入额外运行时开销：dispatch 函数仅首次 import，后续为纯 getattr

## 遗留事项
- `stages.py` 暴露了内部私有名（`_analyze_dependencies` 等）：后续可考虑 `__all__` 仅列出公开 API，内部名通过 `_internal_*` 前缀隔离
- 进一步拆分方向：`__init__.py` 的 `_build_package()` 仍有 130 行，可考虑按阶段抽取到独立 orchestrator，但当前已清晰，暂不做（成本大于收益）

## 下一轮计划
1. iter-151：pyc.py（约 500+ 行）拆分为 pyc_compile / pyc_stamp / source_strip 三模块
2. 保持 pyc.py 作为 facade + __all__ + dispatch 兼容测试 monkeypatch
3. 跑全量测试 + perf baseline 验证无回归
