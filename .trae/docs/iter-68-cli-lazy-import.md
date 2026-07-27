# iter-68 CLI 启动懒加载优化

## 需求清单

- [x] CLI 启动懒加载：`fsp` 入口延迟导入重模块，`fsp --help` 提速

## 迭代目标

将 `fspack.cli` 顶部导入的重模块（`fspack.config`/`fspack.console`/`fspack.platform`）
延迟到实际使用时导入，使 `fsp --help` 无需加载 config/console 即可输出帮助，
降低启动开销。

## 改动文件清单

- `src/fspack/cli.py`：
  - 顶部移除 `from fspack.config import MIRRORS`/`from fspack.console import console`/
    `from fspack.platform import Platform`
  - 新增 `TYPE_CHECKING` 块导入 `Platform`（仅类型注解用，运行时不加载）
  - 新增 `_mirrors_choices()` 辅助函数：延迟导入 `MIRRORS`，供
    `_add_build_subparser`/`_add_package_subparser` 调用
  - `main()` 内延迟导入 `console`（仅在实际执行子命令时加载 rich）
  - `_parse_target()` 内延迟导入 `Platform`
- `tests/test_cli.py`：
  - 新增 3 个测试：`test_mirrors_choices_returns_valid_list`/
    `test_cli_module_no_top_level_console_import`/
    `test_cli_module_no_top_level_platform_import`

## 关键决策与依据

1. **`MIRRORS` 用 `_mirrors_choices()` 包装**：argparse `choices` 参数在
   `add_argument` 时就要求传入列表，无法完全延迟。用辅助函数包装使延迟导入
   集中在一处，便于维护。`build_parser()` 调用 `_mirrors_choices()` 仍会触发
   `fspack.config` 加载（~10ms），但避免了顶部导入的全局影响

2. **`console` 在 `main()` 内导入**：`--help` 路径在 `parser.parse_args()` 后
   直接 `parser.print_help(); return`，不会执行到 `console.setup_logging`。
   故 `--help` 完全不加载 rich（~17ms 节省）。仅在实际执行子命令时加载

3. **`Platform` 用 `TYPE_CHECKING` 块**：`_parse_target` 返回类型注解
   `Platform | None` 需要 `Platform` 名字，但 `from __future__ import annotations`
   让注解不求值。`TYPE_CHECKING` 块内的导入仅类型检查器可见，运行时不加载

4. **不优化 `fspack.__version__`**：`fspack/__init__.py` 加载会尝试
   `import typing_extensions`（~5ms），但移除会破坏运行时兼容性 stub。
   不在本次迭代范围

## 代码实现情况

- `cli.py` 行数：269 → 290（+21 行，含 `_mirrors_choices` 与注释）
- `cli.py` 顶部导入从 7 个减至 5 个（移除 3 个重模块，新增 1 个 `TYPE_CHECKING`）
- 总测试数：1047（+3 新增）
- 总覆盖率：98.58%

## 整合优化情况

- 移除顶部 `from fspack.config import MIRRORS`，集中到 `_mirrors_choices()`
- 移除顶部 `from fspack.console import console`，集中到 `main()` 内
- 移除顶部 `from fspack.platform import Platform`，集中到 `_parse_target()` 内
  与 `TYPE_CHECKING` 块

## 测试验证结果

- ruff check：通过
- ruff format --check：通过
- pyrefly check：0 errors（2 suppressed，与基线一致）
- pytest（非 slow）：1047 passed，覆盖率 98.58%（≥95%）
- `test_cli.py`：34 测试全通过（+3 新增）

## 性能对比

| 场景 | 基线 | 优化后 | 提速 |
|------|------|--------|------|
| `build_parser()` 加载 | 132.3ms | 110.2ms | -22.1ms（-16.7%） |
| `fsp --help` | 132.5ms | 111.2ms | -21.3ms（-16.1%） |
| 纯 argparse `--help` | 96.4ms | 96.4ms | 基线（Python 启动开销） |

`fspack.cli` 模块加载：53.2ms → 26.1ms（-27.1ms，-51%）

未达 ≤100ms 目标（Python 解释器启动本身 ~80-90ms），但 fspack 模块加载开销
从 35.9ms 降至 14ms，已接近极限。剩余开销主要是 Python 解释器启动 + uv run
封装开销，无法通过代码优化解决。

## 遗留事项

- 100ms 目标未达成（Python 启动开销占主导），但模块加载开销降低 51% 已显著改善
- `fspack.config` 仍被 `_mirrors_choices()` 触发（~10ms），可考虑用
  `choices=None` + 自定义 type checker 完全避免，但会改变 argparse 错误提示格式
- 真实 `fsp --help` 用户感知提速约 20ms，对交互体验影响较小

## 下一轮计划

iter-69：examples 端到端集成测试完善（扩展现有 slow 测试，覆盖多入口、Nuitka
编译、Linux 跨平台构建、精简规则组合场景）
