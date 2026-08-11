# iter-154: cli_parser.py 按子命令拆分（build/init/doctor/package）

## 需求清单
- [x] cli_parser.py（467 行）按子命令拆分为 4 个子模块
- [x] facade cli_parser.py 保留 `build_parser()` 公共 API 不变
- [x] 无新增 patch 兼容点（测试未直接 patch cli_parser）
- [x] 修复回归：`import fspack.builder` 不应加载 fspack.console（pipeline plan_printer 延迟导入）
- [x] 全量 2255 测试通过

## 迭代目标
1. 拆分 467 行的 CLI 参数声明，按子命令聚合为 4 个聚焦子模块
2. 新增子命令仅需加一个文件 + 在 facade 注册，修改隔离
3. 顺带修复 plan_printer 顶层导入导致的 `test_pipeline_module_no_top_level_heavy_imports` 回归

## 改动文件清单

### 新增子命令子模块（`src/fspack/` 下）
1. `cli_cmds_build.py`（~245 行）
   - `_add_build_subparser`：build/b 全部 25 个参数声明
   - `_add_run_subparser`：run/r 4 个参数（含 rest 透传、--debug、--entry）
   - `_add_clean_subparser`：clean/c（清 dist）
2. `cli_cmds_package.py`（~100 行）
   - `_add_package_subparser`：package/p 11 个参数（含 ns/签名/递归/extras）
3. `cli_cmds_init.py`（~30 行）
   - `_add_init_subparser`：init/i 模板创建 6 个参数
4. `cli_cmds_doctor.py`（~35 行）
   - `_add_doctor_subparser`：doctor 3 个参数（--test/--bench/--check-cache）
   - `_add_cache_subparser`：cache status/clean 二级子命令 + --dry-run

### 修改
5. `cli_parser.py`（55 行 facade）
   - 从 4 个子模块 re-export 各 `_add_*_subparser`（便于外部引用）
   - `build_parser()` 顶层 parser（version/verbose）+ 注册 7 个子命令
6. `packaging/pipeline/__init__.py`（修复回归）
   - 去掉顶层 `from plan_printer import _print_build_plan`（触发 console 加载）
   - 新增 `_PRINT_BUILD_PLAN_NAME` 常量 + 模块级 `__getattr__`（首次访问时才导入并缓存到 globals）
   - `build()` dry-run 分支内显式 `from plan_printer import` 并 `globals()[name] = val` 后续调用走全局
   - 注释改写避免 `import fspack.console` 字面量（测试 `inspect.getsource` 字符串断言）

## 关键决策与依据

### 决策 1：cli_parser 走子模块而非子包（无 dep_analyzer 式目录）
`cli_parser.py` 467 行拆分后 4 个独立文件均 < 250 行，不涉及跨平台分发逻辑；顶层 `fspack/` 已存在 `cli.py` + `cli_init.py`，再拆 `cli_cmds_*.py` 保持扁平命名一致，避免引入新层级目录。

### 决策 2：pipeline plan_printer 延迟导入采用 `__getattr__` + 显式赋值双层保障
- **双层**：`build()` dry-run 内部首次使用时显式 import 绑定全局（避免后续 `__getattr__` 每次查找开销）；`__getattr__` 兜底 `from pipeline import _print_build_plan`（测试用例）
- **字符串断言小心**：测试用 `inspect.getsource(pipeline)` 截取顶层，断言无 `import fspack.console` 字面量，注释里也不能写（直接断言源代码文件的文本内容）。此处为陷阱点，写 iter-154 记录。

## 代码实现情况
- 拆分后规模：build 245 / package 100 / init 30 / doctor 35 / parser facade 55，全部 < 250 行
- 原 467 行 cli_parser.py → 55 行 facade，减 88%
- 修复 pipeline/__init__.py console 顶层加载回归

## 测试验证结果
```
专项（cli + dry-run）：16 passed
全量回归：2255 passed, 12 skipped in 63.81s

性能基线矩阵（26 基准）：
  classify_entry        3.9 us     256K OPS
  generate_wrapper      7.4 us     135K OPS
  collect_imports       35 us       28K OPS
  cached project_info   270 us       3.7K OPS
  fingerprint           532 us       1.9K OPS
  ensure_env_cache      644 us       1.6K OPS
  cache_hit (构建)      2.6 ms         382 OPS
  parallel_compile      135 ms            7 OPS
  serial_compile        514 ms            2 OPS
```
（baseline 相对 iter-153 基准无显著回归）

## 遗留事项
- 若未来新增 `manifest` 子命令（见 todo iter-165），可直接新建 `cli_cmds_manifest.py`，在 facade 增加注册，零改动既有参数声明
- `cli_cmds_build.py` 仍是最大的子模块（245 行），如 Nuitka 相关参数继续扩展，可再细拆 `cli_cmds_build_opts.py`

## 下一轮计划
1. iter-155：Nuitka mixin Protocol 类型声明，消除 `# type: ignore[attr-defined]` 抑制
2. iter-156：配置加载缓存（`ProjectInfo.from_dir` 按 pyproject.toml mtime 做 lru_cache）
3. 跑专项 + 全量回归验证
